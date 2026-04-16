"""
Fault-tree generation pipeline.

New default flow:
1. Parse the prompt and extract `top_event` + optional `requirements`
2. Match the top event directly on the graph
3. Expand a bounded local subgraph around the matched graph node
4. Collect only chunks directly referenced by that subgraph
5. Ask the LLM to fill the final fault-tree JSON from the graph skeleton + evidence
6. Reuse corrections and validator on the generated draft
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

from openai import OpenAI

from config import GRAPH_TREE_MAX_DEPTH, GRAPH_TREE_MAX_NODES, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from database import (
    collect_subgraph_chunks,
    expand_local_fault_subgraph,
    hydrate_documents_by_chunk_ids,
    list_graph_top_event_candidates,
    match_top_event_from_graph,
)
from validator import validate_full

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

MAX_RETRY = 2
MAX_CHUNKS_FOR_PROMPT = 12
MAX_CHUNK_CHARS = 360
MAX_JSON_REPAIR_RETRY = 1
PROPERTY_FIELDS = (
    "description",
    "errorLevel",
    "priority",
    "probability",
    "showProbability",
    "rule",
    "investigateMethod",
)


def normalize_top_event_name(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = value.replace("（", "(").replace("）", ")")
    value = value.replace("：", ":")
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip("\"'[]{}<>")
    value = re.sub(r"^(请分析|分析|请生成|生成|请针对|针对|关于|处理|诊断|排查)", "", value)
    value = re.sub(r"(故障树|故障分析|故障诊断|排查建议)$", "", value)
    value = value.strip(",:;，。； ")
    return re.sub(r"\s+", " ", value).strip()


def build_top_event_normalized_candidates(name: str, aliases: Optional[List[str]] = None) -> List[str]:
    candidates: List[str] = []

    def add(value: str):
        value = normalize_top_event_name(value)
        if value and value not in candidates:
            candidates.append(value)
        compact = re.sub(r"\s+", "", value)
        if compact and compact not in candidates:
            candidates.append(compact)

    for item in [name] + list(aliases or []):
        add(item)
    return candidates


def parse_user_prompt(prompt: str) -> dict:
    raw = str(prompt or "").strip()
    if not raw:
        raise ValueError("prompt 不能为空")

    normalized = re.sub(r"\s+", " ", raw).strip()
    patterns = [
        r"顶事件(?:为|是)?[:：]?\s*(.+)$",
        r"分析(.+?)(?:故障树|故障|异常)?$",
        r"生成(.+?)(?:故障树)?$",
        r"针对(.+?)(?:进行|生成|分析)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            top_event = normalize_top_event_name(match.group(1))
            if top_event:
                return {"top_event": top_event, "requirements": ""}

    if len(normalized) <= 40 and "\n" not in normalized:
        return {"top_event": normalize_top_event_name(normalized), "requirements": ""}

    prompt_text = f"""
你是工业设备故障树系统的提示词解析器。
请从用户输入中提取：
1. top_event：用户要分析的顶事件
2. requirements：额外要求，没有就返回空字符串

只输出 JSON：
{{
  "top_event": "...",
  "requirements": "..."
}}

用户输入：
{normalized}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt_text}],
        temperature=0.1,
        max_tokens=300,
    )
    parsed = _parse_json(response.choices[0].message.content)
    top_event = normalize_top_event_name(parsed.get("top_event"))
    if not top_event:
        raise ValueError("无法从 prompt 中提取顶事件")
    return {"top_event": top_event, "requirements": str(parsed.get("requirements") or "").strip()}


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _node_is_and(node: Dict[str, Any]) -> bool:
    node_type = str(node.get("node_type") or "").upper()
    if node_type == "AND":
        return True
    return "LogicGate" in (node.get("labels") or [])


def _subgraph_nodes_by_id(subgraph_bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {node["graph_node_id"]: node for node in (subgraph_bundle.get("nodes") or []) if node.get("graph_node_id")}


def _compress_subgraph_to_tree_skeleton(subgraph_bundle: Dict[str, Any]) -> Dict[str, Any]:
    nodes_by_id = _subgraph_nodes_by_id(subgraph_bundle)
    root_id = subgraph_bundle["root"]
    root_node = nodes_by_id[root_id]

    incoming: Dict[str, List[Dict[str, Any]]] = {}
    outgoing: Dict[str, List[Dict[str, Any]]] = {}
    for edge in subgraph_bundle.get("edges") or []:
        incoming.setdefault(edge["target_graph_node_id"], []).append(edge)
        outgoing.setdefault(edge["source_graph_node_id"], []).append(edge)

    fault_nodes = {node_id: node for node_id, node in nodes_by_id.items() if not _node_is_and(node)}
    tree_nodes: List[Dict[str, Any]] = []
    tree_links: List[Dict[str, Any]] = []
    graph_to_tree: Dict[str, str] = {}
    documents_by_node_id: Dict[str, List[Dict[str, Any]]] = {}
    gate_by_tree_id: Dict[str, Optional[str]] = {}

    for graph_node_id, node in fault_nodes.items():
        tree_node_id = _stable_id("node", graph_node_id)
        graph_to_tree[graph_node_id] = tree_node_id
        direct_fault_children = [
            edge["source_graph_node_id"]
            for edge in incoming.get(graph_node_id, [])
            if edge["source_graph_node_id"] in fault_nodes
        ]
        and_gate_children = [
            edge["source_graph_node_id"]
            for edge in incoming.get(graph_node_id, [])
            if edge["source_graph_node_id"] not in fault_nodes and _node_is_and(nodes_by_id[edge["source_graph_node_id"]])
        ]
        has_children = bool(direct_fault_children or and_gate_children)
        if graph_node_id == root_id:
            node_type = "top_event"
        else:
            node_type = "intermediate_event" if has_children else "basic_event"

        gate: Optional[str] = None
        if and_gate_children and not direct_fault_children:
            gate = "AND"
        elif has_children:
            gate = "OR"

        tree_nodes.append(
            {
                "id": tree_node_id,
                "name": node.get("name"),
                "type": node_type,
                "gate": gate,
                "graphNodeId": graph_node_id,
                "kg_key": graph_node_id,
                "source_chunk_ids": node.get("source_chunk_ids") or [],
                "graph_props": {field: node.get(field) for field in PROPERTY_FIELDS},
                "documents_seed": node.get("documents") or [],
            }
        )
        documents_by_node_id[tree_node_id] = []
        gate_by_tree_id[tree_node_id] = gate

    for parent_graph_id, parent_node in fault_nodes.items():
        parent_tree_id = graph_to_tree[parent_graph_id]
        for edge in incoming.get(parent_graph_id, []):
            child_graph_id = edge["source_graph_node_id"]
            if child_graph_id in fault_nodes:
                child_tree_id = graph_to_tree[child_graph_id]
                tree_links.append(
                    {
                        "type": "link",
                        "sourceId": child_tree_id,
                        "targetId": parent_tree_id,
                        "isCondition": False,
                    }
                )
                continue

            gate_node = nodes_by_id.get(child_graph_id)
            if not gate_node or not _node_is_and(gate_node):
                continue
            for gate_edge in incoming.get(child_graph_id, []):
                gate_child_graph_id = gate_edge["source_graph_node_id"]
                if gate_child_graph_id not in fault_nodes:
                    continue
                tree_links.append(
                    {
                        "type": "link",
                        "sourceId": graph_to_tree[gate_child_graph_id],
                        "targetId": parent_tree_id,
                        "isCondition": False,
                    }
                )

    deduped_links = []
    seen = set()
    for link in tree_links:
        key = (link["sourceId"], link["targetId"])
        if key not in seen:
            seen.add(key)
            deduped_links.append(link)

    return {
        "root_tree_node_id": graph_to_tree[root_id],
        "nodes": tree_nodes,
        "links": deduped_links,
        "graph_to_tree": graph_to_tree,
        "gate_by_tree_id": gate_by_tree_id,
    }


def _format_evidence_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    evidence = []
    for chunk in chunks[:MAX_CHUNKS_FOR_PROMPT]:
        content = str(chunk.get("content") or "").strip()
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS] + "..."
        evidence.append(
            {
                "chunk_id": chunk.get("id", chunk.get("chunk_id")),
                "chunk_name": chunk.get("chunk_name", ""),
                "section_path": chunk.get("section_path", ""),
                "source_page": chunk.get("source", ""),
                "content": content,
            }
        )
    return evidence


def build_fault_tree_from_subgraph_and_chunks(
    top_event: str,
    subgraph_bundle: Dict[str, Any],
    evidence_chunks: List[Dict[str, Any]],
    requirements: str = "",
) -> Dict[str, Any]:
    skeleton = _compress_subgraph_to_tree_skeleton(subgraph_bundle)
    evidence = _format_evidence_chunks(evidence_chunks)
    skeleton_for_prompt = {
        "root_tree_node_id": skeleton["root_tree_node_id"],
        "nodes": [
            {
                "id": node["id"],
                "name": node["name"],
                "type": node["type"],
                "gate": node["gate"],
                "graphNodeId": node["graphNodeId"],
                "kg_key": node["kg_key"],
                "graph_props": node["graph_props"],
                "source_chunk_ids": node["source_chunk_ids"],
                "documents_seed": node["documents_seed"],
            }
            for node in skeleton["nodes"]
        ],
        "links": skeleton["links"],
    }

    prompt = f"""
你是工业设备故障树生成助手。请根据“图谱局部子图骨架”和“精简证据 chunks”输出最终故障树 JSON。

要求：
1. 必须保留骨架中的节点和连线，不要凭空新增结构。
2. 图谱中没有单独的 OR 节点；如果某个父节点有多个子节点且未标明 AND，默认 gate=OR。
3. 如果骨架中父节点 gate=AND，则保留为 AND。
4. 顶事件节点 type=top_event，event 必须为 null。
5. intermediate_event 和 basic_event 必须有完整 event 字段。
6. event 中优先使用 graph_props 里的字段，chunks 只用于补充和润色。
7. documents 必须输出完整结构：chunk_id、chunk_name、section_path、source_page。
8. 每个节点都保留 graphNodeId 和 kg_key，值与输入骨架一致。
9. rules 使用数组；如果只有单条 rule 字符串，也请把它转成一条规则对象或留空数组。不要输出未定义字段。
10. 只输出 JSON，不要输出 markdown。

输出格式：
{{
  "nodeList": [...],
  "linkList": [...]
}}

顶事件：{top_event}
额外要求：{requirements or "无"}

图谱骨架：
{json.dumps(skeleton_for_prompt, ensure_ascii=False, indent=2)}

证据 chunks：
{json.dumps(evidence, ensure_ascii=False, indent=2)}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=8192,
    )
    tree = _parse_json(response.choices[0].message.content)
    return _post_process_generated_tree(tree, skeleton, evidence)


def _coerce_rules(rule_value: Any) -> List[Dict[str, Any]]:
    if isinstance(rule_value, list):
        rules = []
        for item in rule_value:
            if isinstance(item, dict):
                rules.append(
                    {
                        "deviceTypeId": str(item.get("deviceTypeId") or ""),
                        "measurePointName": str(item.get("measurePointName") or ""),
                        "symbol": str(item.get("symbol") or ""),
                        "thresholds": [str(x) for x in (item.get("thresholds") or [])],
                        "duration": str(item.get("duration") or ""),
                    }
                )
        return rules
    if isinstance(rule_value, str) and rule_value.strip():
        return [
            {
                "deviceTypeId": "",
                "measurePointName": rule_value.strip(),
                "symbol": "",
                "thresholds": [],
                "duration": "",
            }
        ]
    return []


def _post_process_generated_tree(
    tree: Dict[str, Any],
    skeleton: Dict[str, Any],
    evidence_chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    skeleton_nodes = {node["id"]: node for node in skeleton["nodes"]}
    graph_to_node = {node["graphNodeId"]: node for node in skeleton["nodes"]}
    evidence_doc_map = {
        str(chunk.get("chunk_id")): {
            "chunk_id": chunk.get("chunk_id"),
            "chunk_name": chunk.get("chunk_name", ""),
            "section_path": chunk.get("section_path", ""),
            "source_page": chunk.get("source_page", ""),
        }
        for chunk in evidence_chunks
    }

    node_list = tree.get("nodeList") or []
    if not node_list:
        node_list = []
        for node in skeleton["nodes"]:
            node_list.append(
                {
                    "type": node["type"],
                    "gate": node["gate"],
                    "name": node["name"],
                    "id": node["id"],
                    "transfer": "",
                    "event": None if node["type"] == "top_event" else {},
                    "graphNodeId": node["graphNodeId"],
                    "kg_key": node["kg_key"],
                }
            )

    normalized_nodes = []
    for index, node in enumerate(node_list, start=1):
        node_id = node.get("id") or _stable_id("node", f"generated-{index}")
        graph_node_id = node.get("graphNodeId") or node.get("kg_key")
        skeleton_node = None
        if graph_node_id and graph_node_id in graph_to_node:
            skeleton_node = graph_to_node[graph_node_id]
        elif node_id in skeleton_nodes:
            skeleton_node = skeleton_nodes[node_id]

        node_type = node.get("type") or (skeleton_node.get("type") if skeleton_node else "basic_event")
        gate = node.get("gate") if node.get("gate") is not None else (skeleton_node.get("gate") if skeleton_node else None)
        name = node.get("name") or (skeleton_node.get("name") if skeleton_node else "")
        if node_type == "top_event":
            event = None
        else:
            graph_props = dict((skeleton_node or {}).get("graph_props") or {})
            event = dict(node.get("event") or {})
            merged = {}
            merged["id"] = str(event.get("id") or f"E{index:03d}")
            merged["name"] = name
            merged["description"] = str(event.get("description") or graph_props.get("description") or f"{name}相关异常")
            merged["errorLevel"] = str(event.get("errorLevel") or graph_props.get("errorLevel") or "中")
            merged["priority"] = event.get("priority", graph_props.get("priority", 0))
            merged["probability"] = event.get("probability", graph_props.get("probability", 1e-8))
            merged["showProbability"] = event.get("showProbability", graph_props.get("showProbability", merged["probability"]))
            merged["rule"] = str(event.get("rule") or graph_props.get("rule") or "")
            merged["rules"] = _coerce_rules(event.get("rules") or merged["rule"])
            merged["investigateMethod"] = str(
                event.get("investigateMethod") or graph_props.get("investigateMethod") or f"检查{name}相关状态与报警记录"
            )
            chunk_ids = []
            for doc in (skeleton_node or {}).get("documents_seed") or []:
                if doc.get("chunk_id") not in (None, ""):
                    chunk_ids.append(doc.get("chunk_id"))
            for doc in event.get("documents") or []:
                if isinstance(doc, dict) and doc.get("chunk_id") not in (None, ""):
                    chunk_ids.append(doc.get("chunk_id"))
            documents = []
            for chunk_id in dict.fromkeys(chunk_ids):
                if str(chunk_id) in evidence_doc_map:
                    documents.append(evidence_doc_map[str(chunk_id)])
            merged["documents"] = documents
            event = merged

        normalized_nodes.append(
            {
                "type": node_type,
                "gate": gate,
                "name": name,
                "id": node_id,
                "transfer": node.get("transfer", ""),
                "event": event,
                "graphNodeId": graph_node_id or (skeleton_node.get("graphNodeId") if skeleton_node else None),
                "kg_key": graph_node_id or (skeleton_node.get("kg_key") if skeleton_node else None),
            }
        )

    node_ids = {node["id"] for node in normalized_nodes}
    link_list = []
    seen = set()
    for link in tree.get("linkList") or skeleton["links"]:
        source_id = link.get("sourceId")
        target_id = link.get("targetId")
        if source_id not in node_ids or target_id not in node_ids:
            continue
        key = (source_id, target_id)
        if key in seen:
            continue
        seen.add(key)
        link_list.append(
            {
                "type": "link",
                "sourceId": source_id,
                "targetId": target_id,
                "isCondition": False,
            }
        )

    return {"nodeList": normalized_nodes, "linkList": link_list}


def repair_fault_tree(draft_tree: dict, corrections_hint: str, chunks: list) -> dict:
    prompt = f"""
你会收到一棵草稿故障树和一组历史修正建议。
请只做最小必要修改，尽量保持节点 id 和主体结构不变。

草稿树：
{json.dumps(draft_tree, ensure_ascii=False, indent=2)}

可引用的 chunks：
{json.dumps(_format_evidence_chunks(chunks), ensure_ascii=False, indent=2)}

历史修正建议：
{corrections_hint}

只输出修复后的完整 JSON：
{{
  "nodeList": [...],
  "linkList": [...]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=8192,
    )
    return _parse_json(response.choices[0].message.content)


def generate_fault_tree(
    top_event: str,
    requirements: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    def emit(message: str):
        try:
            print(message)
        finally:
            if log_callback:
                try:
                    log_callback(message)
                except Exception:
                    pass

    normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
    emit(f"[graph-match] matching top event '{top_event}'")
    matched = match_top_event_from_graph(top_event, normalized_candidates=normalized_candidates)
    matched_node = matched["matched_node"]
    emit(f"[graph-match] matched '{top_event}' -> '{matched['matched_name']}'")

    subgraph_bundle = expand_local_fault_subgraph(
        matched["matched_node_id"],
        max_depth=GRAPH_TREE_MAX_DEPTH,
        max_nodes=GRAPH_TREE_MAX_NODES,
    )
    emit(
        f"[graph-subgraph] root={matched['matched_name']} nodes={len(subgraph_bundle.get('nodes') or [])} "
        f"edges={len(subgraph_bundle.get('edges') or [])}"
    )

    chunk_ids = collect_subgraph_chunks(subgraph_bundle, chunk_limit=MAX_CHUNKS_FOR_PROMPT)
    evidence_chunks = hydrate_documents_by_chunk_ids(chunk_ids)
    raw_chunk_docs = []
    chunk_doc_map = {str(item["chunk_id"]): item for item in evidence_chunks}
    for chunk_id in chunk_ids:
        doc = chunk_doc_map.get(str(chunk_id))
        if doc:
            raw_chunk_docs.append(doc)
    from database import get_chunks_by_ids  # local import to keep module surface small

    raw_chunks = get_chunks_by_ids(chunk_ids, limit=max(len(chunk_ids), 1))
    emit(f"[graph-chunks] collected {len(raw_chunks)} evidence chunks")

    draft_tree = None
    previous_issues = None
    for attempt in range(1, MAX_RETRY + 2):
        emit(f"[graph-llm] generating draft tree attempt={attempt}")
        try:
            draft_tree = build_fault_tree_from_subgraph_and_chunks(
                top_event=matched["matched_name"],
                subgraph_bundle=subgraph_bundle,
                evidence_chunks=raw_chunks,
                requirements=requirements,
            )
        except ValueError as exc:
            if attempt > MAX_RETRY:
                raise
            previous_issues = [{"level": "ERROR", "message": str(exc)}]
            continue

        validation = validate_full(draft_tree, skip_semantic=True)
        draft_tree["validation"] = validation
        if validation["passed"]:
            break
        previous_issues = validation["issues"]
        emit(
            f"[graph-llm] draft validation failed errors={validation['error_count']} warnings={validation['warning_count']}"
        )
        if attempt > MAX_RETRY:
            break

    if draft_tree is None:
        raise ValueError(f"Failed to build tree for '{top_event}'")

    final_tree = draft_tree
    try:
        from diff_analyzer import format_corrections_for_repair, get_relevant_corrections

        corrections = get_relevant_corrections(draft_tree)
        if corrections:
            emit(f"[repair] applying {len(corrections)} relevant corrections")
            repaired = repair_fault_tree(draft_tree, format_corrections_for_repair(corrections), raw_chunks)
            repair_validation = validate_full(repaired, skip_semantic=True)
            if repair_validation["passed"]:
                repaired["validation"] = repair_validation
                final_tree = repaired
                emit("[repair] repaired draft accepted")
            else:
                emit("[repair] repaired draft rejected, keeping original draft")
    except Exception as exc:
        emit(f"[repair] skipped due to error: {exc}")

    final_validation = validate_full(final_tree, skip_semantic=False)
    final_tree["validation"] = final_validation
    final_tree["retrieval"] = {
        "source": "graph_local_subgraph",
        "matched_top_event": matched["matched_name"],
        "matched_node_id": matched["matched_node_id"],
        "alternatives": matched.get("alternatives") or [],
        "subgraph_node_count": len(subgraph_bundle.get("nodes") or []),
        "subgraph_edge_count": len(subgraph_bundle.get("edges") or []),
        "chunk_ids": chunk_ids,
    }
    return final_tree


def generate_fault_tree_with_progress(
    top_event: str,
    requirements: str = "",
    progress_callback: Optional[Callable[[int, str, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    if progress_callback:
        progress_callback(10, "prepare", "Preparing generation request")
        progress_callback(25, "graph_match", "Matching top event from graph")
        progress_callback(40, "graph_subgraph", "Expanding local graph subgraph")
        progress_callback(55, "graph_chunks", "Collecting subgraph evidence chunks")
        progress_callback(70, "graph_llm", "Building fault tree from subgraph and chunks")

    tree_data = generate_fault_tree(top_event, requirements, log_callback=log_callback)

    if progress_callback:
        progress_callback(90, "persistence", "Generation completed, persisting result")
    return tree_data


def discover_top_events_from_entity_index(entries: List[dict]) -> List[dict]:
    # Kept for backward compatibility; the new batch flow no longer depends on Mongo entity index.
    results = []
    for entry in entries or []:
        name = normalize_top_event_name(entry.get("entity_name"))
        if not name:
            continue
        if not any(word in name for word in ("故障", "异常", "报警", "停机", "失败", "触发")):
            continue
        results.append(
            {
                "name": name,
                "aliases": [entry.get("entity_name")] if entry.get("entity_name") and entry.get("entity_name") != name else [],
                "source_chunk_ids": list(dict.fromkeys(entry.get("chunk_ids") or [])),
            }
        )
    deduped = {}
    for item in results:
        deduped.setdefault(item["name"], item)
    return sorted(deduped.values(), key=lambda item: item["name"])


def discover_top_events(chunks: List[dict]) -> List[dict]:
    # Backward-compatible wrapper. New default source is the graph itself.
    graph_candidates = list_graph_top_event_candidates()
    return [
        {
            "name": item["name"],
            "aliases": [],
            "source_chunk_ids": item.get("source_chunk_ids") or [],
        }
        for item in graph_candidates
    ]


def _extract_json_text(raw: str) -> str:
    clean = re.sub(r"```json|```", "", raw or "").strip()
    if not clean:
        return ""

    start_obj = clean.find("{")
    start_arr = clean.find("[")
    starts = [index for index in (start_obj, start_arr) if index != -1]
    if not starts:
        return clean
    start = min(starts)
    candidate = clean[start:]

    open_char = candidate[0]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(candidate):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return candidate[: index + 1]
    return candidate


def _try_repair_json_text(candidate: str):
    text = (candidate or "").strip()
    if not text:
        return None

    opens = {"{": "}", "[": "]"}
    stack = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in opens:
            stack.append(opens[char])
        elif char in ("]", "}") and stack and char == stack[-1]:
            stack.pop()

    repaired = text + "".join(reversed(stack))
    for _ in range(MAX_JSON_REPAIR_RETRY):
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            repaired = repaired.rstrip(",")
            continue
    return None


def _parse_json(raw: str) -> dict:
    candidate = _extract_json_text(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_error:
        repaired = _try_repair_json_text(candidate)
        if repaired is not None:
            return repaired
        raise ValueError(f"LLM JSON parse failed: {first_error}\nRaw output:\n{raw}")
