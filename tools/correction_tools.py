from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import (
    correction_episodes_col,
    list_active_repair_patterns as db_list_active_repair_patterns,
    upsert_repair_pattern,
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


def derive_repair_patterns_from_episode(
    episode: Dict[str, Any],
    *,
    activate_expert_confirmed: bool = True,
) -> List[Dict[str, Any]]:
    status = str((episode or {}).get("status") or "")
    expert_confirmed = status == "expert_confirmed"
    patterns = []
    for diff in (episode or {}).get("diffs") or []:
        if not isinstance(diff, dict):
            continue
        operation = _operation_from_diff(diff)
        if not operation:
            continue
        pattern = {
            "issue_code": _issue_code_from_diff(diff),
            "operation": operation,
            "scope_key": str((episode or {}).get("scope_key") or ""),
            "status": "active" if expert_confirmed and activate_expert_confirmed else "pending",
            "source": "correction_episode",
            "episode_id": (episode or {}).get("episode_id"),
            "tree_id": (episode or {}).get("tree_id"),
            "top_event": (episode or {}).get("top_event"),
            "signature": _pattern_signature(diff),
            "description": _pattern_description(diff),
            "success_count": 1 if expert_confirmed else 0,
            "failure_count": 0,
            "operations": [_operation_payload_from_diff(diff, operation)],
            "updated_at": _now(),
        }
        patterns.append(upsert_repair_pattern(pattern))
    return patterns


def _operation_from_diff(diff: Dict[str, Any]) -> str:
    mapping = {
        "gate_changed": "replace_gate",
        "link_added": "add_edge",
        "link_deleted": "remove_edge",
        "name_edited": "update_node",
        "node_added": "add_node",
        "node_deleted": "remove_node",
    }
    return mapping.get(str(diff.get("type") or ""))


def _issue_code_from_diff(diff: Dict[str, Any]) -> str:
    mapping = {
        "gate_changed": "WRONG_GATE",
        "link_added": "MISSING_EDGE",
        "link_deleted": "REDUNDANT_PATH",
        "name_edited": "INACCURATE_NODE_NAME",
        "node_added": "MISSING_NODE",
        "node_deleted": "REDUNDANT_NODE",
    }
    return mapping.get(str(diff.get("type") or ""), "EXPERT_CORRECTION")


def _operation_payload_from_diff(diff: Dict[str, Any], operation: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "op": operation,
        "source": "repair_patterns",
        "reason_issue_code": _issue_code_from_diff(diff),
    }
    if operation == "replace_gate":
        payload["node_name"] = diff.get("node_name")
        payload["gate"] = diff.get("to_gate") or "OR"
    elif operation == "update_node":
        payload["node_name"] = diff.get("node_name") or diff.get("from_name")
        payload["fields"] = {"name": diff.get("to_name")}
    elif operation == "remove_node":
        payload["node_name"] = diff.get("node_name")
    elif operation == "add_edge":
        payload["from_node"] = diff.get("from_node")
        payload["to_node"] = diff.get("to_node")
    elif operation == "remove_edge":
        payload["from_node"] = diff.get("from_node")
        payload["to_node"] = diff.get("to_node")
    elif operation == "add_node":
        payload["node"] = {
            "name": diff.get("node_name"),
            "type": diff.get("node_type") or "basic_event",
            "gate": diff.get("gate"),
        }
    return payload


def _pattern_signature(diff: Dict[str, Any]) -> str:
    compact = {
        "type": diff.get("type"),
        "node_type": diff.get("node_type"),
        "node_name": diff.get("node_name"),
        "from_gate": diff.get("from_gate"),
        "to_gate": diff.get("to_gate"),
        "from_node": diff.get("from_node"),
        "to_node": diff.get("to_node"),
    }
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str)


def _pattern_description(diff: Dict[str, Any]) -> str:
    diff_type = str(diff.get("type") or "")
    if diff_type == "gate_changed":
        return f"Expert changed gate on {diff.get('node_name')} to {diff.get('to_gate')}"
    if diff_type == "name_edited":
        return f"Expert renamed {diff.get('from_name')} to {diff.get('to_name')}"
    if diff_type == "node_added":
        return f"Expert added node {diff.get('node_name')}"
    if diff_type == "node_deleted":
        return f"Expert removed node {diff.get('node_name')}"
    if diff_type == "link_added":
        return f"Expert added edge {diff.get('from_node')} -> {diff.get('to_node')}"
    if diff_type == "link_deleted":
        return f"Expert removed edge {diff.get('from_node')} -> {diff.get('to_node')}"
    return "Expert correction pattern"
