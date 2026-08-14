from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple


REPAIR_PATCH_SCHEMA_VERSION = 1

ALLOWED_REPAIR_OPS = {
    "add_node",
    "update_node",
    "remove_node",
    "add_edge",
    "remove_edge",
    "replace_gate",
    "merge_nodes",
    "mark_uncertain",
}

EVENT_REQUIRED_FIELDS = (
    "id",
    "name",
    "description",
    "errorLevel",
    "priority",
    "probability",
    "showProbability",
    "rules",
    "investigateMethod",
    "documents",
)


def build_repair_patch_from_validation(
    tree_data: Dict[str, Any],
    validation_report: Dict[str, Any],
    *,
    base_artifact_id: Optional[str] = None,
    scope_key: str = "",
    repair_attempt: int = 1,
    patterns: Optional[List[Dict[str, Any]]] = None,
    corrections: Optional[List[Dict[str, Any]]] = None,
    include_warnings: bool = False,
) -> Dict[str, Any]:
    """Build a deterministic second-phase RepairPatch from normalized issues."""

    repairable_issues = [
        issue
        for issue in (validation_report.get("issues") or [])
        if issue.get("repairable")
        and (issue.get("severity") == "error" or include_warnings)
    ]
    operations: List[Dict[str, Any]] = []
    for issue in repairable_issues:
        operations.extend(_operations_for_issue(tree_data, issue))

    operations.extend(_operations_from_patterns(patterns or []))
    operations = _dedupe_operations(operations)
    patch = {
        "schema_version": REPAIR_PATCH_SCHEMA_VERSION,
        "patch_id": "",
        "base_artifact_id": base_artifact_id,
        "repair_attempt": int(repair_attempt or 1),
        "scope_key": str(scope_key or ""),
        "issue_codes": _dedupe_strings([issue.get("issue_code") for issue in repairable_issues]),
        "operations": operations,
        "sources": {
            "validation_report": _summarize_issues(repairable_issues),
            "repair_patterns": _summarize_patterns(patterns or []),
            "corrections": _summarize_corrections(corrections or []),
        },
        "status": "proposed" if operations else "no_reliable_patch",
    }
    patch["patch_id"] = _patch_id(patch)
    return patch


def apply_repair_patch(
    tree_data: Dict[str, Any],
    repair_patch: Dict[str, Any],
    *,
    strict: bool = False,
) -> Dict[str, Any]:
    """Apply a RepairPatch to a copy of tree_data and return audit details."""

    repaired = _copy_tree(tree_data)
    operations = repair_patch.get("operations") or []
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for index, raw_op in enumerate(operations):
        op = _normalize_operation(raw_op, index)
        try:
            changed, detail = _apply_operation(repaired, op)
        except Exception as exc:
            if strict:
                raise
            skipped.append({**op, "status": "failed", "error": str(exc)})
            continue
        if changed:
            applied.append({**op, "status": "applied", **detail})
        else:
            skipped.append({**op, "status": "skipped", **detail})

    _dedupe_links_in_place(repaired)
    return {
        "tree_data": repaired,
        "applied_operations": applied,
        "skipped_operations": skipped,
        "changed": bool(applied),
    }


def _operations_for_issue(tree_data: Dict[str, Any], issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    code = str(issue.get("issue_code") or "").upper()
    node_ids = _issue_node_ids(issue)
    ops: List[Dict[str, Any]] = []

    if code == "MISSING_NODE_ID":
        return _operations_for_missing_node_ids(tree_data, issue)
    if code == "DUPLICATE_NODE_ID":
        return _operations_for_duplicate_node_ids(tree_data, issue)
    if code in {"BROKEN_LINK", "MISSING_LINK_ENDPOINT", "INVALID_LINK"}:
        return _operations_for_invalid_links(tree_data, issue)
    if code == "NO_TOP_EVENT":
        candidate = _choose_top_event_candidate(tree_data)
        if candidate:
            return [
                _op(
                    "update_node",
                    issue,
                    node_id=candidate,
                    fields={"type": "top_event", "event": None},
                    message="Promote a root candidate to top_event.",
                )
            ]
        return [_mark_uncertain(issue)]
    if code == "MULTIPLE_TOP_EVENTS":
        top_ids = _top_event_ids(tree_data)
        for demote_id in top_ids[1:]:
            ops.append(
                _op(
                    "update_node",
                    issue,
                    node_id=demote_id,
                    fields={"type": _infer_non_top_type(tree_data, demote_id)},
                    event_fields=_default_event_fields(demote_id, _node_name(tree_data, demote_id)),
                    message="Demote extra top_event nodes.",
                )
            )
        return ops or [_mark_uncertain(issue)]
    if code == "BASIC_HAS_CHILDREN":
        parent_id = node_ids[0] if node_ids else ""
        if parent_id:
            return [
                _op("update_node", issue, node_id=parent_id, fields={"type": "intermediate_event"}),
                _op("replace_gate", issue, node_id=parent_id, gate="OR"),
            ]
        return [_mark_uncertain(issue)]
    if code == "INTERMEDIATE_WITHOUT_CHILDREN":
        return [
            _op("update_node", issue, node_id=node_id, fields={"type": "basic_event"})
            for node_id in node_ids
        ] or [_mark_uncertain(issue)]
    if code in {"MULTI_CHILD_NO_GATE", "MISSING_GATE"}:
        parent_id = node_ids[0] if node_ids else ""
        return [_op("replace_gate", issue, node_id=parent_id, gate="OR")] if parent_id else [_mark_uncertain(issue)]
    if code == "CYCLE_DETECTED":
        edge = _find_cycle_edge(tree_data, set(node_ids))
        return [_op("remove_edge", issue, source_id=edge[0], target_id=edge[1])] if edge else [_mark_uncertain(issue)]
    if code == "DISCONNECTED_NODES":
        return [
            _op("mark_uncertain", issue, node_id=node_id, message="Disconnected node needs human placement.")
            for node_id in node_ids
        ] or [_mark_uncertain(issue)]
    if code == "MISSING_NODE_NAME":
        return [
            _op("update_node", issue, node_id=node_id, fields={"name": node_id})
            for node_id in node_ids
        ] or [_mark_uncertain(issue)]
    if code == "MISSING_NODE_TYPE":
        return [
            _op("update_node", issue, node_id=node_id, fields={"type": _infer_node_type(tree_data, node_id)})
            for node_id in node_ids
        ] or [_mark_uncertain(issue)]
    if code == "MISSING_EVENT":
        return [
            _op(
                "update_node",
                issue,
                node_id=node_id,
                fields={"event": _default_event_fields(node_id, _node_name(tree_data, node_id))},
            )
            for node_id in node_ids
            if _node_type(tree_data, node_id) != "top_event"
        ] or [_mark_uncertain(issue)]
    if code == "MISSING_EVENT_FIELD":
        fields = _missing_event_fields(issue)
        return [
            _op(
                "update_node",
                issue,
                node_id=node_id,
                event_fields=_default_event_fields(node_id, _node_name(tree_data, node_id), only_fields=fields),
            )
            for node_id in node_ids
            if _node_type(tree_data, node_id) != "top_event"
        ] or [_mark_uncertain(issue)]

    return [_mark_uncertain(issue)]


def _apply_operation(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    name = str(op.get("op") or "")
    if name not in ALLOWED_REPAIR_OPS:
        return False, {"reason": "unsupported_operation"}
    if name == "add_node":
        return _apply_add_node(tree_data, op)
    if name == "update_node":
        return _apply_update_node(tree_data, op)
    if name == "remove_node":
        return _apply_remove_node(tree_data, op)
    if name == "add_edge":
        return _apply_add_edge(tree_data, op)
    if name == "remove_edge":
        return _apply_remove_edge(tree_data, op)
    if name == "replace_gate":
        return _apply_replace_gate(tree_data, op)
    if name == "merge_nodes":
        return _apply_merge_nodes(tree_data, op)
    if name == "mark_uncertain":
        return _apply_mark_uncertain(tree_data, op)
    return False, {"reason": "unhandled_operation"}


def _apply_add_node(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    nodes = _nodes(tree_data)
    node = copy.deepcopy(op.get("node") or {})
    if not isinstance(node, dict):
        return False, {"reason": "node_payload_required"}
    node_id = str(node.get("id") or op.get("node_id") or "").strip()
    node_id = _unique_node_id(tree_data, node_id or "node")
    node["id"] = node_id
    node.setdefault("name", node_id)
    node.setdefault("type", "basic_event")
    if node.get("type") != "top_event" and not isinstance(node.get("event"), dict):
        node["event"] = _default_event_fields(node_id, str(node.get("name") or node_id))
    if node.get("type") == "top_event":
        node["event"] = None
    nodes.append(node)
    return True, {"node_id": node_id}


def _apply_update_node(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    node = _find_node(tree_data, op)
    if not node:
        return False, {"reason": "node_not_found"}
    before = copy.deepcopy(node)
    fields = op.get("fields") if isinstance(op.get("fields"), dict) else {}
    old_node_id = str(node.get("id") or "")
    requested_node_id = str(fields.get("id") or "").strip()
    new_node_id = ""
    if requested_node_id and requested_node_id != old_node_id:
        used_ids = set(_nodes_by_id(tree_data))
        used_ids.discard(old_node_id)
        new_node_id = _unique_from_set(used_ids, requested_node_id)
        node["id"] = new_node_id
        for link in _links(tree_data):
            if str(link.get("sourceId") or "") == old_node_id:
                link["sourceId"] = new_node_id
            if str(link.get("targetId") or "") == old_node_id:
                link["targetId"] = new_node_id
    for key, value in fields.items():
        if key == "id":
            continue
        node[key] = copy.deepcopy(value)
    event_fields = op.get("event_fields") if isinstance(op.get("event_fields"), dict) else {}
    if event_fields:
        if not isinstance(node.get("event"), dict):
            node["event"] = {}
        node["event"].update(copy.deepcopy(event_fields))
    if node.get("type") == "top_event":
        node["event"] = None
    detail = {"node_id": node.get("id")}
    if new_node_id:
        detail["old_node_id"] = old_node_id
        detail["new_node_id"] = new_node_id
    return before != node or bool(new_node_id), detail


def _apply_remove_node(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    node = _find_node(tree_data, op)
    if not node:
        return False, {"reason": "node_not_found"}
    if node.get("type") == "top_event" and not op.get("allow_remove_top"):
        return False, {"reason": "refuse_remove_top_event"}
    node_id = str(node.get("id") or "")
    before_nodes = len(_nodes(tree_data))
    before_links = len(_links(tree_data))
    tree_data["nodeList"] = [item for item in _nodes(tree_data) if str(item.get("id") or "") != node_id]
    tree_data["linkList"] = [
        item
        for item in _links(tree_data)
        if str(item.get("sourceId") or "") != node_id and str(item.get("targetId") or "") != node_id
    ]
    return before_nodes != len(_nodes(tree_data)) or before_links != len(_links(tree_data)), {"node_id": node_id}


def _apply_add_edge(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    source_id = _resolve_node_ref(tree_data, op.get("source_id") or op.get("sourceId") or op.get("from_node"))
    target_id = _resolve_node_ref(tree_data, op.get("target_id") or op.get("targetId") or op.get("to_node"))
    if not source_id or not target_id:
        return False, {"reason": "edge_endpoints_required"}
    if source_id == target_id:
        return False, {"reason": "self_loop_rejected"}
    nodes_by_id = _nodes_by_id(tree_data)
    if source_id not in nodes_by_id or target_id not in nodes_by_id:
        return False, {"reason": "edge_endpoint_not_found"}
    if _edge_exists(tree_data, source_id, target_id):
        return False, {"reason": "edge_already_exists"}
    link = copy.deepcopy(op.get("link") or {})
    if not isinstance(link, dict):
        link = {}
    link.update({"sourceId": source_id, "targetId": target_id})
    link.setdefault("type", "link")
    tree_data["linkList"].append(link)
    return True, {"source_id": source_id, "target_id": target_id}


def _apply_remove_edge(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    source_id = _resolve_node_ref(tree_data, op.get("source_id") or op.get("sourceId") or op.get("from_node"))
    target_id = _resolve_node_ref(tree_data, op.get("target_id") or op.get("targetId") or op.get("to_node"))
    link_id = str(op.get("link_id") or op.get("edge_id") or "").strip()
    link_index = op.get("link_index")
    before = len(_links(tree_data))

    def keep(index: int, link: Dict[str, Any]) -> bool:
        if link_index is not None:
            try:
                if index == int(link_index):
                    return False
            except Exception:
                pass
        if link_id and str(link.get("id") or "") == link_id:
            return False
        if source_id and target_id:
            return not (
                str(link.get("sourceId") or "") == source_id
                and str(link.get("targetId") or "") == target_id
            )
        return True

    tree_data["linkList"] = [link for index, link in enumerate(_links(tree_data)) if keep(index, link)]
    return before != len(_links(tree_data)), {
        "source_id": source_id,
        "target_id": target_id,
        "link_id": link_id,
        "link_index": link_index,
    }


def _apply_replace_gate(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    gate = _normalize_gate(op.get("gate") or op.get("value") or "OR")
    node = _find_node(tree_data, op)
    if not node:
        return False, {"reason": "node_not_found"}
    before = copy.deepcopy(node)
    if node.get("type") == "gate":
        node["gate"] = gate
        node["name"] = gate
        node["label"] = gate
    else:
        node["gate"] = gate
    return before != node, {"node_id": node.get("id"), "gate": gate}


def _apply_merge_nodes(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    canonical_id = str(op.get("canonical_node_id") or "").strip()
    duplicate_id = str(op.get("duplicate_node_id") or op.get("node_id") or "").strip()
    nodes_by_id = _nodes_by_id(tree_data)
    canonical = nodes_by_id.get(canonical_id)
    duplicate = nodes_by_id.get(duplicate_id)
    if not canonical or not duplicate or canonical_id == duplicate_id:
        return False, {"reason": "merge_nodes_need_distinct_existing_nodes"}
    if not canonical.get("name") and duplicate.get("name"):
        canonical["name"] = duplicate.get("name")
    if isinstance(canonical.get("event"), dict) and isinstance(duplicate.get("event"), dict):
        canonical_docs = canonical["event"].get("documents") if isinstance(canonical["event"].get("documents"), list) else []
        duplicate_docs = duplicate["event"].get("documents") if isinstance(duplicate["event"].get("documents"), list) else []
        canonical["event"]["documents"] = _dedupe_json_values(canonical_docs + duplicate_docs)
    for link in _links(tree_data):
        if str(link.get("sourceId") or "") == duplicate_id:
            link["sourceId"] = canonical_id
        if str(link.get("targetId") or "") == duplicate_id:
            link["targetId"] = canonical_id
    tree_data["nodeList"] = [node for node in _nodes(tree_data) if str(node.get("id") or "") != duplicate_id]
    _dedupe_links_in_place(tree_data)
    return True, {"canonical_node_id": canonical_id, "duplicate_node_id": duplicate_id}


def _apply_mark_uncertain(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    node = _find_node(tree_data, op)
    if not node:
        tree_data.setdefault("repair_notes", []).append(_repair_note(op))
        return True, {"tree_note": True}
    before = copy.deepcopy(node)
    node.setdefault("repair", {})
    node["repair"]["uncertain"] = True
    node["repair"]["reason_issue_code"] = op.get("reason_issue_code")
    node["repair"]["message"] = op.get("message") or op.get("reason")
    if isinstance(node.get("event"), dict):
        node["event"]["uncertain"] = True
    return before != node, {"node_id": node.get("id")}


def _normalize_operation(raw_op: Dict[str, Any], index: int) -> Dict[str, Any]:
    op = dict(raw_op or {})
    op["op"] = str(op.get("op") or op.get("operation") or "").strip()
    op.setdefault("op_id", f"op_{index + 1:03d}")
    if "sourceId" in op and "source_id" not in op:
        op["source_id"] = op.get("sourceId")
    if "targetId" in op and "target_id" not in op:
        op["target_id"] = op.get("targetId")
    return op


def _op(op_name: str, issue: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    return {
        "op": op_name,
        "reason_issue_id": issue.get("issue_id"),
        "reason_issue_code": issue.get("issue_code"),
        "source": fields.pop("source", "validation_report"),
        **fields,
    }


def _mark_uncertain(issue: Dict[str, Any]) -> Dict[str, Any]:
    node_ids = _issue_node_ids(issue)
    fields: Dict[str, Any] = {"message": issue.get("message")}
    if node_ids:
        fields["node_id"] = node_ids[0]
    return _op("mark_uncertain", issue, **fields)


def _operations_from_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    operations: List[Dict[str, Any]] = []
    for pattern in patterns[:10]:
        raw_ops = pattern.get("operations")
        if isinstance(raw_ops, list):
            for raw_op in raw_ops:
                if isinstance(raw_op, dict):
                    operations.append(
                        {
                            **raw_op,
                            "source": "repair_patterns",
                            "pattern_id": pattern.get("pattern_id"),
                            "reason_issue_code": raw_op.get("reason_issue_code")
                            or pattern.get("issue_code")
                            or "REPAIR_PATTERN",
                        }
                    )
            continue
        operation = str(pattern.get("operation") or "").strip()
        if operation in ALLOWED_REPAIR_OPS:
            operations.append(
                {
                    "op": operation,
                    "source": "repair_patterns",
                    "pattern_id": pattern.get("pattern_id"),
                    "reason_issue_code": pattern.get("issue_code"),
                }
            )
    return operations


def _operations_for_invalid_links(tree_data: Dict[str, Any], issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    node_ids = set(_nodes_by_id(tree_data))
    ops = []
    for index, link in enumerate(_links(tree_data)):
        source_id = str(link.get("sourceId") or "").strip()
        target_id = str(link.get("targetId") or "").strip()
        if not source_id or not target_id or source_id not in node_ids or target_id not in node_ids:
            ops.append(
                _op(
                    "remove_edge",
                    issue,
                    link_id=link.get("id") or f"link_index_{index}",
                    link_index=index,
                    source_id=source_id,
                    target_id=target_id,
                    message="Remove invalid or broken edge.",
                )
            )
    return ops or [_mark_uncertain(issue)]


def _operations_for_missing_node_ids(tree_data: Dict[str, Any], issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    ops = []
    used_ids = set(_nodes_by_id(tree_data))
    for index, node in enumerate(_nodes(tree_data)):
        if str(node.get("id") or "").strip():
            continue
        new_id = _unique_from_set(used_ids, f"node_{index + 1}")
        used_ids.add(new_id)
        ops.append(
            _op(
                "update_node",
                issue,
                node_index=index,
                node_name=str(node.get("name") or ""),
                fields={"id": new_id},
                message="Assign a generated node id.",
            )
        )
    return ops or [_mark_uncertain(issue)]


def _operations_for_duplicate_node_ids(tree_data: Dict[str, Any], issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    used_ids = {
        str(node.get("id") or "").strip()
        for node in _nodes(tree_data)
        if str(node.get("id") or "").strip()
    }
    ops = []
    for index, node in enumerate(_nodes(tree_data)):
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        if node_id not in seen:
            seen.add(node_id)
            continue
        used_ids.discard(node_id)
        new_id = _unique_from_set(used_ids, f"{node_id}_dup")
        used_ids.add(new_id)
        used_ids.add(node_id)
        ops.append(
            _op(
                "update_node",
                issue,
                node_index=index,
                node_name=str(node.get("name") or ""),
                fields={"id": new_id},
                message="Duplicate node id requires generated replacement id.",
            )
        )
    return ops or [_mark_uncertain(issue)]


def _missing_event_fields(issue: Dict[str, Any]) -> List[str]:
    message = str(issue.get("message") or "")
    found = [field for field in EVENT_REQUIRED_FIELDS if re.search(rf"\b{re.escape(field)}\b", message)]
    return found or list(EVENT_REQUIRED_FIELDS)


def _copy_tree(tree_data: Dict[str, Any]) -> Dict[str, Any]:
    copied = copy.deepcopy(tree_data if isinstance(tree_data, dict) else {})
    if not isinstance(copied.get("nodeList"), list):
        copied["nodeList"] = []
    if not isinstance(copied.get("linkList"), list):
        copied["linkList"] = []
    copied["nodeList"] = [node for node in copied["nodeList"] if isinstance(node, dict)]
    copied["linkList"] = [link for link in copied["linkList"] if isinstance(link, dict)]
    return copied


def _nodes(tree_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(tree_data.get("nodeList"), list):
        tree_data["nodeList"] = []
    return tree_data["nodeList"]


def _links(tree_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(tree_data.get("linkList"), list):
        tree_data["linkList"] = []
    return tree_data["linkList"]


def _nodes_by_id(tree_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(node.get("id") or ""): node
        for node in _nodes(tree_data)
        if str(node.get("id") or "").strip()
    }


def _find_node(tree_data: Dict[str, Any], op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if "node_index" in op:
        try:
            index = int(op.get("node_index"))
            nodes = _nodes(tree_data)
            if 0 <= index < len(nodes):
                return nodes[index]
        except Exception:
            pass
    nodes_by_id = _nodes_by_id(tree_data)
    for key in ("node_id", "target_node_id", "canonical_node_id", "duplicate_node_id"):
        node_id = str(op.get(key) or "").strip()
        if node_id in nodes_by_id:
            return nodes_by_id[node_id]
    node_name = str(op.get("node_name") or "").strip()
    if node_name:
        for node in _nodes(tree_data):
            if str(node.get("name") or "").strip() == node_name:
                return node
    return None


def _resolve_node_ref(tree_data: Dict[str, Any], value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in _nodes_by_id(tree_data):
        return text
    for node in _nodes(tree_data):
        if str(node.get("name") or "").strip() == text:
            return str(node.get("id") or "")
    return text


def _node_name(tree_data: Dict[str, Any], node_id: str) -> str:
    node = _nodes_by_id(tree_data).get(str(node_id or ""))
    return str((node or {}).get("name") or node_id or "")


def _node_type(tree_data: Dict[str, Any], node_id: str) -> str:
    node = _nodes_by_id(tree_data).get(str(node_id or ""))
    return str((node or {}).get("type") or "")


def _issue_node_ids(issue: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    values.extend(issue.get("node_ids") or [])
    for name in issue.get("node_names") or []:
        for part in re.split(r"[,;，；\s]+", str(name or "")):
            if part.strip():
                values.append(part.strip())
    return _dedupe_strings(values)


def _infer_node_type(tree_data: Dict[str, Any], node_id: str) -> str:
    children = [link for link in _links(tree_data) if str(link.get("targetId") or "") == str(node_id or "")]
    parents = [link for link in _links(tree_data) if str(link.get("sourceId") or "") == str(node_id or "")]
    if not parents:
        return "top_event"
    if children:
        return "intermediate_event"
    return "basic_event"


def _infer_non_top_type(tree_data: Dict[str, Any], node_id: str) -> str:
    children = [link for link in _links(tree_data) if str(link.get("targetId") or "") == str(node_id or "")]
    return "intermediate_event" if children else "basic_event"


def _default_event_fields(node_id: str, node_name: str, *, only_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    values = {
        "id": str(node_id or node_name or "event"),
        "name": str(node_name or node_id or "event"),
        "description": "",
        "errorLevel": "",
        "priority": "",
        "probability": "",
        "showProbability": False,
        "rules": [],
        "investigateMethod": "",
        "documents": [],
    }
    if only_fields:
        return {key: values[key] for key in only_fields if key in values}
    return values


def _top_event_ids(tree_data: Dict[str, Any]) -> List[str]:
    return [
        str(node.get("id") or "")
        for node in _nodes(tree_data)
        if str(node.get("type") or "") == "top_event" and str(node.get("id") or "")
    ]


def _choose_top_event_candidate(tree_data: Dict[str, Any]) -> str:
    child_ids = {str(link.get("sourceId") or "") for link in _links(tree_data)}
    parent_ids = {str(link.get("targetId") or "") for link in _links(tree_data)}
    roots = [node_id for node_id in parent_ids if node_id and node_id not in child_ids]
    if len(roots) == 1:
        return roots[0]
    nodes = _nodes(tree_data)
    return str((nodes[0] if nodes else {}).get("id") or "")


def _find_cycle_edge(tree_data: Dict[str, Any], candidate_nodes: set[str]) -> Optional[Tuple[str, str]]:
    adjacency: Dict[str, List[str]] = {}
    for link in _links(tree_data):
        source = str(link.get("sourceId") or "")
        target = str(link.get("targetId") or "")
        if source and target:
            adjacency.setdefault(target, []).append(source)
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node_id: str) -> Optional[Tuple[str, str]]:
        visiting.add(node_id)
        for child in adjacency.get(node_id, []):
            if candidate_nodes and child not in candidate_nodes and node_id not in candidate_nodes:
                continue
            if child in visiting:
                return child, node_id
            if child not in visited:
                found = dfs(child)
                if found:
                    return found
        visiting.remove(node_id)
        visited.add(node_id)
        return None

    for node_id in list(adjacency):
        if node_id not in visited:
            found = dfs(node_id)
            if found:
                return found
    return None


def _edge_exists(tree_data: Dict[str, Any], source_id: str, target_id: str) -> bool:
    return any(
        str(link.get("sourceId") or "") == source_id and str(link.get("targetId") or "") == target_id
        for link in _links(tree_data)
    )


def _dedupe_links_in_place(tree_data: Dict[str, Any]) -> None:
    result = []
    seen = set()
    for link in _links(tree_data):
        source = str(link.get("sourceId") or "")
        target = str(link.get("targetId") or "")
        if not source or not target or source == target:
            continue
        key = (source, target, bool(link.get("isCondition")))
        if key in seen:
            continue
        seen.add(key)
        result.append(link)
    tree_data["linkList"] = result


def _unique_node_id(tree_data: Dict[str, Any], base: str) -> str:
    return _unique_from_set(set(_nodes_by_id(tree_data)), base)


def _unique_from_set(used_ids: set[str], base: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(base or "node")).strip("_") or "node"
    candidate = clean
    index = 1
    while candidate in used_ids:
        index += 1
        candidate = f"{clean}_{index}"
    return candidate


def _normalize_gate(value: Any) -> str:
    gate = str(value or "OR").strip().upper()
    return gate if gate in {"AND", "OR"} else "OR"


def _dedupe_strings(values: List[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _dedupe_json_values(values: List[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _dedupe_operations(operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for operation in operations:
        key = json.dumps(operation, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(operation)
    return result


def _patch_id(patch: Dict[str, Any]) -> str:
    compact = dict(patch)
    compact["patch_id"] = ""
    raw = json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "patch_" + hashlib.sha256(raw).hexdigest()[:12]


def _repair_note(op: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "reason_issue_code": op.get("reason_issue_code"),
        "message": op.get("message") or op.get("reason"),
        "source": op.get("source"),
    }


def _summarize_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "issue_id": issue.get("issue_id"),
            "issue_code": issue.get("issue_code"),
            "severity": issue.get("severity"),
            "node_ids": issue.get("node_ids") or [],
            "repairable": issue.get("repairable"),
        }
        for issue in issues
    ]


def _summarize_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "pattern_id": item.get("pattern_id"),
            "issue_code": item.get("issue_code"),
            "scope_key": item.get("scope_key", ""),
            "status": item.get("status"),
        }
        for item in patterns
    ]


def _summarize_corrections(corrections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "tree_id": item.get("tree_id"),
            "correction_type": item.get("correction_type"),
            "node_name": item.get("node_name"),
            "matched_node_name": item.get("matched_node_name"),
            "similarity": item.get("similarity"),
        }
        for item in corrections
    ]
