"""
generator.py —— 故障树生成模块（v3）

生成流程：
  Step 0  parse_user_prompt()          — 从用户自然语言中抽取顶事件与额外要求
  Step 1  extract_fault_elements()     — LLM#1：从chunks提取故障要素
  Step 2  build_fault_tree()           — LLM#2：生成草稿故障树（含结构校验重试）
  Step 3  repair_fault_tree()          — LLM#3：基于节点级修正记录做定向修复
  Step 4  validate_full()              — 最终语义校验
"""

import json
import re
from typing import Dict, List

from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from database import search_chunks_by_keywords
from validator import validate_full

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

MAX_RETRY = 2
MAX_CHUNKS_FOR_PROMPT = 15
MAX_CHUNK_CHARS = 280
MAX_JSON_REPAIR_RETRY = 1

FORMAT_EXAMPLE = """
{
  "nodeList": [
    {
      "type": "top_event",
      "gate": "OR",
      "name": "登机梯故障",
      "id": "node-abcd1234",
      "transfer": "",
      "event": null
    },
    {
      "type": "intermediate_event",
      "gate": "OR",
      "name": "回收故障",
      "id": "node-efgh5678",
      "transfer": "",
      "event": {
        "id": "E001",
        "name": "回收故障",
        "description": "事件的详细描述",
        "errorLevel": "高/中/低",
        "priority": 0,
        "probability": 1e-8,
        "showProbability": 0.000001,
        "rules": [
          {
            "deviceTypeId": "",
            "measurePointName": "监测点名称",
            "symbol": ">",
            "thresholds": ["阈值"],
            "duration": "3"
          }
        ],
        "investigateMethod": "排查方法描述",
        "documents": []
      }
    },
    {
      "type": "basic_event",
      "gate": null,
      "name": "气路压力不足",
      "id": "node-ijkl9012",
      "transfer": "",
      "event": {
        "id": "E002",
        "name": "气路压力不足",
        "description": "详细描述",
        "errorLevel": "高",
        "priority": 0,
        "probability": 1e-8,
        "showProbability": 0.000001,
        "rules": [],
        "investigateMethod": "排查方法",
        "documents": [
          {
            "chunk_id": 5,
            "chunk_name": "chunk名称",
            "section_path": "1.1.0",
            "source_page": 9
          }
        ]
      }
    }
  ],
  "linkList": [
    {
      "type": "link",
      "sourceId": "子节点id（原因）",
      "targetId": "父节点id（结果）",
      "isCondition": false
    }
  ]
}
"""


def parse_user_prompt(prompt: str) -> dict:
    """
    从用户自然语言prompt中提取顶事件和额外要求。

    策略：
    1. 规则优先：处理“顶事件为XX”“分析XX故障”等常见表达
    2. 短句直通：若prompt本身就是短故障短语，直接视作顶事件
    3. LLM兜底：规则无法可靠提取时，调用大模型抽取
    """
    raw = (prompt or "").strip()
    if not raw:
        raise ValueError("prompt不能为空")

    normalized = _normalize_prompt_text(raw)

    by_rule = _extract_top_event_by_rules(normalized)
    if by_rule:
        return by_rule

    if _looks_like_direct_top_event(normalized):
        return {"top_event": _cleanup_top_event(normalized), "requirements": ""}

    return _extract_top_event_by_llm(normalized)


def extract_fault_elements(top_event: str, chunks: list) -> dict:
    chunks_text = _format_chunks(chunks)
    prompt = f"""你是工业设备故障分析专家，精通FTA故障树分析方法。

## 参考知识（来自设备手册）
{chunks_text}

## 任务
从上述知识中，提取与以下故障相关的所有故障事件、因果关系和触发条件：
顶事件：{top_event}

## 提取规则
1. 顶事件（top_event）：即给定的故障现象，只有一个，必须要有子事件节点
2. 中间事件（intermediate_event）：可以继续向下分解的中间原因，必须要有子事件节点。如果没有子事件节点就必须降级为 basic_event。
3. 底事件（basic_event）：最根本原因，不可再分，必须是具体可检测的故障，不能有子事件节点
4. gate=OR：任一子事件发生就导致父事件
5. gate=AND：所有子事件同时发生才导致父事件
6. rules：每个事件的触发条件，如"电流超过额定值""温度持续超过85℃"，无量化条件可描述文字
7. errorLevel：高/中/低
8. investigateMethod：如何排查这个故障

## 输出格式（严格JSON，无多余文字）
{{
  "events": [
    {{
      "name": "事件名称",
      "type": "top_event/intermediate_event/basic_event",
      "description": "详细描述",
      "errorLevel": "高/中/低",
      "investigateMethod": "排查方法",
      "rules": [
        {{
          "measurePointName": "监测点名称",
          "symbol": ">",
          "thresholds": ["阈值"],
          "duration": "持续时长"
        }}
      ]
    }}
  ],
  "relations": [
    {{"parent": "父事件名称", "child": "子事件名称", "gate": "OR"}}
  ]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=2200,
    )
    return _parse_json(response.choices[0].message.content)


def build_fault_tree(top_event: str, elements: dict, chunks: list, requirements: str = "", previous_issues: list = None) -> dict:
    chunks_text = _format_chunks(chunks)
    elements_text = json.dumps(elements, ensure_ascii=False, indent=2)

    chunks_ref = [
        {
            "chunk_id": c["id"],
            "chunk_name": c.get("chunk_name", ""),
            "section_path": c.get("section_path", ""),
            "source_page": c.get("source", ""),
        }
        for c in chunks
    ]
    chunks_ref_text = json.dumps(chunks_ref, ensure_ascii=False, indent=2)

    retry_hint = ""
    if previous_issues:
        error_msgs = [
            f"- [{i['level']}] {i['message']} (节点: {i.get('node_name', '')})"
            for i in previous_issues
            if i["level"] in ("ERROR", "WARNING")
        ]
        if error_msgs:
            retry_hint = "\n## 上次生成存在以下问题，请修正\n" + "\n".join(error_msgs) + "\n"

    prompt = f"""你是工业设备故障树分析专家，精通FTA方法。

## 参考知识（来自设备手册）
{chunks_text}

## 可用的溯源chunk列表（用于填写documents字段）
{chunks_ref_text}

## 已提取的故障要素
{elements_text}
{retry_hint}
## 用户额外要求
{requirements or '无'}

## 任务
为顶事件“{top_event}”生成完整、规范的故障树。

## 必须严格遵守的输出格式（样例如下）
{FORMAT_EXAMPLE}

## 生成规则
1. 每个节点id格式：node-{{8位十六进制}}，全部唯一，自行生成随机值
2. event.id格式：E001, E002...依次递增
3. 顶事件的event字段固定为null
4. 中间事件和底事件必须填完整event对象，不能缺少任何字段
5. errorLevel必须填写：高/中/低
6. rules：尽量从手册提取触发条件；无量化数据则填[]
7. documents：从溯源chunk列表中选相关的填入，每个节点最多2条
8. investigateMethod：尽量控制在40字以内
9. description：尽量控制在60字以内
10. linkList的sourceId是子节点（原因方），targetId是父节点（结果方）
11. 故障树深度3-5层
12. 节点总数尽量控制在8~18个，避免无关扩展
13. 必须一次性输出完整、闭合、可直接json.loads解析的JSON，禁止省略结尾括号

只输出JSON，不要有任何多余文字或markdown标记。
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=8192,
    )
    return _parse_json(response.choices[0].message.content)


def repair_fault_tree(draft_tree: dict, corrections_hint: str, chunks: list) -> dict:
    draft_text = json.dumps(draft_tree, ensure_ascii=False, indent=2)
    chunks_ref = [
        {
            "chunk_id": c["id"],
            "chunk_name": c.get("chunk_name", ""),
            "section_path": c.get("section_path", ""),
            "source_page": c.get("source", ""),
        }
        for c in chunks
    ]
    chunks_ref_text = json.dumps(chunks_ref, ensure_ascii=False, indent=2)

    prompt = f"""你是工业设备故障树分析专家。
你将收到一棵已生成的故障树草稿，以及来自历史专家修正记录的定向建议。
你的任务是根据建议对草稿进行最小化修改，输出修复后的完整故障树。

## 严格约束
- 只修改建议中明确提到的节点，其余节点、连接、字段一律保持原样
- 禁止重构整体树结构
- 禁止新增建议之外的节点
- 保持所有节点id不变（除非新增节点）
- 输出格式与草稿完全一致（nodeList + linkList）

## 草稿故障树
{draft_text}

## 可用溯源chunk列表
{chunks_ref_text}

{corrections_hint}

## 输出要求
只输出修复后的完整JSON，格式与草稿一致，不要有任何多余文字或markdown标记。
如果你认为某条建议在当前场景下不适用，可以忽略它，但不能因此改动其他节点。
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=8192,
    )
    return _parse_json(response.choices[0].message.content)


def generate_fault_tree(top_event: str, requirements: str = "") -> dict:
    keywords = _extract_keywords(top_event)
    if top_event not in keywords:
        keywords = [top_event] + keywords

    chunks = search_chunks_by_keywords(keywords)
    if not chunks:
        raise ValueError(f"未找到与'{top_event}'相关的知识，请检查chunks数据是否已导入")

    print(f"[生成] 顶事件：{top_event}")
    print(f"[生成] 检索到 {len(chunks)} 个相关chunks，关键词：{keywords}")

    print("[生成] LLM#1：提取故障要素...")
    elements = extract_fault_elements(top_event, chunks)
    print(f"[生成] 提取到 {len(elements.get('events', []))} 个事件，{len(elements.get('relations', []))} 条关系")

    previous_issues = None
    draft_tree = None
    last_error = None
    for attempt in range(1, MAX_RETRY + 2):
        print(f"[生成] LLM#2：生成草稿（第 {attempt} 次）...")
        try:
            draft_tree = build_fault_tree(top_event, elements, chunks, requirements, previous_issues)
        except ValueError as e:
            last_error = str(e)
            print(f"[生成] 草稿JSON解析失败：{last_error}")
            previous_issues = [{"level": "ERROR", "message": f"模型输出不是完整JSON：{last_error}", "node_name": ""}]
            if attempt <= MAX_RETRY:
                continue
            raise

        validation = validate_full(draft_tree, skip_semantic=True)
        draft_tree["validation"] = validation

        if validation["passed"]:
            print("[生成] 草稿结构校验通过")
            break

        print(
            f"[生成] 草稿校验失败，ERROR={validation['error_count']}，"
            f"{'重试中...' if attempt <= MAX_RETRY else '已达最大重试次数'}"
        )
        # 调试输出：打印 ERROR 级别的校验问题，便于定位生成失败原因
        try:
            err_issues = [i for i in (validation.get("issues") or []) if i.get("level") == "ERROR"]
            if err_issues:
                print("[生成] 草稿结构校验 ERROR 详情：")
                for idx, iss in enumerate(err_issues, start=1):
                    code = iss.get("code", "")
                    msg = iss.get("message", "")
                    node_id = iss.get("node_id", "") or iss.get("nodeId", "")
                    node_name = iss.get("node_name", "") or iss.get("nodeName", "")
                    where = " ".join([p for p in [node_name, node_id] if p])
                    suffix = f"（{where}）" if where else ""
                    print(f"  - [{idx}] {code}: {msg}{suffix}")
        except Exception as _e:
            # 避免调试输出影响主流程
            pass
        previous_issues = validation["issues"]

    final_tree = draft_tree
    try:
        from diff_analyzer import get_relevant_corrections, format_corrections_for_repair

        corrections = get_relevant_corrections(draft_tree)
        if corrections:
            print(f"[修复] 检索到 {len(corrections)} 条相关历史修正，启动定向修复...")
            corrections_hint = format_corrections_for_repair(corrections)
            repaired = repair_fault_tree(draft_tree, corrections_hint, chunks)
            repair_validation = validate_full(repaired, skip_semantic=True)
            if repair_validation["passed"]:
                repaired["validation"] = repair_validation
                final_tree = repaired
                print("[修复] 修复版本结构校验通过，采用修复结果")
            else:
                print("[修复] 修复版本结构校验失败，回退到草稿版本")
        else:
            print("[修复] 暂无相关历史修正记录，跳过修复步骤")
    except Exception as e:
        print(f"[修复] 修复步骤异常（{e}），回退到草稿版本")

    final_validation = validate_full(final_tree, skip_semantic=False)
    final_tree["validation"] = final_validation
    return final_tree


def _extract_keywords(text: str) -> List[str]:
    try:
        import jieba.analyse

        keywords = jieba.analyse.extract_tags(text, topK=8)
        return keywords if keywords else [text]
    except Exception:
        return [text]


def _format_chunks(chunks: list) -> str:
    lines = []
    for chunk in (chunks or [])[:MAX_CHUNKS_FOR_PROMPT]:
        content = (chunk.get("content", "") or "").strip()
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS] + "..."
        lines.append(
            f"[chunk_id={chunk['id']} | {chunk.get('chunk_name', '')} | 章节:{chunk.get('section_path', '')} | 页码:{chunk.get('source', '')}]\n"
            f"{content}\n"
            f"{'─' * 50}"
        )
    return "\n".join(lines)


def _extract_json_text(raw: str) -> str:
    clean = re.sub(r"```json|```", "", raw or "").strip()
    if not clean:
        return ""

    start_obj = clean.find("{")
    start_arr = clean.find("[")
    starts = [i for i in (start_obj, start_arr) if i != -1]
    if not starts:
        return clean
    start = min(starts)
    candidate = clean[start:]

    open_char = candidate[0]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for idx, ch in enumerate(candidate):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return candidate[: idx + 1]
    return candidate


def _parse_json(raw: str) -> dict:
    candidate = _extract_json_text(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_error:
        repaired = _try_repair_json_text(candidate)
        if repaired is not None:
            return repaired
        raise ValueError(f"大模型输出JSON解析失败：{first_error}\n原始输出：\n{raw}")


def _try_repair_json_text(candidate: str):
    text = (candidate or "").strip()
    if not text:
        return None

    # 轻量修补：若只是缺少结尾括号，则尝试自动补齐
    opens = {'{': '}', '[': ']'}
    stack = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in opens:
            stack.append(opens[ch])
        elif ch in (']', '}') and stack and ch == stack[-1]:
            stack.pop()
    if in_string:
        text += '"'
    if stack:
        text += ''.join(reversed(stack))
    try:
        return json.loads(text)
    except Exception:
        return None


def _normalize_prompt_text(text: str) -> str:
    text = text.strip()
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_top_event_by_rules(text: str) -> Dict[str, str]:
    patterns = [
        r"顶事件(?:为|是|：|:)?\s*[\"']?([^\"'，。,；;！!？?]+?)[\"']?(?:的故障树|故障树|$)",
        r"生成(?:一个|一棵)?(?:顶事件为)?\s*[\"']?([^\"'，。,；;！!？?]+?故障)[\"']?(?:的故障树|故障树|$)",
        r"分析[\"']?([^\"'，。,；;！!？?]+?故障)[\"']?(?:，|,|并|并且|然后|$)",
        r"针对[\"']?([^\"'，。,；;！!？?]+?故障)[\"']?(?:进行分析|生成|构建|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            top_event = _cleanup_top_event(match.group(1))
            if top_event:
                requirements = _extract_requirements_text(text, top_event)
                return {"top_event": top_event, "requirements": requirements}

    quoted = re.findall(r'[\"\']([^\"\']+)[\"\']', text)
    for item in quoted:
        item = _cleanup_top_event(item)
        if item and _looks_like_direct_top_event(item):
            requirements = _extract_requirements_text(text, item)
            return {"top_event": item, "requirements": requirements}

    return {}


def _looks_like_direct_top_event(text: str) -> bool:
    cleaned = _cleanup_top_event(text)
    if not cleaned:
        return False

    if len(cleaned) > 24:
        return False

    deny_words = ["故障树", "生成", "构建", "分析", "提取", "按照", "规范", "要求", "溯源", "数据库", "检索"]
    if any(word in cleaned for word in deny_words if cleaned != word):
        # 若短语本身包含故障/异常/错误等核心名词，允许通过
        if not any(flag in cleaned for flag in ["故障", "异常", "错误", "失效", "失灵", "报警", "停机", "过热", "过流"]):
            return False

    return any(flag in cleaned for flag in ["故障", "异常", "错误", "失效", "失灵", "报警", "停机", "过热", "过流"])


def _cleanup_top_event(text: str) -> str:
    text = (text or "").strip().strip('"').strip("'")
    text = re.sub(r"^(我要|我想|请|帮我|给我|生成|构建|分析)+", "", text)
    text = re.sub(r"(的故障树|故障树)$", "", text)
    text = re.sub(r"^[：:，,\s]+|[：:，,；;。.!！?？\s]+$", "", text)
    return text.strip()


def _extract_requirements_text(full_prompt: str, top_event: str) -> str:
    req = full_prompt
    req = req.replace(top_event, " ")
    req = re.sub(r"顶事件(?:为|是|：|:)?", " ", req)
    req = re.sub(r"我要|我想|请|帮我|给我", " ", req)
    req = re.sub(r"生成(?:一个|一棵)?|构建(?:一个|一棵)?|分析|请分析|请生成|请构建", " ", req)
    req = re.sub(r"故障树", " ", req)
    req = re.sub(r"\s+", " ", req).strip(" ，,；;。.!！?？")
    return req


def _extract_top_event_by_llm(prompt: str) -> Dict[str, str]:
    llm_prompt = f"""你是工业故障分析系统的输入解析器。
请从用户输入中提取：
1. top_event：用户真正想分析的顶事件，必须是一个简短明确的故障现象或故障对象
2. requirements：除顶事件外，用户对生成故障树的其他要求

用户输入：
{prompt}

输出要求：
- 只输出严格JSON
- 不要输出解释
- top_event不能为空
- 如果用户输入本身就是一个故障短语，则requirements置为空字符串

输出格式：
{{
  "top_event": "顶事件",
  "requirements": "额外要求"
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": llm_prompt}],
        temperature=0,
        max_tokens=600,
    )
    parsed = _parse_json(response.choices[0].message.content)
    top_event = _cleanup_top_event(parsed.get("top_event", ""))
    if not top_event:
        raise ValueError("无法从prompt中提取顶事件，请改成如“传感器故障”或“请生成顶事件为传感器故障的故障树”这样的输入")
    return {"top_event": top_event, "requirements": (parsed.get("requirements") or "").strip()}
