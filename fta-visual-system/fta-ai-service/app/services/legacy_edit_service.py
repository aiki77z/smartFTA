import json
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL
from app.schemas import EditRequest, EditResponse, SelectedFile
from app.services.tree_utils import diff_graph, normalize_to_graph

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


def _extract_edge_delete_intent(instruction: str) -> Optional[Tuple[str, str]]:
    if not instruction:
        return None
    s = instruction.strip()
    m = re.search(
        r"删(?:除|掉).{0,16}?(?:连线|线|边).{0,12}?([^\n，,。；;]+?)\s*(?:到|->|→|—>|至)\s*([^\n，,。；;]+)",
        s,
    )
    if m:
        a = m.group(1).strip().strip("“”\"'")
        b = m.group(2).strip().strip("“”\"'")
        if a and b:
            return a, b
    m2 = re.search(
        r"删(?:除|掉).{0,16}?(?:连线|线|边).{0,12}?([^\n，,。；;]+?)\s*(?:和|与)\s*([^\n，,。；;]+?)\s*(?:之间|中间)",
        s,
    )
    if m2:
        a = m2.group(1).strip().strip("“”\"'")
        b = m2.group(2).strip().strip("“”\"'")
        if a and b:
            return a, b
    return None


def _best_match_node_id(nodes: List[Dict[str, Any]], needle: str) -> Optional[str]:
    needle = (needle or "").strip()
    if not needle:
        return None
    for n in nodes:
        if str(n.get("id") or "") == needle:
            return str(n.get("id"))
    for n in nodes:
        if str(n.get("label") or "").strip() == needle:
            return str(n.get("id"))
    candidates: List[Tuple[int, str]] = []
    for n in nodes:
        label = str(n.get("label") or "")
        if needle and needle in label:
            candidates.append((len(label), str(n.get("id"))))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    return None


def _remove_edge_in_json(updated_tree_json: Any, src_id: str, tgt_id: str) -> Any:
    if not isinstance(updated_tree_json, dict):
        return updated_tree_json

    def prune_link_list(container: Dict[str, Any]) -> bool:
        ll = container.get("linkList")
        if not isinstance(ll, list):
            return False
        before = len(ll)

        def keep(e: Any) -> bool:
            if not isinstance(e, dict):
                return True
            s = e.get("source") or e.get("from") or e.get("child") or e.get("source_id")
            t = e.get("target") or e.get("to") or e.get("parent") or e.get("target_id")
            if s is None or t is None:
                return True
            return not (str(s) == str(src_id) and str(t) == str(tgt_id))

        container["linkList"] = [e for e in ll if keep(e)]
        return len(container["linkList"]) != before

    def prune_edges(container: Dict[str, Any]) -> bool:
        el = container.get("edges")
        if not isinstance(el, list):
            return False
        before = len(el)

        def keep(e: Any) -> bool:
            if not isinstance(e, dict):
                return True
            s = e.get("source")
            t = e.get("target")
            if s is None or t is None:
                return True
            return not (str(s) == str(src_id) and str(t) == str(tgt_id))

        container["edges"] = [e for e in el if keep(e)]
        return len(container["edges"]) != before

    if isinstance(updated_tree_json.get("tree_data"), dict):
        td = dict(updated_tree_json["tree_data"])
        if prune_link_list(td) or prune_edges(td):
            updated_tree_json = dict(updated_tree_json)
            updated_tree_json["tree_data"] = td
    prune_link_list(updated_tree_json)
    prune_edges(updated_tree_json)
    return updated_tree_json


def postprocess_edges_for_instruction(instruction: str, prev_tree_json: Any, updated_tree_json: Any) -> Any:
    intent = _extract_edge_delete_intent(instruction)
    if not intent:
        return updated_tree_json

    prev_nodes, prev_edges = normalize_to_graph(prev_tree_json)
    next_nodes, next_edges = normalize_to_graph(updated_tree_json)
    if not prev_nodes or not prev_edges:
        return updated_tree_json

    prev_edge_set = {(e.get("source"), e.get("target")) for e in prev_edges}
    next_edge_set = {(e.get("source"), e.get("target")) for e in next_edges}
    if prev_edge_set != next_edge_set:
        return updated_tree_json

    a_text, b_text = intent
    a_id = _best_match_node_id(prev_nodes, a_text) or _best_match_node_id(next_nodes, a_text)
    b_id = _best_match_node_id(prev_nodes, b_text) or _best_match_node_id(next_nodes, b_text)
    if not a_id or not b_id or (a_id, b_id) not in prev_edge_set:
        return updated_tree_json
    return _remove_edge_in_json(updated_tree_json, a_id, b_id)


def _openai_client() -> "OpenAI":
    if OpenAI is None:
        raise RuntimeError("openai package not available")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


def call_llm_edit(instruction: str, tree_json: Any, selected_files: List[SelectedFile]) -> Dict[str, Any]:
    files_text = ", ".join([f.name or f.id for f in selected_files]) if selected_files else "(none)"
    system = (
        "You are an expert Fault Tree Analysis (FTA) editor.\n"
        "Task: Modify the given fault-tree JSON according to the user's instruction.\n"
        "You can modify BOTH nodes AND connections (edges/links). If the user asks to delete/add/change a line/edge, you MUST update linkList/edges accordingly.\n"
        "Return ONLY valid JSON with keys:\n"
        "  updated_tree_json: the full updated JSON\n"
        "  rationale: short explanation in Chinese\n"
        "Constraints:\n"
        "- Keep the overall JSON shape compatible with the input as much as possible.\n"
        "- Preserve node ids if possible; only create new ids when adding nodes.\n"
        "- When deleting a node, also remove any edges connected to it.\n"
        "- Do not leave dangling edges (source/target must exist in nodeList/nodes).\n"
        "- Do not wrap output in markdown.\n"
    )
    user = {
        "instruction": instruction,
        "selected_files": files_text,
        "tree_json": tree_json,
    }
    resp = _openai_client().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        temperature=0.2,
    )
    content = resp.choices[0].message.content or ""
    try:
        parsed = json.loads(content)
    except Exception as e:
        raise RuntimeError(f"LLM returned non-JSON: {e}\nRaw:\n{content[:1200]}")
    if not isinstance(parsed, dict) or "updated_tree_json" not in parsed:
        raise RuntimeError("LLM JSON missing updated_tree_json")
    return parsed


def edit_fault_tree(req: EditRequest) -> EditResponse:
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    if req.tree_json is None:
        raise HTTPException(status_code=400, detail="tree_json is required")

    out = call_llm_edit(instruction, req.tree_json, req.selected_files)
    updated = postprocess_edges_for_instruction(instruction, req.tree_json, out.get("updated_tree_json"))
    return EditResponse(
        updated_tree_json=updated,
        diff=diff_graph(req.tree_json, updated),
        rationale=str(out.get("rationale") or ""),
    )
