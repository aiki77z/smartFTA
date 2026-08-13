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
    safe_metadata = metadata or {}
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
        "payload": content,
        "content": content,
        "content_hash": _content_hash(content),
        "metadata": safe_metadata,
        "created_at": now,
        "updated_at": now,
    }
    agent_artifacts_col.insert_one(doc)
    return _strip_mongo_id(doc) or {}

