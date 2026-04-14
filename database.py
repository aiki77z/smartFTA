from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient

from config import MONGO_DB_NAME, MONGO_URI

client = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

trees_col = db["fault_trees"]
versions_col = db["fault_tree_versions"]
chunks_col = db["chunks"]
entity_reverse_index_col = db["entity_reverse_index"]
top_event_catalog_col = db["top_event_catalog"]
generation_jobs_col = db["generation_jobs"]
generation_job_items_col = db["generation_job_items"]


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


def _chunk_sort_key(doc: Dict[str, Any]):
    chunk_id = _get_chunk_identifier(doc)
    try:
        return (0, int(chunk_id))
    except (TypeError, ValueError):
        return (1, str(chunk_id or ""))


def _fetch_chunks_by_identifiers(chunk_ids: List[Any], limit: int) -> List[Dict[str, Any]]:
    normalized_ids = _dedupe_keep_order(chunk_ids)
    if not normalized_ids:
        return []

    numeric_ids = []
    for chunk_id in normalized_ids:
        try:
            numeric_ids.append(int(str(chunk_id).strip()))
        except (TypeError, ValueError):
            continue

    query = {
        "$or": [
            {"chunk_id": {"$in": normalized_ids}},
            {"id": {"$in": _dedupe_keep_order(normalized_ids + numeric_ids)}},
        ]
    }
    docs = list(chunks_col.find(query, {"_id": 0}))
    docs.sort(key=_chunk_sort_key)
    return docs[:limit]


def fetch_chunks_by_ids(chunk_ids: List[Any], limit: int = 8) -> List[Dict[str, Any]]:
    return _fetch_chunks_by_identifiers(chunk_ids, limit)


def _ensure_indexes():
    index_specs = [
        (trees_col, [("catalog_name", ASCENDING), ("updated_at", DESCENDING)]),
        (trees_col, [("normalized_top_event", ASCENDING), ("updated_at", DESCENDING)]),
        (entity_reverse_index_col, [("entity_type", ASCENDING), ("count", DESCENDING)]),
        (entity_reverse_index_col, [("entity_name", ASCENDING)]),
        (top_event_catalog_col, [("normalized_name", ASCENDING)]),
        (top_event_catalog_col, [("normalized_aliases", ASCENDING)]),
        (generation_jobs_col, [("status", ASCENDING), ("updated_at", DESCENDING)]),
        (generation_job_items_col, [("job_id", ASCENDING), ("status", ASCENDING)]),
        (generation_job_items_col, [("normalized_top_event", ASCENDING), ("status", ASCENDING)]),
    ]

    for collection, keys in index_specs:
        try:
            collection.create_index(keys)
        except Exception:
            pass


_ensure_indexes()


def import_chunks(chunks: list):
    """Import the full chunk list into MongoDB and replace old data."""
    chunks_col.drop()
    if chunks:
        chunks_col.insert_many(chunks)
    print(f"Imported {len(chunks)} chunks")


def import_entity_reverse_index(entries: list):
    """Import aggregated entity reverse-index data into MongoDB and replace old data."""
    entity_reverse_index_col.drop()
    if entries:
        entity_reverse_index_col.insert_many(entries)
    print(f"Imported {len(entries)} reverse-index entities")


def list_entity_reverse_index() -> List[Dict[str, Any]]:
    cursor = entity_reverse_index_col.find({}, {"_id": 0}).sort([("count", DESCENDING), ("entity_name", ASCENDING)])
    return list(cursor)


def search_chunks_by_entity_names(entity_names: List[str], limit: int = 8) -> list:
    """
    Recall chunks directly from the entity reverse index using exact entity names.
    """
    cleaned_names = []
    for name in entity_names or []:
        text = str(name).strip()
        if text and text not in cleaned_names:
            cleaned_names.append(text)

    if not cleaned_names:
        return []

    reverse_index_hits = list(
        entity_reverse_index_col.find(
            {"entity_name": {"$in": cleaned_names}},
            {"_id": 0, "chunk_ids": 1},
        )
    )
    indexed_chunk_ids = []
    for hit in reverse_index_hits:
        indexed_chunk_ids.extend(hit.get("chunk_ids") or [])

    return _fetch_chunks_by_identifiers(indexed_chunk_ids, limit)


def search_chunks_by_keywords(keywords: list, limit: int = 8) -> list:
    """
    Search related chunks using exact keyword hit first, then fuzzy text match.
    """
    cleaned_keywords = []
    for kw in keywords or []:
        text = str(kw).strip()
        if text and text not in cleaned_keywords:
            cleaned_keywords.append(text)

    if not cleaned_keywords:
        return []

    results = []
    seen_ids = set()

    reverse_index_hits = list(
        entity_reverse_index_col.find(
            {"entity_name": {"$in": cleaned_keywords}},
            {"_id": 0, "chunk_ids": 1},
        )
    )
    indexed_chunk_ids = []
    for hit in reverse_index_hits:
        indexed_chunk_ids.extend(hit.get("chunk_ids") or [])

    for doc in _fetch_chunks_by_identifiers(indexed_chunk_ids, limit):
        doc_id = _get_chunk_identifier(doc)
        if doc_id not in seen_ids:
            results.append(doc)
            seen_ids.add(doc_id)

    if len(results) >= limit:
        return results[:limit]

    exact_hits = chunks_col.find({"key_word": {"$in": cleaned_keywords}}, limit=limit)
    for doc in exact_hits:
        doc_id = _get_chunk_identifier(doc)
        if doc_id not in seen_ids:
            results.append(doc)
            seen_ids.add(doc_id)

    if len(results) >= limit:
        return results[:limit]

    regex_clauses = []
    for kw in cleaned_keywords:
        regex = {"$regex": re.escape(kw), "$options": "i"}
        regex_clauses.extend(
            [
                {"chunk_name": regex},
                {"content": regex},
                {"chapter": regex},
                {"section": regex},
                {"subsection": regex},
            ]
        )

    if regex_clauses:
        fuzzy_hits = chunks_col.find({"$or": regex_clauses}, limit=limit * 3)
        for doc in fuzzy_hits:
            doc_id = _get_chunk_identifier(doc)
            if doc_id not in seen_ids:
                results.append(doc)
                seen_ids.add(doc_id)
            if len(results) >= limit:
                break

    return results[:limit]


def list_all_chunks() -> List[Dict[str, Any]]:
    chunks = list(chunks_col.find({}, {"_id": 0}))
    return sorted(chunks, key=_chunk_sort_key)


def get_chunk_by_id(chunk_id: Any) -> dict:
    candidates = _dedupe_keep_order([chunk_id, str(chunk_id).strip()])
    try:
        numeric_value = int(str(chunk_id).strip())
        candidates = _dedupe_keep_order(candidates + [numeric_value])
    except (TypeError, ValueError):
        pass

    return chunks_col.find_one(
        {
            "$or": [
                {"id": {"$in": candidates}},
                {"chunk_id": {"$in": candidates}},
            ]
        }
    )


def create_tree(
    tree_id: str,
    top_event: str,
    catalog_name: Optional[str] = None,
    normalized_top_event: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    job_id: Optional[str] = None,
    job_item_id: Optional[str] = None,
):
    trees_col.insert_one(
        {
            "_id": tree_id,
            "top_event": top_event,
            "catalog_name": catalog_name or top_event,
            "normalized_top_event": normalized_top_event or top_event,
            "query_aliases": _dedupe_keep_order(aliases),
            "source_chunk_ids": _dedupe_keep_order(source_chunk_ids),
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
) -> Optional[Dict[str, Any]]:
    conditions = []
    candidate_names = _dedupe_keep_order([catalog_name, top_event] + (aliases or []))

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
        "tree_data": version.get("tree_data"),
        "version_data": version,
    }


def save_version(tree_id: str, tree_data: dict, editor: str, description: str, is_ai: bool) -> int:
    latest = versions_col.find_one({"tree_id": tree_id}, sort=[("version", -1)])
    new_version = (latest["version"] + 1) if latest else 1

    versions_col.insert_one(
        {
            "tree_id": tree_id,
            "version": new_version,
            "is_ai_generated": is_ai,
            "created_at": _now(),
            "editor": editor,
            "description": description,
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
    aliases: Optional[List[str]] = None,
    normalized_aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    aliases = _dedupe_keep_order([alias for alias in aliases or [] if alias != name])
    normalized_aliases = _dedupe_keep_order(
        [alias for alias in normalized_aliases or [] if alias and alias != normalized_name]
    )
    source_chunk_ids = _dedupe_keep_order(source_chunk_ids)

    existing = top_event_catalog_col.find_one({"_id": normalized_name})
    now = _now()

    if existing:
        merged_aliases = _dedupe_keep_order((existing.get("aliases") or []) + aliases)
        merged_normalized_aliases = _dedupe_keep_order(
            (existing.get("normalized_aliases") or []) + normalized_aliases
        )
        merged_source_chunk_ids = _dedupe_keep_order((existing.get("source_chunk_ids") or []) + source_chunk_ids)
        top_event_catalog_col.update_one(
            {"_id": normalized_name},
            {
                "$set": {
                    "updated_at": now,
                    "aliases": merged_aliases,
                    "normalized_aliases": merged_normalized_aliases,
                    "source_chunk_ids": merged_source_chunk_ids,
                }
            },
        )
    else:
        top_event_catalog_col.insert_one(
            {
                "_id": normalized_name,
                "name": name,
                "normalized_name": normalized_name,
                "aliases": aliases,
                "normalized_aliases": normalized_aliases,
                "source_chunk_ids": source_chunk_ids,
                "created_at": now,
                "updated_at": now,
            }
        )

    return get_top_event_catalog(normalized_name)


def get_top_event_catalog(normalized_name: str) -> Optional[Dict[str, Any]]:
    return _strip_mongo_id(top_event_catalog_col.find_one({"_id": normalized_name}))


def resolve_top_event_catalog(
    *,
    normalized_candidates: List[str],
) -> Optional[Dict[str, Any]]:
    normalized_candidates = _dedupe_keep_order(normalized_candidates)
    if not normalized_candidates:
        return None

    doc = top_event_catalog_col.find_one(
        {
            "$or": [
                {"normalized_name": {"$in": normalized_candidates}},
                {"normalized_aliases": {"$in": normalized_candidates}},
                {"_id": {"$in": normalized_candidates}},
            ]
        }
    )
    return _strip_mongo_id(doc)


def list_top_event_catalog(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    cursor = top_event_catalog_col.find({}, {"_id": 0}).sort("name", ASCENDING)
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)


def create_generation_job(
    *,
    job_type: str,
    total: int,
    top_event: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    job_id = f"{job_type}_{uuid4().hex[:10]}"
    now = _now()
    status = "completed" if total == 0 else "pending"
    doc = {
        "_id": job_id,
        "job_id": job_id,
        "job_type": job_type,
        "top_event": top_event,
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
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    requirements: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item_id = f"item_{uuid4().hex[:12]}"
    now = _now()
    doc = {
        "_id": item_id,
        "item_id": item_id,
        "job_id": job_id,
        "top_event": top_event,
        "normalized_top_event": normalized_top_event,
        "aliases": _dedupe_keep_order(aliases),
        "source_chunk_ids": _dedupe_keep_order(source_chunk_ids),
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


def get_generation_job_item(item_id: str) -> Optional[Dict[str, Any]]:
    return _decorate_runtime_fields(generation_job_items_col.find_one({"_id": item_id}))


def list_generation_job_items(job_id: str) -> List[Dict[str, Any]]:
    cursor = generation_job_items_col.find({"job_id": job_id}, {"_id": 0}).sort("created_at", ASCENDING)
    return [_decorate_runtime_fields(doc) for doc in cursor]


def find_active_job_item_by_top_event(normalized_top_event: str) -> Optional[Dict[str, Any]]:
    doc = generation_job_items_col.find_one(
        {
            "normalized_top_event": normalized_top_event,
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
