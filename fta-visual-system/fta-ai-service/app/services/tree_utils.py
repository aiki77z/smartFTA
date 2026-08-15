from typing import Any, Dict, List, Tuple

from app.schemas import GraphDiff


def node_display_label(node: Dict[str, Any]) -> str:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
    event = meta.get("event") if isinstance(meta.get("event"), dict) else {}
    return str(
        node.get("name")
        or node.get("label")
        or node.get("title")
        or data.get("label")
        or data.get("name")
        or event.get("name")
        or event.get("label")
        or node.get("id")
        or ""
    )


def node_compare_signature(node: Dict[str, Any]) -> Dict[str, Any]:
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
    event = meta.get("event") if isinstance(meta.get("event"), dict) else {}
    own_event = node.get("event") if isinstance(node.get("event"), dict) else {}
    raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    event_like = {**raw_event, **own_event, **event}
    keys = [
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
    ]
    return {
        "gate": node.get("gate") or node.get("gateLabel") or meta.get("gateLabel") or raw.get("gate"),
        "props": {key: event_like.get(key, node.get(key)) for key in keys if event_like.get(key, node.get(key)) is not None},
    }


def edge_compare_signature(edge: Dict[str, Any]) -> Dict[str, Any]:
    meta = edge.get("meta") if isinstance(edge.get("meta"), dict) else {}
    raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
    relation = edge.get("relation") if isinstance(edge.get("relation"), dict) else {}
    meta_relation = meta.get("relation") if isinstance(meta.get("relation"), dict) else {}
    raw_relation = raw.get("relation") if isinstance(raw.get("relation"), dict) else {}
    keys = [
        "relation_type",
        "polarity",
        "certainty",
        "evidence",
        "evidence_texts",
        "documents",
        "gate_type",
        "member_relation",
        "gate_relation",
    ]
    merged = {**raw_relation, **meta_relation, **relation}
    props = {key: merged.get(key, edge.get(key)) for key in keys if merged.get(key, edge.get(key)) is not None}
    return {"props": props}


def normalize_to_graph(obj: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not obj:
        return [], []

    if isinstance(obj, dict) and isinstance(obj.get("tree_data"), dict):
        td = obj["tree_data"]
        if isinstance(td.get("nodeList"), list) and isinstance(td.get("linkList"), list):
            obj = td

    if isinstance(obj, dict) and isinstance(obj.get("nodeList"), list) and isinstance(obj.get("linkList"), list):
        nodes = []
        for n in obj["nodeList"]:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("id") or n.get("event_id") or n.get("node_id") or "")
            if not nid:
                continue
            label = node_display_label(n) or nid
            raw_type = str(n.get("type") or n.get("rawType") or n.get("event_type") or "")
            node_type = "event"
            if raw_type in ("1", "top", "top_event"):
                node_type = "top"
            elif raw_type in ("2", "intermediate", "intermediate_event"):
                node_type = "intermediate"
            elif raw_type in ("3", "basic", "basic_event"):
                node_type = "basic"
            elif raw_type.upper() in ("AND", "OR") or n.get("gateLabel"):
                node_type = "gate"
            nodes.append({"id": nid, "label": str(label), "type": node_type, **node_compare_signature(n)})

        edges = []
        for idx, e in enumerate(obj["linkList"]):
            if not isinstance(e, dict):
                continue
            src = e.get("source") or e.get("sourceId") or e.get("from") or e.get("child") or e.get("source_id")
            tgt = e.get("target") or e.get("targetId") or e.get("to") or e.get("parent") or e.get("target_id")
            if src is None or tgt is None:
                continue
            edges.append({"id": str(e.get("id") or f"e-{idx}"), "source": str(src), "target": str(tgt), **edge_compare_signature(e)})
        return nodes, edges

    if isinstance(obj, dict) and isinstance(obj.get("nodes"), list) and isinstance(obj.get("edges"), list):
        nodes = []
        for n in obj["nodes"]:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("id") or "")
            if not nid:
                continue
            label = node_display_label(n) or nid
            nodes.append({"id": nid, "label": str(label), "type": str(n.get("type") or "event"), **node_compare_signature(n)})
        edges = []
        for idx, e in enumerate(obj["edges"]):
            if not isinstance(e, dict):
                continue
            src = e.get("source")
            tgt = e.get("target")
            if src is None or tgt is None:
                continue
            edges.append({"id": str(e.get("id") or f"e-{idx}"), "source": str(src), "target": str(tgt), **edge_compare_signature(e)})
        return nodes, edges

    return [], []


def diff_graph(prev_json: Any, next_json: Any) -> GraphDiff:
    prev_nodes, prev_edges = normalize_to_graph(prev_json)
    next_nodes, next_edges = normalize_to_graph(next_json)
    prev_map = {n["id"]: n for n in prev_nodes}
    next_map = {n["id"]: n for n in next_nodes}

    def edge_key(e: Dict[str, Any]) -> str:
        return f'{e.get("source","")}->{e.get("target","")}'

    prev_edge_set = {edge_key(e) for e in prev_edges}
    next_edge_set = {edge_key(e) for e in next_edges}
    prev_edge_map = {edge_key(e): e for e in prev_edges}
    next_edge_map = {edge_key(e): e for e in next_edges}

    added = []
    modified = []
    removed = []
    for nid, n in next_map.items():
        if nid not in prev_map:
            added.append(nid)
        else:
            p = prev_map[nid]
            if (
                p.get("label") != n.get("label")
                or p.get("type") != n.get("type")
                or p.get("gate") != n.get("gate")
                or p.get("props") != n.get("props")
            ):
                modified.append(nid)
    for nid in prev_map:
        if nid not in next_map:
            removed.append(nid)

    for key in sorted(prev_edge_set & next_edge_set):
        prev_edge = prev_edge_map.get(key) or {}
        next_edge = next_edge_map.get(key) or {}
        if prev_edge.get("props") != next_edge.get("props"):
            target = str(next_edge.get("target") or "")
            if target and target in next_map and target not in modified:
                modified.append(target)

    return GraphDiff(
        added=added,
        modified=modified,
        removed=removed,
        added_edges=sorted(list(next_edge_set - prev_edge_set)),
        removed_edges=sorted(list(prev_edge_set - next_edge_set)),
    )


def summarize_tree(tree_json: Any) -> Dict[str, Any]:
    nodes, edges = normalize_to_graph(tree_json)
    top_nodes = [n for n in nodes if n.get("type") == "top"]
    top_event = top_nodes[0].get("label") if top_nodes else (nodes[0].get("label") if nodes else "")
    by_type: Dict[str, int] = {}
    for node in nodes:
        t = str(node.get("type") or "event")
        by_type[t] = by_type.get(t, 0) + 1
    main_branches = []
    if top_nodes:
        top_id = top_nodes[0].get("id")
        child_ids = {e.get("source") for e in edges if e.get("target") == top_id}
        if not child_ids:
            child_ids = {e.get("target") for e in edges if e.get("source") == top_id}
        label_by_id = {n.get("id"): n.get("label") for n in nodes}
        main_branches = [label_by_id.get(cid) for cid in child_ids if label_by_id.get(cid)]
    return {
        "top_event": top_event,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_type_counts": by_type,
        "main_branches": main_branches[:12],
    }
