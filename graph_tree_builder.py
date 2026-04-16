from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from config import (
    GRAPH_TREE_MAX_DEPTH,
    GRAPH_TREE_MAX_NODES,
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)
from database import fetch_chunks_by_ids
from validator import validate_full

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None


CAUSAL_RELATIONS = ["故障触发"]
SUPPORT_RELATIONS = [
    "参数配置",
    "测试测量",
    "故障处理",
    "组成关系",
    "依赖关系",
    "通讯连接",
    "状态变化",
    "控制操作",
    "比较关系",
    "功能支持",
]


def is_graph_tree_available() -> bool:
    return bool(GraphDatabase and NEO4J_PASSWORD)


@lru_cache(maxsize=1)
def _get_driver():
    if not is_graph_tree_available():
        return None
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _dedupe_keep_order(values: Iterable[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values or []:
        if value in (None, ""):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _normalize_name(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _entity_family(entity_type: str) -> str:
    value = str(entity_type or "")
    if "故障现象" in value or "报警" in value:
        return "fault"
    if "硬件组件" in value or "元器件" in value:
        return "component"
    if "参数" in value or "数据" in value:
        return "parameter"
    if "系统" in value or "设备" in value:
        return "system"
    if "方法" in value or "概念" in value:
        return "method"
    if "工具" in value or "仪器" in value:
        return "tool"
    return "other"


def _event_name(entity_name: str, entity_type: str, *, top_event: str = "") -> str:
    name = _normalize_name(entity_name)
    if not name:
        return ""
    if top_event and name == top_event:
        return top_event

    # 图谱里的实体类型和树里的事件文案不是一一对应的，这里做稳定映射。
    family = _entity_family(entity_type)
    if family == "fault":
        return name
    if family == "component":
        if name.endswith(("故障", "异常", "损坏", "失效")):
            return name
        return f"{name}故障"
    if family == "parameter":
        if name.endswith(("异常", "错误", "不匹配", "丢失")):
            return name
        return f"{name}参数异常"
    if family == "system":
        if "通信" in name or "通讯" in name or "PROFI" in name.upper() or "DRIVE-CLIQ" in name.upper():
            return f"{name}通讯异常"
        return f"{name}系统异常"
    return f"{name}异常"


def _split_composite_cause(name: str) -> List[str]:
    text = _normalize_name(name)
    if not text:
        return []

    # 只有拆成少量短片段时，才把 “A&B&C” 这类文本视为 AND 组合原因。
    strong_separators = r"[&＆+＋]"
    weak_separators = r"(?:、|,|，|/|\s+和\s+|\s+与\s+)"
    if re.search(strong_separators, text):
        parts = re.split(strong_separators, text)
    elif re.search(weak_separators, text):
        parts = re.split(weak_separators, text)
    else:
        return []

    cleaned = [_normalize_name(part) for part in parts if _normalize_name(part)]
    if len(cleaned) < 2 or len(cleaned) > 6:
        return []
    if any(len(part) > 40 for part in cleaned):
        return []
    return _dedupe_keep_order(cleaned)


def _make_document_map(chunk_ids: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    ordered_ids = _dedupe_keep_order(chunk_ids)
    if not ordered_ids:
        return {}
    chunks = fetch_chunks_by_ids(ordered_ids, limit=max(len(ordered_ids), 1))
    docs = {}
    for chunk in chunks:
        chunk_id = chunk.get("id", chunk.get("chunk_id"))
        if chunk_id in (None, ""):
            continue
        docs[str(chunk_id)] = {
            "chunk_id": chunk_id,
            "chunk_name": chunk.get("chunk_name", ""),
            "section_path": chunk.get("section_path", ""),
            "source_page": chunk.get("source", ""),
            "file": chunk.get("file", ""),
        }
    return docs


def _documents_for(chunk_ids: Iterable[Any], document_map: Dict[str, Dict[str, Any]], limit: int = 2) -> List[Dict[str, Any]]:
    docs = []
    for chunk_id in _dedupe_keep_order(chunk_ids):
        doc = document_map.get(str(chunk_id))
        if doc and doc not in docs:
            docs.append(doc)
        if len(docs) >= limit:
            break
    return docs


def _event_payload(event_id: str, name: str, entity: Dict[str, Any], support_edges: List[Dict[str, Any]], documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    entity_name = entity.get("name") or name
    entity_type = entity.get("entity_type") or ""
    family = _entity_family(entity_type)

    # 支撑边不直接决定树主干，主要用于补充规则、排查方法和简短说明。
    methods = []
    parameters = []
    related = []
    for edge in support_edges:
        other_name = edge.get("other_name")
        relation_type = edge.get("relation_type")
        other_type = edge.get("other_type") or ""
        other_family = _entity_family(other_type)
        if not other_name:
            continue
        if relation_type == "故障处理" or other_family in {"method", "tool"}:
            methods.append(other_name)
        elif other_family == "parameter" or relation_type in {"参数配置", "测试测量", "比较关系"}:
            parameters.append(other_name)
        else:
            related.append(other_name)

    rules = []
    for parameter in _dedupe_keep_order(parameters)[:3]:
        rules.append(
            {
                "deviceTypeId": "",
                "measurePointName": parameter,
                "symbol": "异常",
                "thresholds": ["需结合设备配置或诊断值确认"],
                "duration": "",
            }
        )
    if not rules and family == "parameter":
        rules.append(
            {
                "deviceTypeId": "",
                "measurePointName": entity_name,
                "symbol": "异常",
                "thresholds": ["需检查参数配置或取值"],
                "duration": "",
            }
        )

    investigate = "；".join(_dedupe_keep_order(methods)[:2])
    if not investigate:
        investigate = f"检查{entity_name}相关状态、连接和诊断记录"

    relation_hint = "、".join(_dedupe_keep_order(related + parameters)[:4])
    if relation_hint:
        description = f"{entity_name}相关异常，关联{relation_hint}。"
    else:
        description = f"{entity_name}相关异常可能导致上级故障。"

    return {
        "id": event_id,
        "name": name,
        "description": description[:80],
        "errorLevel": "中",
        "priority": 0,
        "probability": 1e-8,
        "showProbability": 0.000001,
        "rules": rules,
        "investigateMethod": investigate[:80],
        "documents": documents,
    }


def resolve_graph_top_event(top_event: str, aliases: Optional[List[str]] = None) -> Dict[str, Any]:
    driver = _get_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and ensure the neo4j package is installed.")

    names = _dedupe_keep_order([top_event] + list(aliases or []))
    names = [_normalize_name(name) for name in names if _normalize_name(name)]
    if not names:
        raise ValueError("top_event is empty")
    # catalog 里可能残留历史的“无空格标准名”，这里同时保留压缩空格版本做兼容匹配。
    compact_names = _dedupe_keep_order([re.sub(r"\s+", "", name) for name in names])

    cypher = """
    UNWIND $names AS query
    MATCH (f:Entity:FaultPhenomenon)
    // 同时支持原始文本和去空格文本，避免 prompt/catalog 规范化差异导致漏匹配。
    WITH f, query, replace(f.name, ' ', '') AS compact_name, replace(query, ' ', '') AS compact_query
    WHERE
      f.name = query
      OR compact_name = compact_query
      OR f.name CONTAINS query
      OR query CONTAINS f.name
      OR compact_name CONTAINS compact_query
      OR compact_query CONTAINS compact_name
    OPTIONAL MATCH (cause:Entity)-[r:RELATION {relation_type:'故障触发'}]->(f)
    OPTIONAL MATCH (f)-[:MENTIONED_IN]->(c:Chunk)
    RETURN
      f.name AS name,
      f.entity_type AS entity_type,
      collect(DISTINCT query) AS matched_queries,
      count(DISTINCT cause) AS cause_count,
      collect(DISTINCT c.chunk_id) AS source_chunk_ids
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(cypher, names=names).data()

    if not rows:
        raise ValueError(f"Neo4j graph has no FaultPhenomenon matching '{top_event}'")

    def score(row: Dict[str, Any]) -> Tuple[int, int, int, int]:
        name = _normalize_name(row.get("name"))
        compact_name = re.sub(r"\s+", "", name)
        exact = 1 if name in names or compact_name in compact_names else 0
        contains = 1 if any(
            name and (name in query or query in name or compact_name in re.sub(r"\s+", "", query) or re.sub(r"\s+", "", query) in compact_name)
            for query in names
        ) else 0
        return (
            exact,
            contains,
            int(row.get("cause_count") or 0),
            len(row.get("source_chunk_ids") or []),
        )

    best = sorted(rows, key=score, reverse=True)[0]
    return {
        "name": _normalize_name(best.get("name")),
        "entity_type": best.get("entity_type") or "故障现象与报警",
        "source_chunk_ids": _dedupe_keep_order(best.get("source_chunk_ids") or []),
        "matched_queries": _dedupe_keep_order(best.get("matched_queries") or []),
    }


def _fetch_causal_edges(top_name: str, depth: int) -> List[Dict[str, Any]]:
    driver = _get_driver()
    if driver is None:
        return []
    safe_depth = max(1, min(int(depth or 1), 5))
    cypher = f"""
    MATCH path = (cause:Entity)-[:RELATION*1..{safe_depth}]->(top:Entity:FaultPhenomenon {{name:$top_name}})
    WHERE all(r IN relationships(path) WHERE r.relation_type IN $causal_relations)
    // 多跳 path 先展平成逐层 child -> parent 边，后面统一按这一层结果组树。
    WITH path, relationships(path) AS rels, nodes(path) AS path_nodes
    UNWIND range(0, size(rels) - 1) AS i
    WITH path_nodes[i] AS child, rels[i] AS rel, path_nodes[i + 1] AS parent
    RETURN DISTINCT
      child.name AS child_name,
      child.entity_type AS child_type,
      parent.name AS parent_name,
      parent.entity_type AS parent_type,
      rel.relation_type AS relation_type,
      rel.chunk_id AS chunk_id
    LIMIT $limit
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        return session.run(
            cypher,
            top_name=top_name,
            causal_relations=CAUSAL_RELATIONS,
            limit=max(GRAPH_TREE_MAX_NODES * 12, 120),
        ).data()


def _fetch_support_edges(entity_names: List[str]) -> List[Dict[str, Any]]:
    driver = _get_driver()
    if driver is None or not entity_names:
        return []
    cypher = """
    UNWIND $names AS name
    MATCH (n:Entity {name:name})-[r:RELATION]-(other:Entity)
    WHERE r.relation_type IN $support_relations
    RETURN DISTINCT
      n.name AS name,
      n.entity_type AS entity_type,
      other.name AS other_name,
      other.entity_type AS other_type,
      r.relation_type AS relation_type,
      r.chunk_id AS chunk_id
    LIMIT $limit
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        return session.run(
            cypher,
            names=entity_names,
            support_relations=SUPPORT_RELATIONS,
            limit=max(len(entity_names) * 12, 80),
        ).data()


def _add_link(links: List[Dict[str, Any]], source_id: str, target_id: str) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    item = {"type": "link", "sourceId": source_id, "targetId": target_id, "isCondition": False}
    if item not in links:
        links.append(item)


def generate_fault_tree_from_graph(
    top_event: str,
    aliases: Optional[List[str]] = None,
    requirements: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    def emit(line: str) -> None:
        try:
            print(line)
        finally:
            if log_callback:
                try:
                    log_callback(line)
                except Exception:
                    pass

    top = resolve_graph_top_event(top_event, aliases=aliases)
    top_name = top["name"]
    emit(f"[graph-tree] resolved top_event='{top_event}' as '{top_name}'")

    # 树主干只来自 “故障触发” 因果边；chunks 仅用于 documents 溯源。
    causal_edges = _fetch_causal_edges(top_name, GRAPH_TREE_MAX_DEPTH)
    if not causal_edges:
        raise ValueError(f"Neo4j graph has no causal '故障触发' edges for top event '{top_name}'")

    entities: Dict[str, Dict[str, Any]] = {
        top_name: {"name": top_name, "entity_type": top.get("entity_type") or "故障现象与报警", "chunk_ids": top.get("source_chunk_ids") or []}
    }
    normal_children: Dict[str, List[str]] = {}
    and_groups: Dict[str, List[Dict[str, Any]]] = {}
    adjacency_chunk_ids: Dict[str, List[Any]] = {top_name: list(top.get("source_chunk_ids") or [])}

    # 先整理出 parent -> children 结构，再决定哪些子节点需要包进 AND gate。
    for edge in causal_edges:
        child_name = _normalize_name(edge.get("child_name"))
        parent_name = _normalize_name(edge.get("parent_name"))
        if not child_name or not parent_name or child_name == parent_name:
            continue
        child_type = edge.get("child_type") or ""
        parent_type = edge.get("parent_type") or ""
        chunk_id = edge.get("chunk_id")

        entities.setdefault(child_name, {"name": child_name, "entity_type": child_type, "chunk_ids": []})
        entities.setdefault(parent_name, {"name": parent_name, "entity_type": parent_type, "chunk_ids": []})
        for name in (child_name, parent_name):
            if chunk_id not in (None, ""):
                adjacency_chunk_ids.setdefault(name, []).append(chunk_id)
                entities[name].setdefault("chunk_ids", []).append(chunk_id)

        parts = _split_composite_cause(child_name)
        if parts:
            group = {
                "composite_name": child_name,
                "parts": parts,
                "entity_type": child_type,
                "chunk_ids": [chunk_id] if chunk_id not in (None, "") else [],
            }
            if group not in and_groups.setdefault(parent_name, []):
                and_groups[parent_name].append(group)
            for part in parts:
                entities.setdefault(part, {"name": part, "entity_type": child_type, "chunk_ids": []})
                if chunk_id not in (None, ""):
                    adjacency_chunk_ids.setdefault(part, []).append(chunk_id)
                    entities[part].setdefault("chunk_ids", []).append(chunk_id)
            continue

        children = normal_children.setdefault(parent_name, [])
        if child_name not in children:
            children.append(child_name)

    all_graph_names = _dedupe_keep_order(entities.keys())
    # 再补参数、方法、依赖等支撑边，它们只参与节点说明，不直接决定树结构。
    support_edges = _fetch_support_edges(all_graph_names)
    support_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for edge in support_edges:
        name = _normalize_name(edge.get("name"))
        if not name:
            continue
        support_by_name.setdefault(name, []).append(edge)
        chunk_id = edge.get("chunk_id")
        if chunk_id not in (None, ""):
            adjacency_chunk_ids.setdefault(name, []).append(chunk_id)
            entities.setdefault(name, {"name": name, "entity_type": edge.get("entity_type") or "", "chunk_ids": []})
            entities[name].setdefault("chunk_ids", []).append(chunk_id)

    parent_names = set(normal_children.keys()) | set(and_groups.keys())
    child_names = set()
    for children in normal_children.values():
        child_names.update(children)
    for groups in and_groups.values():
        for group in groups:
            child_names.update(group["parts"])

    relevant_names = _dedupe_keep_order([top_name] + list(parent_names) + list(child_names))
    if len(relevant_names) > GRAPH_TREE_MAX_NODES:
        relevant_names = relevant_names[:GRAPH_TREE_MAX_NODES]
    relevant_set = set(relevant_names)

    all_chunk_ids = []
    for name in relevant_names:
        all_chunk_ids.extend(adjacency_chunk_ids.get(name) or [])
        all_chunk_ids.extend(entities.get(name, {}).get("chunk_ids") or [])
    document_map = _make_document_map(all_chunk_ids)

    node_by_entity: Dict[str, str] = {}
    node_list: List[Dict[str, Any]] = []
    event_counter = 1

    for name in relevant_names:
        entity = entities.get(name, {"name": name, "entity_type": ""})
        mapped_name = top_name if name == top_name else _event_name(name, entity.get("entity_type") or "", top_event=top_name)
        node_id = _stable_id("node", f"{name}|{entity.get('entity_type') or ''}|{mapped_name}")
        node_by_entity[name] = node_id
        has_children = name in parent_names
        if name == top_name:
            node_type = "top_event"
            event = None
        else:
            node_type = "intermediate_event" if has_children else "basic_event"
            event_id = f"E{event_counter:03d}"
            event_counter += 1
            event = _event_payload(
                event_id,
                mapped_name,
                entity,
                support_by_name.get(name) or [],
                _documents_for(adjacency_chunk_ids.get(name) or entity.get("chunk_ids") or [], document_map),
            )
        node_list.append(
            {
                "type": node_type,
                "gate": None,
                "name": mapped_name,
                "id": node_id,
                "transfer": "",
                "event": event,
                "source_entity": name,
                "source_entity_type": entity.get("entity_type") or "",
            }
        )

    link_list: List[Dict[str, Any]] = []

    def add_gate(parent_name: str, gate_type: str, suffix: str) -> str:
        gate_type = gate_type.upper()
        gate_id = _stable_id("gate", f"{parent_name}|{gate_type}|{suffix}")
        if not any(node.get("id") == gate_id for node in node_list):
            node_list.append(
                {
                    "type": "gate",
                    "gate": gate_type,
                    "name": gate_type,
                    "id": gate_id,
                    "transfer": "",
                    "event": None,
                    "source_entity": parent_name,
                    "source_entity_type": "logic_gate",
                }
            )
        return gate_id

    # 输出层面统一显式生成 gate 节点，避免前后端对 event.gate 的隐式约定不一致。
    for parent_name in relevant_names:
        if parent_name not in relevant_set:
            continue
        parent_id = node_by_entity.get(parent_name)
        if not parent_id:
            continue

        children = [child for child in normal_children.get(parent_name, []) if child in relevant_set and node_by_entity.get(child)]
        groups = and_groups.get(parent_name, []) or []
        valid_groups = []
        for group_index, group in enumerate(groups):
            parts = [part for part in group["parts"] if part in relevant_set and node_by_entity.get(part)]
            if len(parts) >= 2:
                valid_groups.append((group_index, parts))

        if not children and not valid_groups:
            continue

        if not children and len(valid_groups) == 1:
            group_index, parts = valid_groups[0]
            and_gate_id = add_gate(parent_name, "AND", f"and-{group_index}")
            for part in parts:
                _add_link(link_list, node_by_entity[part], and_gate_id)
            _add_link(link_list, and_gate_id, parent_id)
            continue

        or_gate_id = add_gate(parent_name, "OR", "default")
        for child in children:
            _add_link(link_list, node_by_entity[child], or_gate_id)
        for group_index, parts in valid_groups:
            and_gate_id = add_gate(parent_name, "AND", f"and-{group_index}")
            for part in parts:
                _add_link(link_list, node_by_entity[part], and_gate_id)
            _add_link(link_list, and_gate_id, or_gate_id)
        _add_link(link_list, or_gate_id, parent_id)

    tree_data = {
        "nodeList": node_list,
        "linkList": link_list,
        "retrieval": {
            "source": "neo4j_relations_direct",
            "top_event": top_name,
            "matched_queries": top.get("matched_queries") or [],
            "causal_relation_types": CAUSAL_RELATIONS,
            "support_relation_types": SUPPORT_RELATIONS,
            "causal_edges": len(causal_edges),
            "requirements": requirements or "",
        },
    }
    validation = validate_full(tree_data, skip_semantic=True)
    tree_data["validation"] = validation
    emit(
        f"[graph-tree] built nodes={len(node_list)} links={len(link_list)} "
        f"validation_passed={validation.get('passed')}"
    )
    return tree_data
