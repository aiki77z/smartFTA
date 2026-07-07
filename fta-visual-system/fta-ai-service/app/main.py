import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    # openai >= 1.x
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


load_dotenv()


class SelectedFile(BaseModel):
    id: str
    name: Optional[str] = None


class EditRequest(BaseModel):
    instruction: str = Field(..., description="Natural language edit instruction")
    tree_json: Any = Field(..., description="Current fault tree JSON (any supported shape)")
    selected_files: List[SelectedFile] = Field(default_factory=list)


class GraphDiff(BaseModel):
    added: List[str] = Field(default_factory=list)
    modified: List[str] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)
    added_edges: List[str] = Field(default_factory=list)
    removed_edges: List[str] = Field(default_factory=list)


class EditResponse(BaseModel):
    updated_tree_json: Any
    diff: GraphDiff
    rationale: str = ""


def _normalize_to_graph(obj: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Best-effort normalize various tree json shapes to {nodes, edges}.
    Mirrors frontend normalizeToGraph() behavior loosely.
    """
    if not obj:
        return [], []

    # raw-fta wrapper: { tree_data: { nodeList, linkList } }
    if isinstance(obj, dict) and isinstance(obj.get("tree_data"), dict):
        td = obj["tree_data"]
        if isinstance(td.get("nodeList"), list) and isinstance(td.get("linkList"), list):
            obj = td

    # raw-fta core: { nodeList, linkList }
    if isinstance(obj, dict) and isinstance(obj.get("nodeList"), list) and isinstance(obj.get("linkList"), list):
        nodes = []
        for n in obj["nodeList"]:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("id") or n.get("event_id") or n.get("node_id") or "")
            if not nid:
                continue
            label = n.get("name") or n.get("label") or n.get("title") or nid
            raw_type = str(n.get("type") or n.get("rawType") or n.get("event_type") or "")
            # map common codes
            t = "event"
            if raw_type in ("1", "top", "top_event"):
                t = "top"
            elif raw_type in ("2", "intermediate", "intermediate_event"):
                t = "intermediate"
            elif raw_type in ("3", "basic", "basic_event"):
                t = "basic"
            elif raw_type.upper() in ("AND", "OR") or n.get("gateLabel"):
                t = "gate"
            nodes.append({"id": nid, "label": str(label), "type": t})

        edges = []
        for idx, e in enumerate(obj["linkList"]):
            if not isinstance(e, dict):
                continue
            src = e.get("source") or e.get("from") or e.get("child") or e.get("source_id")
            tgt = e.get("target") or e.get("to") or e.get("parent") or e.get("target_id")
            if src is None or tgt is None:
                continue
            edges.append({"id": str(e.get("id") or f"e-{idx}"), "source": str(src), "target": str(tgt)})
        return nodes, edges

    # graph shape: { nodes, edges }
    if isinstance(obj, dict) and isinstance(obj.get("nodes"), list) and isinstance(obj.get("edges"), list):
        nodes = []
        for n in obj["nodes"]:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("id") or "")
            if not nid:
                continue
            label = n.get("label") or n.get("name") or n.get("title") or nid
            t = n.get("type") or "event"
            nodes.append({"id": nid, "label": str(label), "type": str(t)})
        edges = []
        for idx, e in enumerate(obj["edges"]):
            if not isinstance(e, dict):
                continue
            src = e.get("source")
            tgt = e.get("target")
            if src is None or tgt is None:
                continue
            edges.append({"id": str(e.get("id") or f"e-{idx}"), "source": str(src), "target": str(tgt)})
        return nodes, edges

    # treeData-ish: { tree_data: { nodes, edges } } already handled above partially
    return [], []


def _diff_graph(prev_json: Any, next_json: Any) -> GraphDiff:
    prev_nodes, prev_edges = _normalize_to_graph(prev_json)
    next_nodes, next_edges = _normalize_to_graph(next_json)
    prev_map = {n["id"]: n for n in prev_nodes}
    next_map = {n["id"]: n for n in next_nodes}

    added: List[str] = []
    modified: List[str] = []
    removed: List[str] = []

    def edge_key(e: Dict[str, Any]) -> str:
        # 用 source->target 的稳定 key；id 可能是 e-0 这类前端生成值
        return f'{e.get("source","")}->{e.get("target","")}'

    prev_edge_set = {edge_key(e) for e in prev_edges}
    next_edge_set = {edge_key(e) for e in next_edges}
    added_edges = sorted(list(next_edge_set - prev_edge_set))
    removed_edges = sorted(list(prev_edge_set - next_edge_set))

    for nid, n in next_map.items():
        if nid not in prev_map:
            added.append(nid)
        else:
            p = prev_map[nid]
            if p.get("label") != n.get("label") or p.get("type") != n.get("type"):
                modified.append(nid)

    for nid in prev_map.keys():
        if nid not in next_map:
            removed.append(nid)

    return GraphDiff(
        added=added,
        modified=modified,
        removed=removed,
        added_edges=added_edges,
        removed_edges=removed_edges,
    )


def _extract_edge_delete_intent(instruction: str) -> Optional[Tuple[str, str]]:
    """
    尝试从自然语言中解析「删除 A 到 B 的连线/边」意图，返回 (a_text, b_text)。
    只做轻量启发式；解析失败返回 None。
    """
    if not instruction:
        return None
    s = instruction.strip()
    # 常见说法：删除 A 到 B 的线 / 删除 A -> B 的连线
    m = re.search(
        r"删(?:除|掉).{0,16}?(?:连线|线|边).{0,12}?([^\n，,。；;]+?)\s*(?:到|->|→|—>|至)\s*([^\n，,。；;]+)",
        s,
    )
    if m:
        a = m.group(1).strip().strip("“”\"'")
        b = m.group(2).strip().strip("“”\"'")
        if a and b:
            return a, b
    # 删除 A 和 B 之间的线
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
    # 1) id 精确匹配
    for n in nodes:
        if str(n.get("id") or "") == needle:
            return str(n.get("id"))
    # 2) label 精确匹配
    for n in nodes:
        if str(n.get("label") or "").strip() == needle:
            return str(n.get("id"))
    # 3) 子串匹配：取 label 最短者（更具体）
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
    """
    在 updated_tree_json 中删除一条边（source=src_id, target=tgt_id）。
    兼容 raw-fta: tree_data.nodeList/linkList 或 nodeList/linkList，以及 graph: nodes/edges。
    """
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

    changed = False
    # wrapper tree_data
    if isinstance(updated_tree_json.get("tree_data"), dict):
        td = dict(updated_tree_json["tree_data"])
        if prune_link_list(td) or prune_edges(td):
            updated_tree_json = dict(updated_tree_json)
            updated_tree_json["tree_data"] = td
            changed = True
    # top-level raw-fta
    if prune_link_list(updated_tree_json):
        changed = True
    # top-level graph
    if prune_edges(updated_tree_json):
        changed = True
    return updated_tree_json if changed else updated_tree_json


def _postprocess_edges_for_instruction(instruction: str, prev_tree_json: Any, updated_tree_json: Any) -> Any:
    """
    如果用户意图明确是“删除某条连线”，但 LLM 输出未改变边，则尝试在服务端补删该边。
    """
    intent = _extract_edge_delete_intent(instruction)
    if not intent:
        return updated_tree_json

    prev_nodes, prev_edges = _normalize_to_graph(prev_tree_json)
    next_nodes, next_edges = _normalize_to_graph(updated_tree_json)
    if not prev_nodes or not prev_edges:
        return updated_tree_json

    prev_edge_set = {(e.get("source"), e.get("target")) for e in prev_edges}
    next_edge_set = {(e.get("source"), e.get("target")) for e in next_edges}
    # 若边已经变化，则认为模型已处理
    if prev_edge_set != next_edge_set:
        return updated_tree_json

    a_text, b_text = intent
    a_id = _best_match_node_id(prev_nodes, a_text) or _best_match_node_id(next_nodes, a_text)
    b_id = _best_match_node_id(prev_nodes, b_text) or _best_match_node_id(next_nodes, b_text)
    if not a_id or not b_id:
        return updated_tree_json

    # 只在旧图确实存在这条边时才删（避免误删）
    if (a_id, b_id) not in prev_edge_set:
        return updated_tree_json

    return _remove_edge_in_json(updated_tree_json, a_id, b_id)


def _openai_client() -> "OpenAI":
    if OpenAI is None:
        raise RuntimeError("openai package not available")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    return OpenAI(api_key=api_key, base_url=base_url)


def _call_llm_edit(instruction: str, tree_json: Any, selected_files: List[SelectedFile]) -> Dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"

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

    client = _openai_client()
    resp = client.chat.completions.create(
        model=model,
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


app = FastAPI(title="FTA AI Assistant Service", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "service": "fta-ai-assistant",
        "version": "0.2.0",
        "legacy_editor_endpoint": "/api/fta-edit",
        "assistant_endpoint": "/api/assistant/message",
    }


@app.get("/")
def root():
    return {
        "service": "FTA AI Assistant Service",
        "version": "0.2.0",
        "endpoints": {
            "legacy_edit": "/api/fta-edit",
            "assistant_message": "/api/assistant/message",
            "assistant_session": "/api/assistant/session/{session_id}",
        },
    }


@app.post("/api/fta-edit", response_model=EditResponse)
def fta_edit(req: EditRequest):
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    if req.tree_json is None:
        raise HTTPException(status_code=400, detail="tree_json is required")

    try:
        out = _call_llm_edit(instruction, req.tree_json, req.selected_files)
        updated = out.get("updated_tree_json")
        updated = _postprocess_edges_for_instruction(instruction, req.tree_json, updated)
        rationale = str(out.get("rationale") or "")
        diff = _diff_graph(req.tree_json, updated)
        return EditResponse(updated_tree_json=updated, diff=diff, rationale=rationale)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# AssistantAgent endpoints. They are added alongside the legacy editor endpoint
# so the current frontend can keep using /api/fta-edit during migration.
from app.agents.assistant_agent import assistant_agent  # noqa: E402
from app.memory.session_store import session_store  # noqa: E402
from app.schemas import (  # noqa: E402
    AssistantMessageRequest,
    AssistantMessageResponse,
    AssistantSessionResponse,
    AssistantTruncateRequest,
    AssistantTruncateResponse,
)


@app.post("/api/assistant/message", response_model=AssistantMessageResponse)
def assistant_message(req: AssistantMessageRequest):
    try:
        return assistant_agent.handle_message(req)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/assistant/session/{session_id}", response_model=AssistantSessionResponse)
def assistant_session(session_id: str):
    return AssistantSessionResponse(
        session_id=session_id,
        memory=session_store.get_session(session_id),
        messages=session_store.list_messages(session_id),
        pending_actions=session_store.list_pending_actions(session_id),
        messages_path=session_store.session_messages_path(session_id),
    )


@app.post("/api/assistant/session/{session_id}/truncate", response_model=AssistantTruncateResponse)
def assistant_truncate_session(session_id: str, req: AssistantTruncateRequest):
    result = session_store.truncate_messages(
        session_id,
        frontend_message_id=req.frontend_message_id,
        keep_before_index=req.keep_before_index,
    )
    return AssistantTruncateResponse(
        session_id=session_id,
        removed_count=result["removed_count"],
        remaining_count=result["remaining_count"],
        messages_path=session_store.session_messages_path(session_id),
    )

