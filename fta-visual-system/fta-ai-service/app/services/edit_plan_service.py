import copy
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL
from app.schemas import EditRequest, EditResponse, SelectedFile
from app.services.legacy_edit_service import edit_fault_tree
from app.services.tree_utils import diff_graph, node_display_label, normalize_to_graph

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


SUPPORTED_OPS = {
    "rename_node",
    "delete_node",
    "delete_edge",
    "add_edge",
    "add_child_node",
    "insert_gate",
    "move_subtree",
    "set_node_type",
    "set_node_properties",
    "set_gate_type",
    "set_edge_properties",
}


def plan_edit_fault_tree(req: EditRequest) -> EditResponse:
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    if req.tree_json is None:
        raise HTTPException(status_code=400, detail="tree_json is required")

    is_type_change_request = bool(_node_type_from_text(instruction))
    try:
        plan = call_llm_edit_plan(instruction, req.tree_json, req.selected_files)
        updated, report = execute_edit_plan(req.tree_json, plan)
        if report["applied_count"] > 0:
            return EditResponse(
                updated_tree_json=updated,
                diff=diff_graph(req.tree_json, updated),
                rationale=_rationale_with_report(plan, report, "已按结构化编辑计划执行。"),
            )
    except Exception:
        pass

    if is_type_change_request:
        return EditResponse(
            updated_tree_json=req.tree_json,
            diff=diff_graph(req.tree_json, req.tree_json),
            rationale="未能在当前故障树中安全匹配到需要修改类型的节点。",
        )

    return edit_fault_tree(req)


def _rationale_with_report(plan: Dict[str, Any], report: Dict[str, Any], fallback: str) -> str:
    text = str(plan.get("rationale") or fallback)
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if errors:
        text += f" 其中 {len(errors)} 个操作未能安全执行。"
    return text


def call_llm_edit_plan(instruction: str, tree_json: Any, selected_files: List[SelectedFile]) -> Dict[str, Any]:
    if OpenAI is None:
        raise RuntimeError("openai package not available")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")

    nodes, edges = normalize_to_graph(tree_json)
    files_text = ", ".join([f.name or f.id for f in selected_files]) if selected_files else "(none)"
    system = (
        "You are an expert Fault Tree Analysis edit planner.\n"
        "Return ONLY JSON. Do not return markdown.\n"
        "Create a deterministic edit plan for the current graph. Do not rewrite the full tree JSON.\n"
        "Supported operations:\n"
        "- rename_node: {op, target_id?, target_label?, new_label}\n"
        "- delete_node: {op, target_id?, target_label?}\n"
        "- delete_edge: {op, source_id?, source_label?, target_id?, target_label?}. Use this when the user asks to delete/remove a connection between two nodes. If the user says 'delete the edge between A and B', identify the two endpoint nodes; the executor will remove the existing edge between them.\n"
        "- add_edge: {op, source_id?, source_label?, target_id?, target_label?}. In FTA direction, source is ALWAYS the child/cause event and target is ALWAYS the parent/effect event. If one endpoint is a top event, the top event must be target and the other node must be source. In Chinese phrasing, '把A作为父/B作为子', 'A为父B为子', or 'A是父事件B是子事件' means source=B and target=A.\n"
        "- add_child_node: {op, parent_id?, parent_label?, label, node_type?}\n"
        "- insert_gate: {op, parent_id?, parent_label?, gate_type}. Insert an AND/OR gate under a parent event and attach its existing direct children to that gate.\n"
        "- move_subtree: {op, node_id?, node_label?, new_parent_id?, new_parent_label?}. Move a node/subtree under another parent event by rewiring its direct parent edge.\n"
        "- set_node_type: {op, target_id?, target_label?, node_type}. Use this when the user asks to make a node a 顶事件/top event, 中间事件/intermediate event, or 底事件/basic event.\n"
        "- set_node_properties: {op, target_id?, target_label?, properties}. Use this for node/event attributes such as description, priority, probability, showProbability, investigateMethod, rule, rules, documents.\n"
        "- set_gate_type: {op, target_id?, target_label?, gate_type}. Use target_id/target_label for either a gate node or the parent event whose logic gate should change. gate_type must be AND or OR. When the user says to change all gates above/upstream/ancestor of a node, inspect graph.nodes and graph.edges yourself, follow FTA edges from child/cause source to parent/effect target toward the top event, identify every matching gate node or parent event with that gate on the ancestor path, and emit one set_gate_type operation for each gate. For example, '把节点X上方所有AND门换成OR门' means: find node X, walk source -> target edges upward, select only ancestor gates whose current gate is AND, then output multiple set_gate_type operations with gate_type OR.\n"
        "- set_edge_properties: {op, source_id?, source_label?, target_id?, target_label?, properties}. Use this for relation attributes such as relation_type, polarity, certainty, evidence, documents.\n"
        "- validate_edit_plan: this is executed automatically by the backend before applying operations; do not emit it as an operation.\n"
        "For complex edits, return multiple operations in the exact order they should be applied. "
        "Each operation is one tool call and the executor will apply them sequentially. Do not stop after the first requested change; cover every clause in the user instruction.\n"
        "If the user says 'delete edge A-B, then add edge C-D', output two operations: first delete_edge, then add_edge.\n"
        "Use add_child_node only when the user asks to add one event under an existing parent.\n"
        "If the request needs broad restructuring, still output the closest small operations and explain limits in rationale.\n"
        "JSON shape: {\"operations\": [...], \"rationale\": \"short Chinese explanation\"}."
    )
    user = {
        "instruction": instruction,
        "selected_files": files_text,
        "graph": {"nodes": nodes, "edges": edges},
    }
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        temperature=0,
    )
    content = resp.choices[0].message.content or "{}"
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("operations"), list):
        raise RuntimeError("edit plan JSON missing operations")
    return parsed


def _node_type_from_text(text: str) -> Optional[str]:
    value = str(text or "").lower()
    if "底事件" in value or "basic event" in value or "basic_event" in value:
        return "basic"
    if "中间事件" in value or "intermediate event" in value or "intermediate_event" in value:
        return "intermediate"
    if "顶事件" in value or "top event" in value or "top_event" in value:
        return "top"
    return None


def validate_edit_plan(tree_json: Any, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    nodes = _node_list(tree_json) or []
    edges = _edge_list(tree_json) or []
    node_ids = {_node_id(node) for node in nodes if _node_id(node)}
    node_type_by_id = {
        _node_id(node): _normalize_node_type(node.get("type") or node.get("rawType") or node.get("event_type"))
        for node in nodes
        if _node_id(node)
    }
    simulated_edges = [(_edge_source(edge), _edge_target(edge)) for edge in edges]

    for index, op in enumerate(operations[:12]):
        name = str(op.get("op") or op.get("operation") or "").strip()
        if name == "validate_edit_plan":
            continue
        if name not in SUPPORTED_OPS:
            errors.append({"op": name, "index": index, "error": "unsupported operation"})
            continue

        if name in {"add_edge", "set_edge_properties", "delete_edge"}:
            source_id = _resolve_node_id_for_validation(nodes, op.get("source_id"), op.get("source_label"))
            target_id = _resolve_node_id_for_validation(nodes, op.get("target_id"), op.get("target_label"))
            if not source_id or not target_id:
                errors.append({"op": name, "index": index, "error": "source or target node not found"})
                continue
            if name == "add_edge":
                source_id, target_id = _normalize_fta_edge_direction(nodes, source_id, target_id)
                if source_id == target_id:
                    errors.append({"op": name, "index": index, "error": "self edge is not allowed"})
                    continue
                if (source_id, target_id) in simulated_edges:
                    warnings.append({"op": name, "index": index, "warning": "edge already exists"})
                    continue
                if _path_exists(simulated_edges, source_id, target_id):
                    errors.append({"op": name, "index": index, "error": "edge would create a cycle"})
                    continue
                simulated_edges.append((source_id, target_id))
            elif name == "delete_edge":
                simulated_edges = [(s, t) for s, t in simulated_edges if not _same_undirected_edge(s, t, source_id, target_id)]
            continue

        if name == "move_subtree":
            node_id = _resolve_node_id_for_validation(nodes, op.get("node_id") or op.get("target_id"), op.get("node_label") or op.get("target_label"))
            new_parent_id = _resolve_node_id_for_validation(nodes, op.get("new_parent_id") or op.get("parent_id"), op.get("new_parent_label") or op.get("parent_label"))
            if not node_id or not new_parent_id:
                errors.append({"op": name, "index": index, "error": "node or new parent not found"})
                continue
            if node_id == new_parent_id:
                errors.append({"op": name, "index": index, "error": "cannot move a node under itself"})
                continue
            if _path_exists(simulated_edges, node_id, new_parent_id):
                errors.append({"op": name, "index": index, "error": "move would create a cycle"})
                continue
            simulated_edges = [(s, t) for s, t in simulated_edges if s != node_id]
            simulated_edges.append((node_id, new_parent_id))
            continue

        if name == "delete_node":
            node_id = _resolve_node_id_for_validation(nodes, op.get("target_id"), op.get("target_label"))
            if node_id:
                node_type_by_id.pop(node_id, None)
                simulated_edges = [(s, t) for s, t in simulated_edges if s != node_id and t != node_id]
            continue

        if name == "set_node_type":
            node_id = _resolve_node_id_for_validation(nodes, op.get("target_id"), op.get("target_label"))
            node_type = _normalize_node_type(op.get("node_type"))
            if node_id and node_type:
                node_type_by_id[node_id] = node_type
            continue

        if name in {"rename_node", "add_child_node", "insert_gate", "set_node_properties", "set_gate_type"}:
            # These operations are checked by the executor because they may create
            # new ids or intentionally change validation-sensitive structure.
            continue

    parent_ids = {target for _source, target in simulated_edges}
    for node in nodes:
        node_id = _node_id(node)
        if not node_id or node_id not in node_ids or node_id not in node_type_by_id:
            continue
        node_type = node_type_by_id.get(node_id)
        if node_type == "basic" and node_id in parent_ids:
            errors.append({"node_id": node_id, "error": "basic event has children after edit plan"})
        if node_type == "top" and node_id not in parent_ids and len(node_ids) > 1:
            warnings.append({"node_id": node_id, "warning": "top event has no child causes after edit plan"})

    return {"passed": not errors, "errors": errors, "warnings": warnings}


def _resolve_node_id_for_validation(nodes: List[Dict[str, Any]], node_id: Any = None, label: Any = None) -> Optional[str]:
    target_id = str(node_id or "").strip()
    if target_id:
        for node in nodes:
            if _node_id(node) == target_id:
                return target_id
    target_label = str(label or "").strip()
    if target_label:
        exact = [node for node in nodes if _node_label(node) == target_label]
        if len(exact) == 1:
            return _node_id(exact[0])
        contains = [node for node in nodes if target_label in _node_label(node)]
        if len(contains) == 1:
            return _node_id(contains[0])
    return None


def _path_exists(edges: List[Tuple[str, str]], start: str, goal: str) -> bool:
    children_by_parent: Dict[str, List[str]] = {}
    for source, target in edges:
        children_by_parent.setdefault(source, [])
        children_by_parent.setdefault(target, [])
        children_by_parent[target].append(source)
    stack = [start]
    seen = set()
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children_by_parent.get(current) or [])
    return False


def _same_undirected_edge(source: str, target: str, a: str, b: str) -> bool:
    return (source == a and target == b) or (source == b and target == a)


def _normalize_fta_edge_direction(nodes: List[Dict[str, Any]], source_id: str, target_id: str) -> Tuple[str, str]:
    node_by_id = {_node_id(node): node for node in nodes if _node_id(node)}
    source_type = _normalize_node_type((node_by_id.get(source_id) or {}).get("type") or (node_by_id.get(source_id) or {}).get("rawType") or (node_by_id.get(source_id) or {}).get("event_type"))
    target_type = _normalize_node_type((node_by_id.get(target_id) or {}).get("type") or (node_by_id.get(target_id) or {}).get("rawType") or (node_by_id.get(target_id) or {}).get("event_type"))
    if source_type == "top" and target_type != "top":
        return target_id, source_id
    if source_type in {"top", "intermediate"} and target_type == "basic":
        return target_id, source_id
    return source_id, target_id


def execute_edit_plan(tree_json: Any, plan: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    updated = copy.deepcopy(tree_json)
    operations = [op for op in plan.get("operations") or [] if isinstance(op, dict)]
    report: Dict[str, Any] = {"applied": [], "errors": [], "warnings": [], "applied_count": 0}
    validation = validate_edit_plan(tree_json, operations)
    report["validation"] = validation
    if validation.get("errors"):
        report["errors"].extend(validation["errors"])
        report["applied_count"] = 0
        return updated, report
    report["warnings"].extend(validation.get("warnings") or [])

    for op in operations[:12]:
        name = str(op.get("op") or op.get("operation") or "").strip()
        if name not in SUPPORTED_OPS:
            report["errors"].append({"op": name, "error": "unsupported operation"})
            continue
        try:
            changed = _apply_operation(updated, name, op)
            if changed:
                report["applied"].append({"op": name, "target": op.get("target_id") or op.get("target_label") or op.get("label")})
            else:
                report["errors"].append({"op": name, "error": "target not found or no-op"})
        except Exception as exc:
            report["errors"].append({"op": name, "error": str(exc)})

    report["applied_count"] = len(report["applied"])
    return updated, report


def _apply_operation(tree_json: Any, op_name: str, op: Dict[str, Any]) -> bool:
    if op_name == "rename_node":
        node = _find_node(tree_json, op.get("target_id"), op.get("target_label"))
        new_label = str(op.get("new_label") or "").strip()
        if not node or not new_label:
            return False
        _set_node_label(node, new_label)
        return True

    if op_name == "delete_node":
        node_id = _find_node_id(tree_json, op.get("target_id"), op.get("target_label"))
        if not node_id:
            return False
        return _delete_node(tree_json, node_id)

    if op_name == "delete_edge":
        source_id = _find_node_id(tree_json, op.get("source_id"), op.get("source_label"))
        target_id = _find_node_id(tree_json, op.get("target_id"), op.get("target_label"))
        if not source_id or not target_id:
            return False
        return _delete_edge(tree_json, source_id, target_id)

    if op_name == "add_edge":
        source_id = _find_node_id(tree_json, op.get("source_id"), op.get("source_label"))
        target_id = _find_node_id(tree_json, op.get("target_id"), op.get("target_label"))
        if not source_id or not target_id or source_id == target_id:
            return False
        nodes = _node_list(tree_json) or []
        source_id, target_id = _normalize_fta_edge_direction(nodes, source_id, target_id)
        return _add_edge(tree_json, source_id, target_id)

    if op_name == "add_child_node":
        parent_id = _find_node_id(tree_json, op.get("parent_id"), op.get("parent_label"))
        label = str(op.get("label") or "").strip()
        if not parent_id or not label:
            return False
        return _add_child_node(tree_json, parent_id, label, str(op.get("node_type") or "basic"))

    if op_name == "insert_gate":
        parent_id = _find_node_id(tree_json, op.get("parent_id"), op.get("parent_label") or op.get("target_label"))
        gate_type = _normalize_gate_type(op.get("gate_type") or op.get("new_gate_type") or "OR")
        if not parent_id or not gate_type:
            return False
        return _insert_gate(tree_json, parent_id, gate_type)

    if op_name == "move_subtree":
        node_id = _find_node_id(tree_json, op.get("node_id") or op.get("target_id"), op.get("node_label") or op.get("target_label"))
        new_parent_id = _find_node_id(tree_json, op.get("new_parent_id") or op.get("parent_id"), op.get("new_parent_label") or op.get("parent_label"))
        if not node_id or not new_parent_id or node_id == new_parent_id:
            return False
        return _move_subtree(tree_json, node_id, new_parent_id)

    if op_name == "set_node_type":
        node = _find_node(tree_json, op.get("target_id"), op.get("target_label"))
        node_type = _normalize_node_type(op.get("node_type"))
        if not node or not node_type:
            return False
        return _set_node_type(node, node_type)

    if op_name == "set_node_properties":
        node = _find_node(tree_json, op.get("target_id"), op.get("target_label"))
        properties = op.get("properties")
        if not node or not isinstance(properties, dict) or not properties:
            return False
        return _set_node_properties(node, properties)

    if op_name == "set_gate_type":
        gate_type = _normalize_gate_type(op.get("gate_type") or op.get("new_gate_type") or op.get("type"))
        if not gate_type:
            return False
        target = _find_node(tree_json, op.get("target_id"), op.get("target_label"))
        if not target:
            return False
        return _set_gate_type(target, gate_type)

    if op_name == "set_edge_properties":
        source_id = _find_node_id(tree_json, op.get("source_id"), op.get("source_label"))
        target_id = _find_node_id(tree_json, op.get("target_id"), op.get("target_label"))
        properties = op.get("properties")
        if not source_id or not target_id or not isinstance(properties, dict) or not properties:
            return False
        return _set_edge_properties(tree_json, source_id, target_id, properties)

    return False


def _graph_container(tree_json: Any) -> Optional[Dict[str, Any]]:
    if isinstance(tree_json, dict) and isinstance(tree_json.get("tree_data"), dict):
        return tree_json["tree_data"]
    if isinstance(tree_json, dict):
        return tree_json
    return None


def _node_list(tree_json: Any) -> Optional[List[Dict[str, Any]]]:
    container = _graph_container(tree_json)
    if not container:
        return None
    if isinstance(container.get("nodeList"), list):
        return container["nodeList"]
    if isinstance(container.get("nodes"), list):
        return container["nodes"]
    return None


def _edge_list(tree_json: Any) -> Optional[List[Dict[str, Any]]]:
    container = _graph_container(tree_json)
    if not container:
        return None
    if isinstance(container.get("linkList"), list):
        return container["linkList"]
    if isinstance(container.get("edges"), list):
        return container["edges"]
    return None


def _find_node(tree_json: Any, node_id: Any = None, label: Any = None) -> Optional[Dict[str, Any]]:
    nodes = _node_list(tree_json) or []
    target_id = str(node_id or "").strip()
    target_label = str(label or "").strip()
    if target_id:
        for node in nodes:
            if str(node.get("id") or node.get("event_id") or node.get("node_id") or "") == target_id:
                return node
    if target_label:
        for node in nodes:
            if _node_label(node) == target_label:
                return node
        matches = [node for node in nodes if target_label in _node_label(node)]
        if len(matches) == 1:
            return matches[0]
    return None


def _find_node_id(tree_json: Any, node_id: Any = None, label: Any = None) -> Optional[str]:
    node = _find_node(tree_json, node_id, label)
    if not node:
        return None
    return _node_id(node)


def _node_id(node: Dict[str, Any]) -> str:
    return str(node.get("id") or node.get("event_id") or node.get("node_id") or "")


def _node_label(node: Dict[str, Any]) -> str:
    return node_display_label(node).strip()


def _set_node_label(node: Dict[str, Any], label: str) -> None:
    if "name" in node:
        node["name"] = label
    if "label" in node or "name" not in node:
        node["label"] = label
    if "title" in node:
        node["title"] = label
    data = node.get("data") if isinstance(node.get("data"), dict) else None
    if data is not None:
        data["label"] = label
        if "name" in data:
            data["name"] = label
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else None
    event = meta.get("event") if isinstance(meta, dict) and isinstance(meta.get("event"), dict) else None
    if event is not None:
        event["name"] = label
        if "label" in event:
            event["label"] = label


def _normalize_node_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"1", "top", "top_event", "top event", "顶事件"}:
        return "top"
    if text in {"2", "intermediate", "intermediate_event", "intermediate event", "中间事件"}:
        return "intermediate"
    if text in {"3", "basic", "basic_event", "basic event", "底事件"}:
        return "basic"
    return ""


def _set_node_type(node: Dict[str, Any], node_type: str) -> bool:
    old = _normalize_node_type(node.get("type") or node.get("rawType") or node.get("event_type"))
    container_uses_string_type = str(node.get("type") or "").endswith("_event")
    container_uses_numeric_type = str(node.get("type") or "").strip() in {"1", "2", "3"} or isinstance(node.get("type"), int)
    if container_uses_string_type:
        next_type: Any = {
            "top": "top_event",
            "intermediate": "intermediate_event",
            "basic": "basic_event",
        }[node_type]
    elif container_uses_numeric_type:
        next_type = {"top": "1", "intermediate": "2", "basic": "3"}[node_type]
    else:
        next_type = node_type
    node["type"] = next_type
    if "rawType" in node:
        node["rawType"] = next_type
    if "event_type" in node:
        node["event_type"] = next_type
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else None
    if meta is not None:
        meta["rawType"] = next_type
        raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else None
        if raw is not None:
            raw["type"] = next_type
    return old != node_type or str(node.get("type") or "") != str(next_type)


def _set_node_properties(node: Dict[str, Any], properties: Dict[str, Any]) -> bool:
    blocked = {"id", "_id", "type", "rawType", "event_type", "children", "source", "target"}
    before = copy.deepcopy(node)
    clean = {str(k): v for k, v in properties.items() if str(k) not in blocked}
    if not clean:
        return False

    event_keys = {
        "name",
        "description",
        "errorLevel",
        "priority",
        "probability",
        "showProbability",
        "rule",
        "rules",
        "investigateMethod",
        "documents",
        "message",
    }
    for key, value in clean.items():
        if key == "label":
            _set_node_label(node, str(value))
            continue
        node[key] = value

    meta = node.get("meta") if isinstance(node.get("meta"), dict) else None
    event = meta.get("event") if isinstance(meta, dict) and isinstance(meta.get("event"), dict) else None
    raw = meta.get("raw") if isinstance(meta, dict) and isinstance(meta.get("raw"), dict) else None
    raw_event = raw.get("event") if isinstance(raw, dict) and isinstance(raw.get("event"), dict) else None
    if event is not None:
        for key, value in clean.items():
            if key in event_keys:
                event[key] = value
    if raw_event is not None:
        for key, value in clean.items():
            if key in event_keys:
                raw_event[key] = value
    if raw is not None:
        for key, value in clean.items():
            if key in {"name", "description", "priority", "probability", "showProbability", "investigateMethod"}:
                raw[key] = value
    if isinstance(node.get("event"), dict):
        for key, value in clean.items():
            if key in event_keys:
                node["event"][key] = value
    return before != node


def _normalize_gate_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"AND", "与", "与门", "AND门"}:
        return "AND"
    if text in {"OR", "或", "或门", "OR门"}:
        return "OR"
    return ""


def _gate_value_for_existing_style(current: Any, gate_type: str) -> Any:
    if isinstance(current, int):
        return 1 if gate_type == "AND" else 2
    text = str(current or "").strip()
    if text in {"1", "2", ""}:
        return "1" if gate_type == "AND" else "2"
    return gate_type


def _set_gate_type(node: Dict[str, Any], gate_type: str) -> bool:
    before = copy.deepcopy(node)
    if str(node.get("type") or "").lower() == "gate":
        node["label"] = gate_type
        meta = node.get("meta") if isinstance(node.get("meta"), dict) else None
        if meta is not None:
            meta["gateLabel"] = gate_type
            raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else None
            if raw is not None:
                raw["label"] = gate_type
                raw["gate"] = gate_type
        return before != node

    current_gate = node.get("gate")
    node["gate"] = _gate_value_for_existing_style(current_gate, gate_type)
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else None
    if meta is not None:
        meta["gateLabel"] = gate_type
        meta["gateCode"] = 1 if gate_type == "AND" else 2
        raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else None
        if raw is not None:
            raw["gate"] = _gate_value_for_existing_style(raw.get("gate"), gate_type)
    return before != node


def _delete_node(tree_json: Any, node_id: str) -> bool:
    nodes = _node_list(tree_json)
    edges = _edge_list(tree_json)
    if nodes is None:
        return False
    before_nodes = len(nodes)
    nodes[:] = [node for node in nodes if str(node.get("id") or node.get("event_id") or node.get("node_id") or "") != node_id]
    if edges is not None:
        edges[:] = [edge for edge in edges if not _edge_touches(edge, node_id)]
    return len(nodes) != before_nodes


def _delete_edge(tree_json: Any, source_id: str, target_id: str) -> bool:
    edges = _edge_list(tree_json)
    if edges is None:
        return False
    before = len(edges)
    edges[:] = [edge for edge in edges if not _same_undirected_edge(_edge_source(edge), _edge_target(edge), source_id, target_id)]
    return len(edges) != before


def _add_edge(tree_json: Any, source_id: str, target_id: str) -> bool:
    edges = _edge_list(tree_json)
    if edges is None:
        return False
    for edge in edges:
        if _edge_source(edge) == source_id and _edge_target(edge) == target_id:
            return False

    container = _graph_container(tree_json) or {}
    if isinstance(container.get("linkList"), list):
        edges.append({
            "type": "link",
            "sourceId": source_id,
            "targetId": target_id,
            "isCondition": False,
        })
        return True

    edges.append({
        "id": f"e-{source_id}-{target_id}",
        "source": source_id,
        "target": target_id,
    })
    return True


def _insert_gate(tree_json: Any, parent_id: str, gate_type: str) -> bool:
    parent = _find_node(tree_json, parent_id, None)
    if not parent:
        return False
    container = _graph_container(tree_json) or {}
    edges = _edge_list(tree_json)
    nodes = _node_list(tree_json)
    if edges is None or nodes is None:
        return False

    existing_children = [_edge_source(edge) for edge in edges if _edge_target(edge) == parent_id]
    if not existing_children:
        return False

    if isinstance(container.get("linkList"), list):
        return _set_gate_type(parent, gate_type)

    gate_id = f"{parent_id}-gate"
    existing_ids = {_node_id(node) for node in nodes}
    if gate_id in existing_ids:
        gate_node = _find_node(tree_json, gate_id, None)
        return _set_gate_type(gate_node, gate_type) if gate_node else False

    new_gate = {
        "id": gate_id,
        "label": gate_type,
        "type": "gate",
        "meta": {"forNodeId": parent_id, "gateLabel": gate_type},
    }
    edges[:] = [edge for edge in edges if not (_edge_target(edge) == parent_id and _edge_source(edge) in existing_children)]
    nodes.append(new_gate)
    for child_id in existing_children:
        edges.append({"id": f"e-{child_id}-{gate_id}", "source": child_id, "target": gate_id})
    edges.append({"id": f"e-{gate_id}-{parent_id}", "source": gate_id, "target": parent_id, "relation": gate_type})
    return True


def _move_subtree(tree_json: Any, node_id: str, new_parent_id: str) -> bool:
    edges = _edge_list(tree_json)
    if edges is None:
        return False
    before = copy.deepcopy(edges)
    edges[:] = [edge for edge in edges if _edge_source(edge) != node_id]
    if not _add_edge(tree_json, node_id, new_parent_id):
        edges[:] = before
        return False
    return before != edges


def _set_edge_properties(tree_json: Any, source_id: str, target_id: str, properties: Dict[str, Any]) -> bool:
    edges = _edge_list(tree_json)
    if edges is None:
        return False
    edge = next((item for item in edges if _edge_source(item) == source_id and _edge_target(item) == target_id), None)
    if not edge:
        return False
    before = copy.deepcopy(edge)
    blocked = {"id", "_id", "source", "target", "sourceId", "targetId", "from", "to"}
    clean = {str(k): v for k, v in properties.items() if str(k) not in blocked}
    if not clean:
        return False
    relation = edge.get("relation") if isinstance(edge.get("relation"), dict) else {}
    if not relation:
        relation = {}
        edge["relation"] = relation
    for key, value in clean.items():
        if key in {"relation_type", "polarity", "certainty", "evidence", "evidence_texts", "documents", "gate_type", "member_relation", "gate_relation"}:
            relation[key] = value
        else:
            edge[key] = value
    meta = edge.get("meta") if isinstance(edge.get("meta"), dict) else None
    if meta is not None:
        meta_relation = meta.get("relation") if isinstance(meta.get("relation"), dict) else {}
        meta_relation.update(relation)
        meta["relation"] = meta_relation
        raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else None
        if raw is not None:
            raw_relation = raw.get("relation") if isinstance(raw.get("relation"), dict) else {}
            raw_relation.update(relation)
            raw["relation"] = raw_relation
    return before != edge


def _add_child_node(tree_json: Any, parent_id: str, label: str, node_type: str) -> bool:
    container = _graph_container(tree_json)
    nodes = _node_list(tree_json)
    edges = _edge_list(tree_json)
    if not container or nodes is None or edges is None:
        return False
    new_id = _new_node_id(nodes)
    if isinstance(container.get("nodeList"), list):
        raw_type = "2" if node_type in {"intermediate", "intermediate_event"} else "3"
        nodes.append({"id": new_id, "name": label, "label": label, "type": raw_type})
        edges.append({"id": f"e-{new_id}-{parent_id}", "source": new_id, "target": parent_id})
        return True
    nodes.append({"id": new_id, "label": label, "type": node_type or "basic"})
    edges.append({"id": f"e-{new_id}-{parent_id}", "source": new_id, "target": parent_id})
    return True


def _edge_source(edge: Dict[str, Any]) -> str:
    return str(edge.get("source") or edge.get("sourceId") or edge.get("from") or edge.get("child") or edge.get("source_id") or "")


def _edge_target(edge: Dict[str, Any]) -> str:
    return str(edge.get("target") or edge.get("targetId") or edge.get("to") or edge.get("parent") or edge.get("target_id") or "")


def _edge_touches(edge: Dict[str, Any], node_id: str) -> bool:
    return _edge_source(edge) == node_id or _edge_target(edge) == node_id


def _new_node_id(nodes: List[Dict[str, Any]]) -> str:
    existing = {str(node.get("id") or node.get("event_id") or node.get("node_id") or "") for node in nodes}
    stamp = int(time.time() * 1000)
    base = f"n-plan-{stamp}"
    if base not in existing:
        return base
    index = 1
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"
