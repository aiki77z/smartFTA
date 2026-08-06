from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pymongo import ASCENDING, ReturnDocument

from database import agent_events_col, agent_runs_col


def _now() -> datetime:
    return datetime.utcnow()


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


def append_agent_event(
    run_id: str,
    event_type: str,
    *,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    now = _now()
    run_doc = agent_runs_col.find_one_and_update(
        {"_id": run_id},
        {"$inc": {"last_event_seq": 1}, "$set": {"updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not run_doc:
        raise ValueError(f"agent run not found: {run_id}")
    seq = int(run_doc.get("last_event_seq") or 1)
    doc = {
        "_id": f"evt_{uuid4().hex[:12]}",
        "run_id": run_id,
        "event_seq": seq,
        "type": str(event_type or ""),
        "stage": stage,
        "message": message,
        "payload": payload or {},
        "created_at": now,
    }
    agent_events_col.insert_one(doc)
    return _strip_mongo_id(doc) or {}


def list_agent_events(run_id: str, after_event_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return []
    after = max(0, int(after_event_seq or 0))
    safe_limit = max(1, min(int(limit or 200), 1000))
    cursor = (
        agent_events_col.find({"run_id": run_id, "event_seq": {"$gt": after}})
        .sort("event_seq", ASCENDING)
        .limit(safe_limit)
    )
    return [_strip_mongo_id(doc) or {} for doc in cursor]

