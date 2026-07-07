import copy
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL
from app.schemas import EditRequest, EditResponse, SelectedFile
from app.services.legacy_edit_service import edit_fault_tree
from app.services.tree_utils import diff_graph, normalize_to_graph

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


SUPPORTED_OPS = {"rename_node", "delete_node", "delete_edge", "add_child_node"}


def plan_edit_fault_tree(req: EditRequest) -> EditResponse:
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    if req.tree_json is None:
        raise HTTPException(status_code=400, detail="tree_json is required")

    try:
        plan = call_llm_edit_plan(instruction, req.tree_json, req.selected_files)
        updated, report = execute_edit_plan(req.tree_json, plan)
        if report["applied_count"] > 0 and not report["errors"]:
            return EditResponse(
                updated_tree_json=updated,
                diff=diff_graph(req.tree_json, updated),
                rationale=str(plan.get("rationale") or "已按结构化编辑计划执行。"),
            )
    except Exception:
        pass

    return edit_fault_tree(req)


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
        "- delete_edge: {op, source_id?, source_label?, target_id?, target_label?}\n"
        "- add_child_node: {op, parent_id?, parent_label?, label, node_type?}\n"
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


def execute_edit_plan(tree_json: Any, plan: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    updated = copy.deepcopy(tree_json)
    operations = [op for op in plan.get("operations") or [] if isinstance(op, dict)]
    report: Dict[str, Any] = {"applied": [], "errors": [], "applied_count": 0}

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

    if op_name == "add_child_node":
        parent_id = _find_node_id(tree_json, op.get("parent_id"), op.get("parent_label"))
        label = str(op.get("label") or "").strip()
        if not parent_id or not label:
            return False
        return _add_child_node(tree_json, parent_id, label, str(op.get("node_type") or "basic"))

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
    return str(node.get("id") or node.get("event_id") or node.get("node_id") or "")


def _node_label(node: Dict[str, Any]) -> str:
    return str(node.get("name") or node.get("label") or node.get("title") or "").strip()


def _set_node_label(node: Dict[str, Any], label: str) -> None:
    if "name" in node:
        node["name"] = label
    if "label" in node or "name" not in node:
        node["label"] = label
    if "title" in node:
        node["title"] = label


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
    edges[:] = [edge for edge in edges if not (_edge_source(edge) == source_id and _edge_target(edge) == target_id)]
    return len(edges) != before


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
    return str(edge.get("source") or edge.get("from") or edge.get("child") or edge.get("source_id") or "")


def _edge_target(edge: Dict[str, Any]) -> str:
    return str(edge.get("target") or edge.get("to") or edge.get("parent") or edge.get("target_id") or "")


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
