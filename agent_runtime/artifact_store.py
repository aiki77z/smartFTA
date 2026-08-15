from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from pymongo import DESCENDING

from database import agent_artifacts_col


def _now() -> datetime:
    return datetime.utcnow()


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


def _content_hash(content: Any) -> str:
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _mongo_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _mongo_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mongo_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_mongo_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool, datetime)) or value is None:
        return value
    return str(value)


def put_agent_artifact(
    *,
    run_id: str,
    artifact_type: str,
    content: Any,
    version: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    parent_artifact_id: Optional[str] = None,
    producer: Optional[str] = None,
) -> Dict[str, Any]:
    run_id = str(run_id or "").strip()
    artifact_type = str(artifact_type or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    if not artifact_type:
        raise ValueError("artifact_type is required")
    if version is None:
        latest = agent_artifacts_col.find_one(
            {"run_id": run_id, "type": artifact_type},
            sort=[("version", DESCENDING)],
        )
        version = int((latest or {}).get("version") or 0) + 1
    now = _now()
    artifact_id = f"art_{uuid4().hex[:12]}"
    safe_content = _mongo_safe(content)
    safe_metadata = _mongo_safe(metadata or {})
    doc = {
        "_id": artifact_id,
        "artifact_id": artifact_id,
        "run_id": run_id,
        "type": artifact_type,
        "version": int(version),
        "parent_artifact_id": str(parent_artifact_id).strip() if parent_artifact_id else None,
        "producer": str(producer or safe_metadata.get("producer") or "").strip() or None,
        # payload is the published contract field. content is retained for
        # compatibility with the initial runtime implementation.
        "payload": safe_content,
        "content": safe_content,
        "content_hash": _content_hash(safe_content),
        "metadata": safe_metadata,
        "created_at": now,
        "updated_at": now,
    }
    agent_artifacts_col.insert_one(doc)
    return _strip_mongo_id(doc) or {}


def get_latest_agent_artifact(run_id: str, artifact_type: str) -> Optional[Dict[str, Any]]:
    """Return the latest immutable artifact of a type for workflow recovery."""
    run_id = str(run_id or "").strip()
    artifact_type = str(artifact_type or "").strip()
    if not run_id or not artifact_type:
        return None
    doc = agent_artifacts_col.find_one(
        {"run_id": run_id, "type": artifact_type},
        sort=[("version", DESCENDING)],
    )
    return _strip_mongo_id(doc)


def list_agent_artifacts(
    run_id: str,
    artifact_type: Optional[str] = None,
    *,
    limit: int = 100,
) -> list[Dict[str, Any]]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return []
    query: Dict[str, Any] = {"run_id": run_id}
    artifact_type = str(artifact_type or "").strip()
    if artifact_type:
        query["type"] = artifact_type
    safe_limit = max(1, min(int(limit or 100), 1000))
    cursor = agent_artifacts_col.find(query).sort([("created_at", 1), ("version", 1)]).limit(safe_limit)
    return [_strip_mongo_id(doc) or {} for doc in cursor]

