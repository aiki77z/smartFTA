from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pymongo import ReturnDocument

from database import agent_runs_col

from .policies import CONTRACT_VERSION, RUN_STATUS_QUEUED, STAGE_SCOPE


def _now() -> datetime:
    return datetime.utcnow()


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


def _dedupe_keep_order(values: Optional[List[Any]]) -> List[str]:
    result: List[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def make_scope_key(selected_file_version_ids: Optional[List[Any]]) -> str:
    return "|".join(_dedupe_keep_order(selected_file_version_ids))


def create_agent_run(
    *,
    task_type: str,
    prompt: str,
    selected_file_version_ids: Optional[List[Any]] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    canvas_id: Optional[str] = None,
    tree_id: Optional[str] = None,
    tree_version: Optional[int] = None,
    execution_mode: str = "async",
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    run_id = f"run_{uuid4().hex[:12]}"
    now = _now()
    scope_ids = _dedupe_keep_order(selected_file_version_ids)
    doc = {
        "_id": run_id,
        "run_id": run_id,
        "contract_version": CONTRACT_VERSION,
        "task_type": str(task_type or "generate_fault_tree"),
        "prompt": str(prompt or ""),
        "selected_file_version_ids": scope_ids,
        "scope_key": make_scope_key(scope_ids),
        "session_id": str(session_id).strip() if session_id else None,
        "project_id": str(project_id).strip() if project_id else None,
        "canvas_id": str(canvas_id).strip() if canvas_id else None,
        "requested_top_event": None,
        "resolved_top_event": None,
        "normalized_top_event": None,
        "graph_node_id": None,
        "requirements": "",
        "generation_job_id": None,
        "generation_job_item_id": None,
        "last_generation_job_event_seq": 0,
        "retry_count": 0,
        "repair_attempt_count": 0,
        "tree_id": str(tree_id).strip() if tree_id else None,
        "tree_version": tree_version,
        "review_tree_artifact_id": None,
        "execution_mode": execution_mode,
        "status": RUN_STATUS_QUEUED,
        # A newly persisted run is ready to execute scope resolution.  The
        # lifecycle remains queued until the executor starts it.
        "current_stage": STAGE_SCOPE,
        "progress": {"completed": 0, "total": 0},
        "confirmation": None,
        "last_event_seq": 0,
        "result": None,
        "error": None,
        "options": options or {},
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    agent_runs_col.insert_one(doc)
    return _strip_mongo_id(doc) or {}


def get_agent_run(run_id: str) -> Optional[Dict[str, Any]]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    return _strip_mongo_id(agent_runs_col.find_one({"_id": run_id}))


def update_agent_run(run_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    payload = dict(fields or {})
    payload["updated_at"] = _now()
    agent_runs_col.update_one({"_id": run_id}, {"$set": payload})
    return get_agent_run(run_id)


def list_agent_runs_by_status(statuses: List[str], limit: int = 200) -> List[Dict[str, Any]]:
    values = [str(status or "").strip() for status in statuses if str(status or "").strip()]
    if not values:
        return []
    safe_limit = max(1, min(int(limit or 200), 1000))
    cursor = agent_runs_col.find({"status": {"$in": values}}).sort("updated_at", 1).limit(safe_limit)
    return [_strip_mongo_id(doc) or {} for doc in cursor]


def claim_agent_confirmation(
    run_id: str,
    *,
    confirmation_id: str,
    candidate_ref: str,
    fields: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Atomically claim a pending confirmation for one candidate snapshot."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    payload = dict(fields or {})
    payload["updated_at"] = _now()
    doc = agent_runs_col.find_one_and_update(
        {
            "_id": run_id,
            "status": "waiting_confirmation",
            "confirmation.confirmation_id": confirmation_id,
            "confirmation.status": "waiting",
            "confirmation.candidates.candidate_ref": candidate_ref,
        },
        {"$set": payload},
        return_document=ReturnDocument.AFTER,
    )
    return _strip_mongo_id(doc)

