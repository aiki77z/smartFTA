import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"
DEFAULT_OUTPUT_CSV = BASE_DIR / "ai_annotation_draft.csv"
DEFAULT_RAW_JSONL = BASE_DIR / "raw_ai_annotations.jsonl"

CSV_COLUMNS = [
    "sample_id",
    "file_id",
    "chapter_id",
    "source_type",
    "context_chunk_id",
    "target_chunk_id",
    "context_text",
    "text",
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
GENERAL_DOC_TYPES = ["pdf", "docx", "doc", "txt", "md", "markdown", "manual_pdf", "manual_document", "unstructured"]


def load_env_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
    # MongoDB chunks currently store the human-readable chapter name in `chapter`.
    # Prefer that field because it matches how users inspect chunk metadata.
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


def fetch_chunks(args: argparse.Namespace) -> List[Dict[str, Any]]:
    from pymongo import MongoClient

    query: Dict[str, Any] = {}
    if args.file_version_id:
        query["file_version_id"] = args.file_version_id
    if args.file_id:
        query["file_id"] = args.file_id
    client = MongoClient(args.mongo_uri)
    chunks = list(client[args.mongo_db][args.collection].find(query))
    chunks = sort_chunks(chunks)
    return chunks[: args.limit] if args.limit else chunks


def build_prompt(context_text: str, target_text: str, context_chunk_id: str, target_chunk_id: str) -> str:
    return f"""你是工业设备故障知识标注助手。请阅读一个滑动窗口样本，并生成供人工审核的标注草稿。

窗口字段：
- context_text：前一个 chunk，仅用于理解上下文。
- text：当前 target chunk，是主要标注对象。

标注范围：
1. 只标注与当前 target chunk 相关的内容。
2. 单独只出现在 context_text 中、且没有和当前 text 发生关系的实体或关系，不要输出。
3. 出现在当前 text 中的实体和关系要输出。
4. context_text 中的实体只有在参与跨 chunk 关系或跨 chunk 逻辑组时才输出到 [CONTEXT_ENTITY]。
5. 如果故障事件需要从原文概括生成，可以输出 normalized_name 和 mention；start/end 可填 -1，但 evidence 必须来自原文。
6. 不要编造没有证据的关系。不能确定时 certainty 填 possible。

实体类型只能使用以下 5 类：
1. 故障事件：设备异常、故障现象、故障原因、停机/失效/异常状态等。例如：编码器通信故障、反馈信号丢失、主轴保护停机。
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
6. 参与组合：故障事件 -> 逻辑组。该关系不要在 [RELATION] 中直接输出，遇到 AND/OR 组合时输出 [LOGIC_GROUP]。
7. 组合导致：逻辑组 -> 故障事件。该关系不要在 [RELATION] 中直接输出，遇到 AND/OR 组合时输出 [LOGIC_GROUP]。

只输出以下四个段落，不要输出 JSON，不要解释。字段用英文竖线 | 分隔。

[CONTEXT_ENTITY]
mention | type | normalized_name | start | end | annotation_role | evidence

[TARGET_ENTITY]
mention | type | normalized_name | start | end | evidence

[RELATION]
source | relation_type | target | cross_chunk | involved_chunk_ids | evidence | polarity | certainty

[LOGIC_GROUP]
logic_type | members | result | involved_chunk_ids | evidence

格式规则：
- source/target/members/result 优先填写实体 normalized_name，也可填写 mention。
- [RELATION] 中 relation_type 只能使用：故障触发、故障表征、故障分类、故障处理、规则触发。
- cross_chunk 只能填 true 或 false。
- involved_chunk_ids 用分号连接，例如 {target_chunk_id} 或 {context_chunk_id};{target_chunk_id}。
- evidence 如果涉及多个 chunk，用两个证据片段拼接，格式为：chunk_id::text_field::证据文本;;chunk_id::text_field::证据文本。
- text_field 只能是 context_text 或 text。
- polarity 只能是 positive 或 negative。
- certainty 只能是 certain 或 possible。
- [LOGIC_GROUP] 表示多个故障事件通过同一个逻辑门连接到上层故障事件的一组组合逻辑。
- logic_type 只能填 AND 或 OR。members 只能是故障事件实体，result 只能是故障事件实体。
- 例如“润滑油压力过低且轴承温度过高时触发保护停机”应输出：
  AND | 润滑油压力过低;轴承温度过高 | 保护停机 | {target_chunk_id} | {target_chunk_id}::text::润滑油压力过低且轴承温度过高时触发保护停机
- LOGIC_GROUP 在图谱中等价于：members --参与组合--> 逻辑组，逻辑组 --组合导致--> result。
- 如果某个段落没有内容，保留段落标题但下面留空。

【context_chunk_id】
{context_chunk_id or ""}

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
    sections = {"CONTEXT_ENTITY": [], "TARGET_ENTITY": [], "RELATION": [], "LOGIC_GROUP": []}
    current: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        marker = re.fullmatch(r"\[(CONTEXT_ENTITY|TARGET_ENTITY|RELATION|LOGIC_GROUP)\]", line, re.I)
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


def find_span(text: str, mention: str, start: int, end: int) -> Tuple[Optional[int], Optional[int]]:
    if start >= 0 and end >= start:
        return start, end
    if not mention:
        return None, None
    idx = text.find(mention)
    if idx < 0:
        return None, None
    return idx, idx + len(mention)


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


def normalize_choice(value: str, allowed: List[str], default: str) -> str:
    return value if value in allowed else default


def parse_entities(
    lines: List[str],
    chunk_id: str,
    text_field: str,
    text: str,
    id_prefix: str,
    is_context: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    entities: List[Dict[str, Any]] = []
    name_to_id: Dict[str, str] = {}
    for line in lines:
        parts = split_pipe_line(line)
        if len(parts) < 3:
            continue
        entity_id = f"{id_prefix}_E{len(entities) + 1}"
        start_raw = parse_int(parts[3]) if len(parts) > 3 else -1
        end_raw = parse_int(parts[4]) if len(parts) > 4 else -1
        start, end = find_span(text, parts[0], start_raw, end_raw)
        entity = {
            "id": entity_id,
            "mention": parts[0],
            "type": parts[1],
            "normalized_name": parts[2] or parts[0],
            "chunk_id": chunk_id,
            "text_field": text_field,
            "start": start,
            "end": end,
        }
        if is_context:
            entity["annotation_role"] = parts[5] if len(parts) > 5 and parts[5] else "cross_chunk_relation_endpoint"
            if len(parts) > 6:
                entity["evidence"] = parts[6]
        elif len(parts) > 5:
            entity["evidence"] = parts[5]
        entities.append(entity)
        for key in {entity["mention"], entity["normalized_name"], entity["id"]}:
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


def parse_evidence(value: str) -> List[Dict[str, str]]:
    evidence_items: List[Dict[str, str]] = []
    for item in str(value or "").split(";;"):
        item = item.strip()
        if not item:
            continue
        parts = item.split("::", 2)
        if len(parts) == 3:
            evidence_items.append({"chunk_id": parts[0], "text_field": parts[1], "text": parts[2]})
        else:
            evidence_items.append({"chunk_id": "", "text_field": "", "text": item})
    return evidence_items


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
        members = [resolve_ref(item, name_to_id) for item in re.split(r"[;,，；]", parts[1]) if item.strip()]
        groups.append(
            {
                "id": f"LG{len(groups) + 1}",
                "logic_type": parts[0],
                "members": members,
                "result": resolve_ref(parts[2], name_to_id),
                "relation_type": "组合导致",
                "involved_chunk_ids": parse_involved_chunks(parts[3] if len(parts) > 3 else "", target_chunk_id),
                "evidence": parts[4] if len(parts) > 4 else "",
            }
        )
    return groups


def parse_llm_output(
    llm_output: str,
    context_chunk_id: str,
    target_chunk_id: str,
    context_text: str,
    target_text: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    sections = split_sections(llm_output)
    context_entities, context_map = parse_entities(
        sections["CONTEXT_ENTITY"], context_chunk_id, "context_text", context_text, context_chunk_id or "context", True
    )
    target_entities, target_map = parse_entities(
        sections["TARGET_ENTITY"], target_chunk_id, "text", target_text, target_chunk_id, False
    )
    name_to_id = {**context_map, **target_map}
    relations = parse_relations(sections["RELATION"], name_to_id, target_chunk_id)
    logic_groups = parse_logic_groups(sections["LOGIC_GROUP"], name_to_id, target_chunk_id)
    return context_entities, target_entities, relations, logic_groups


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_sample_id(chunk: Dict[str, Any]) -> str:
    return f"{str(chunk.get('file_version_id') or get_file_id(chunk) or 'unknown_file')}_chunk_{get_chunk_id(chunk)}"


def build_row(
    chunk: Dict[str, Any],
    context_chunk: Optional[Dict[str, Any]],
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
        "context_entities_json": json.dumps(context_entities, ensure_ascii=False),
        "target_entities_json": json.dumps(target_entities, ensure_ascii=False),
        "relations_json": json.dumps(relations, ensure_ascii=False),
        "logic_groups_json": json.dumps(logic_groups, ensure_ascii=False),
        "status": "draft",
        "reviewer_notes": "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AI draft annotations from MongoDB chunks.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="环境变量文件路径，默认读取本目录 .env。")
    parser.add_argument("--mongo-uri", default=None)
    parser.add_argument("--mongo-db", default=None)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--file-version-id", default=None, help="只处理指定文件版本。")
    parser.add_argument("--file-id", default=None)
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--raw-jsonl", default=str(DEFAULT_RAW_JSONL))
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个 chunk，用于试跑。")
    parser.add_argument("--sleep", type=float, default=0.0, help="每次 LLM 调用后的等待秒数。")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=2200)
    parser.add_argument("--dry-run", action="store_true", help="不调用 LLM，只生成空标注 CSV 和 raw prompt。")
    parser.add_argument("--debug-context", action="store_true", help="打印每个 chunk 是否使用上下文及原因。")
    args = parser.parse_args()

    load_env_files([Path(args.env_file)])
    args.mongo_uri = args.mongo_uri or os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    args.mongo_db = args.mongo_db or os.getenv("MONGO_DB_NAME", "fault-tree-trial")
    args.collection = args.collection or os.getenv("ANNOTATION_CHUNKS_COLLECTION", "chunks")
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = os.getenv("OPENAI_MODEL_NAME", "qwen3.5-plus")
    if not args.dry_run and not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY，请在本目录 .env 中配置。")

    chunks = fetch_chunks(args)
    if not chunks:
        raise RuntimeError("没有从 MongoDB 查询到 chunks，请检查数据库名、集合名或过滤条件。")

    client = None
    if not args.dry_run:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)

    raw_path = Path(args.raw_jsonl)
    if raw_path.exists():
        raw_path.unlink()
    output_rows: List[Dict[str, Any]] = []

    for index, chunk in enumerate(chunks):
        prev_chunk = chunks[index - 1] if index > 0 else None
        use_context, context_reason = explain_context_decision(prev_chunk, chunk)
        context_chunk = prev_chunk if use_context else None
        context_chunk_id = get_chunk_id(context_chunk) if context_chunk else ""
        target_chunk_id = get_chunk_id(chunk)
        context_text = get_chunk_text(context_chunk)
        target_text = get_chunk_text(chunk)
        prompt = build_prompt(context_text, target_text, context_chunk_id, target_chunk_id)

        if args.dry_run:
            llm_output = ""
            context_entities: List[Dict[str, Any]] = []
            target_entities: List[Dict[str, Any]] = []
            relations: List[Dict[str, Any]] = []
            logic_groups: List[Dict[str, Any]] = []
        else:
            llm_output = call_llm(client, model, prompt, args.temperature, args.max_tokens)
            context_entities, target_entities, relations, logic_groups = parse_llm_output(
                llm_output, context_chunk_id, target_chunk_id, context_text, target_text
            )

        row = build_row(chunk, context_chunk, context_entities, target_entities, relations, logic_groups)
        output_rows.append(row)
        append_jsonl(
            raw_path,
            {
                "sample_id": row["sample_id"],
                "file_id": row["file_id"],
                "chapter_id": row["chapter_id"],
                "source_type": row["source_type"],
                "context_chunk_id": context_chunk_id or None,
                "target_chunk_id": target_chunk_id,
                "uses_context": bool(context_chunk),
                "context_reason": context_reason,
                "model": model if not args.dry_run else "dry-run",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "prompt": prompt,
                "llm_output": llm_output,
            },
        )
        debug_suffix = f" reason={context_reason}" if args.debug_context else ""
        print(
            f"[{index + 1}/{len(chunks)}] {row['sample_id']} "
            f"context={bool(context_chunk)}{debug_suffix} "
            f"target_entities={len(target_entities)} relations={len(relations)}"
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    write_csv(Path(args.output_csv), output_rows)
    print(f"CSV 已保存: {args.output_csv}")
    print(f"Raw JSONL 已保存: {args.raw_jsonl}")


if __name__ == "__main__":
    main()
