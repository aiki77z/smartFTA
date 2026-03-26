"""
generator.py —— 故障树生成模块（v2：草稿生成 + 定向修复）

生成流程：
  Step 1  extract_fault_elements()   — LLM#1：从chunks提取故障要素
  Step 2  build_fault_tree()         — LLM#2：生成草稿故障树（含结构校验重试）
  Step 3  repair_fault_tree()        — LLM#3：基于节点级修正记录做定向修复
                                       （仅当corrections_col有相关记录时触发）
  Step 4  validate_full()            — 最终语义校验

修复步骤的设计原则：
  - 修复提示只列出草稿中实际出现的节点的修正建议，不引入无关信息
  - 明确禁止模型重构整体树结构，只允许针对标注节点做最小改动
  - 修复后重新做结构校验，若校验失败则回退到草稿版本
"""

import json
import re
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from database import search_chunks_by_keywords
from validator import validate_full

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

MAX_RETRY = 2

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


# ─────────────────────────────────────────────
# Step 1：故障要素提取
# ─────────────────────────────────────────────

def extract_fault_elements(top_event: str, chunks: list) -> dict:
    chunks_text = _format_chunks(chunks)
    prompt = f"""你是工业设备故障分析专家，精通FTA故障树分析方法。

## 参考知识（来自设备手册）
{chunks_text}

## 任务
从上述知识中，提取与以下故障相关的所有故障事件、因果关系和触发条件：
顶事件：{top_event}

## 提取规则
1. 顶事件（top_event）：即给定的故障现象，只有一个
2. 中间事件（intermediate_event）：可以继续向下分解的中间原因
3. 底事件（basic_event）：最根本原因，不可再分，必须是具体可检测的故障
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
        temperature=0.2
    )
    return _parse_json(response.choices[0].message.content)


# ─────────────────────────────────────────────
# Step 2：草稿故障树生成
# ─────────────────────────────────────────────

def build_fault_tree(top_event: str, elements: dict, chunks: list,
                     requirements: str = "", previous_issues: list = None) -> dict:
    chunks_text   = _format_chunks(chunks)
    elements_text = json.dumps(elements, ensure_ascii=False, indent=2)

    chunks_ref = [
        {
            "chunk_id":     c["id"],
            "chunk_name":   c.get("chunk_name", ""),
            "section_path": c.get("section_path", ""),
            "source_page":  c.get("source", "")
        }
        for c in chunks
    ]
    chunks_ref_text = json.dumps(chunks_ref, ensure_ascii=False, indent=2)

    retry_hint = ""
    if previous_issues:
        error_msgs = [
            f"- [{i['level']}] {i['message']} (节点: {i.get('node_name', '')})"
            for i in previous_issues if i["level"] in ("ERROR", "WARNING")
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
{requirements or "无"}

## 任务
为顶事件"{top_event}"生成完整、规范的故障树。

## 必须严格遵守的输出格式（样例如下）
{FORMAT_EXAMPLE}

## 生成规则
1. 每个节点id格式：node-{{8位十六进制}}，全部唯一，自行生成随机值
2. event.id格式：E001, E002...依次递增
3. 顶事件的event字段固定为null
4. 中间事件和底事件必须填完整event对象，不能缺少任何字段
5. errorLevel必须填写：高/中/低
6. rules：尽量从手册提取触发条件；无量化数据则填[]
7. documents：从溯源chunk列表中选相关的填入
8. investigateMethod：1-2句话描述排查方法
9. linkList的sourceId是子节点（原因方），targetId是父节点（结果方）
10. 故障树深度3-5层

只输出JSON，不要有任何多余文字或markdown标记。
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    return _parse_json(response.choices[0].message.content)


# ─────────────────────────────────────────────
# Step 3：定向修复（基于节点级历史修正）
# ─────────────────────────────────────────────

def repair_fault_tree(draft_tree: dict, corrections_hint: str, chunks: list) -> dict:
    """
    对草稿故障树做定向修复。

    设计约束：
      - 只修改 corrections_hint 中明确提到的节点
      - 禁止重构整体树结构或调整未提及节点
      - 输出格式与草稿完全一致（nodeList + linkList）
      - 若模型输出无法解析，则抛出异常由调用方回退到草稿
    """
    draft_text  = json.dumps(draft_tree, ensure_ascii=False, indent=2)
    chunks_ref  = [
        {"chunk_id": c["id"], "chunk_name": c.get("chunk_name", ""),
         "section_path": c.get("section_path", ""), "source_page": c.get("source", "")}
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
        temperature=0.1   # 修复任务要求更确定性的输出
    )
    return _parse_json(response.choices[0].message.content)


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

def generate_fault_tree(top_event: str, requirements: str = "") -> dict:
    """
    完整生成流程：
      1. 检索知识chunks
      2. LLM#1 提取故障要素
      3. LLM#2 生成草稿（含结构校验重试）
      4. 节点级修正检索
      5. LLM#3 定向修复（有修正记录时触发）
      6. 最终语义校验
    """
    keywords = _extract_keywords(top_event)
    chunks   = search_chunks_by_keywords(keywords)

    if not chunks:
        raise ValueError(f"未找到与'{top_event}'相关的知识，请检查chunks数据是否已导入")

    print(f"[生成] 检索到 {len(chunks)} 个相关chunks，关键词：{keywords}")

    # Step 1
    print("[生成] LLM#1：提取故障要素...")
    elements = extract_fault_elements(top_event, chunks)
    print(f"[生成] 提取到 {len(elements.get('events', []))} 个事件，"
          f"{len(elements.get('relations', []))} 条关系")

    # Step 2：草稿生成（含重试）
    previous_issues = None
    draft_tree      = None

    for attempt in range(1, MAX_RETRY + 2):
        print(f"[生成] LLM#2：生成草稿（第 {attempt} 次）...")
        draft_tree = build_fault_tree(
            top_event, elements, chunks, requirements, previous_issues
        )
        validation = validate_full(draft_tree, skip_semantic=True)
        draft_tree["validation"] = validation

        if validation["passed"]:
            print(f"[生成] 草稿结构校验通过")
            break
        else:
            print(f"[生成] 草稿校验失败，ERROR={validation['error_count']}，"
                  f"{'重试中...' if attempt <= MAX_RETRY else '已达最大重试次数'}")
            previous_issues = validation["issues"]

    # Step 3：定向修复
    final_tree = draft_tree
    try:
        from diff_analyzer import get_relevant_corrections, format_corrections_for_repair

        corrections      = get_relevant_corrections(draft_tree)

        if corrections:
            print(f"[修复] 检索到 {len(corrections)} 条相关历史修正，启动定向修复...")
            corrections_hint = format_corrections_for_repair(corrections)
            repaired         = repair_fault_tree(draft_tree, corrections_hint, chunks)

            # 修复后做结构校验，失败则回退草稿
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

    # Step 4：最终语义校验
    final_validation         = validate_full(final_tree, skip_semantic=False)
    final_tree["validation"] = final_validation

    return final_tree


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _extract_keywords(text: str) -> list:
    try:
        import jieba.analyse
        keywords = jieba.analyse.extract_tags(text, topK=8)
        return keywords if keywords else [text]
    except ImportError:
        return [text]


def _format_chunks(chunks: list) -> str:
    lines = []
    for chunk in chunks:
        lines.append(
            f"[chunk_id={chunk['id']} | {chunk.get('chunk_name', '')} | "
            f"章节:{chunk.get('section_path', '')} | 页码:{chunk.get('source', '')}]\n"
            f"{chunk.get('content', '')}\n"
            f"{'─' * 50}"
        )
    return "\n".join(lines)


def _parse_json(raw: str) -> dict:
    clean = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        raise ValueError(f"大模型输出JSON解析失败：{e}\n原始输出：\n{raw}")
