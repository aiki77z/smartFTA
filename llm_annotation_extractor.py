from __future__ import annotations

import csv
import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


CSV_COLUMNS = [
    "sample_id",
    "file_id",
    "chapter_id",
    "source_type",
    "context_chunk_id",
    "target_chunk_id",
    "context_text",
    "text",
    "entities_json",
    "context_entities_json",
    "target_entities_json",
    "relations_json",
    "logic_groups_json",
    "status",
    "reviewer_notes",
]

ENTITY_TYPES = ["故障事件", "故障类别", "报警码", "维修方法", "触发规则"]
RELATION_TYPES = ["故障触发", "故障表征", "故障分类", "故障处理", "规则触发", "参与组合", "组合导致"]

SPECIAL_SOURCE_TOKENS = ["work_order", "maintenance", "repair", "case", "工单", "维修记录", "检修记录"]
GENERAL_DOC_TYPES = ["pdf", "docx", "doc", "txt", "md", "markdown", "manual_pdf", "manual_document", "standard_document", "unstructured"]


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def load_env_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def get_chunk_id(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("chunk_id") or chunk.get("id") or "")


def get_chunk_index(chunk: Dict[str, Any]) -> Optional[int]:
    try:
        return int(get_chunk_id(chunk))
    except (TypeError, ValueError):
        return None


def get_chunk_text(chunk: Optional[Dict[str, Any]]) -> str:
    if not chunk:
        return ""
    return str(chunk.get("content") or chunk.get("text") or chunk.get("raw_content") or "")


def get_file_id(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("file_id") or chunk.get("source_doc_id") or chunk.get("file_name") or "")


def normalize_section_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " / ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def get_chapter_id(chunk: Dict[str, Any]) -> str:
    for key in ["chapter", "chapter_id", "section", "heading"]:
        value = normalize_section_value(chunk.get(key))
        if value:
            return value
    section_path = chunk.get("section_path")
    if isinstance(section_path, list) and section_path:
        return str(section_path[0]).strip()
    if isinstance(section_path, str) and section_path:
        return section_path.split("/", 1)[0].strip()
    return ""


def get_source_type(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("source_type") or chunk.get("doc_type") or "unknown")


def is_plain_text_chunk(chunk: Dict[str, Any]) -> bool:
    chunk_type = str(chunk.get("type") or chunk.get("chunk_type") or "").lower()
    source_type = get_source_type(chunk).lower()
    if any(token in chunk_type for token in ["table", "表格", "structured"]):
        return False
    if any(token in source_type for token in ["table", "表格", "structured"]):
        return False
    if chunk.get("structured_table") or chunk.get("table"):
        return False
    return True


def is_general_unstructured_doc(chunk: Dict[str, Any]) -> bool:
    source_type = get_source_type(chunk).lower()
    if any(token in source_type for token in SPECIAL_SOURCE_TOKENS):
        return False
    if any(token in source_type for token in GENERAL_DOC_TYPES):
        return True
    file_name = str(chunk.get("file_name") or chunk.get("source_file") or chunk.get("doc_name") or "").lower()
    return any(file_name.endswith(f".{ext}") for ext in ["pdf", "docx", "doc", "txt", "md"])


def same_section(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    left_chapter = normalize_section_value(left.get("chapter"))
    right_chapter = normalize_section_value(right.get("chapter"))
    if left_chapter or right_chapter:
        return left_chapter == right_chapter
    left_chapter_id = normalize_section_value(left.get("chapter_id"))
    right_chapter_id = normalize_section_value(right.get("chapter_id"))
    if left_chapter_id or right_chapter_id:
        return left_chapter_id == right_chapter_id
    left_path = left.get("section_path")
    right_path = right.get("section_path")
    if left_path or right_path:
        return left_path == right_path
    left_section = normalize_section_value(left.get("section") or left.get("heading"))
    right_section = normalize_section_value(right.get("section") or right.get("heading"))
    if left_section or right_section:
        return left_section == right_section
    return True


def explain_context_decision(prev_chunk: Optional[Dict[str, Any]], curr_chunk: Dict[str, Any]) -> Tuple[bool, str]:
    if not prev_chunk:
        return False, "no_previous_chunk"
    if not is_general_unstructured_doc(prev_chunk) or not is_general_unstructured_doc(curr_chunk):
        return False, "not_general_unstructured_doc"
    if get_file_id(prev_chunk) != get_file_id(curr_chunk):
        return False, "different_file_id"
    if str(prev_chunk.get("file_version_id") or "") != str(curr_chunk.get("file_version_id") or ""):
        return False, "different_file_version_id"
    prev_index = get_chunk_index(prev_chunk)
    curr_index = get_chunk_index(curr_chunk)
    if prev_index is None or curr_index is None or curr_index - prev_index != 1:
        return False, "non_continuous_chunk_id"
    if not is_plain_text_chunk(prev_chunk) or not is_plain_text_chunk(curr_chunk):
        return False, "not_plain_text_chunk"
    if not same_section(prev_chunk, curr_chunk):
        return False, "different_chapter"
    return True, "use_context"


def sort_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(chunk: Dict[str, Any]) -> Tuple[str, str, int, str]:
        return (
            get_file_id(chunk),
            str(chunk.get("file_version_id") or ""),
            get_chunk_index(chunk) if get_chunk_index(chunk) is not None else 10**9,
            get_chunk_id(chunk),
        )

    return sorted(chunks, key=key)


def load_chunks_from_json(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"chunks json must be a list: {path}")
    return sort_chunks([item for item in data if isinstance(item, dict)])


def build_prompt(context_text: str, target_text: str, context_chunk_id: str, target_chunk_id: str) -> str:
    context_chunk_id_for_prompt = context_chunk_id or ""
    return f"""你是工业设备故障知识标注助手。请阅读一个滑动窗口样本，生成供人工审核的标注草稿。

窗口字段：
- context_text：前一个 chunk，仅用于理解上下文。
- text：当前 target chunk，是主要标注对象。

标注范围：
1. 只标注与当前 target chunk 相关的内容。
2. 完全只出现在 context_text 中、且与当前 text 无关的实体和关系，不要标注。
3. 出现在当前 text 中的实体、关系、逻辑组要标注。
4. context_text 中的实体只有在参与跨 chunk 关系、跨 chunk 逻辑组，或与当前 text 中同一实体形成同一实体多证据时，才输出。
5. 同一实体在 context_text 和 text 中都被提及时，只保留一个实体，不要拆成两个实体；用 evidence 数组记录它在不同 chunk 中的所有原文依据。
6. 实体本身不记录 start/end；mention 负责表达实体含义，evidence 负责记录原文定位。
7. 不要编造没有证据的实体或关系。不能确定时 certainty 填 possible。

实体类型只能使用以下 5 类：
1. 故障事件：设备异常、故障现象、故障原因、停机、失效、异常状态等。例如：编码器通信故障、反馈信号丢失、主轴保护停机。
2. 故障类别：对故障事件的类别归纳。例如：通信故障、温度故障、润滑系统故障。
3. 报警码：故障码、报警号、错误码。例如：F01000。
4. 维修方法：检查、处理、修复、观察、更换、紧固、清洗等措施。例如：更换编码器、重新紧固接头。
5. 触发规则：阈值、条件组合、持续时间、逻辑判断等规则。例如：温度超过80℃持续3秒。

关系类型只能使用以下 7 类，并严格遵守起点和终点类型：
1. 故障触发：故障事件 -> 故障事件。例如：接头松动 -> 反馈信号丢失。
2. 故障表征：报警码 -> 故障事件。例如：F01000 -> 编码器通信故障。
3. 故障分类：故障事件 -> 故障类别。例如：编码器故障 -> 通信故障。
4. 故障处理：维修方法 -> 故障事件。例如：更换编码器 -> 编码器故障。
5. 规则触发：触发规则 -> 故障事件。例如：温度超过80℃持续3秒 -> 过温停机。
6. 参与组合：故障事件 -> 逻辑组。不要在 [RELATION] 中直接输出；只有遇到 AND 组合时才输出 [LOGIC_GROUP]。
7. 组合导致：逻辑组 -> 故障事件。不要在 [RELATION] 中直接输出；只有遇到 AND 组合时才输出 [LOGIC_GROUP]。

只输出以下三个段落，不要输出 JSON，不要解释。字段用英文竖线 | 分隔。

[ENTITY]
mention | type | normalized_name | evidence

[RELATION]
source | relation_type | target | cross_chunk | involved_chunk_ids | evidence | polarity | certainty

[LOGIC_GROUP]
logic_type | members | result | involved_chunk_ids | evidence

evidence 格式：
- evidence 是一个数组式字符串，用 ;; 分隔多个证据片段。
- 每个证据片段格式固定为：chunk_id::text_field::start::end::text
- chunk_id 只能填写 {context_chunk_id_for_prompt or target_chunk_id} 或 {target_chunk_id}。
- text_field 只能填写 context_text 或 text。
- start/end 是该证据片段在对应字段中的字符起止位置，必须是整数。
- text 必须是原文中从 start 到 end 对应的连续片段。
- 如果实体是概括出来的事件，mention 可以是概括表达，但 evidence.text 必须来自原文。
- 同一实体在两个 chunk 或同一 chunk 多处出现时，只输出一行 [ENTITY]，把所有提及位置都放进 evidence。

格式规则：
- source/target/members/result 优先填写实体 normalized_name，也可填写 mention。
- [RELATION] 中 relation_type 只能使用：故障触发、故障表征、故障分类、故障处理、规则触发。
- cross_chunk 只能填 true 或 false。
- involved_chunk_ids 用分号连接，例如 {target_chunk_id} 或 {context_chunk_id_for_prompt};{target_chunk_id}。
- 关系 evidence 要记录支持该关系判断的所有关键证据；跨 chunk 关系必须同时记录两侧相关 evidence，并在 involved_chunk_ids 中包含两个 chunk_id。
- polarity 只能是 positive 或 negative。
- certainty 只能是 certain 或 possible。
- [LOGIC_GROUP] 只表示多个故障事件共同满足后才导致上层故障事件的一组 AND 组合逻辑。
- logic_type 只能填 AND。members 和 result 必须引用 [ENTITY] 中的故障事件。
- 不要输出 OR 逻辑组。如果原文表示“任一事件均可导致上层事件”“A 或 B 导致 C”，应拆成多条普通 [RELATION]：A | 故障触发 | C，以及 B | 故障触发 | C。
- LOGIC_GROUP 单独保存，不作为普通实体；它的 members 和 result 通过实体引用，并用 evidence 记录逻辑表达对应的原文依据。
- 如果某个段落没有内容，保留段落标题但下面留空。

示例：
[ENTITY]
润滑油压力过低 | 故障事件 | 润滑油压力过低 | {target_chunk_id}::text::0::7::润滑油压力过低
轴承温度过高 | 故障事件 | 轴承温度过高 | {target_chunk_id}::text::8::15::轴承温度过高
保护停机 | 故障事件 | 保护停机 | {target_chunk_id}::text::18::22::保护停机

[RELATION]
润滑油压力过低 | 故障触发 | 保护停机 | false | {target_chunk_id} | {target_chunk_id}::text::0::22::润滑油压力过低且轴承温度过高时触发保护停机 | positive | possible

[LOGIC_GROUP]
AND | 润滑油压力过低;轴承温度过高 | 保护停机 | {target_chunk_id} | {target_chunk_id}::text::0::22::润滑油压力过低且轴承温度过高时触发保护停机

【context_chunk_id】
{context_chunk_id_for_prompt}

【context_text】
{context_text or ""}

【target_chunk_id】
{target_chunk_id}

【text】
{target_text}
"""


def call_llm(client: Any, model: str, prompt: str, temperature: float, max_tokens: int) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你只输出用户要求的标注草稿格式，不要输出额外解释。"},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def split_sections(text: str) -> Dict[str, List[str]]:
    sections = {"ENTITY": [], "CONTEXT_ENTITY": [], "TARGET_ENTITY": [], "RELATION": [], "LOGIC_GROUP": []}
    current: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        marker = re.fullmatch(r"\[(ENTITY|CONTEXT_ENTITY|TARGET_ENTITY|RELATION|LOGIC_GROUP)\]", line, re.I)
        if marker:
            current = marker.group(1).upper()
            continue
        if current and line:
            sections[current].append(line)
    return sections


def split_pipe_line(line: str) -> List[str]:
    line = re.sub(r"^\s*[-*\d.、]+\s*", "", line).strip()
    return [part.strip() for part in line.split("|")]


def parse_int(value: str, default: int = -1) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


def normalize_choice(value: str, allowed: List[str], default: str) -> str:
    return value if value in allowed else default


def parse_evidence(value: str) -> List[Dict[str, Any]]:
    evidence_items: List[Dict[str, Any]] = []
    for item in str(value or "").split(";;"):
        item = item.strip()
        if not item:
            continue
        parts = item.split("::", 4)
        if len(parts) == 5:
            evidence_items.append(
                {
                    "chunk_id": parts[0],
                    "text_field": parts[1],
                    "start": parse_int(parts[2], 0),
                    "end": parse_int(parts[3], 0),
                    "text": parts[4],
                }
            )
        elif len(parts) == 3:
            evidence_items.append({"chunk_id": parts[0], "text_field": parts[1], "start": None, "end": None, "text": parts[2]})
        else:
            evidence_items.append({"chunk_id": "", "text_field": "", "start": None, "end": None, "text": item})
    return evidence_items


def parse_unified_entities(lines: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    entities: List[Dict[str, Any]] = []
    name_to_id: Dict[str, str] = {}
    seen: Dict[str, str] = {}
    for line in lines:
        parts = split_pipe_line(line)
        if len(parts) < 3:
            continue
        mention = parts[0]
        entity_type = parts[1]
        normalized_name = parts[2] or mention
        evidence = parse_evidence(parts[3] if len(parts) > 3 else "")
        compact_name = re.sub(r"\s+", "", normalized_name or mention)
        dedupe_key = f"{entity_type}::{compact_name}"
        if dedupe_key in seen:
            entity = next(item for item in entities if item["id"] == seen[dedupe_key])
            entity["evidence"].extend(evidence)
            continue
        entity_id = f"E{len(entities) + 1}"
        entity = {
            "id": entity_id,
            "mention": mention,
            "type": entity_type,
            "normalized_name": normalized_name,
            "evidence": evidence,
        }
        entities.append(entity)
        seen[dedupe_key] = entity_id
        for key in {mention, normalized_name, entity_id}:
            if key:
                name_to_id[key] = entity_id
    return entities, name_to_id


def resolve_ref(value: str, name_to_id: Dict[str, str]) -> str:
    text = str(value).strip()
    if text in name_to_id:
        return name_to_id[text]
    compact = re.sub(r"\s+", "", text)
    for name, entity_id in name_to_id.items():
        if re.sub(r"\s+", "", name) == compact:
            return entity_id
    return text


def parse_involved_chunks(value: str, default_chunk_id: str) -> List[str]:
    chunks = [item.strip() for item in re.split(r"[;,，；]", value or "") if item.strip()]
    return chunks or [default_chunk_id]


def parse_relations(lines: List[str], name_to_id: Dict[str, str], target_chunk_id: str) -> List[Dict[str, Any]]:
    relations: List[Dict[str, Any]] = []
    for line in lines:
        parts = split_pipe_line(line)
        if len(parts) < 3:
            continue
        involved = parse_involved_chunks(parts[4] if len(parts) > 4 else "", target_chunk_id)
        relations.append(
            {
                "id": f"R{len(relations) + 1}",
                "source": resolve_ref(parts[0], name_to_id),
                "relation_type": parts[1],
                "target": resolve_ref(parts[2], name_to_id),
                "cross_chunk": parse_bool(parts[3]) if len(parts) > 3 else len(involved) > 1,
                "involved_chunk_ids": involved,
                "evidence": parse_evidence(parts[5] if len(parts) > 5 else ""),
                "polarity": normalize_choice(parts[6], ["positive", "negative"], "positive") if len(parts) > 6 else "positive",
                "certainty": normalize_choice(parts[7], ["certain", "possible"], "possible") if len(parts) > 7 else "possible",
            }
        )
    return relations


def parse_logic_groups(lines: List[str], name_to_id: Dict[str, str], target_chunk_id: str) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for line in lines:
        parts = split_pipe_line(line)
        if len(parts) < 3:
            continue
        logic_type = str(parts[0]).strip().upper()
        if logic_type != "AND":
            continue
        members = [resolve_ref(item, name_to_id) for item in re.split(r"[;,，；]", parts[1]) if item.strip()]
        groups.append(
            {
                "id": f"LG{len(groups) + 1}",
                "logic_type": "AND",
                "members": members,
                "result": resolve_ref(parts[2], name_to_id),
                "relation_type": "组合导致",
                "involved_chunk_ids": parse_involved_chunks(parts[3] if len(parts) > 3 else "", target_chunk_id),
                "evidence": parse_evidence(parts[4] if len(parts) > 4 else ""),
            }
        )
    return groups


def parse_llm_output(
    llm_output: str,
    context_chunk_id: str,
    target_chunk_id: str,
    context_text: str,
    target_text: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    sections = split_sections(llm_output)
    entities, name_to_id = parse_unified_entities(sections["ENTITY"])
    relations = parse_relations(sections["RELATION"], name_to_id, target_chunk_id)
    logic_groups = parse_logic_groups(sections["LOGIC_GROUP"], name_to_id, target_chunk_id)
    return entities, [], [], relations, logic_groups


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_sample_id(chunk: Dict[str, Any]) -> str:
    return f"{str(chunk.get('file_version_id') or get_file_id(chunk) or 'unknown_file')}_chunk_{get_chunk_id(chunk)}"


def build_row(
    chunk: Dict[str, Any],
    context_chunk: Optional[Dict[str, Any]],
    entities: List[Dict[str, Any]],
    context_entities: List[Dict[str, Any]],
    target_entities: List[Dict[str, Any]],
    relations: List[Dict[str, Any]],
    logic_groups: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "sample_id": build_sample_id(chunk),
        "file_id": get_file_id(chunk),
        "chapter_id": get_chapter_id(chunk),
        "source_type": get_source_type(chunk),
        "context_chunk_id": get_chunk_id(context_chunk) if context_chunk else "",
        "target_chunk_id": get_chunk_id(chunk),
        "context_text": get_chunk_text(context_chunk),
        "text": get_chunk_text(chunk),
        "entities_json": json.dumps(entities, ensure_ascii=False),
        "context_entities_json": json.dumps(context_entities, ensure_ascii=False),
        "target_entities_json": json.dumps(target_entities, ensure_ascii=False),
        "relations_json": json.dumps(relations, ensure_ascii=False),
        "logic_groups_json": json.dumps(logic_groups, ensure_ascii=False),
        "status": "draft",
        "reviewer_notes": "",
    }


def process_chunk_for_annotation(
    *,
    index: int,
    total: int,
    chunk: Dict[str, Any],
    prev_chunk: Optional[Dict[str, Any]],
    client: Any,
    model: str,
    temperature: float,
    max_tokens: int,
    dry_run: bool,
    debug_context: bool,
    sleep_seconds: float,
) -> Dict[str, Any]:
    use_context, context_reason = explain_context_decision(prev_chunk, chunk)
    context_chunk = prev_chunk if use_context else None
    context_chunk_id = get_chunk_id(context_chunk) if context_chunk else ""
    target_chunk_id = get_chunk_id(chunk)
    context_text = get_chunk_text(context_chunk)
    target_text = get_chunk_text(chunk)
    prompt = build_prompt(context_text, target_text, context_chunk_id, target_chunk_id)

    if dry_run:
        llm_output = ""
        entities: List[Dict[str, Any]] = []
        context_entities: List[Dict[str, Any]] = []
        target_entities: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []
        logic_groups: List[Dict[str, Any]] = []
    else:
        llm_output = call_llm(client, model, prompt, temperature, max_tokens)
        entities, context_entities, target_entities, relations, logic_groups = parse_llm_output(
            llm_output, context_chunk_id, target_chunk_id, context_text, target_text
        )

    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

    row = build_row(chunk, context_chunk, entities, context_entities, target_entities, relations, logic_groups)
    raw_record = {
        "sample_id": row["sample_id"],
        "file_id": row["file_id"],
        "chapter_id": row["chapter_id"],
        "source_type": row["source_type"],
        "context_chunk_id": context_chunk_id or None,
        "target_chunk_id": target_chunk_id,
        "uses_context": bool(context_chunk),
        "context_reason": context_reason,
        "model": model if not dry_run else "dry-run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "llm_output": llm_output,
    }
    debug_suffix = f" reason={context_reason}" if debug_context else ""
    log_line = (
        f"[{index + 1}/{total}] {row['sample_id']} "
        f"context={bool(context_chunk)}{debug_suffix} "
        f"entities={len(entities)} relations={len(relations)} logic_groups={len(logic_groups)}"
    )
    return {"index": index, "row": row, "raw_record": raw_record, "log_line": log_line}


def build_error_result(index: int, total: int, chunk: Dict[str, Any], prev_chunk: Optional[Dict[str, Any]], model: str, error: BaseException) -> Dict[str, Any]:
    use_context, context_reason = explain_context_decision(prev_chunk, chunk)
    context_chunk = prev_chunk if use_context else None
    row = build_row(chunk, context_chunk, [], [], [], [], [])
    error_text = f"{type(error).__name__}: {error}"
    row["status"] = "needs_fix"
    row["reviewer_notes"] = f"LLM annotation failed: {error_text}"
    raw_record = {
        "sample_id": row["sample_id"],
        "file_id": row["file_id"],
        "chapter_id": row["chapter_id"],
        "source_type": row["source_type"],
        "context_chunk_id": get_chunk_id(context_chunk) if context_chunk else None,
        "target_chunk_id": get_chunk_id(chunk),
        "uses_context": bool(context_chunk),
        "context_reason": context_reason,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "llm_output": "",
        "error": error_text,
        "traceback": traceback.format_exc(),
    }
    return {"index": index, "row": row, "raw_record": raw_record, "log_line": f"[{index + 1}/{total}] {row['sample_id']} FAILED {error_text}", "failed": True}


def generate_annotation_dataset(
    *,
    chunks: List[Dict[str, Any]],
    output_csv: Path,
    raw_jsonl: Path,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float = 0.1,
    max_tokens: int = 2200,
    workers: int = 1,
    dry_run: bool = False,
    debug_context: bool = False,
    sleep_seconds: float = 0.0,
    continue_on_error: bool = True,
) -> Dict[str, Any]:
    if os.getenv("LLM_MODE", "single") == "two-stage":
        from llm_ft_extractor import generate_annotation_dataset_two_stage

        return generate_annotation_dataset_two_stage(
            chunks=chunks,
            output_csv=output_csv,
            raw_jsonl=raw_jsonl,
            base_url=os.getenv("LLM_FT_BASE_URL", base_url),
            api_key=os.getenv("LLM_FT_API_KEY", api_key),
            entity_model=os.getenv("LLM_FT_ENTITY_MODEL", ""),
            relation_model=os.getenv("LLM_FT_RELATION_MODEL", ""),
            entity_rules_file=os.getenv("LLM_FT_ENTITY_RULES_FILE", ""),
            temperature=temperature,
            max_tokens=max_tokens,
            workers=workers,
            dry_run=dry_run,
            debug_context=debug_context,
            sleep_seconds=sleep_seconds,
            continue_on_error=continue_on_error,
        )

    if output_csv.exists():
        output_csv.unlink()
    if raw_jsonl.exists():
        raw_jsonl.unlink()
    chunks = sort_chunks(chunks)
    client = None
    if not dry_run:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
    indexed_inputs = [(index, chunk, chunks[index - 1] if index > 0 else None) for index, chunk in enumerate(chunks)]
    results: List[Dict[str, Any]] = []
    if workers <= 1:
        for index, chunk, prev_chunk in indexed_inputs:
            try:
                result = process_chunk_for_annotation(
                    index=index,
                    total=len(chunks),
                    chunk=chunk,
                    prev_chunk=prev_chunk,
                    client=client,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    dry_run=dry_run,
                    debug_context=debug_context,
                    sleep_seconds=sleep_seconds,
                )
            except Exception as exc:
                if not continue_on_error:
                    raise
                result = build_error_result(index, len(chunks), chunk, prev_chunk, model, exc)
            print(result["log_line"])
            results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    process_chunk_for_annotation,
                    index=index,
                    total=len(chunks),
                    chunk=chunk,
                    prev_chunk=prev_chunk,
                    client=client,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    dry_run=dry_run,
                    debug_context=debug_context,
                    sleep_seconds=sleep_seconds,
                ): (index, chunk, prev_chunk)
                for index, chunk, prev_chunk in indexed_inputs
            }
            for future in as_completed(future_map):
                index, chunk, prev_chunk = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    if not continue_on_error:
                        raise
                    result = build_error_result(index, len(chunks), chunk, prev_chunk, model, exc)
                print(result["log_line"])
                results.append(result)
    results.sort(key=lambda item: item["index"])
    rows = [item["row"] for item in results]
    write_csv(output_csv, rows)
    for item in results:
        append_jsonl(raw_jsonl, item["raw_record"])
    return {
        "samples": len(rows),
        "failed": sum(1 for item in results if item.get("failed")),
        "output_csv": str(output_csv),
        "raw_jsonl": str(raw_jsonl),
    }
