from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from openai import OpenAI
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    MONGO_DB_NAME,
    MONGO_URI,
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None

client = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

trees_col = db["fault_trees"]
versions_col = db["fault_tree_versions"]
files_col = db["files"]
file_versions_col = db["file_versions"]
chunks_col = db["chunks"]
work_orders_col = db["work_orders"]
maintenance_cases_col = db["maintenance_cases"]
top_event_catalog_col = db["top_event_catalog"]
generation_jobs_col = db["generation_jobs"]
generation_job_items_col = db["generation_job_items"]

TOP_EVENT_PRIORITY_HINTS = ("故障", "异常", "报警", "停机", "失败", "超时", "触发", "中断")
TOP_EVENT_NEGATIVE_HINTS = ("接线错误", "接口松动", "参数错误", "过流", "过热", "损坏", "松动")
GRAPH_PROPERTY_UPDATE_FIELDS = {
    "description",
    "errorLevel",
    "priority",
    "probability",
    "showProbability",
    "rule",
    "investigateMethod",
}

STATUS_INACTIVE_VALUES = ("deleted", "archived")
CHUNK_REF_SEPARATOR = "::"
TOP_EVENT_VECTOR_CANDIDATE_LIMIT = 10
TOP_EVENT_VECTOR_CANDIDATE_MAX = 15

_embedding_client: Optional[OpenAI] = None
_top_event_embedding_cache: Dict[str, List[float]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _dedupe_keep_order(values: Optional[List[Any]]) -> List[Any]:
    seen = set()
    result = []
    for value in values or []:
        if value in (None, ""):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip()


def _normalize_file_name(value: Any) -> str:
    text = _normalize_identifier(value)
    text = re.sub(r"[\\/]+", "/", text)
    return text.lower()


def _make_chunk_ref(file_version_id: Any, chunk_id: Any) -> str:
    version = _normalize_identifier(file_version_id)
    chunk = _normalize_identifier(chunk_id)
    if version and chunk:
        return f"{version}{CHUNK_REF_SEPARATOR}{chunk}"
    return chunk


def _split_chunk_ref(value: Any) -> tuple[Optional[str], str]:
    text = _normalize_identifier(value)
    if not text:
        return None, ""
    if CHUNK_REF_SEPARATOR not in text:
        return None, text
    file_version_id, chunk_id = text.split(CHUNK_REF_SEPARATOR, 1)
    return _normalize_identifier(file_version_id) or None, _normalize_identifier(chunk_id)


def _make_catalog_doc_id(file_version_id: Any, normalized_name: Any) -> str:
    return f"{_normalize_identifier(file_version_id)}::{_normalize_identifier(normalized_name)}"


def _embedding_openai() -> Optional[OpenAI]:
    global _embedding_client
    if not EMBEDDING_API_KEY or not EMBEDDING_MODEL:
        return None
    if _embedding_client is None:
        _embedding_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)
    return _embedding_client


def _build_top_event_semantic_text(
    name: str,
    *,
    normalized_name: Optional[str] = None,
    aliases: Optional[List[str]] = None,
) -> str:
    cleaned_aliases = _dedupe_keep_order([_normalize_identifier(alias) for alias in aliases or [] if _normalize_identifier(alias)])
    lines = [
        f"top_event:{_normalize_identifier(name)}",
        f"normalized_name:{_normalize_identifier(normalized_name) or _normalize_identifier(name)}",
    ]
    if cleaned_aliases:
        lines.append(f"aliases:{' | '.join(cleaned_aliases)}")
    return "\n".join(line for line in lines if line.strip())


def _embed_strings_ordered(strings: List[str]) -> List[Optional[List[float]]]:
    if not strings or not EMBEDDING_MODEL:
        return [None] * len(strings)
    client = _embedding_openai()
    if not client:
        return [None] * len(strings)

    unique: List[str] = []
    seen: Dict[str, int] = {}
    for text in strings:
        key = text or ""
        if key not in seen:
            seen[key] = len(unique)
            unique.append(key)

    vecs_for_unique: List[Optional[List[float]]] = [None] * len(unique)
    to_request: List[str] = []
    request_slots: List[int] = []
    for index, text in enumerate(unique):
        if text in _top_event_embedding_cache:
            vecs_for_unique[index] = _top_event_embedding_cache[text]
        else:
            to_request.append(text)
            request_slots.append(index)

    batch_size = max(1, int(EMBEDDING_BATCH_SIZE or 10))
    for start in range(0, len(to_request), batch_size):
        chunk = to_request[start : start + batch_size]
        try:
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=chunk)
            for offset, item in enumerate(response.data):
                vec = item.embedding
                text = chunk[offset]
                _top_event_embedding_cache[text] = vec
                vecs_for_unique[request_slots[start + offset]] = vec
        except Exception:
            for offset, text in enumerate(chunk):
                vecs_for_unique[request_slots[start + offset]] = _top_event_embedding_cache.get(text)

    lookup = {text: vecs_for_unique[index] for index, text in enumerate(unique)}
    return [lookup.get(text or "") for text in strings]


def _build_top_event_embedding_fields(semantic_text: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "semantic_text": semantic_text,
        "embedding": None,
        "embedding_model": None,
        "embedding_updated_at": None,
    }
    if not semantic_text or not EMBEDDING_MODEL:
        return payload

    embedding = _embed_strings_ordered([semantic_text])[0]
    if embedding:
        payload["embedding"] = embedding
        payload["embedding_model"] = EMBEDDING_MODEL
        payload["embedding_updated_at"] = _now()
    return payload


def _cosine_similarity(left: Optional[List[float]], right: Optional[List[float]]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    numerator = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for lvalue, rvalue in zip(left, right):
        numerator += float(lvalue) * float(rvalue)
        left_norm += float(lvalue) * float(lvalue)
        right_norm += float(rvalue) * float(rvalue)
    if left_norm <= 0.0 or right_norm <= 0.0:
        return -1.0
    return numerator / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _normalize_chunk_refs(
    values: Optional[List[Any]],
    *,
    file_version_id: Optional[Any] = None,
) -> List[str]:
    normalized_file_version_id = _normalize_identifier(file_version_id)
    result: List[str] = []
    seen = set()

    for value in values or []:
        version_id, chunk_id = _split_chunk_ref(value)
        plain_chunk_id = _normalize_identifier(chunk_id)
        effective_version_id = _normalize_identifier(version_id) or normalized_file_version_id
        if not plain_chunk_id:
            continue
        chunk_ref = _make_chunk_ref(effective_version_id, plain_chunk_id)
        if not chunk_ref or chunk_ref in seen:
            continue
        seen.add(chunk_ref)
        result.append(chunk_ref)

    return result


def _normalize_file_version_ids(values: Optional[List[Any]], *, fallback_to_active: bool = False) -> List[str]:
    normalized = _dedupe_keep_order([_normalize_identifier(value) for value in (values or []) if _normalize_identifier(value)])
    if normalized or not fallback_to_active:
        return normalized
    return list_active_file_version_ids()


def _make_scope_key(file_version_ids: Optional[List[Any]]) -> str:
    normalized = sorted(_normalize_file_version_ids(file_version_ids))
    return "|".join(normalized)


def _build_file_version_filter(
    selected_file_version_ids: Optional[List[Any]],
    *,
    field_name: str = "file_version_id",
    fallback_to_active: bool = False,
) -> Optional[Dict[str, Any]]:
    normalized = _normalize_file_version_ids(selected_file_version_ids, fallback_to_active=fallback_to_active)
    if not normalized:
        return None
    return {field_name: {"$in": normalized}}


def list_active_file_version_ids() -> List[str]:
    cursor = file_versions_col.find({"is_active": True, "status": {"$nin": list(STATUS_INACTIVE_VALUES)}}, {"_id": 0, "file_version_id": 1})
    result = []
    for doc in cursor:
        file_version_id = _normalize_identifier(doc.get("file_version_id"))
        if file_version_id:
            result.append(file_version_id)
    return _dedupe_keep_order(result)


def resolve_selected_file_version_ids(
    selected_file_version_ids: Optional[List[Any]],
    *,
    fallback_to_active: bool = True,
    require_existing: bool = True,
    require_active: bool = True,
) -> List[str]:
    explicit_selection = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    normalized = _normalize_file_version_ids(selected_file_version_ids, fallback_to_active=fallback_to_active)
    if not normalized:
        return []

    effective_require_active = require_active and not explicit_selection
    if not require_existing and not effective_require_active:
        return normalized

    query: Dict[str, Any] = {
        "file_version_id": {"$in": normalized},
        "status": {"$nin": list(STATUS_INACTIVE_VALUES)},
    }
    if effective_require_active:
        query["is_active"] = True
    docs = list(file_versions_col.find(query, {"_id": 0, "file_version_id": 1}))
    found = {_normalize_identifier(doc.get("file_version_id")) for doc in docs if _normalize_identifier(doc.get("file_version_id"))}
    missing = [file_version_id for file_version_id in normalized if file_version_id not in found]
    if missing and require_existing:
        raise ValueError(f"Unknown or inactive file_version_id(s): {', '.join(missing)}")
    return [file_version_id for file_version_id in normalized if file_version_id in found] if require_existing or effective_require_active else normalized


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    copied = dict(doc)
    copied.pop("_id", None)
    return copied


def _seconds_between(started_at: Optional[datetime], finished_at: Optional[datetime] = None) -> Optional[float]:
    if not started_at:
        return None
    end_time = finished_at or _now()
    return round(max(0.0, (end_time - started_at).total_seconds()), 3)


def _decorate_runtime_fields(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    copied = _strip_mongo_id(doc)
    if not copied:
        return None

    started_at = copied.get("started_at")
    finished_at = copied.get("finished_at")
    copied["elapsed_seconds"] = _seconds_between(started_at, finished_at)
    copied["duration_seconds"] = _seconds_between(started_at, finished_at) if finished_at else None
    return copied


def _get_chunk_identifier(doc: Optional[Dict[str, Any]]) -> Any:
    if not doc:
        return None
    return doc.get("id", doc.get("chunk_id"))


def _normalize_chunk_import_doc(
    doc: Dict[str, Any],
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    normalized = dict(doc)
    chunk_id = normalized.get("chunk_id")
    doc_id = normalized.get("id")
    if chunk_id in (None, "") and doc_id not in (None, ""):
        normalized["chunk_id"] = doc_id
    if doc_id in (None, "") and chunk_id not in (None, ""):
        normalized["id"] = chunk_id
    normalized_file_id = _normalize_identifier(file_id) or _normalize_identifier(normalized.get("file_id"))
    normalized_file_version_id = _normalize_identifier(file_version_id) or _normalize_identifier(normalized.get("file_version_id"))
    normalized["file_id"] = normalized_file_id
    normalized["file_version_id"] = normalized_file_version_id
    normalized["is_active"] = bool(normalized.get("is_active", is_active))
    if normalized_file_version_id and normalized.get("chunk_id") not in (None, ""):
        normalized["chunk_uid"] = _make_chunk_ref(normalized_file_version_id, normalized.get("chunk_id"))

    # Keep the newer kb-v2 fields, but also materialize the older Luna2000-style
    # fields that GNR and existing debug data already consume reliably.
    body_text = ""
    for key in ("content", "text", "markdown", "raw_text", "page_content", "body"):
        value = normalized.get(key)
        if value not in (None, ""):
            body_text = str(value)
            break
    if body_text:
        normalized.setdefault("content", body_text)
        normalized.setdefault("text", body_text)
        normalized.setdefault("markdown", body_text)

    chunk_name = _normalize_identifier(
        normalized.get("chunk_name")
        or normalized.get("title")
        or normalized.get("heading")
        or normalized.get("chapter")
    )
    section_path = _normalize_identifier(
        normalized.get("section_path")
        or normalized.get("section")
        or normalized.get("chapter_id")
        or normalized.get("chapter")
    )
    chapter = _normalize_identifier(normalized.get("chapter") or normalized.get("section") or chunk_name)
    if chunk_name:
        normalized.setdefault("chunk_name", chunk_name)
        normalized.setdefault("chapter_title", _normalize_identifier(normalized.get("chapter_title")) or chunk_name)
    if section_path:
        normalized.setdefault("section_path", section_path)
        normalized.setdefault("chapter_id", _normalize_identifier(normalized.get("chapter_id")) or section_path)
    if chapter:
        normalized.setdefault("chapter", chapter)
    return normalized


def _normalize_source_record_import_doc(
    doc: Dict[str, Any],
    *,
    source_record_type: str,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    normalized = dict(doc)
    normalized_file_id = _normalize_identifier(file_id) or _normalize_identifier(normalized.get("file_id"))
    normalized_file_version_id = _normalize_identifier(file_version_id) or _normalize_identifier(normalized.get("file_version_id"))
    normalized["file_id"] = normalized_file_id
    normalized["file_version_id"] = normalized_file_version_id
    normalized["is_active"] = bool(normalized.get("is_active", is_active))
    normalized["source_record_type"] = (
        _normalize_identifier(normalized.get("source_record_type")) or _normalize_identifier(source_record_type)
    )

    source_record_id = _normalize_identifier(normalized.get("source_record_id"))
    if not source_record_id:
        fallback_keys = ("record_id", "case_id", "work_order_no")
        for key in fallback_keys:
            candidate = _normalize_identifier(normalized.get(key))
            if candidate:
                source_record_id = candidate
                break
    normalized["source_record_id"] = source_record_id

    if normalized_file_version_id and source_record_id:
        normalized["_id"] = f"{normalized_file_version_id}{CHUNK_REF_SEPARATOR}{source_record_id}"
    return normalized


def _format_scope_mismatch_examples(mismatches: List[str], *, max_items: int = 5) -> str:
    preview = mismatches[:max_items]
    suffix = "" if len(mismatches) <= max_items else f" ... (+{len(mismatches) - max_items} more)"
    return "; ".join(preview) + suffix


def assert_chunk_artifacts_align_with_file_version(
    chunks: List[Dict[str, Any]],
    *,
    file_id: str,
    file_version_id: str,
) -> None:
    normalized_file_id = _normalize_identifier(file_id)
    normalized_file_version_id = _normalize_identifier(file_version_id)
    mismatches: List[str] = []
    for index, chunk in enumerate(chunks or []):
        if not isinstance(chunk, dict):
            continue
        chunk_label = _normalize_identifier(chunk.get("chunk_id") or chunk.get("id")) or f"index={index}"
        embedded_file_id = _normalize_identifier(chunk.get("file_id"))
        embedded_file_version_id = _normalize_identifier(chunk.get("file_version_id"))
        if embedded_file_id and embedded_file_id != normalized_file_id:
            mismatches.append(
                f"chunk {chunk_label} file_id={embedded_file_id} != expected {normalized_file_id}"
            )
        if embedded_file_version_id and embedded_file_version_id != normalized_file_version_id:
            mismatches.append(
                f"chunk {chunk_label} file_version_id={embedded_file_version_id} != expected {normalized_file_version_id}"
            )
    if mismatches:
        raise ValueError(
            "Chunk artifacts do not align with target file version: "
            + _format_scope_mismatch_examples(mismatches)
        )


def assert_relation_artifacts_align_with_file_version(
    relation_rows: List[Dict[str, Any]],
    *,
    file_id: str,
    file_version_id: str,
) -> None:
    normalized_file_id = _normalize_identifier(file_id)
    normalized_file_version_id = _normalize_identifier(file_version_id)
    mismatches: List[str] = []
    for row_index, row in enumerate(relation_rows or []):
        if not isinstance(row, dict):
            continue
        chunk_label = _normalize_identifier(row.get("chunk_id")) or f"row={row_index}"
        row_file_id = _normalize_identifier(row.get("file_id"))
        row_file_version_id = _normalize_identifier(row.get("file_version_id"))
        if row_file_id and row_file_id != normalized_file_id:
            mismatches.append(
                f"relation row {chunk_label} file_id={row_file_id} != expected {normalized_file_id}"
            )
        if row_file_version_id and row_file_version_id != normalized_file_version_id:
            mismatches.append(
                f"relation row {chunk_label} file_version_id={row_file_version_id} != expected {normalized_file_version_id}"
            )
        for rel_index, rel in enumerate(row.get("relations") or []):
            if not isinstance(rel, dict):
                continue
            rel_file_id = _normalize_identifier(rel.get("file_id"))
            rel_file_version_id = _normalize_identifier(rel.get("file_version_id"))
            rel_label = (
                f"relation row {chunk_label} item={rel_index} "
                f"({rel.get('entity1') or '?'}->{rel.get('entity2') or '?'})"
            )
            if rel_file_id and rel_file_id != normalized_file_id:
                mismatches.append(f"{rel_label} file_id={rel_file_id} != expected {normalized_file_id}")
            if rel_file_version_id and rel_file_version_id != normalized_file_version_id:
                mismatches.append(
                    f"{rel_label} file_version_id={rel_file_version_id} != expected {normalized_file_version_id}"
                )
    if mismatches:
        raise ValueError(
            "Relation artifacts do not align with target file version: "
            + _format_scope_mismatch_examples(mismatches)
        )


def assert_source_record_artifacts_align_with_file_version(
    records: List[Dict[str, Any]],
    *,
    file_id: str,
    file_version_id: str,
    expected_source_record_type: str,
) -> None:
    normalized_file_id = _normalize_identifier(file_id)
    normalized_file_version_id = _normalize_identifier(file_version_id)
    normalized_record_type = _normalize_identifier(expected_source_record_type)
    mismatches: List[str] = []
    for index, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        record_label = _normalize_identifier(record.get("source_record_id") or record.get("record_id") or record.get("case_id")) or f"index={index}"
        embedded_file_id = _normalize_identifier(record.get("file_id"))
        embedded_file_version_id = _normalize_identifier(record.get("file_version_id"))
        embedded_record_type = _normalize_identifier(record.get("source_record_type"))
        embedded_record_id = _normalize_identifier(record.get("source_record_id"))
        if embedded_file_id and embedded_file_id != normalized_file_id:
            mismatches.append(f"record {record_label} file_id={embedded_file_id} != expected {normalized_file_id}")
        if embedded_file_version_id and embedded_file_version_id != normalized_file_version_id:
            mismatches.append(
                f"record {record_label} file_version_id={embedded_file_version_id} != expected {normalized_file_version_id}"
            )
        if embedded_record_type and embedded_record_type != normalized_record_type:
            mismatches.append(
                f"record {record_label} source_record_type={embedded_record_type} != expected {normalized_record_type}"
            )
        if not embedded_record_id:
            fallback_id = _normalize_identifier(record.get("record_id") or record.get("case_id") or record.get("work_order_no"))
            if not fallback_id:
                mismatches.append(f"record {record_label} missing source_record_id")
    if mismatches:
        raise ValueError(
            "Source record artifacts do not align with target file version: "
            + _format_scope_mismatch_examples(mismatches)
        )


def _coerce_int_identifier(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _get_max_chunk_numeric_identifier() -> int:
    max_identifier = 0
    for doc in chunks_col.find({}, {"_id": 0, "chunk_id": 1, "id": 1}):
        for key in ("chunk_id", "id"):
            numeric_value = _coerce_int_identifier(doc.get(key))
            if numeric_value is not None and numeric_value > max_identifier:
                max_identifier = numeric_value
    return max_identifier


def _neo4j_available() -> bool:
    return bool(GraphDatabase and NEO4J_PASSWORD)


_neo4j_driver = None


def _get_neo4j_driver():
    global _neo4j_driver
    if not _neo4j_available():
        return None
    if _neo4j_driver is None:
        _neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _neo4j_driver


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", _normalize_text(value))


def _looks_like_fault_code(name: str) -> bool:
    text = _normalize_text(name)
    if re.fullmatch(r"[FA]\d{5}(?:\([A-Z]\))?", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z]{1,6}=?[0-9A-F]{3,6}", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[0-9A-F]{3,6}", text, flags=re.IGNORECASE):
        return True
    return False


def _is_fault_like_entity_type(entity_type: Any) -> bool:
    text = _normalize_text(entity_type)
    if not text:
        return True
    positive_tokens = ("故障", "异常", "报警", "现象")
    return any(token in text for token in positive_tokens)


def _parse_maybe_json(value: Any, default):
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _coerce_chunk_ids(value: Any) -> List[Any]:
    parsed = _parse_maybe_json(value, value)
    if isinstance(parsed, list):
        return _dedupe_keep_order(parsed)
    if isinstance(parsed, str) and parsed.strip():
        if "," in parsed:
            return _dedupe_keep_order([part.strip() for part in parsed.split(",") if part.strip()])
        return [parsed.strip()]
    return []


def _coerce_documents(value: Any) -> List[Dict[str, Any]]:
    parsed = _parse_maybe_json(value, [])
    if not isinstance(parsed, list):
        return []
    docs = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        chunk_id = item.get("chunk_id") or item.get("id")
        if chunk_id in (None, ""):
            continue
        doc = {"chunk_id": chunk_id}
        if item.get("chunk_uid") not in (None, ""):
            doc["chunk_uid"] = item.get("chunk_uid")
        if item.get("file_version_id") not in (None, ""):
            doc["file_version_id"] = item.get("file_version_id")
        docs.append(doc)
    return docs


def _decode_graph_node(raw: Dict[str, Any]) -> Dict[str, Any]:
    props = dict(raw.get("props") or {})
    documents = _coerce_documents(props.get("documents"))
    source_chunk_ids = _coerce_chunk_ids(props.get("source_chunk_ids"))
    if not source_chunk_ids and documents:
        source_chunk_ids = _dedupe_keep_order([doc.get("chunk_id") for doc in documents])
    file_id = props.get("file_id") or raw.get("file_id")
    file_version_id = props.get("file_version_id") or raw.get("file_version_id")
    return {
        "graph_node_id": raw.get("graph_node_id"),
        "file_id": file_id,
        "file_version_id": file_version_id,
        "is_active": bool(props.get("is_active", True)),
        "name": props.get("name") or "",
        "normalized_name": props.get("normalized_name") or props.get("name") or "",
        "entity_type": props.get("entity_type") or "",
        "node_type": props.get("node_type") or ("AND" if "LogicGate" in (raw.get("labels") or []) else "FAULT"),
        "labels": raw.get("labels") or [],
        "description": props.get("description") or "",
        "errorLevel": props.get("errorLevel") or "",
        "priority": props.get("priority"),
        "probability": props.get("probability"),
        "showProbability": props.get("showProbability"),
        "rule": props.get("rule") or "",
        "investigateMethod": props.get("investigateMethod") or "",
        "documents": documents,
        "source_chunk_ids": source_chunk_ids,
        "source_chunk_refs": [_make_chunk_ref(file_version_id, chunk_id) for chunk_id in source_chunk_ids if chunk_id not in (None, "")],
        "support_count": props.get("support_count"),
        "raw_props": props,
    }


def _decode_graph_relation(raw: Dict[str, Any]) -> Dict[str, Any]:
    props = dict(raw.get("rel_props") or {})
    source_chunk_ids = _coerce_chunk_ids(props.get("source_chunk_ids"))
    chunk_id = props.get("chunk_id")
    if chunk_id not in (None, "") and chunk_id not in source_chunk_ids:
        source_chunk_ids.insert(0, chunk_id)
    file_id = props.get("file_id") or raw.get("file_id")
    file_version_id = props.get("file_version_id") or raw.get("file_version_id")
    return {
        "source_graph_node_id": raw.get("source_graph_node_id"),
        "target_graph_node_id": raw.get("target_graph_node_id"),
        "file_id": file_id,
        "file_version_id": file_version_id,
        "is_active": bool(props.get("is_active", True)),
        "relation_type": props.get("relation_type") or "触发",
        "chunk_id": chunk_id,
        "source_chunk_ids": source_chunk_ids,
        "source_chunk_refs": [_make_chunk_ref(file_version_id, item) for item in source_chunk_ids if item not in (None, "")],
        "support_count": props.get("support_count"),
        "raw_props": props,
    }


def _score_top_event_candidate(node: Dict[str, Any]) -> tuple:
    name = _normalize_text(node.get("name"))
    support = int(node.get("support_count") or len(node.get("source_chunk_ids") or []))
    positive = sum(1 for hint in TOP_EVENT_PRIORITY_HINTS if hint in name)
    negative = sum(1 for hint in TOP_EVENT_NEGATIVE_HINTS if hint in name)
    length_ok = 1 if 2 <= len(name) <= 30 else 0
    return (positive, length_ok, support, -negative, len(name))


def _chunk_sort_key(doc: Dict[str, Any]):
    chunk_id = _get_chunk_identifier(doc)
    try:
        return (0, int(chunk_id))
    except (TypeError, ValueError):
        return (1, str(chunk_id or ""))


def _fetch_chunks_by_identifiers(
    chunk_ids: List[Any],
    limit: int,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    normalized_ids = _dedupe_keep_order(chunk_ids)
    if not normalized_ids:
        return []

    numeric_ids = []
    chunk_uids = []
    per_version_filters: List[Dict[str, Any]] = []
    for chunk_id in normalized_ids:
        version_id, plain_chunk_id = _split_chunk_ref(chunk_id)
        if version_id and plain_chunk_id:
            chunk_uids.append(_make_chunk_ref(version_id, plain_chunk_id))
            per_version_filters.append({"file_version_id": version_id, "chunk_id": plain_chunk_id})
            chunk_id = plain_chunk_id
        try:
            numeric_ids.append(int(str(chunk_id).strip()))
        except (TypeError, ValueError):
            continue

    chunk_matchers: List[Dict[str, Any]] = [
        {"chunk_id": {"$in": normalized_ids}},
        {"id": {"$in": _dedupe_keep_order(normalized_ids + numeric_ids)}},
    ]
    if chunk_uids:
        chunk_matchers.append({"chunk_uid": {"$in": chunk_uids}})
    chunk_matchers.extend(per_version_filters)

    query: Dict[str, Any] = {"$or": chunk_matchers}
    version_filter = _build_file_version_filter(selected_file_version_ids)
    if version_filter:
        query = {"$and": [query, version_filter]}
    docs = list(chunks_col.find(query, {"_id": 0}))
    docs.sort(key=_chunk_sort_key)
    return docs[:limit]


def fetch_chunks_by_ids(
    chunk_ids: List[Any],
    limit: int = 8,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    return _fetch_chunks_by_identifiers(chunk_ids, limit, selected_file_version_ids=selected_file_version_ids)


def get_chunks_by_ids(
    chunk_ids: List[Any],
    limit: Optional[int] = None,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    effective_limit = limit if limit is not None else max(len(_dedupe_keep_order(chunk_ids)), 1)
    return _fetch_chunks_by_identifiers(chunk_ids, effective_limit, selected_file_version_ids=selected_file_version_ids)


def get_chunk_by_id(chunk_id: Any) -> Optional[Dict[str, Any]]:
    chunks = _fetch_chunks_by_identifiers([chunk_id], 1)
    return chunks[0] if chunks else None


def list_all_chunks(
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    query = _build_file_version_filter(selected_file_version_ids) or {}
    chunks = list(chunks_col.find(query, {"_id": 0}))
    chunks.sort(key=_chunk_sort_key)
    return chunks

def hydrate_documents_by_chunk_ids(
    chunk_ids: List[Any],
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    documents = []
    for chunk in get_chunks_by_ids(chunk_ids, selected_file_version_ids=selected_file_version_ids):
        chunk_id = _get_chunk_identifier(chunk)
        if chunk_id in (None, ""):
            continue
        documents.append(
            {
                "chunk_uid": chunk.get("chunk_uid") or _make_chunk_ref(chunk.get("file_version_id"), chunk_id),
                "chunk_id": chunk_id,
                "chunk_name": chunk.get("chunk_name", ""),
                "section_path": chunk.get("section_path", ""),
                "source_page": chunk.get("source", ""),
                "file_id": chunk.get("file_id"),
                "file_version_id": chunk.get("file_version_id"),
                "file": chunk.get("file"),
            }
        )
    return documents


def match_top_event_from_graph(
    top_event_query: str,
    normalized_candidates: Optional[List[str]] = None,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    driver = _get_neo4j_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and install the neo4j package.")

    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    queries = _dedupe_keep_order([top_event_query] + list(normalized_candidates or []))
    queries = [_normalize_text(item) for item in queries if _normalize_text(item)]
    compact_queries = _dedupe_keep_order([_compact_text(item) for item in queries])
    if not queries:
        raise ValueError("top_event_query is empty")

    cypher = """
    MATCH (n:Entity)
    WITH n, elementId(n) AS graph_node_id, replace(coalesce(n.name, ''), ' ', '') AS compact_name
    WHERE
      ($enforce_active_only = false OR coalesce(n.is_active, true) = true)
      AND ($selected_file_version_ids = [] OR coalesce(n.file_version_id, '') IN $selected_file_version_ids)
      AND (
      n.name IN $queries
      OR coalesce(n.normalized_name, '') IN $queries
      OR compact_name IN $compact_queries
      OR any(query IN $queries WHERE n.name CONTAINS query OR query CONTAINS n.name)
      OR any(query IN $compact_queries WHERE compact_name CONTAINS query OR query CONTAINS compact_name)
      )
    RETURN
      graph_node_id,
      labels(n) AS labels,
      properties(n) AS props
    LIMIT 30
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(
            cypher,
            queries=queries,
            compact_queries=compact_queries,
            selected_file_version_ids=scoped_file_version_ids,
            enforce_active_only=(not explicit_scope),
        ).data()

    candidates = [
        node
        for node in (_decode_graph_node(row) for row in rows)
        if _is_fault_like_entity_type(node.get("entity_type")) and str(node.get("node_type") or "").upper() != "AND"
    ]
    if not candidates:
        raise ValueError(f"Neo4j graph has no matching top event for '{top_event_query}'")

    scored = []
    for node in candidates:
        name = _normalize_text(node.get("name"))
        normalized_name = _normalize_text(node.get("normalized_name"))
        compact_name = _compact_text(name)
        exact = 1 if name in queries else 0
        normalized_exact = 1 if normalized_name in queries else 0
        compact_exact = 1 if compact_name in compact_queries else 0
        contains = 1 if any(name and (name in q or q in name) for q in queries) else 0
        score = (
            exact,
            normalized_exact,
            compact_exact,
            contains,
            int(node.get("support_count") or len(node.get("source_chunk_ids") or [])),
            len(name),
        )
        scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][1]
    if _looks_like_fault_code(best.get("name", "")):
        with driver.session(database=NEO4J_DATABASE) as session:
            alias_row = session.run(
                """
                MATCH (code:Entity)-[:RELATION {relation_type:'触发'}]->(target:Entity)
                WHERE elementId(code) = $graph_node_id
                  AND ($enforce_active_only = false OR coalesce(target.is_active, true) = true)
                  AND ($enforce_active_only = false OR coalesce(code.is_active, true) = true)
                  AND ($selected_file_version_ids = [] OR coalesce(code.file_version_id, '') IN $selected_file_version_ids)
                  AND ($selected_file_version_ids = [] OR coalesce(target.file_version_id, '') IN $selected_file_version_ids)
                RETURN elementId(target) AS graph_node_id, labels(target) AS labels, properties(target) AS props
                LIMIT 1
                """,
                graph_node_id=best.get("graph_node_id"),
                selected_file_version_ids=scoped_file_version_ids,
                enforce_active_only=(not explicit_scope),
            ).single()
        if alias_row:
            alias_node = _decode_graph_node(dict(alias_row))
            if _is_fault_like_entity_type(alias_node.get("entity_type")):
                best = alias_node

    alternatives = []
    for _, node in scored[1:6]:
        alternatives.append(
            {
                "graph_node_id": node.get("graph_node_id"),
                "name": node.get("name"),
                "normalized_name": node.get("normalized_name"),
            }
        )

    best_normalized_name = _normalize_text(best.get("normalized_name") or best.get("name"))
    scoped_matches = []
    for _, node in scored:
        node_normalized_name = _normalize_text(node.get("normalized_name") or node.get("name"))
        if node_normalized_name != best_normalized_name:
            continue
        scoped_matches.append(
            {
                "graph_node_id": node.get("graph_node_id"),
                "name": node.get("name"),
                "normalized_name": node.get("normalized_name"),
                "file_id": node.get("file_id"),
                "file_version_id": node.get("file_version_id"),
            }
        )

    return {
        "matched_node_id": best.get("graph_node_id"),
        "matched_name": best.get("name"),
        "matched_node": best,
        "matched_nodes": scoped_matches or [
            {
                "graph_node_id": best.get("graph_node_id"),
                "name": best.get("name"),
                "normalized_name": best.get("normalized_name"),
                "file_id": best.get("file_id"),
                "file_version_id": best.get("file_version_id"),
            }
        ],
        "score": 1.0,
        "alternatives": alternatives,
    }


def get_graph_node_by_id(
    graph_node_id: str,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    driver = _get_neo4j_driver()
    if driver is None:
        return None
    graph_node_id = _normalize_identifier(graph_node_id)
    if not graph_node_id:
        return None

    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    with driver.session(database=NEO4J_DATABASE) as session:
        row = session.run(
            """
            MATCH (n)
            WHERE elementId(n) = $graph_node_id
              AND ($enforce_active_only = false OR coalesce(n.is_active, true) = true)
              AND ($selected_file_version_ids = [] OR coalesce(n.file_version_id, '') IN $selected_file_version_ids)
            RETURN elementId(n) AS graph_node_id, labels(n) AS labels, properties(n) AS props
            LIMIT 1
            """,
            graph_node_id=graph_node_id,
            selected_file_version_ids=scoped_file_version_ids,
            enforce_active_only=(not explicit_scope),
        ).single()
    if not row:
        return None
    return _decode_graph_node(dict(row))


def expand_local_fault_subgraph(
    root_node_id: str,
    max_depth: int = 3,
    max_nodes: int = 20,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    driver = _get_neo4j_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and install the neo4j package.")
    if not root_node_id:
        raise ValueError("root_node_id is empty")

    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    safe_depth = max(1, min(int(max_depth or 3), 4))
    safe_max_nodes = max(1, min(int(max_nodes or 20), 50))

    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    edge_keys = set()
    gate_groups: List[Dict[str, Any]] = []
    support_chunk_ids: List[Any] = []
    frontier = [root_node_id]
    visited = {root_node_id}
    depths = {root_node_id: 0}

    node_query = """
    MATCH (n)
    WHERE elementId(n) = $node_id
      AND ($enforce_active_only = false OR coalesce(n.is_active, true) = true)
      AND ($selected_file_version_ids = [] OR coalesce(n.file_version_id, '') IN $selected_file_version_ids)
    RETURN elementId(n) AS graph_node_id, labels(n) AS labels, properties(n) AS props
    """
    expand_query = """
    UNWIND $frontier AS parent_id
    MATCH (child)-[r:RELATION {relation_type:'触发'}]->(parent)
    WHERE elementId(parent) = parent_id
      AND ($enforce_active_only = false OR coalesce(child.is_active, true) = true)
      AND ($enforce_active_only = false OR coalesce(parent.is_active, true) = true)
      AND ($enforce_active_only = false OR coalesce(r.is_active, true) = true)
      AND ($selected_file_version_ids = [] OR coalesce(child.file_version_id, '') IN $selected_file_version_ids)
      AND ($selected_file_version_ids = [] OR coalesce(parent.file_version_id, '') IN $selected_file_version_ids)
      AND ($selected_file_version_ids = [] OR coalesce(r.file_version_id, '') IN $selected_file_version_ids)
    RETURN
      elementId(child) AS source_graph_node_id,
      labels(child) AS source_labels,
      properties(child) AS source_props,
      elementId(parent) AS target_graph_node_id,
      labels(parent) AS target_labels,
      properties(parent) AS target_props,
      properties(r) AS rel_props
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        root_row = session.run(
            node_query,
            node_id=root_node_id,
            selected_file_version_ids=scoped_file_version_ids,
            enforce_active_only=(not explicit_scope),
        ).single()
        if not root_row:
            raise ValueError(f"Neo4j graph has no node with id '{root_node_id}'")
        root_node = _decode_graph_node(dict(root_row))
        nodes_by_id[root_node_id] = {**root_node, "depth": 0}

        for depth in range(1, safe_depth + 1):
            if not frontier or len(nodes_by_id) >= safe_max_nodes:
                break
            rows = session.run(
                expand_query,
                frontier=frontier,
                selected_file_version_ids=scoped_file_version_ids,
                enforce_active_only=(not explicit_scope),
            ).data()
            next_frontier = []
            for row in rows:
                source_node = _decode_graph_node(
                    {
                        "graph_node_id": row.get("source_graph_node_id"),
                        "labels": row.get("source_labels"),
                        "props": row.get("source_props"),
                    }
                )
                target_node = _decode_graph_node(
                    {
                        "graph_node_id": row.get("target_graph_node_id"),
                        "labels": row.get("target_labels"),
                        "props": row.get("target_props"),
                    }
                )
                source_id = source_node["graph_node_id"]
                target_id = target_node["graph_node_id"]
                nodes_by_id.setdefault(target_id, {**target_node, "depth": depths.get(target_id, depth - 1)})

                if source_id not in nodes_by_id and len(nodes_by_id) >= safe_max_nodes:
                    continue

                if source_id not in nodes_by_id:
                    depths[source_id] = depth
                    nodes_by_id[source_id] = {**source_node, "depth": depth}
                if source_id not in visited:
                    visited.add(source_id)
                    next_frontier.append(source_id)

                edge = _decode_graph_relation(
                    {
                        "source_graph_node_id": source_id,
                        "target_graph_node_id": target_id,
                        "rel_props": row.get("rel_props"),
                    }
                )
                edge_key = (
                    source_id,
                    target_id,
                    edge.get("relation_type"),
                    edge.get("file_version_id"),
                    tuple(edge.get("source_chunk_refs") or edge.get("source_chunk_ids") or []),
                )
                if edge_key not in edge_keys:
                    edge_keys.add(edge_key)
                    edges.append(edge)
                    support_chunk_ids.extend(edge.get("source_chunk_refs") or edge.get("source_chunk_ids") or [])
            frontier = next_frontier

    for node in nodes_by_id.values():
        support_chunk_ids.extend(node.get("source_chunk_refs") or node.get("source_chunk_ids") or [])

    child_targets = {}
    for edge in edges:
        child_targets.setdefault(edge["target_graph_node_id"], []).append(edge["source_graph_node_id"])
    for node in nodes_by_id.values():
        if str(node.get("node_type")).upper() == "AND":
            gate_groups.append(
                {
                    "gate_node_id": node["graph_node_id"],
                    "gate_type": "AND",
                    "depth": node.get("depth", 0),
                    "input_node_ids": child_targets.get(node["graph_node_id"], []),
                }
            )

    return {
        "root": root_node_id,
        "roots": [root_node_id],
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
        "gate_groups": gate_groups,
        "support_chunk_ids": _dedupe_keep_order(support_chunk_ids),
        "source_file_version_ids": _dedupe_keep_order(
            [node.get("file_version_id") for node in nodes_by_id.values() if node.get("file_version_id")]
        ),
    }


def expand_scoped_local_fault_subgraph(
    root_node_ids: List[Any],
    *,
    max_depth: int = 3,
    max_nodes: int = 20,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    roots = _dedupe_keep_order([_normalize_identifier(node_id) for node_id in root_node_ids if _normalize_identifier(node_id)])
    if not roots:
        raise ValueError("root_node_ids is empty")

    merged_nodes: Dict[str, Dict[str, Any]] = {}
    merged_edges: List[Dict[str, Any]] = []
    merged_gate_groups: List[Dict[str, Any]] = []
    edge_keys = set()
    gate_keys = set()
    support_chunk_ids: List[Any] = []
    source_file_version_ids: List[str] = []

    safe_per_root_nodes = max(1, int(max_nodes or 20))
    for root_node_id in roots:
        bundle = expand_local_fault_subgraph(
            root_node_id,
            max_depth=max_depth,
            max_nodes=safe_per_root_nodes,
            selected_file_version_ids=selected_file_version_ids,
        )
        for node in bundle.get("nodes") or []:
            merged_nodes.setdefault(node["graph_node_id"], node)
        for edge in bundle.get("edges") or []:
            edge_key = (
                edge.get("source_graph_node_id"),
                edge.get("target_graph_node_id"),
                edge.get("relation_type"),
                edge.get("file_version_id"),
                tuple(edge.get("source_chunk_refs") or edge.get("source_chunk_ids") or []),
            )
            if edge_key in edge_keys:
                continue
            edge_keys.add(edge_key)
            merged_edges.append(edge)
        for gate_group in bundle.get("gate_groups") or []:
            gate_key = gate_group.get("gate_node_id")
            if gate_key in gate_keys:
                continue
            gate_keys.add(gate_key)
            merged_gate_groups.append(gate_group)
        support_chunk_ids.extend(bundle.get("support_chunk_ids") or [])
        source_file_version_ids.extend(bundle.get("source_file_version_ids") or [])

    return {
        "root": roots[0],
        "roots": roots,
        "nodes": list(merged_nodes.values()),
        "edges": merged_edges,
        "gate_groups": merged_gate_groups,
        "support_chunk_ids": _dedupe_keep_order(support_chunk_ids),
        "source_file_version_ids": _dedupe_keep_order(source_file_version_ids),
    }


def collect_subgraph_chunks(subgraph_bundle: Dict[str, Any], chunk_limit: int = 12) -> List[Any]:
    scores: Dict[str, float] = {}
    bundle_nodes = subgraph_bundle.get("nodes") or []
    bundle_edges = subgraph_bundle.get("edges") or []
    root_id = subgraph_bundle.get("root")

    node_depth = {node.get("graph_node_id"): int(node.get("depth") or 0) for node in bundle_nodes}
    for node in bundle_nodes:
        weight = 10 if node.get("graph_node_id") == root_id else max(3, 8 - int(node.get("depth") or 0) * 2)
        for chunk_id in node.get("source_chunk_refs") or node.get("source_chunk_ids") or []:
            key = str(chunk_id)
            scores[key] = scores.get(key, 0.0) + weight
        for doc in node.get("documents") or []:
            chunk_id = doc.get("chunk_id")
            if chunk_id in (None, ""):
                continue
            key = _make_chunk_ref(node.get("file_version_id"), chunk_id)
            scores[key] = scores.get(key, 0.0) + weight

    for edge in bundle_edges:
        source_depth = node_depth.get(edge.get("source_graph_node_id"), 3)
        weight = max(4, 9 - source_depth)
        for chunk_id in edge.get("source_chunk_refs") or edge.get("source_chunk_ids") or []:
            key = str(chunk_id)
            scores[key] = scores.get(key, 0.0) + weight

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [chunk_id for chunk_id, _ in ranked[: max(1, int(chunk_limit or 12))]]


def list_graph_top_event_candidates(
    limit: Optional[int] = None,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    driver = _get_neo4j_driver()
    if driver is None:
        return []

    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    cypher = """
    MATCH (n:Entity:FaultPhenomenon)
    WHERE ($enforce_active_only = false OR coalesce(n.is_active, true) = true)
      AND ($selected_file_version_ids = [] OR coalesce(n.file_version_id, '') IN $selected_file_version_ids)
    RETURN elementId(n) AS graph_node_id, labels(n) AS labels, properties(n) AS props
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(
            cypher,
            selected_file_version_ids=scoped_file_version_ids,
            enforce_active_only=(not explicit_scope),
        ).data()

    decoded = [_decode_graph_node(row) for row in rows]
    filtered = []
    for node in decoded:
        if str(node.get("node_type") or "").upper() == "AND":
            continue
        name = _normalize_text(node.get("name"))
        if not name:
            continue
        filtered.append(node)

    filtered.sort(key=_score_top_event_candidate, reverse=True)
    if limit:
        filtered = filtered[:limit]

    return [
        {
            "graph_node_id": node.get("graph_node_id"),
            "name": node.get("name"),
            "normalized_name": node.get("normalized_name"),
            "file_id": node.get("file_id"),
            "file_version_id": node.get("file_version_id"),
            "support_count": int(node.get("support_count") or len(node.get("source_chunk_ids") or [])),
            "source_chunk_ids": node.get("source_chunk_ids") or [],
            "source_chunk_refs": node.get("source_chunk_refs") or [],
            "documents": node.get("documents") or [],
        }
        for node in filtered
    ]


def update_graph_node_properties(graph_node_id: str, properties: Dict[str, Any]) -> bool:
    driver = _get_neo4j_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and install the neo4j package.")
    if not graph_node_id:
        return False

    update_props = {}
    for key, value in (properties or {}).items():
        if key not in GRAPH_PROPERTY_UPDATE_FIELDS:
            continue
        if value is None:
            continue
        update_props[key] = value

    if not update_props:
        return False

    with driver.session(database=NEO4J_DATABASE) as session:
        result = session.run(
            """
            MATCH (n)
            WHERE elementId(n) = $graph_node_id
            SET n += $props,
                n.updated_at = datetime()
            RETURN count(n) AS updated
            """,
            graph_node_id=graph_node_id,
            props=update_props,
        ).single()
    return bool(result and result.get("updated"))


def _ensure_indexes():
    index_specs = [
        (files_col, [("normalized_name", ASCENDING)]),
        (file_versions_col, [("file_id", ASCENDING), ("version_no", DESCENDING)]),
        (file_versions_col, [("file_version_id", ASCENDING)]),
        (file_versions_col, [("is_active", ASCENDING), ("status", ASCENDING)]),
        (chunks_col, [("chunk_id", ASCENDING)]),
        (chunks_col, [("id", ASCENDING)]),
        (chunks_col, [("chunk_uid", ASCENDING)]),
        (chunks_col, [("file_version_id", ASCENDING), ("chunk_id", ASCENDING)]),
        (chunks_col, [("file_id", ASCENDING), ("file_version_id", ASCENDING), ("is_active", ASCENDING)]),
        (work_orders_col, [("file_version_id", ASCENDING), ("source_record_id", ASCENDING)]),
        (work_orders_col, [("file_id", ASCENDING), ("file_version_id", ASCENDING), ("is_active", ASCENDING)]),
        (maintenance_cases_col, [("file_version_id", ASCENDING), ("source_record_id", ASCENDING)]),
        (maintenance_cases_col, [("file_id", ASCENDING), ("file_version_id", ASCENDING), ("is_active", ASCENDING)]),
        (trees_col, [("catalog_name", ASCENDING), ("updated_at", DESCENDING)]),
        (trees_col, [("normalized_top_event", ASCENDING), ("updated_at", DESCENDING)]),
        (trees_col, [("source_scope_key", ASCENDING), ("normalized_top_event", ASCENDING), ("updated_at", DESCENDING)]),
        (top_event_catalog_col, [("normalized_name", ASCENDING)]),
        (top_event_catalog_col, [("normalized_aliases", ASCENDING)]),
        (top_event_catalog_col, [("file_version_id", ASCENDING), ("normalized_name", ASCENDING)]),
        (top_event_catalog_col, [("graph_node_id", ASCENDING)]),
        (generation_jobs_col, [("status", ASCENDING), ("updated_at", DESCENDING)]),
        (generation_jobs_col, [("job_type", ASCENDING), ("source_scope_key", ASCENDING), ("updated_at", DESCENDING)]),
        (generation_job_items_col, [("job_id", ASCENDING), ("status", ASCENDING)]),
        (generation_job_items_col, [("normalized_top_event", ASCENDING), ("status", ASCENDING)]),
        (generation_job_items_col, [("source_scope_key", ASCENDING), ("normalized_top_event", ASCENDING), ("status", ASCENDING)]),
    ]

    try:
        top_event_catalog_col.drop_index("name_1")
    except Exception:
        pass

    for collection, keys in index_specs:
        try:
            if collection is files_col and keys == [("normalized_name", ASCENDING)]:
                for index_name, spec in collection.index_information().items():
                    if spec.get("key") == [("normalized_name", ASCENDING)] and not spec.get("unique"):
                        collection.drop_index(index_name)
                collection.create_index(keys, unique=True)
            elif collection is top_event_catalog_col and keys == [("file_version_id", ASCENDING), ("normalized_name", ASCENDING)]:
                collection.create_index(keys, unique=True)
            else:
                collection.create_index(keys)
        except Exception:
            if collection is files_col and keys == [("normalized_name", ASCENDING)]:
                raise


_ensure_indexes()


def _import_source_records(
    records: list,
    *,
    collection: Any,
    source_record_type: str,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    normalized_records = []
    skipped = 0
    for record in records or []:
        if not isinstance(record, dict):
            skipped += 1
            continue
        normalized_records.append(
            _normalize_source_record_import_doc(
                record,
                source_record_type=source_record_type,
                file_id=file_id,
                file_version_id=file_version_id,
                is_active=is_active,
            )
        )

    scoped_query: Dict[str, Any] = {}
    if _normalize_identifier(file_version_id):
        scoped_query["file_version_id"] = _normalize_identifier(file_version_id)
    elif _normalize_identifier(file_id):
        scoped_query["file_id"] = _normalize_identifier(file_id)

    if scoped_query:
        collection.delete_many(scoped_query)
    else:
        collection.delete_many({})

    if normalized_records:
        collection.insert_many(normalized_records)

    return {
        "scope": scoped_query or "all",
        "received": len(records or []),
        "processed": len(normalized_records),
        "inserted": len(normalized_records),
        "updated": 0,
        "skipped": skipped,
    }


def import_chunks(
    chunks: list,
    mode: str = "replace",
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    """
    Import chunks into MongoDB.

    mode="replace": clear old data first, then insert the full input.
    mode="append": keep existing data and assign new global auto-increment chunk IDs.
    """
    normalized_mode = str(mode or "replace").strip().lower()
    if normalized_mode not in {"replace", "append"}:
        raise ValueError("chunks import mode must be 'replace' or 'append'")

    normalized_chunks = []
    skipped = 0
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            skipped += 1
            continue
        normalized_chunks.append(
            _normalize_chunk_import_doc(
                chunk,
                file_id=file_id,
                file_version_id=file_version_id,
                is_active=is_active,
            )
        )

    version_scoped = bool(_normalize_identifier(file_id) or _normalize_identifier(file_version_id))
    scoped_query: Dict[str, Any] = {}
    if _normalize_identifier(file_version_id):
        scoped_query["file_version_id"] = _normalize_identifier(file_version_id)
    elif _normalize_identifier(file_id):
        scoped_query["file_id"] = _normalize_identifier(file_id)

    if normalized_mode == "replace":
        if version_scoped:
            chunks_col.delete_many(scoped_query)
        else:
            chunks_col.drop()
            _ensure_indexes()
        if normalized_chunks:
            chunks_col.insert_many(normalized_chunks)
        stats = {
            "mode": "replace",
            "scope": scoped_query or "all",
            "received": len(chunks or []),
            "processed": len(normalized_chunks),
            "inserted": len(normalized_chunks),
            "updated": 0,
            "skipped": skipped,
        }
        print(f"Imported {len(normalized_chunks)} chunks with mode=replace")
        return stats

    next_identifier = _get_max_chunk_numeric_identifier() if not version_scoped else 0
    remapped_chunks = []
    remapped_count = 0
    start_chunk_id = next_identifier + 1 if normalized_chunks else None
    for chunk in normalized_chunks:
        remapped_chunk = dict(chunk)
        original_chunk_id = remapped_chunk.get("chunk_id")
        original_id = remapped_chunk.get("id")
        if not version_scoped:
            next_identifier += 1
            remapped_chunk["chunk_id"] = next_identifier
            remapped_chunk["id"] = next_identifier
            if original_chunk_id not in (None, "") and original_chunk_id != next_identifier:
                remapped_chunk["original_chunk_id"] = original_chunk_id
            if original_id not in (None, "") and original_id != next_identifier:
                remapped_chunk["original_id"] = original_id
        chunk_value = remapped_chunk.get("chunk_id")
        if remapped_chunk.get("file_version_id") and chunk_value not in (None, ""):
            remapped_chunk["chunk_uid"] = _make_chunk_ref(remapped_chunk.get("file_version_id"), chunk_value)
        remapped_chunks.append(remapped_chunk)
        if not version_scoped and (original_chunk_id != next_identifier or original_id != next_identifier):
            remapped_count += 1

    if remapped_chunks:
        chunks_col.insert_many(remapped_chunks)

    stats = {
        "mode": "append",
        "scope": scoped_query or "all",
        "received": len(chunks or []),
        "processed": len(normalized_chunks),
        "inserted": len(remapped_chunks),
        "updated": 0,
        "skipped": skipped,
        "remapped": remapped_count,
        "start_chunk_id": start_chunk_id,
        "end_chunk_id": next_identifier if remapped_chunks else None,
    }
    print(
        "Imported chunks with mode=append "
        f"(processed={stats['processed']}, inserted={stats['inserted']}, "
        f"remapped={stats['remapped']}, id_range={stats['start_chunk_id']}..{stats['end_chunk_id']})"
    )
    return stats


def import_work_orders(
    records: list,
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    stats = _import_source_records(
        records,
        collection=work_orders_col,
        source_record_type="work_order",
        file_id=file_id,
        file_version_id=file_version_id,
        is_active=is_active,
    )
    print(f"Imported {stats['inserted']} work order records")
    return stats


def import_maintenance_cases(
    records: list,
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    stats = _import_source_records(
        records,
        collection=maintenance_cases_col,
        source_record_type="maintenance_case",
        file_id=file_id,
        file_version_id=file_version_id,
        is_active=is_active,
    )
    print(f"Imported {stats['inserted']} maintenance case records")
    return stats


def get_file_version(file_version_id: str) -> Optional[Dict[str, Any]]:
    return _strip_mongo_id(file_versions_col.find_one({"file_version_id": file_version_id}))


def _parse_version_no_from_explicit_file_version_id(
    file_version_id: str,
    *,
    file_id: Optional[str] = None,
) -> tuple[str, int]:
    normalized_file_version_id = _normalize_identifier(file_version_id)
    match = re.fullmatch(r"(.+)_v(\d+)", normalized_file_version_id)
    if not match:
        raise ValueError(
            f"Invalid file_version_id '{normalized_file_version_id}', expected format '<file_id>_v<version_no>'"
        )
    parsed_file_id = _normalize_identifier(match.group(1))
    version_no = int(match.group(2))
    normalized_file_id = _normalize_identifier(file_id)
    if normalized_file_id and parsed_file_id != normalized_file_id:
        raise ValueError(
            f"file_version_id '{normalized_file_version_id}' does not belong to file_id '{normalized_file_id}'"
        )
    if version_no <= 0:
        raise ValueError(f"Invalid version number in file_version_id '{normalized_file_version_id}'")
    return parsed_file_id, version_no


def reserve_file_version_record(
    *,
    file_name: str,
    source: str = "knowledge_upload",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_file_name = _normalize_file_name(file_name)
    if not normalized_file_name:
        raise ValueError("file_name is required")

    now = _now()
    new_file_id = f"file_{uuid4().hex[:12]}"
    file_doc = files_col.find_one_and_update(
        {"normalized_name": normalized_file_name},
        {
            "$setOnInsert": {
                "_id": new_file_id,
                "file_id": new_file_id,
                "normalized_name": normalized_file_name,
                "current_file_version_id": None,
                "created_at": now,
            },
            "$set": {
                "name": file_name,
                "status": "processing",
                "source": source,
                "updated_at": now,
            },
            "$inc": {"latest_version_no": 1},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    file_id = _normalize_identifier(file_doc.get("file_id") or file_doc.get("_id"))
    version_no = int(file_doc["latest_version_no"])
    file_version_id = f"{file_id}_v{version_no}"
    doc = {
        "_id": file_version_id,
        "file_version_id": file_version_id,
        "file_id": file_id,
        "file_name": file_name,
        "normalized_file_name": normalized_file_name,
        "version_no": version_no,
        "status": "processing",
        "is_active": False,
        "source": source,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
    }
    file_versions_col.insert_one(doc)
    return _strip_mongo_id(doc)


def create_file_version_record(
    *,
    file_name: str,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    source: str = "knowledge_import",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_file_name = _normalize_file_name(file_name)
    if not normalized_file_name:
        raise ValueError("file_name is required")

    normalized_explicit_file_version_id = _normalize_identifier(file_version_id)
    if not normalized_explicit_file_version_id and not _normalize_identifier(file_id):
        return reserve_file_version_record(file_name=file_name, source=source, metadata=metadata)

    now = _now()
    explicit_file_id = ""
    explicit_version_no: Optional[int] = None
    if normalized_explicit_file_version_id:
        explicit_file_id, explicit_version_no = _parse_version_no_from_explicit_file_version_id(
            normalized_explicit_file_version_id,
            file_id=file_id,
        )

    file_doc = None
    normalized_file_id = _normalize_identifier(file_id) or explicit_file_id
    if normalized_file_id:
        file_doc = files_col.find_one({"_id": normalized_file_id})
    if not file_doc:
        file_doc = files_col.find_one({"normalized_name": normalized_file_name})
        if file_doc and normalized_file_id and file_doc.get("_id") != normalized_file_id:
            raise ValueError(
                f"file_name '{file_name}' is already associated with file_id '{file_doc.get('_id')}', "
                f"not '{normalized_file_id}'"
            )

    if file_doc:
        normalized_file_id = file_doc["_id"]
        version_no = explicit_version_no if explicit_version_no is not None else int(file_doc.get("latest_version_no") or 0) + 1
        files_col.update_one(
            {"_id": normalized_file_id},
            {
                "$set": {
                    "name": file_name,
                    "normalized_name": normalized_file_name,
                    "status": "processing",
                    "updated_at": now,
                    "source": source,
                }
            },
        )
    else:
        normalized_file_id = normalized_file_id or f"file_{uuid4().hex[:12]}"
        version_no = explicit_version_no if explicit_version_no is not None else 1
        files_col.insert_one(
            {
                "_id": normalized_file_id,
                "file_id": normalized_file_id,
                "name": file_name,
                "normalized_name": normalized_file_name,
                "status": "processing",
                "source": source,
                "latest_version_no": int(version_no or 0),
                "current_file_version_id": None,
                "created_at": now,
                "updated_at": now,
            }
        )

    if explicit_version_no is not None:
        files_col.update_one(
            {"_id": normalized_file_id},
            {
                "$max": {"latest_version_no": explicit_version_no},
                "$set": {"updated_at": now},
            },
        )

    resolved_file_version_id = normalized_explicit_file_version_id or f"{normalized_file_id}_v{version_no}"
    existing_version = file_versions_col.find_one({"_id": resolved_file_version_id})
    if existing_version:
        if existing_version.get("file_id") != normalized_file_id:
            raise ValueError(
                f"file_version_id '{resolved_file_version_id}' is already bound to file_id '{existing_version.get('file_id')}'"
            )
        file_versions_col.update_one(
            {"_id": resolved_file_version_id},
            {
                "$set": {
                    "file_name": file_name,
                    "normalized_file_name": normalized_file_name,
                    "version_no": int(existing_version.get("version_no") or version_no),
                    "status": "processing",
                    "is_active": False,
                    "source": source,
                    "metadata": metadata or {},
                    "updated_at": now,
                }
            },
        )
        return get_file_version(resolved_file_version_id) or {}

    doc = {
        "_id": resolved_file_version_id,
        "file_version_id": resolved_file_version_id,
        "file_id": normalized_file_id,
        "file_name": file_name,
        "normalized_file_name": normalized_file_name,
        "version_no": version_no,
        "status": "processing",
        "is_active": False,
        "source": source,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
    }
    file_versions_col.insert_one(doc)
    return _strip_mongo_id(doc)


def mark_file_version_import_failed(file_version_id: str, error: str) -> None:
    now = _now()
    file_version = file_versions_col.find_one({"file_version_id": file_version_id})
    if not file_version:
        return
    file_versions_col.update_one(
        {"file_version_id": file_version_id},
        {"$set": {"status": "failed", "is_active": False, "error": error, "updated_at": now}},
    )
    files_col.update_one(
        {"_id": file_version["file_id"]},
        {"$set": {"status": "failed", "updated_at": now, "last_error": error}},
    )


def _set_neo4j_file_version_active_state(file_id: str, file_version_id: str) -> None:
    driver = _get_neo4j_driver()
    if driver is None:
        return

    with driver.session(database=NEO4J_DATABASE) as session:
        session.run(
            """
            MATCH (n)
            WHERE coalesce(n.file_id, '') = $file_id
            SET n.is_active = CASE WHEN coalesce(n.file_version_id, '') = $file_version_id THEN true ELSE false END,
                n.updated_at = datetime()
            """,
            file_id=file_id,
            file_version_id=file_version_id,
        ).consume()
        session.run(
            """
            MATCH ()-[r:RELATION]->()
            WHERE coalesce(r.file_id, '') = $file_id
            SET r.is_active = CASE WHEN coalesce(r.file_version_id, '') = $file_version_id THEN true ELSE false END,
                r.updated_at = datetime()
            """,
            file_id=file_id,
            file_version_id=file_version_id,
        ).consume()
        session.run(
            """
            MATCH ()-[r:MENTIONED_IN]->()
            WHERE coalesce(r.file_version_id, '') <> ''
              AND coalesce(startNode(r).file_id, endNode(r).file_id, '') = $file_id
            SET r.is_active = CASE WHEN coalesce(r.file_version_id, '') = $file_version_id THEN true ELSE false END,
                r.updated_at = datetime()
            """,
            file_id=file_id,
            file_version_id=file_version_id,
        ).consume()


def activate_file_version(file_id: str, file_version_id: str) -> Dict[str, Any]:
    normalized_file_id = _normalize_identifier(file_id)
    normalized_file_version_id = _normalize_identifier(file_version_id)
    if not normalized_file_id or not normalized_file_version_id:
        raise ValueError("file_id and file_version_id are required")
    version_doc = get_file_version(normalized_file_version_id)
    if not version_doc or version_doc.get("file_id") != normalized_file_id:
        raise ValueError(f"Unknown file version '{normalized_file_version_id}' for file '{normalized_file_id}'")

    now = _now()
    file_versions_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": {"$ne": normalized_file_version_id}},
        {"$set": {"is_active": False, "status": "inactive", "updated_at": now}},
    )
    file_versions_col.update_one(
        {"file_id": normalized_file_id, "file_version_id": normalized_file_version_id},
        {"$set": {"is_active": True, "status": "active", "updated_at": now}},
    )
    files_col.update_one(
        {"_id": normalized_file_id},
        {
            "$set": {
                "status": "active",
                "current_file_version_id": normalized_file_version_id,
                "updated_at": now,
            },
            "$max": {"latest_version_no": int(version_doc.get("version_no") or 0)},
        },
    )
    chunks_col.update_many(
        {"file_id": normalized_file_id},
        {
            "$set": {
                "updated_at": now,
            }
        },
    )
    chunks_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": {"$ne": normalized_file_version_id}},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    chunks_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": normalized_file_version_id},
        {"$set": {"is_active": True, "updated_at": now}},
    )
    work_orders_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": {"$ne": normalized_file_version_id}},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    work_orders_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": normalized_file_version_id},
        {"$set": {"is_active": True, "updated_at": now}},
    )
    maintenance_cases_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": {"$ne": normalized_file_version_id}},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    maintenance_cases_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": normalized_file_version_id},
        {"$set": {"is_active": True, "updated_at": now}},
    )
    top_event_catalog_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": {"$ne": normalized_file_version_id}},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    top_event_catalog_col.update_many(
        {"file_id": normalized_file_id, "file_version_id": normalized_file_version_id},
        {"$set": {"is_active": True, "updated_at": now}},
    )
    _set_neo4j_file_version_active_state(normalized_file_id, normalized_file_version_id)
    return get_file_version(normalized_file_version_id) or {}


def archive_file(file_id: str, *, status: str = "archived") -> Optional[Dict[str, Any]]:
    normalized_file_id = _normalize_identifier(file_id)
    if not normalized_file_id:
        raise ValueError("file_id is required")

    now = _now()
    files_col.update_one(
        {"_id": normalized_file_id},
        {
            "$set": {
                "status": status,
                "updated_at": now,
            }
        },
    )
    file_versions_col.update_many(
        {"file_id": normalized_file_id},
        {"$set": {"is_active": False, "status": status, "updated_at": now}},
    )
    chunks_col.update_many(
        {"file_id": normalized_file_id},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    work_orders_col.update_many(
        {"file_id": normalized_file_id},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    maintenance_cases_col.update_many(
        {"file_id": normalized_file_id},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    top_event_catalog_col.update_many(
        {"file_id": normalized_file_id},
        {"$set": {"is_active": False, "updated_at": now}},
    )

    driver = _get_neo4j_driver()
    if driver is not None:
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run(
                """
                MATCH (n)
                WHERE coalesce(n.file_id, '') = $file_id
                SET n.is_active = false,
                    n.updated_at = datetime()
                """,
                file_id=normalized_file_id,
            ).consume()
            session.run(
                """
                MATCH ()-[r]->()
                WHERE coalesce(r.file_id, '') = $file_id
                   OR coalesce(r.file_version_id, '') IN $file_version_ids
                SET r.is_active = false,
                    r.updated_at = datetime()
                """,
                file_id=normalized_file_id,
                file_version_ids=file_versions_col.distinct("file_version_id", {"file_id": normalized_file_id}),
            ).consume()

    return get_file(normalized_file_id)


def create_tree(
    tree_id: str,
    top_event: str,
    catalog_name: Optional[str] = None,
    normalized_top_event: Optional[str] = None,
    requested_top_event: Optional[str] = None,
    resolved_top_event: Optional[str] = None,
    graph_node_id: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    source_file_version_ids: Optional[List[str]] = None,
    source_scope_key: Optional[str] = None,
    job_id: Optional[str] = None,
    job_item_id: Optional[str] = None,
):
    trees_col.insert_one(
        {
            "_id": tree_id,
            "top_event": top_event,
            "requested_top_event": requested_top_event or top_event,
            "resolved_top_event": resolved_top_event or catalog_name or top_event,
            "catalog_name": catalog_name or top_event,
            "normalized_top_event": normalized_top_event or top_event,
            "graph_node_id": graph_node_id,
            "query_aliases": _dedupe_keep_order(aliases),
            "source_chunk_ids": _dedupe_keep_order(source_chunk_ids),
            "source_file_version_ids": _dedupe_keep_order(source_file_version_ids),
            "source_scope_key": source_scope_key or _make_scope_key(source_file_version_ids),
            "job_id": job_id,
            "job_item_id": job_item_id,
            "created_at": _now(),
            "updated_at": _now(),
            "current_version": 0,
            "status": "generating",
        }
    )


def update_tree_status(tree_id: str, status: str, **extra_fields):
    payload = {"status": status, "updated_at": _now()}
    payload.update({k: v for k, v in extra_fields.items() if v is not None})
    trees_col.update_one({"_id": tree_id}, {"$set": payload})


def get_tree_meta(tree_id: str) -> dict:
    return trees_col.find_one({"_id": tree_id})


def find_tree_by_top_event(
    top_event: str,
    normalized_top_event: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    catalog_name: Optional[str] = None,
    source_file_version_ids: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    conditions = []
    candidate_names = _dedupe_keep_order([catalog_name, top_event] + (aliases or []))
    scope_key = _make_scope_key(source_file_version_ids)

    if catalog_name:
        conditions.append({"catalog_name": catalog_name})
    if normalized_top_event:
        conditions.append({"normalized_top_event": normalized_top_event})
    if candidate_names:
        conditions.append({"top_event": {"$in": candidate_names}})
        conditions.append({"query_aliases": {"$in": candidate_names}})

    if not conditions:
        return None

    meta = trees_col.find_one(
        {
            "status": {"$in": ["ai_generated", "expert_modified", "rolled_back"]},
            "source_scope_key": scope_key,
            "$or": conditions,
        },
        sort=[("updated_at", DESCENDING)],
    )
    if not meta or not meta.get("current_version"):
        return None

    version = get_version(meta["_id"], meta["current_version"])
    if not version:
        return None

    return {
        "tree_id": meta["_id"],
        "version": meta["current_version"],
        "top_event": meta.get("top_event"),
        "catalog_name": meta.get("catalog_name"),
        "status": meta.get("status"),
        "updated_at": meta.get("updated_at"),
        "source_file_version_ids": meta.get("source_file_version_ids") or [],
        "tree_data": version.get("tree_data"),
        "version_data": version,
    }


def save_version(
    tree_id: str,
    tree_data: dict,
    editor: str,
    description: str,
    is_ai: bool,
    *,
    requested_top_event: Optional[str] = None,
    resolved_top_event: Optional[str] = None,
    normalized_top_event: Optional[str] = None,
    source_file_version_ids: Optional[List[Any]] = None,
    evidence_chunk_ids: Optional[List[Any]] = None,
    subgraph_node_ids: Optional[List[Any]] = None,
) -> int:
    latest = versions_col.find_one({"tree_id": tree_id}, sort=[("version", -1)])
    new_version = (latest["version"] + 1) if latest else 1
    source_scope_key = _make_scope_key(source_file_version_ids)

    versions_col.insert_one(
        {
            "tree_id": tree_id,
            "version": new_version,
            "is_ai_generated": is_ai,
            "created_at": _now(),
            "editor": editor,
            "description": description,
            "requested_top_event": requested_top_event,
            "resolved_top_event": resolved_top_event,
            "normalized_top_event": normalized_top_event,
            "source_scope_key": source_scope_key,
            "source_file_version_ids": _dedupe_keep_order(source_file_version_ids),
            "evidence_chunk_ids": _dedupe_keep_order(evidence_chunk_ids),
            "subgraph_node_ids": _dedupe_keep_order(subgraph_node_ids),
            "tree_data": tree_data,
        }
    )

    trees_col.update_one(
        {"_id": tree_id},
        {
            "$set": {
                "current_version": new_version,
                "updated_at": _now(),
                "status": "ai_generated" if is_ai else "expert_modified",
                "requested_top_event": requested_top_event,
                "resolved_top_event": resolved_top_event,
                "normalized_top_event": normalized_top_event,
                "source_scope_key": source_scope_key,
                "source_file_version_ids": _dedupe_keep_order(source_file_version_ids),
                "source_chunk_ids": _dedupe_keep_order(evidence_chunk_ids),
            }
        },
    )
    return new_version


def get_version(tree_id: str, version: int = None) -> dict:
    if version is None:
        meta = get_tree_meta(tree_id)
        if not meta:
            return None
        version = meta["current_version"]

    return versions_col.find_one({"tree_id": tree_id, "version": version}, {"_id": 0})


def rollback_version(tree_id: str, target_version: int):
    target = versions_col.find_one({"tree_id": tree_id, "version": target_version})
    if not target:
        raise ValueError(f"Version {target_version} does not exist")

    trees_col.update_one(
        {"_id": tree_id},
        {"$set": {"current_version": target_version, "updated_at": _now(), "status": "rolled_back"}},
    )


def get_version_list(tree_id: str) -> list:
    versions = versions_col.find({"tree_id": tree_id}, {"tree_data": 0, "_id": 0}).sort("version", 1)
    return list(versions)


def upsert_top_event_catalog_entry(
    *,
    name: str,
    normalized_name: str,
    file_id: str,
    file_version_id: str,
    aliases: Optional[List[str]] = None,
    normalized_aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[Any]] = None,
    graph_node_id: Optional[str] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    normalized_file_id = _normalize_identifier(file_id)
    normalized_file_version_id = _normalize_identifier(file_version_id)
    if not normalized_file_id or not normalized_file_version_id:
        raise ValueError("file_id and file_version_id are required for top_event_catalog entries")

    aliases = _dedupe_keep_order([alias for alias in aliases or [] if alias != name])
    normalized_aliases = _dedupe_keep_order(
        [alias for alias in normalized_aliases or [] if alias and alias != normalized_name]
    )
    semantic_text = _build_top_event_semantic_text(
        name,
        normalized_name=normalized_name,
        aliases=aliases + normalized_aliases,
    )
    source_chunk_ids = _normalize_chunk_refs(source_chunk_ids, file_version_id=normalized_file_version_id)
    doc_id = _make_catalog_doc_id(normalized_file_version_id, normalized_name)
    existing = top_event_catalog_col.find_one({"_id": doc_id})
    now = _now()

    if existing:
        merged_aliases = _dedupe_keep_order((existing.get("aliases") or []) + aliases)
        merged_normalized_aliases = _dedupe_keep_order(
            (existing.get("normalized_aliases") or []) + normalized_aliases
        )
        merged_source_chunk_ids = _normalize_chunk_refs(
            (existing.get("source_chunk_ids") or []) + source_chunk_ids,
            file_version_id=normalized_file_version_id,
        )
        previous_semantic_text = existing.get("semantic_text") or ""
        embedding_payload = {}
        if previous_semantic_text != semantic_text:
            embedding_payload = _build_top_event_embedding_fields(semantic_text)
        top_event_catalog_col.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "name": name,
                    "display_name": name,
                    "updated_at": now,
                    "file_id": normalized_file_id,
                    "file_version_id": normalized_file_version_id,
                    "is_active": bool(is_active),
                    "aliases": merged_aliases,
                    "normalized_aliases": merged_normalized_aliases,
                    "source_chunk_ids": merged_source_chunk_ids,
                    "graph_node_id": graph_node_id or existing.get("graph_node_id"),
                    **embedding_payload,
                }
            },
        )
    else:
        embedding_payload = _build_top_event_embedding_fields(semantic_text)
        top_event_catalog_col.insert_one(
            {
                "_id": doc_id,
                "file_id": normalized_file_id,
                "file_version_id": normalized_file_version_id,
                "is_active": bool(is_active),
                "name": name,
                "display_name": name,
                "normalized_name": normalized_name,
                "aliases": aliases,
                "normalized_aliases": normalized_aliases,
                **embedding_payload,
                "source_chunk_ids": source_chunk_ids,
                "graph_node_id": graph_node_id,
                "created_at": now,
                "updated_at": now,
            }
        )

    return get_top_event_catalog(normalized_name, file_version_id=normalized_file_version_id)


def _merge_catalog_entries(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    cleaned = [entry for entry in entries if entry]
    if not cleaned:
        return None
    primary = cleaned[0]
    return {
        "name": primary.get("name"),
        "display_name": primary.get("display_name") or primary.get("name"),
        "normalized_name": primary.get("normalized_name"),
        "aliases": _dedupe_keep_order([alias for entry in cleaned for alias in (entry.get("aliases") or [])]),
        "normalized_aliases": _dedupe_keep_order(
            [alias for entry in cleaned for alias in (entry.get("normalized_aliases") or [])]
        ),
        "source_chunk_ids": _normalize_chunk_refs(
            [chunk_id for entry in cleaned for chunk_id in (entry.get("source_chunk_ids") or [])],
            file_version_id=primary.get("file_version_id"),
        ),
        "graph_node_id": primary.get("graph_node_id"),
        "graph_node_ids": _dedupe_keep_order([entry.get("graph_node_id") for entry in cleaned if entry.get("graph_node_id")]),
        "file_ids": _dedupe_keep_order([entry.get("file_id") for entry in cleaned if entry.get("file_id")]),
        "file_version_ids": _dedupe_keep_order(
            [entry.get("file_version_id") for entry in cleaned if entry.get("file_version_id")]
        ),
        "semantic_text": primary.get("semantic_text"),
        "embedding": primary.get("embedding"),
        "embedding_model": primary.get("embedding_model"),
        "embedding_updated_at": primary.get("embedding_updated_at"),
        "catalog_entries": cleaned,
    }


def get_top_event_catalog(
    normalized_name: str,
    *,
    file_version_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    query: Dict[str, Any] = {"normalized_name": normalized_name}
    if file_version_id:
        query["file_version_id"] = _normalize_identifier(file_version_id)
    docs = list(top_event_catalog_col.find(query))
    return _merge_catalog_entries(docs)


def resolve_top_event_catalog(
    *,
    normalized_candidates: List[str],
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    normalized_candidates = _dedupe_keep_order(normalized_candidates)
    if not normalized_candidates:
        return None

    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    query: Dict[str, Any] = {
        "$or": [
            {"normalized_name": {"$in": normalized_candidates}},
            {"normalized_aliases": {"$in": normalized_candidates}},
        ],
    }
    if not explicit_scope:
        query["is_active"] = True
    version_filter = _build_file_version_filter(scoped_file_version_ids)
    if version_filter:
        query = {"$and": [query, version_filter]}
    docs = list(top_event_catalog_col.find(query, {"_id": 0}).sort("updated_at", DESCENDING))
    return _merge_catalog_entries(docs)


def list_top_event_catalog(
    limit: Optional[int] = None,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    query, _ = _build_scoped_top_event_catalog_query(selected_file_version_ids)

    docs = list(top_event_catalog_col.find(query, {"_id": 0}).sort("name", ASCENDING))
    merged_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs:
        merged_by_name.setdefault(doc.get("normalized_name") or doc.get("name"), []).append(doc)

    items = [_merge_catalog_entries(entries) for entries in merged_by_name.values()]
    items = [item for item in items if item]
    items.sort(key=lambda item: item.get("name") or "")
    if limit:
        items = items[:limit]
    return items


def _build_scoped_top_event_catalog_query(
    selected_file_version_ids: Optional[List[Any]] = None,
) -> tuple[Dict[str, Any], List[str]]:
    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    query: Dict[str, Any] = {}
    if not explicit_scope:
        query["is_active"] = True
    version_filter = _build_file_version_filter(scoped_file_version_ids)
    if version_filter:
        query = {"$and": [query, version_filter]}
    return query, scoped_file_version_ids


def ensure_top_event_catalog_embeddings(
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    query, scoped_file_version_ids = _build_scoped_top_event_catalog_query(selected_file_version_ids)
    docs = list(top_event_catalog_col.find(query))
    if not docs:
        return {
            "scope_file_version_ids": scoped_file_version_ids,
            "total_docs": 0,
            "refreshed_docs": 0,
            "embedding_enabled": bool(EMBEDDING_MODEL and _embedding_openai()),
        }

    refreshed_docs = 0
    semantic_updates: List[Dict[str, Any]] = []
    refresh_docs: List[Dict[str, Any]] = []
    refresh_texts: List[str] = []
    for doc in docs:
        semantic_text = doc.get("semantic_text") or _build_top_event_semantic_text(
            doc.get("name") or "",
            normalized_name=doc.get("normalized_name"),
            aliases=(doc.get("aliases") or []) + (doc.get("normalized_aliases") or []),
        )
        if semantic_text != doc.get("semantic_text"):
            semantic_updates.append(
                {
                    "_id": doc["_id"],
                    "semantic_text": semantic_text,
                }
            )
            doc["semantic_text"] = semantic_text

        needs_embedding = bool(
            EMBEDDING_MODEL
            and (
                not isinstance(doc.get("embedding"), list)
                or not doc.get("embedding")
                or doc.get("embedding_model") != EMBEDDING_MODEL
            )
        )
        if needs_embedding:
            refresh_docs.append(doc)
            refresh_texts.append(doc["semantic_text"])

    for item in semantic_updates:
        top_event_catalog_col.update_one(
            {"_id": item["_id"]},
            {"$set": {"semantic_text": item["semantic_text"], "updated_at": _now()}},
        )

    if refresh_docs:
        refreshed_embeddings = _embed_strings_ordered(refresh_texts)
        for doc, embedding in zip(refresh_docs, refreshed_embeddings):
            if not embedding:
                continue
            refreshed_docs += 1
            top_event_catalog_col.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "semantic_text": doc["semantic_text"],
                        "embedding": embedding,
                        "embedding_model": EMBEDDING_MODEL,
                        "embedding_updated_at": _now(),
                        "updated_at": _now(),
                    }
                },
            )

    return {
        "scope_file_version_ids": scoped_file_version_ids,
        "total_docs": len(docs),
        "refreshed_docs": refreshed_docs,
        "embedding_enabled": bool(EMBEDDING_MODEL and _embedding_openai()),
    }


def ensure_top_event_catalog_for_scope(
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    catalog = list_top_event_catalog(selected_file_version_ids=scoped_file_version_ids)
    existing_version_ids = set(
        top_event_catalog_col.distinct(
            "file_version_id",
            {"file_version_id": {"$in": scoped_file_version_ids}},
        )
    )
    rebuilt_file_version_ids: List[str] = []
    for file_version_id in scoped_file_version_ids:
        if file_version_id in existing_version_ids:
            continue
        rebuild_top_event_catalog_for_file_version(file_version_id)
        rebuilt_file_version_ids.append(file_version_id)

    if rebuilt_file_version_ids:
        catalog = list_top_event_catalog(selected_file_version_ids=scoped_file_version_ids)
    embedding_result = ensure_top_event_catalog_embeddings(selected_file_version_ids=scoped_file_version_ids)
    return {
        "catalog": catalog,
        "rebuilt_file_version_ids": rebuilt_file_version_ids,
        "embedding_result": embedding_result,
    }


def get_top_event_catalog_by_graph_node_id(
    graph_node_id: str,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    graph_node_id = _normalize_identifier(graph_node_id)
    if not graph_node_id:
        return None

    explicit_scope = bool(_normalize_file_version_ids(selected_file_version_ids, fallback_to_active=False))
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    query: Dict[str, Any] = {"graph_node_id": graph_node_id}
    if not explicit_scope:
        query["is_active"] = True
    version_filter = _build_file_version_filter(scoped_file_version_ids)
    if version_filter:
        query = {"$and": [query, version_filter]}
    docs = list(top_event_catalog_col.find(query, {"_id": 0}).sort("updated_at", DESCENDING))
    return _merge_catalog_entries(docs)


def _fallback_top_event_similarity(query_text: str, entry: Dict[str, Any]) -> float:
    query_compact = _compact_text(query_text)
    names = _dedupe_keep_order(
        [
            entry.get("name"),
            entry.get("normalized_name"),
            *(entry.get("aliases") or []),
            *(entry.get("normalized_aliases") or []),
        ]
    )
    best = 0.0
    for name in names:
        candidate = _compact_text(name)
        if not query_compact or not candidate:
            continue
        if query_compact == candidate:
            return 1.0
        overlap = 0
        if query_compact in candidate or candidate in query_compact:
            overlap = min(len(query_compact), len(candidate))
        score = overlap / max(len(query_compact), len(candidate))
        best = max(best, score)
    return best


def search_top_event_catalog_semantic(
    query_text: str,
    *,
    selected_file_version_ids: Optional[List[Any]] = None,
    limit: int = TOP_EVENT_VECTOR_CANDIDATE_LIMIT,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or TOP_EVENT_VECTOR_CANDIDATE_LIMIT), TOP_EVENT_VECTOR_CANDIDATE_MAX))
    query, scoped_file_version_ids = _build_scoped_top_event_catalog_query(selected_file_version_ids)

    ensure_top_event_catalog_embeddings(selected_file_version_ids=scoped_file_version_ids)
    docs = list(top_event_catalog_col.find(query))
    if not docs:
        return []

    query_embedding = _embed_strings_ordered([query_text])[0] if EMBEDDING_MODEL else None
    for doc in docs:
        semantic_text = doc.get("semantic_text") or _build_top_event_semantic_text(
            doc.get("name") or "",
            normalized_name=doc.get("normalized_name"),
            aliases=(doc.get("aliases") or []) + (doc.get("normalized_aliases") or []),
        )
        doc["semantic_text"] = semantic_text

    grouped: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        normalized_name = doc.get("normalized_name") or doc.get("name")
        if not normalized_name:
            continue
        similarity = (
            _cosine_similarity(query_embedding, doc.get("embedding"))
            if query_embedding and isinstance(doc.get("embedding"), list)
            else _fallback_top_event_similarity(query_text, doc)
        )
        score = float(similarity if similarity >= 0.0 else 0.0)
        bucket = grouped.setdefault(
            normalized_name,
            {
                "name": doc.get("name"),
                "display_name": doc.get("display_name") or doc.get("name"),
                "normalized_name": normalized_name,
                "aliases": [],
                "normalized_aliases": [],
                "graph_node_id": doc.get("graph_node_id"),
                "graph_node_ids": [],
                "file_ids": [],
                "file_version_ids": [],
                "source_chunk_ids": [],
                "score": score,
                "match_type": "vector" if query_embedding else "lexical",
            },
        )
        bucket["aliases"] = _dedupe_keep_order((bucket.get("aliases") or []) + (doc.get("aliases") or []))
        bucket["normalized_aliases"] = _dedupe_keep_order((bucket.get("normalized_aliases") or []) + (doc.get("normalized_aliases") or []))
        bucket["graph_node_ids"] = _dedupe_keep_order((bucket.get("graph_node_ids") or []) + ([doc.get("graph_node_id")] if doc.get("graph_node_id") else []))
        bucket["file_ids"] = _dedupe_keep_order((bucket.get("file_ids") or []) + ([doc.get("file_id")] if doc.get("file_id") else []))
        bucket["file_version_ids"] = _dedupe_keep_order((bucket.get("file_version_ids") or []) + ([doc.get("file_version_id")] if doc.get("file_version_id") else []))
        bucket["source_chunk_ids"] = _normalize_chunk_refs(
            (bucket.get("source_chunk_ids") or []) + (doc.get("source_chunk_ids") or []),
            file_version_id=doc.get("file_version_id"),
        )
        if score > float(bucket.get("score") or 0.0):
            bucket["score"] = score
            bucket["name"] = doc.get("name")
            bucket["display_name"] = doc.get("display_name") or doc.get("name")
            bucket["graph_node_id"] = doc.get("graph_node_id")

    ranked = sorted(
        grouped.values(),
        key=lambda item: (
            -float(item.get("score") or 0.0),
            item.get("display_name") or item.get("name") or "",
        ),
    )
    return ranked[:limit]


_FAULT_LIKE_KEYWORDS = ("故障", "异常", "报警", "停机", "失败", "触发", "错误", "失效")


def _fault_like_name_heuristic(name: str) -> bool:
    text = _normalize_text(name)
    if not text or len(text) < 2 or len(text) > 80:
        return False
    return any(k in text for k in _FAULT_LIKE_KEYWORDS)


def rebuild_top_event_catalog_for_file_version(file_version_id: str) -> List[Dict[str, Any]]:
    file_version = get_file_version(file_version_id)
    if not file_version:
        raise ValueError(f"Unknown file_version_id: {file_version_id}")

    top_event_catalog_col.delete_many({"file_version_id": file_version_id})
    candidates = list_graph_top_event_candidates(selected_file_version_ids=[file_version_id])
    created = []
    for item in candidates:
        name = _normalize_text(item.get("name"))
        normalized_name = _normalize_text(item.get("normalized_name") or name)
        if not name or not normalized_name:
            continue
        created.append(
            upsert_top_event_catalog_entry(
                name=name,
                normalized_name=normalized_name,
                file_id=file_version["file_id"],
                file_version_id=file_version_id,
                aliases=[name],
                normalized_aliases=[candidate for candidate in _dedupe_keep_order([normalized_name, _compact_text(name)]) if candidate != normalized_name],
                source_chunk_ids=item.get("source_chunk_refs") or item.get("source_chunk_ids") or [],
                graph_node_id=item.get("graph_node_id"),
                is_active=bool(file_version.get("is_active")),
            )
        )
    return created


def repair_top_event_catalog_source_chunk_ids(*, file_version_id: Optional[str] = None) -> int:
    query: Dict[str, Any] = {}
    normalized_file_version_id = _normalize_identifier(file_version_id)
    if normalized_file_version_id:
        query["file_version_id"] = normalized_file_version_id

    docs = list(top_event_catalog_col.find(query, {"_id": 1, "file_version_id": 1, "source_chunk_ids": 1}))
    updated = 0
    for doc in docs:
        normalized_refs = _normalize_chunk_refs(
            doc.get("source_chunk_ids") or [],
            file_version_id=doc.get("file_version_id"),
        )
        if normalized_refs == (doc.get("source_chunk_ids") or []):
            continue
        top_event_catalog_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {"source_chunk_ids": normalized_refs, "updated_at": _now()}},
        )
        updated += 1
    return updated


def create_generation_job(
    *,
    job_type: str,
    total: int,
    top_event: Optional[str] = None,
    source_file_version_ids: Optional[List[Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    job_id = f"{job_type}_{uuid4().hex[:10]}"
    now = _now()
    scope_ids = _dedupe_keep_order(source_file_version_ids)
    status = "completed" if total == 0 else "pending"
    doc = {
        "_id": job_id,
        "job_id": job_id,
        "job_type": job_type,
        "top_event": top_event,
        "source_file_version_ids": scope_ids,
        "source_scope_key": _make_scope_key(scope_ids),
        "status": status,
        "total": total,
        "success": 0,
        "failed": 0,
        "running": 0,
        "pending": total,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": now if total == 0 else None,
        "duration_seconds": 0.0 if total == 0 else None,
        "completion_logged_at": None,
    }
    generation_jobs_col.insert_one(doc)
    return _strip_mongo_id(doc)


def update_job_status(job_id: str, *, status: Optional[str] = None, **extra_fields) -> Optional[Dict[str, Any]]:
    current = generation_jobs_col.find_one({"_id": job_id})
    payload = {"updated_at": _now()}
    if status is not None:
        payload["status"] = status
        if status == "running":
            payload["started_at"] = payload["updated_at"]
        if status in {"completed", "partial_failed", "failed"}:
            payload["finished_at"] = payload["updated_at"]
    started_at = payload.get("started_at") or (current or {}).get("started_at")
    finished_at = payload.get("finished_at") or (current or {}).get("finished_at")
    if finished_at and started_at:
        payload["duration_seconds"] = _seconds_between(started_at, finished_at)
    payload.update({k: v for k, v in extra_fields.items() if v is not None})
    generation_jobs_col.update_one({"_id": job_id}, {"$set": payload})
    return get_generation_job(job_id)


def create_generation_job_item(
    *,
    job_id: str,
    top_event: str,
    normalized_top_event: str,
    requested_top_event: Optional[str] = None,
    resolved_top_event: Optional[str] = None,
    graph_node_id: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    source_file_version_ids: Optional[List[Any]] = None,
    requirements: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item_id = f"item_{uuid4().hex[:12]}"
    now = _now()
    scope_ids = _dedupe_keep_order(source_file_version_ids)
    doc = {
        "_id": item_id,
        "item_id": item_id,
        "job_id": job_id,
        "top_event": top_event,
        "requested_top_event": requested_top_event or top_event,
        "resolved_top_event": resolved_top_event or top_event,
        "normalized_top_event": normalized_top_event,
        "graph_node_id": graph_node_id,
        "aliases": _dedupe_keep_order(aliases),
        "source_chunk_ids": _dedupe_keep_order(source_chunk_ids),
        "source_file_version_ids": scope_ids,
        "source_scope_key": _make_scope_key(scope_ids),
        "requirements": requirements or "",
        "status": "pending",
        "progress": 0,
        "stage": "queued",
        "message": "Queued",
        # 增量事件流：用于前端实时展示“多智能体”进度消息（而不是覆盖 message 字段）
        # event_seq 单调递增，便于前端去重/断点续传；events 保留最近 N 条
        "event_seq": 0,
        "events": [],
        "tree_id": None,
        "error": None,
        "execution_owner": None,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
    }
    generation_job_items_col.insert_one(doc)
    refresh_generation_job(job_id)
    return _strip_mongo_id(doc)


def append_generation_job_item_event(
    item_id: str,
    *,
    agent: str,
    text: str,
    level: str = "INFO",
    stage: Optional[str] = None,
    progress: Optional[int] = None,
    kind: str = "log",
    extra: Optional[Dict[str, Any]] = None,
    max_events: int = 200,
) -> Optional[Dict[str, Any]]:
    """
    Append a progress/log event to a job item.
    Stored on the job-item so frontend can poll and render messages in real-time.
    """
    item = generation_job_items_col.find_one({"_id": item_id}, {"event_seq": 1})
    if not item:
        return None
    next_seq = int(item.get("event_seq") or 0) + 1
    now = _now()
    payload = {
        "seq": next_seq,
        "ts": now,
        "agent": str(agent or "").strip() or "Agent",
        "level": str(level or "INFO").upper(),
        "kind": str(kind or "log"),
        "text": str(text or "").rstrip(),
    }
    if stage is not None:
        payload["stage"] = stage
    if progress is not None:
        try:
            payload["progress"] = max(0, min(100, int(progress)))
        except Exception:
            payload["progress"] = None
    if extra and isinstance(extra, dict):
        payload["extra"] = extra

    generation_job_items_col.update_one(
        {"_id": item_id},
        {
            "$set": {"event_seq": next_seq, "updated_at": now},
            "$push": {"events": {"$each": [payload], "$slice": -abs(int(max_events))}},
        },
    )
    refresh_generation_job(item.get("job_id"))
    return get_generation_job_item(item_id)

def update_generation_job_item(
    item_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    tree_id: Optional[str] = None,
    error: Optional[str] = None,
    **extra_fields,
) -> Optional[Dict[str, Any]]:
    item = generation_job_items_col.find_one({"_id": item_id})
    if not item:
        return None

    payload: Dict[str, Any] = {"updated_at": _now()}
    if status is not None:
        payload["status"] = status
        if status == "running" and not item.get("started_at"):
            payload["started_at"] = payload["updated_at"]
        if status in {"success", "failed"}:
            payload["finished_at"] = payload["updated_at"]
    if progress is not None:
        payload["progress"] = max(0, min(100, int(progress)))
    if stage is not None:
        payload["stage"] = stage
    if message is not None:
        payload["message"] = message
    if tree_id is not None:
        payload["tree_id"] = tree_id
    if error is not None:
        payload["error"] = error

    started_at = payload.get("started_at") or item.get("started_at")
    finished_at = payload.get("finished_at") or item.get("finished_at")
    if finished_at and started_at:
        payload["duration_seconds"] = _seconds_between(started_at, finished_at)

    payload.update({k: v for k, v in extra_fields.items() if v is not None})
    generation_job_items_col.update_one({"_id": item_id}, {"$set": payload})
    refresh_generation_job(item["job_id"])
    return get_generation_job_item(item_id)


def claim_generation_job_item(
    item_id: str,
    *,
    execution_owner: str,
    allowed_statuses: Optional[List[str]] = None,
    progress: int = 5,
    stage: str = "prepare",
    message: str = "Preparing generation task",
    **extra_fields,
) -> Optional[Dict[str, Any]]:
    allowed_statuses = allowed_statuses or ["pending"]
    now = _now()
    payload: Dict[str, Any] = {
        "status": "running",
        "progress": max(0, min(100, int(progress))),
        "stage": stage,
        "message": message,
        "execution_owner": execution_owner,
        "updated_at": now,
    }
    payload.update({k: v for k, v in extra_fields.items() if v is not None})

    result = generation_job_items_col.update_one(
        {
            "_id": item_id,
            "status": {"$in": allowed_statuses},
        },
        {
            "$set": payload,
        },
    )
    if result.modified_count == 0:
        return None

    generation_job_items_col.update_one(
        {"_id": item_id, "started_at": None},
        {"$set": {"started_at": now}},
    )
    doc = generation_job_items_col.find_one({"_id": item_id})
    if doc:
        refresh_generation_job(doc["job_id"])
    return get_generation_job_item(item_id)


def get_generation_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _decorate_runtime_fields(generation_jobs_col.find_one({"_id": job_id}))


def find_latest_generation_job_by_scope(
    *,
    job_type: str,
    source_file_version_ids: Optional[List[Any]],
    statuses: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    target_scope_key = _make_scope_key(source_file_version_ids)
    query: Dict[str, Any] = {
        "job_type": job_type,
        "source_scope_key": target_scope_key,
    }
    if statuses:
        query["status"] = {"$in": statuses}
    doc = generation_jobs_col.find_one(query, sort=[("updated_at", DESCENDING)])
    if doc:
        return _decorate_runtime_fields(doc)

    legacy_query: Dict[str, Any] = {"job_type": job_type}
    if statuses:
        legacy_query["status"] = {"$in": statuses}
    cursor = generation_jobs_col.find(legacy_query).sort("updated_at", DESCENDING).limit(200)
    for item in cursor:
        legacy_scope_ids = item.get("source_file_version_ids") or (item.get("metadata") or {}).get("selected_file_version_ids") or []
        if _make_scope_key(legacy_scope_ids) == target_scope_key:
            return _decorate_runtime_fields(item)
    return None


def get_generation_job_item(item_id: str) -> Optional[Dict[str, Any]]:
    return _decorate_runtime_fields(generation_job_items_col.find_one({"_id": item_id}))


def fail_orphan_running_generation_items_after_restart(
    *,
    message: str = "服务已重启，上次生成被中断；请重新提交。",
) -> int:
    """
    进程重启后，内存中的生成线程已消失，但 Mongo 中可能仍为 running，会导致后续请求在
    _wait_for_single_item_result 中无限等待。启动时将这些项标为 failed。
    """
    cursor = generation_job_items_col.find({"status": "running"}, {"job_id": 1})
    job_ids = list({doc.get("job_id") for doc in cursor if doc.get("job_id")})
    now = _now()
    result = generation_job_items_col.update_many(
        {"status": "running"},
        {
            "$set": {
                "status": "failed",
                "error": message,
                "stage": "failed",
                "progress": 100,
                "finished_at": now,
                "updated_at": now,
            }
        },
    )
    for jid in job_ids:
        try:
            refresh_generation_job(jid)
        except Exception:
            pass
    return int(result.modified_count or 0)


def list_generation_job_items(job_id: str) -> List[Dict[str, Any]]:
    cursor = generation_job_items_col.find({"job_id": job_id}, {"_id": 0}).sort("created_at", ASCENDING)
    return [_decorate_runtime_fields(doc) for doc in cursor]


def requeue_generation_job_item(
    item_id: str,
    *,
    allowed_statuses: Optional[List[str]] = None,
    message: str = "Queued for continue",
    stage: str = "queued",
) -> Optional[Dict[str, Any]]:
    allowed_statuses = allowed_statuses or ["pending", "running"]
    payload = {
        "status": "pending",
        "progress": 0,
        "stage": stage,
        "message": message,
        "execution_owner": None,
        "error": None,
        "finished_at": None,
        "duration_seconds": None,
        "updated_at": _now(),
    }
    result = generation_job_items_col.update_one(
        {
            "_id": item_id,
            "status": {"$in": allowed_statuses},
        },
        {"$set": payload},
    )
    if result.modified_count == 0:
        return None
    doc = generation_job_items_col.find_one({"_id": item_id})
    if doc:
        refresh_generation_job(doc["job_id"])
    return get_generation_job_item(item_id)


def prepare_generation_job_for_continue(
    job_id: str,
    *,
    stale_after_seconds: int = 300,
) -> Dict[str, Any]:
    job = refresh_generation_job(job_id)
    if not job:
        raise ValueError(f"Unknown job_id: {job_id}")

    now = _now()
    stale_item_ids: List[str] = []
    pending_item_ids: List[str] = []
    active_running_item_ids: List[str] = []

    items = list_generation_job_items(job_id)
    for item in items:
        item_id = item.get("item_id")
        if not item_id:
            continue
        status = item.get("status")
        if status == "pending":
            pending_item_ids.append(item_id)
            continue
        if status != "running":
            continue
        updated_at = item.get("updated_at") or item.get("started_at")
        if not updated_at:
            stale_item_ids.append(item_id)
            continue
        age_seconds = max(0.0, (now - updated_at).total_seconds())
        if age_seconds >= max(1, int(stale_after_seconds or 300)):
            stale_item_ids.append(item_id)
        else:
            active_running_item_ids.append(item_id)

    resumed_item_ids: List[str] = []
    for item_id in stale_item_ids:
        resumed = requeue_generation_job_item(
            item_id,
            allowed_statuses=["running"],
            message="Requeued after stale running item",
            stage="requeued",
        )
        if resumed:
            resumed_item_ids.append(item_id)

    queue_item_ids = _dedupe_keep_order(pending_item_ids + resumed_item_ids)
    job = refresh_generation_job(job_id) or job
    return {
        "job": job,
        "queue_item_ids": queue_item_ids,
        "stale_requeued_item_ids": resumed_item_ids,
        "active_running_item_ids": active_running_item_ids,
    }


def find_active_job_item_by_top_event(normalized_top_event: str) -> Optional[Dict[str, Any]]:
    return find_active_job_item_by_top_event_and_scope(normalized_top_event, [])


def find_active_job_item_by_top_event_and_scope(
    normalized_top_event: str,
    source_file_version_ids: Optional[List[Any]],
) -> Optional[Dict[str, Any]]:
    doc = generation_job_items_col.find_one(
        {
            "normalized_top_event": normalized_top_event,
            "source_scope_key": _make_scope_key(source_file_version_ids),
            "status": {"$in": ["pending", "running"]},
        },
        sort=[("updated_at", DESCENDING)],
    )
    return _decorate_runtime_fields(doc)


def refresh_generation_job(job_id: str) -> Optional[Dict[str, Any]]:
    items = list(
        generation_job_items_col.find(
            {"job_id": job_id},
            {"status": 1, "started_at": 1, "finished_at": 1},
        )
    )
    total = len(items)
    counts = {
        "success": 0,
        "failed": 0,
        "running": 0,
        "pending": 0,
    }
    for item in items:
        status = item.get("status")
        if status in counts:
            counts[status] += 1

    started_candidates = [item.get("started_at") for item in items if item.get("started_at")]
    finished_candidates = [item.get("finished_at") for item in items if item.get("finished_at")]
    started_at = min(started_candidates) if started_candidates else None

    if total == 0:
        status = "completed"
        finished_at = _now()
    elif counts["success"] == total:
        status = "completed"
        finished_at = _now()
    elif counts["failed"] == total:
        status = "failed"
        finished_at = _now()
    elif counts["success"] + counts["failed"] == total:
        status = "partial_failed" if counts["failed"] else "completed"
        finished_at = _now()
    elif counts["running"] > 0:
        status = "running"
        finished_at = None
    else:
        status = "pending"
        finished_at = None

    if finished_candidates and status in {"completed", "partial_failed", "failed"}:
        finished_at = max(finished_candidates)

    duration_seconds = _seconds_between(started_at, finished_at) if started_at else None

    generation_jobs_col.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": status,
                "total": total,
                "success": counts["success"],
                "failed": counts["failed"],
                "running": counts["running"],
                "pending": counts["pending"],
                "updated_at": _now(),
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": duration_seconds,
            },
            "$setOnInsert": {"created_at": _now()},
        },
    )

    return get_generation_job(job_id)


def try_mark_job_completion_logged(job_id: str) -> bool:
    result = generation_jobs_col.update_one(
        {
            "_id": job_id,
            "completion_logged_at": None,
            "status": {"$in": ["completed", "partial_failed", "failed"]},
        },
        {"$set": {"completion_logged_at": _now()}},
    )
    return result.modified_count > 0
