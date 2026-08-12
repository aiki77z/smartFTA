from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import (
    correction_episodes_col,
    list_active_repair_patterns as db_list_active_repair_patterns,
    upsert_correction_episode,
)


def _now() -> datetime:
    return datetime.utcnow()


def _dedupe(values: Optional[List[Any]]) -> List[Any]:
    result: List[Any] = []
    seen = set()
    for value in values or []:
        if value in (None, ""):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def build_structure_signature(tree_data: Dict[str, Any]) -> str:
    nodes = tree_data.get("nodeList") or []
    links = tree_data.get("linkList") or []
    compact = {
        "nodes": sorted(
            [
                {
                    "id": node.get("id"),
                    "name": node.get("name"),
                    "type": node.get("type"),
                    "gate": node.get("gate"),
                }
                for node in nodes
                if isinstance(node, dict)
            ],
            key=lambda item: str(item.get("id") or ""),
        ),
        "links": sorted(
            [
                {
                    "sourceId": link.get("sourceId"),
                    "targetId": link.get("targetId"),
                    "isCondition": bool(link.get("isCondition")),
                }
                for link in links
                if isinstance(link, dict)
            ],
            key=lambda item: (str(item.get("sourceId") or ""), str(item.get("targetId") or "")),
        ),
    }
    raw = json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_tree_diff_summary(diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    touched_nodes: List[str] = []
    for diff in diffs or []:
        diff_type = str(diff.get("type") or "unknown")
        by_type[diff_type] = by_type.get(diff_type, 0) + 1
        for key in ("node_name", "from_name", "to_name", "from_node", "to_node", "parent_name"):
            value = str(diff.get(key) or "").strip()
            if value and value not in touched_nodes:
                touched_nodes.append(value)
    return {
        "change_count": len(diffs or []),
        "by_type": by_type,
        "touched_nodes": touched_nodes[:50],
    }


def build_correction_episode(
    *,
    tree_id: str,
    ai_version: int,
    expert_version: int,
    ai_tree_data: Dict[str, Any],
    expert_tree_data: Dict[str, Any],
    diffs: List[Dict[str, Any]],
    scope_key: str = "",
    top_event: str = "",
    validation_report: Optional[Dict[str, Any]] = None,
    status: str = "expert_confirmed",
    source: str = "expert_save",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "tree_id": str(tree_id),
        "ai_version": int(ai_version),
        "expert_version": int(expert_version),
        "top_event": str(top_event or ""),
        "scope_key": str(scope_key or ""),
        "status": str(status or "pending"),
        "source": str(source or "expert_save"),
        "diffs": diffs or [],
        "summary": build_tree_diff_summary(diffs or []),
        "validation_report": validation_report or {},
        "structure_signatures": {
            "before": build_structure_signature(ai_tree_data or {}),
            "after": build_structure_signature(expert_tree_data or {}),
        },
        "metadata": metadata or {},
        "created_at": _now(),
        "updated_at": _now(),
    }


def store_correction_episode(episode: Dict[str, Any]) -> Dict[str, Any]:
    return upsert_correction_episode(episode)


def find_active_repair_patterns(
    *,
    issue_codes: Optional[List[str]] = None,
    scope_key: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    codes = _dedupe([str(code or "").strip().upper() for code in issue_codes or []])
    return db_list_active_repair_patterns(issue_codes=codes, scope_key=scope_key, limit=limit)


def list_recent_correction_episodes(
    *,
    scope_key: str = "",
    status: Optional[str] = "expert_confirmed",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if scope_key:
        query["scope_key"] = scope_key
    if status:
        query["status"] = status
    cursor = correction_episodes_col.find(query, {"_id": 0}).sort("created_at", -1).limit(max(1, int(limit or 20)))
    return list(cursor)
