from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient

from config import MONGO_DB_NAME, MONGO_URI, NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None

client = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

trees_col = db["fault_trees"]
versions_col = db["fault_tree_versions"]
chunks_col = db["chunks"]
entity_reverse_index_col = db["entity_reverse_index"]
top_event_catalog_col = db["top_event_catalog"]
generation_jobs_col = db["generation_jobs"]
generation_job_items_col = db["generation_job_items"]

TOP_EVENT_PRIORITY_HINTS = ("故障", "异常", "报警", "停机", "失败", "超时", "触发", "中断")
TOP_EVENT_NEGATIVE_HINTS = ("接线错误", "接口松动", "参数错误", "过流", "过热", "损坏", "松动")
GRAPH_PROPERTY_UPDATE_FIELDS = {
    "description",
    "errorLevel",
    "priority",
    "probability",
    "showProbability",
    "rule",
    "investigateMethod",
}


def _now() -> datetime:
    return datetime.utcnow()


def _dedupe_keep_order(values: Optional[List[Any]]) -> List[Any]:
    seen = set()
    result = []
    for value in values or []:
        if value in (None, ""):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    copied = dict(doc)
    copied.pop("_id", None)
    return copied


def _seconds_between(started_at: Optional[datetime], finished_at: Optional[datetime] = None) -> Optional[float]:
    if not started_at:
        return None
    end_time = finished_at or _now()
    return round(max(0.0, (end_time - started_at).total_seconds()), 3)


def _decorate_runtime_fields(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    copied = _strip_mongo_id(doc)
    if not copied:
        return None

    started_at = copied.get("started_at")
    finished_at = copied.get("finished_at")
    copied["elapsed_seconds"] = _seconds_between(started_at, finished_at)
    copied["duration_seconds"] = _seconds_between(started_at, finished_at) if finished_at else None
    return copied


def _get_chunk_identifier(doc: Optional[Dict[str, Any]]) -> Any:
    if not doc:
        return None
    return doc.get("id", doc.get("chunk_id"))


def _neo4j_available() -> bool:
    return bool(GraphDatabase and NEO4J_PASSWORD)


_neo4j_driver = None


def _get_neo4j_driver():
    global _neo4j_driver
    if not _neo4j_available():
        return None
    if _neo4j_driver is None:
        _neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _neo4j_driver


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", _normalize_text(value))


def _looks_like_fault_code(name: str) -> bool:
    text = _normalize_text(name)
    if re.fullmatch(r"[FA]\d{5}(?:\([A-Z]\))?", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z]{1,6}=?[0-9A-F]{3,6}", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[0-9A-F]{3,6}", text, flags=re.IGNORECASE):
        return True
    return False


def _parse_maybe_json(value: Any, default):
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _coerce_chunk_ids(value: Any) -> List[Any]:
    parsed = _parse_maybe_json(value, value)
    if isinstance(parsed, list):
        return _dedupe_keep_order(parsed)
    if isinstance(parsed, str) and parsed.strip():
        if "," in parsed:
            return _dedupe_keep_order([part.strip() for part in parsed.split(",") if part.strip()])
        return [parsed.strip()]
    return []


def _coerce_documents(value: Any) -> List[Dict[str, Any]]:
    parsed = _parse_maybe_json(value, [])
    if not isinstance(parsed, list):
        return []
    docs = []
    for item in parsed:
        if isinstance(item, dict) and item.get("chunk_id") not in (None, ""):
            docs.append({"chunk_id": item.get("chunk_id")})
    return docs


def _decode_graph_node(raw: Dict[str, Any]) -> Dict[str, Any]:
    props = dict(raw.get("props") or {})
    documents = _coerce_documents(props.get("documents"))
    source_chunk_ids = _coerce_chunk_ids(props.get("source_chunk_ids"))
    if not source_chunk_ids and documents:
        source_chunk_ids = _dedupe_keep_order([doc.get("chunk_id") for doc in documents])
    return {
        "graph_node_id": raw.get("graph_node_id"),
        "name": props.get("name") or "",
        "normalized_name": props.get("normalized_name") or props.get("name") or "",
        "entity_type": props.get("entity_type") or "",
        "node_type": props.get("node_type") or ("AND" if "LogicGate" in (raw.get("labels") or []) else "FAULT"),
        "labels": raw.get("labels") or [],
        "description": props.get("description") or "",
        "errorLevel": props.get("errorLevel") or "",
        "priority": props.get("priority"),
        "probability": props.get("probability"),
        "showProbability": props.get("showProbability"),
        "rule": props.get("rule") or "",
        "investigateMethod": props.get("investigateMethod") or "",
        "documents": documents,
        "source_chunk_ids": source_chunk_ids,
        "support_count": props.get("support_count"),
        "raw_props": props,
    }


def _decode_graph_relation(raw: Dict[str, Any]) -> Dict[str, Any]:
    props = dict(raw.get("rel_props") or {})
    source_chunk_ids = _coerce_chunk_ids(props.get("source_chunk_ids"))
    chunk_id = props.get("chunk_id")
    if chunk_id not in (None, "") and chunk_id not in source_chunk_ids:
        source_chunk_ids.insert(0, chunk_id)
    return {
        "source_graph_node_id": raw.get("source_graph_node_id"),
        "target_graph_node_id": raw.get("target_graph_node_id"),
        "relation_type": props.get("relation_type") or "触发",
        "chunk_id": chunk_id,
        "source_chunk_ids": source_chunk_ids,
        "support_count": props.get("support_count"),
        "raw_props": props,
    }


def _score_top_event_candidate(node: Dict[str, Any]) -> tuple:
    name = _normalize_text(node.get("name"))
    support = int(node.get("support_count") or len(node.get("source_chunk_ids") or []))
    positive = sum(1 for hint in TOP_EVENT_PRIORITY_HINTS if hint in name)
    negative = sum(1 for hint in TOP_EVENT_NEGATIVE_HINTS if hint in name)
    length_ok = 1 if 2 <= len(name) <= 30 else 0
    return (positive, length_ok, support, -negative, len(name))


def _chunk_sort_key(doc: Dict[str, Any]):
    chunk_id = _get_chunk_identifier(doc)
    try:
        return (0, int(chunk_id))
    except (TypeError, ValueError):
        return (1, str(chunk_id or ""))


def _fetch_chunks_by_identifiers(chunk_ids: List[Any], limit: int) -> List[Dict[str, Any]]:
    normalized_ids = _dedupe_keep_order(chunk_ids)
    if not normalized_ids:
        return []

    numeric_ids = []
    for chunk_id in normalized_ids:
        try:
            numeric_ids.append(int(str(chunk_id).strip()))
        except (TypeError, ValueError):
            continue

    query = {
        "$or": [
            {"chunk_id": {"$in": normalized_ids}},
            {"id": {"$in": _dedupe_keep_order(normalized_ids + numeric_ids)}},
        ]
    }
    docs = list(chunks_col.find(query, {"_id": 0}))
    docs.sort(key=_chunk_sort_key)
    return docs[:limit]


def fetch_chunks_by_ids(chunk_ids: List[Any], limit: int = 8) -> List[Dict[str, Any]]:
    return _fetch_chunks_by_identifiers(chunk_ids, limit)


def get_chunks_by_ids(chunk_ids: List[Any], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    effective_limit = limit if limit is not None else max(len(_dedupe_keep_order(chunk_ids)), 1)
    return _fetch_chunks_by_identifiers(chunk_ids, effective_limit)


def hydrate_documents_by_chunk_ids(chunk_ids: List[Any]) -> List[Dict[str, Any]]:
    documents = []
    for chunk in get_chunks_by_ids(chunk_ids):
        chunk_id = _get_chunk_identifier(chunk)
        if chunk_id in (None, ""):
            continue
        documents.append(
            {
                "chunk_id": chunk_id,
                "chunk_name": chunk.get("chunk_name", ""),
                "section_path": chunk.get("section_path", ""),
                "source_page": chunk.get("source", ""),
            }
        )
    return documents


def match_top_event_from_graph(top_event_query: str, normalized_candidates: Optional[List[str]] = None) -> Dict[str, Any]:
    driver = _get_neo4j_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and install the neo4j package.")

    queries = _dedupe_keep_order([top_event_query] + list(normalized_candidates or []))
    queries = [_normalize_text(item) for item in queries if _normalize_text(item)]
    compact_queries = _dedupe_keep_order([_compact_text(item) for item in queries])
    if not queries:
        raise ValueError("top_event_query is empty")

    cypher = """
    MATCH (n:Entity)
    WHERE (n:FaultPhenomenon OR n.entity_type = '故障现象与报警')
    WITH n, elementId(n) AS graph_node_id, replace(coalesce(n.name, ''), ' ', '') AS compact_name
    WHERE
      n.name IN $queries
      OR coalesce(n.normalized_name, '') IN $queries
      OR compact_name IN $compact_queries
      OR any(query IN $queries WHERE n.name CONTAINS query OR query CONTAINS n.name)
      OR any(query IN $compact_queries WHERE compact_name CONTAINS query OR query CONTAINS compact_name)
    RETURN
      graph_node_id,
      labels(n) AS labels,
      properties(n) AS props
    LIMIT 30
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(cypher, queries=queries, compact_queries=compact_queries).data()

    candidates = [_decode_graph_node(row) for row in rows]
    if not candidates:
        raise ValueError(f"Neo4j graph has no matching top event for '{top_event_query}'")

    scored = []
    for node in candidates:
        name = _normalize_text(node.get("name"))
        normalized_name = _normalize_text(node.get("normalized_name"))
        compact_name = _compact_text(name)
        exact = 1 if name in queries else 0
        normalized_exact = 1 if normalized_name in queries else 0
        compact_exact = 1 if compact_name in compact_queries else 0
        contains = 1 if any(name and (name in q or q in name) for q in queries) else 0
        score = (
            exact,
            normalized_exact,
            compact_exact,
            contains,
            int(node.get("support_count") or len(node.get("source_chunk_ids") or [])),
            len(name),
        )
        scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][1]
    if _looks_like_fault_code(best.get("name", "")):
        with driver.session(database=NEO4J_DATABASE) as session:
            alias_row = session.run(
                """
                MATCH (code:Entity)-[:RELATION {relation_type:'触发'}]->(target:Entity)
                WHERE elementId(code) = $graph_node_id
                  AND (target:FaultPhenomenon OR target.entity_type = '故障现象与报警')
                RETURN elementId(target) AS graph_node_id, labels(target) AS labels, properties(target) AS props
                LIMIT 1
                """,
                graph_node_id=best.get("graph_node_id"),
            ).single()
        if alias_row:
            best = _decode_graph_node(dict(alias_row))

    alternatives = []
    for _, node in scored[1:6]:
        alternatives.append(
            {
                "graph_node_id": node.get("graph_node_id"),
                "name": node.get("name"),
                "normalized_name": node.get("normalized_name"),
            }
        )

    return {
        "matched_node_id": best.get("graph_node_id"),
        "matched_name": best.get("name"),
        "matched_node": best,
        "score": 1.0,
        "alternatives": alternatives,
    }


def expand_local_fault_subgraph(root_node_id: str, max_depth: int = 3, max_nodes: int = 20) -> Dict[str, Any]:
    driver = _get_neo4j_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and install the neo4j package.")
    if not root_node_id:
        raise ValueError("root_node_id is empty")

    safe_depth = max(1, min(int(max_depth or 3), 4))
    safe_max_nodes = max(1, min(int(max_nodes or 20), 50))

    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    edge_keys = set()
    gate_groups: List[Dict[str, Any]] = []
    support_chunk_ids: List[Any] = []
    frontier = [root_node_id]
    visited = {root_node_id}
    depths = {root_node_id: 0}

    node_query = """
    MATCH (n)
    WHERE elementId(n) = $node_id
    RETURN elementId(n) AS graph_node_id, labels(n) AS labels, properties(n) AS props
    """
    expand_query = """
    UNWIND $frontier AS parent_id
    MATCH (child)-[r:RELATION {relation_type:'触发'}]->(parent)
    WHERE elementId(parent) = parent_id
    RETURN
      elementId(child) AS source_graph_node_id,
      labels(child) AS source_labels,
      properties(child) AS source_props,
      elementId(parent) AS target_graph_node_id,
      labels(parent) AS target_labels,
      properties(parent) AS target_props,
      properties(r) AS rel_props
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        root_row = session.run(node_query, node_id=root_node_id).single()
        if not root_row:
            raise ValueError(f"Neo4j graph has no node with id '{root_node_id}'")
        root_node = _decode_graph_node(dict(root_row))
        nodes_by_id[root_node_id] = {**root_node, "depth": 0}

        for depth in range(1, safe_depth + 1):
            if not frontier or len(nodes_by_id) >= safe_max_nodes:
                break
            rows = session.run(expand_query, frontier=frontier).data()
            next_frontier = []
            for row in rows:
                source_node = _decode_graph_node(
                    {
                        "graph_node_id": row.get("source_graph_node_id"),
                        "labels": row.get("source_labels"),
                        "props": row.get("source_props"),
                    }
                )
                target_node = _decode_graph_node(
                    {
                        "graph_node_id": row.get("target_graph_node_id"),
                        "labels": row.get("target_labels"),
                        "props": row.get("target_props"),
                    }
                )
                source_id = source_node["graph_node_id"]
                target_id = target_node["graph_node_id"]
                nodes_by_id.setdefault(target_id, {**target_node, "depth": depths.get(target_id, depth - 1)})

                if source_id not in nodes_by_id and len(nodes_by_id) >= safe_max_nodes:
                    continue

                if source_id not in nodes_by_id:
                    depths[source_id] = depth
                    nodes_by_id[source_id] = {**source_node, "depth": depth}
                if source_id not in visited:
                    visited.add(source_id)
                    next_frontier.append(source_id)

                edge = _decode_graph_relation(
                    {
                        "source_graph_node_id": source_id,
                        "target_graph_node_id": target_id,
                        "rel_props": row.get("rel_props"),
                    }
                )
                edge_key = (source_id, target_id, edge.get("relation_type"), tuple(edge.get("source_chunk_ids") or []))
                if edge_key not in edge_keys:
                    edge_keys.add(edge_key)
                    edges.append(edge)
                    support_chunk_ids.extend(edge.get("source_chunk_ids") or [])
            frontier = next_frontier

    for node in nodes_by_id.values():
        support_chunk_ids.extend(node.get("source_chunk_ids") or [])

    child_targets = {}
    for edge in edges:
        child_targets.setdefault(edge["target_graph_node_id"], []).append(edge["source_graph_node_id"])
    for node in nodes_by_id.values():
        if str(node.get("node_type")).upper() == "AND":
            gate_groups.append(
                {
                    "gate_node_id": node["graph_node_id"],
                    "gate_type": "AND",
                    "depth": node.get("depth", 0),
                    "input_node_ids": child_targets.get(node["graph_node_id"], []),
                }
            )

    return {
        "root": root_node_id,
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
        "gate_groups": gate_groups,
        "support_chunk_ids": _dedupe_keep_order(support_chunk_ids),
    }


def collect_subgraph_chunks(subgraph_bundle: Dict[str, Any], chunk_limit: int = 12) -> List[Any]:
    scores: Dict[str, float] = {}
    bundle_nodes = subgraph_bundle.get("nodes") or []
    bundle_edges = subgraph_bundle.get("edges") or []
    root_id = subgraph_bundle.get("root")

    node_depth = {node.get("graph_node_id"): int(node.get("depth") or 0) for node in bundle_nodes}
    for node in bundle_nodes:
        weight = 10 if node.get("graph_node_id") == root_id else max(3, 8 - int(node.get("depth") or 0) * 2)
        for chunk_id in node.get("source_chunk_ids") or []:
            key = str(chunk_id)
            scores[key] = scores.get(key, 0.0) + weight
        for doc in node.get("documents") or []:
            chunk_id = doc.get("chunk_id")
            if chunk_id in (None, ""):
                continue
            key = str(chunk_id)
            scores[key] = scores.get(key, 0.0) + weight

    for edge in bundle_edges:
        source_depth = node_depth.get(edge.get("source_graph_node_id"), 3)
        weight = max(4, 9 - source_depth)
        for chunk_id in edge.get("source_chunk_ids") or []:
            key = str(chunk_id)
            scores[key] = scores.get(key, 0.0) + weight

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [chunk_id for chunk_id, _ in ranked[: max(1, int(chunk_limit or 12))]]


def list_graph_top_event_candidates(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    driver = _get_neo4j_driver()
    if driver is None:
        return []

    cypher = """
    MATCH (n:Entity)
    WHERE (n:FaultPhenomenon OR n.entity_type = '故障现象与报警')
    RETURN elementId(n) AS graph_node_id, labels(n) AS labels, properties(n) AS props
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(cypher).data()

    decoded = [_decode_graph_node(row) for row in rows]
    filtered = []
    for node in decoded:
        if str(node.get("node_type") or "").upper() == "AND":
            continue
        name = _normalize_text(node.get("name"))
        if not name or len(name) < 2 or len(name) > 40:
            continue
        if _looks_like_fault_code(name):
            continue
        filtered.append(node)

    filtered.sort(key=_score_top_event_candidate, reverse=True)
    if limit:
        filtered = filtered[:limit]

    return [
        {
            "graph_node_id": node.get("graph_node_id"),
            "name": node.get("name"),
            "normalized_name": node.get("normalized_name"),
            "support_count": int(node.get("support_count") or len(node.get("source_chunk_ids") or [])),
            "source_chunk_ids": node.get("source_chunk_ids") or [],
            "documents": node.get("documents") or [],
        }
        for node in filtered
    ]


def update_graph_node_properties(graph_node_id: str, properties: Dict[str, Any]) -> bool:
    driver = _get_neo4j_driver()
    if driver is None:
        raise ValueError("Neo4j is not configured. Set NEO4J_PASSWORD and install the neo4j package.")
    if not graph_node_id:
        return False

    update_props = {}
    for key, value in (properties or {}).items():
        if key not in GRAPH_PROPERTY_UPDATE_FIELDS:
            continue
        if value is None:
            continue
        update_props[key] = value

    if not update_props:
        return False

    with driver.session(database=NEO4J_DATABASE) as session:
        result = session.run(
            """
            MATCH (n)
            WHERE elementId(n) = $graph_node_id
            SET n += $props,
                n.updated_at = datetime()
            RETURN count(n) AS updated
            """,
            graph_node_id=graph_node_id,
            props=update_props,
        ).single()
    return bool(result and result.get("updated"))


def _ensure_indexes():
    index_specs = [
        (trees_col, [("catalog_name", ASCENDING), ("updated_at", DESCENDING)]),
        (trees_col, [("normalized_top_event", ASCENDING), ("updated_at", DESCENDING)]),
        (entity_reverse_index_col, [("entity_type", ASCENDING), ("count", DESCENDING)]),
        (entity_reverse_index_col, [("entity_name", ASCENDING)]),
        (top_event_catalog_col, [("normalized_name", ASCENDING)]),
        (top_event_catalog_col, [("normalized_aliases", ASCENDING)]),
        (generation_jobs_col, [("status", ASCENDING), ("updated_at", DESCENDING)]),
        (generation_job_items_col, [("job_id", ASCENDING), ("status", ASCENDING)]),
        (generation_job_items_col, [("normalized_top_event", ASCENDING), ("status", ASCENDING)]),
    ]

    for collection, keys in index_specs:
        try:
            collection.create_index(keys)
        except Exception:
            pass


_ensure_indexes()


def import_chunks(chunks: list):
    """Import the full chunk list into MongoDB and replace old data."""
    chunks_col.drop()
    if chunks:
        chunks_col.insert_many(chunks)
    print(f"Imported {len(chunks)} chunks")


def import_entity_reverse_index(entries: list):
    """Import aggregated entity reverse-index data into MongoDB and replace old data."""
    entity_reverse_index_col.drop()
    if entries:
        entity_reverse_index_col.insert_many(entries)
    print(f"Imported {len(entries)} reverse-index entities")


def list_entity_reverse_index() -> List[Dict[str, Any]]:
    cursor = entity_reverse_index_col.find({}, {"_id": 0}).sort([("count", DESCENDING), ("entity_name", ASCENDING)])
    return list(cursor)


def search_chunks_by_entity_names(entity_names: List[str], limit: int = 8) -> list:
    """
    Recall chunks directly from the entity reverse index using exact entity names.
    """
    cleaned_names = []
    for name in entity_names or []:
        text = str(name).strip()
        if text and text not in cleaned_names:
            cleaned_names.append(text)

    if not cleaned_names:
        return []

    reverse_index_hits = list(
        entity_reverse_index_col.find(
            {"entity_name": {"$in": cleaned_names}},
            {"_id": 0, "chunk_ids": 1},
        )
    )
    indexed_chunk_ids = []
    for hit in reverse_index_hits:
        indexed_chunk_ids.extend(hit.get("chunk_ids") or [])

    return _fetch_chunks_by_identifiers(indexed_chunk_ids, limit)


def search_chunks_by_keywords(keywords: list, limit: int = 8) -> list:
    """
    Search related chunks using exact keyword hit first, then fuzzy text match.
    """
    cleaned_keywords = []
    for kw in keywords or []:
        text = str(kw).strip()
        if text and text not in cleaned_keywords:
            cleaned_keywords.append(text)

    if not cleaned_keywords:
        return []

    results = []
    seen_ids = set()

    reverse_index_hits = list(
        entity_reverse_index_col.find(
            {"entity_name": {"$in": cleaned_keywords}},
            {"_id": 0, "chunk_ids": 1},
        )
    )
    indexed_chunk_ids = []
    for hit in reverse_index_hits:
        indexed_chunk_ids.extend(hit.get("chunk_ids") or [])

    for doc in _fetch_chunks_by_identifiers(indexed_chunk_ids, limit):
        doc_id = _get_chunk_identifier(doc)
        if doc_id not in seen_ids:
            results.append(doc)
            seen_ids.add(doc_id)

    if len(results) >= limit:
        return results[:limit]

    exact_hits = chunks_col.find({"key_word": {"$in": cleaned_keywords}}, limit=limit)
    for doc in exact_hits:
        doc_id = _get_chunk_identifier(doc)
        if doc_id not in seen_ids:
            results.append(doc)
            seen_ids.add(doc_id)

    if len(results) >= limit:
        return results[:limit]

    regex_clauses = []
    for kw in cleaned_keywords:
        regex = {"$regex": re.escape(kw), "$options": "i"}
        regex_clauses.extend(
            [
                {"chunk_name": regex},
                {"content": regex},
                {"chapter": regex},
                {"section": regex},
                {"subsection": regex},
            ]
        )

    if regex_clauses:
        fuzzy_hits = chunks_col.find({"$or": regex_clauses}, limit=limit * 3)
        for doc in fuzzy_hits:
            doc_id = _get_chunk_identifier(doc)
            if doc_id not in seen_ids:
                results.append(doc)
                seen_ids.add(doc_id)
            if len(results) >= limit:
                break

    return results[:limit]


def list_all_chunks() -> List[Dict[str, Any]]:
    chunks = list(chunks_col.find({}, {"_id": 0}))
    return sorted(chunks, key=_chunk_sort_key)


def get_chunk_by_id(chunk_id: Any) -> dict:
    candidates = _dedupe_keep_order([chunk_id, str(chunk_id).strip()])
    try:
        numeric_value = int(str(chunk_id).strip())
        candidates = _dedupe_keep_order(candidates + [numeric_value])
    except (TypeError, ValueError):
        pass

    return chunks_col.find_one(
        {
            "$or": [
                {"id": {"$in": candidates}},
                {"chunk_id": {"$in": candidates}},
            ]
        }
    )


def create_tree(
    tree_id: str,
    top_event: str,
    catalog_name: Optional[str] = None,
    normalized_top_event: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    job_id: Optional[str] = None,
    job_item_id: Optional[str] = None,
):
    trees_col.insert_one(
        {
            "_id": tree_id,
            "top_event": top_event,
            "catalog_name": catalog_name or top_event,
            "normalized_top_event": normalized_top_event or top_event,
            "query_aliases": _dedupe_keep_order(aliases),
            "source_chunk_ids": _dedupe_keep_order(source_chunk_ids),
            "job_id": job_id,
            "job_item_id": job_item_id,
            "created_at": _now(),
            "updated_at": _now(),
            "current_version": 0,
            "status": "generating",
        }
    )


def update_tree_status(tree_id: str, status: str, **extra_fields):
    payload = {"status": status, "updated_at": _now()}
    payload.update({k: v for k, v in extra_fields.items() if v is not None})
    trees_col.update_one({"_id": tree_id}, {"$set": payload})


def get_tree_meta(tree_id: str) -> dict:
    return trees_col.find_one({"_id": tree_id})


def find_tree_by_top_event(
    top_event: str,
    normalized_top_event: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    catalog_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conditions = []
    candidate_names = _dedupe_keep_order([catalog_name, top_event] + (aliases or []))

    if catalog_name:
        conditions.append({"catalog_name": catalog_name})
    if normalized_top_event:
        conditions.append({"normalized_top_event": normalized_top_event})
    if candidate_names:
        conditions.append({"top_event": {"$in": candidate_names}})
        conditions.append({"query_aliases": {"$in": candidate_names}})

    if not conditions:
        return None

    meta = trees_col.find_one(
        {
            "status": {"$in": ["ai_generated", "expert_modified", "rolled_back"]},
            "$or": conditions,
        },
        sort=[("updated_at", DESCENDING)],
    )
    if not meta or not meta.get("current_version"):
        return None

    version = get_version(meta["_id"], meta["current_version"])
    if not version:
        return None

    return {
        "tree_id": meta["_id"],
        "version": meta["current_version"],
        "top_event": meta.get("top_event"),
        "catalog_name": meta.get("catalog_name"),
        "status": meta.get("status"),
        "updated_at": meta.get("updated_at"),
        "tree_data": version.get("tree_data"),
        "version_data": version,
    }


def save_version(tree_id: str, tree_data: dict, editor: str, description: str, is_ai: bool) -> int:
    latest = versions_col.find_one({"tree_id": tree_id}, sort=[("version", -1)])
    new_version = (latest["version"] + 1) if latest else 1

    versions_col.insert_one(
        {
            "tree_id": tree_id,
            "version": new_version,
            "is_ai_generated": is_ai,
            "created_at": _now(),
            "editor": editor,
            "description": description,
            "tree_data": tree_data,
        }
    )

    trees_col.update_one(
        {"_id": tree_id},
        {
            "$set": {
                "current_version": new_version,
                "updated_at": _now(),
                "status": "ai_generated" if is_ai else "expert_modified",
            }
        },
    )
    return new_version


def get_version(tree_id: str, version: int = None) -> dict:
    if version is None:
        meta = get_tree_meta(tree_id)
        if not meta:
            return None
        version = meta["current_version"]

    return versions_col.find_one({"tree_id": tree_id, "version": version}, {"_id": 0})


def rollback_version(tree_id: str, target_version: int):
    target = versions_col.find_one({"tree_id": tree_id, "version": target_version})
    if not target:
        raise ValueError(f"Version {target_version} does not exist")

    trees_col.update_one(
        {"_id": tree_id},
        {"$set": {"current_version": target_version, "updated_at": _now(), "status": "rolled_back"}},
    )


def get_version_list(tree_id: str) -> list:
    versions = versions_col.find({"tree_id": tree_id}, {"tree_data": 0, "_id": 0}).sort("version", 1)
    return list(versions)


def upsert_top_event_catalog_entry(
    *,
    name: str,
    normalized_name: str,
    aliases: Optional[List[str]] = None,
    normalized_aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    aliases = _dedupe_keep_order([alias for alias in aliases or [] if alias != name])
    normalized_aliases = _dedupe_keep_order(
        [alias for alias in normalized_aliases or [] if alias and alias != normalized_name]
    )
    source_chunk_ids = _dedupe_keep_order(source_chunk_ids)

    existing = top_event_catalog_col.find_one({"_id": normalized_name})
    now = _now()

    if existing:
        merged_aliases = _dedupe_keep_order((existing.get("aliases") or []) + aliases)
        merged_normalized_aliases = _dedupe_keep_order(
            (existing.get("normalized_aliases") or []) + normalized_aliases
        )
        merged_source_chunk_ids = _dedupe_keep_order((existing.get("source_chunk_ids") or []) + source_chunk_ids)
        top_event_catalog_col.update_one(
            {"_id": normalized_name},
            {
                "$set": {
                    "updated_at": now,
                    "aliases": merged_aliases,
                    "normalized_aliases": merged_normalized_aliases,
                    "source_chunk_ids": merged_source_chunk_ids,
                }
            },
        )
    else:
        top_event_catalog_col.insert_one(
            {
                "_id": normalized_name,
                "name": name,
                "normalized_name": normalized_name,
                "aliases": aliases,
                "normalized_aliases": normalized_aliases,
                "source_chunk_ids": source_chunk_ids,
                "created_at": now,
                "updated_at": now,
            }
        )

    return get_top_event_catalog(normalized_name)


def get_top_event_catalog(normalized_name: str) -> Optional[Dict[str, Any]]:
    return _strip_mongo_id(top_event_catalog_col.find_one({"_id": normalized_name}))


def resolve_top_event_catalog(
    *,
    normalized_candidates: List[str],
) -> Optional[Dict[str, Any]]:
    normalized_candidates = _dedupe_keep_order(normalized_candidates)
    if not normalized_candidates:
        return None

    doc = top_event_catalog_col.find_one(
        {
            "$or": [
                {"normalized_name": {"$in": normalized_candidates}},
                {"normalized_aliases": {"$in": normalized_candidates}},
                {"_id": {"$in": normalized_candidates}},
            ]
        }
    )
    return _strip_mongo_id(doc)


def list_top_event_catalog(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    cursor = top_event_catalog_col.find({}, {"_id": 0}).sort("name", ASCENDING)
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)


def create_generation_job(
    *,
    job_type: str,
    total: int,
    top_event: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    job_id = f"{job_type}_{uuid4().hex[:10]}"
    now = _now()
    status = "completed" if total == 0 else "pending"
    doc = {
        "_id": job_id,
        "job_id": job_id,
        "job_type": job_type,
        "top_event": top_event,
        "status": status,
        "total": total,
        "success": 0,
        "failed": 0,
        "running": 0,
        "pending": total,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": now if total == 0 else None,
        "duration_seconds": 0.0 if total == 0 else None,
        "completion_logged_at": None,
    }
    generation_jobs_col.insert_one(doc)
    return _strip_mongo_id(doc)


def update_job_status(job_id: str, *, status: Optional[str] = None, **extra_fields) -> Optional[Dict[str, Any]]:
    current = generation_jobs_col.find_one({"_id": job_id})
    payload = {"updated_at": _now()}
    if status is not None:
        payload["status"] = status
        if status == "running":
            payload["started_at"] = payload["updated_at"]
        if status in {"completed", "partial_failed", "failed"}:
            payload["finished_at"] = payload["updated_at"]
    started_at = payload.get("started_at") or (current or {}).get("started_at")
    finished_at = payload.get("finished_at") or (current or {}).get("finished_at")
    if finished_at and started_at:
        payload["duration_seconds"] = _seconds_between(started_at, finished_at)
    payload.update({k: v for k, v in extra_fields.items() if v is not None})
    generation_jobs_col.update_one({"_id": job_id}, {"$set": payload})
    return get_generation_job(job_id)


def create_generation_job_item(
    *,
    job_id: str,
    top_event: str,
    normalized_top_event: str,
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    requirements: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item_id = f"item_{uuid4().hex[:12]}"
    now = _now()
    doc = {
        "_id": item_id,
        "item_id": item_id,
        "job_id": job_id,
        "top_event": top_event,
        "normalized_top_event": normalized_top_event,
        "aliases": _dedupe_keep_order(aliases),
        "source_chunk_ids": _dedupe_keep_order(source_chunk_ids),
        "requirements": requirements or "",
        "status": "pending",
        "progress": 0,
        "stage": "queued",
        "message": "Queued",
        # 增量事件流：用于前端实时展示“多智能体”进度消息（而不是覆盖 message 字段）
        # event_seq 单调递增，便于前端去重/断点续传；events 保留最近 N 条
        "event_seq": 0,
        "events": [],
        "tree_id": None,
        "error": None,
        "execution_owner": None,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
    }
    generation_job_items_col.insert_one(doc)
    refresh_generation_job(job_id)
    return _strip_mongo_id(doc)


def append_generation_job_item_event(
    item_id: str,
    *,
    agent: str,
    text: str,
    level: str = "INFO",
    stage: Optional[str] = None,
    progress: Optional[int] = None,
    kind: str = "log",
    extra: Optional[Dict[str, Any]] = None,
    max_events: int = 200,
) -> Optional[Dict[str, Any]]:
    """
    Append a progress/log event to a job item.
    Stored on the job-item so frontend can poll and render messages in real-time.
    """
    item = generation_job_items_col.find_one({"_id": item_id}, {"event_seq": 1})
    if not item:
        return None
    next_seq = int(item.get("event_seq") or 0) + 1
    now = _now()
    payload = {
        "seq": next_seq,
        "ts": now,
        "agent": str(agent or "").strip() or "Agent",
        "level": str(level or "INFO").upper(),
        "kind": str(kind or "log"),
        "text": str(text or "").rstrip(),
    }
    if stage is not None:
        payload["stage"] = stage
    if progress is not None:
        try:
            payload["progress"] = max(0, min(100, int(progress)))
        except Exception:
            payload["progress"] = None
    if extra and isinstance(extra, dict):
        payload["extra"] = extra

    generation_job_items_col.update_one(
        {"_id": item_id},
        {
            "$set": {"event_seq": next_seq, "updated_at": now},
            "$push": {"events": {"$each": [payload], "$slice": -abs(int(max_events))}},
        },
    )
    refresh_generation_job(item.get("job_id"))
    return get_generation_job_item(item_id)

def update_generation_job_item(
    item_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    tree_id: Optional[str] = None,
    error: Optional[str] = None,
    **extra_fields,
) -> Optional[Dict[str, Any]]:
    item = generation_job_items_col.find_one({"_id": item_id})
    if not item:
        return None

    payload: Dict[str, Any] = {"updated_at": _now()}
    if status is not None:
        payload["status"] = status
        if status == "running" and not item.get("started_at"):
            payload["started_at"] = payload["updated_at"]
        if status in {"success", "failed"}:
            payload["finished_at"] = payload["updated_at"]
    if progress is not None:
        payload["progress"] = max(0, min(100, int(progress)))
    if stage is not None:
        payload["stage"] = stage
    if message is not None:
        payload["message"] = message
    if tree_id is not None:
        payload["tree_id"] = tree_id
    if error is not None:
        payload["error"] = error

    started_at = payload.get("started_at") or item.get("started_at")
    finished_at = payload.get("finished_at") or item.get("finished_at")
    if finished_at and started_at:
        payload["duration_seconds"] = _seconds_between(started_at, finished_at)

    payload.update({k: v for k, v in extra_fields.items() if v is not None})
    generation_job_items_col.update_one({"_id": item_id}, {"$set": payload})
    refresh_generation_job(item["job_id"])
    return get_generation_job_item(item_id)


def claim_generation_job_item(
    item_id: str,
    *,
    execution_owner: str,
    allowed_statuses: Optional[List[str]] = None,
    progress: int = 5,
    stage: str = "prepare",
    message: str = "Preparing generation task",
    **extra_fields,
) -> Optional[Dict[str, Any]]:
    allowed_statuses = allowed_statuses or ["pending"]
    now = _now()
    payload: Dict[str, Any] = {
        "status": "running",
        "progress": max(0, min(100, int(progress))),
        "stage": stage,
        "message": message,
        "execution_owner": execution_owner,
        "updated_at": now,
    }
    payload.update({k: v for k, v in extra_fields.items() if v is not None})

    result = generation_job_items_col.update_one(
        {
            "_id": item_id,
            "status": {"$in": allowed_statuses},
        },
        {
            "$set": payload,
        },
    )
    if result.modified_count == 0:
        return None

    generation_job_items_col.update_one(
        {"_id": item_id, "started_at": None},
        {"$set": {"started_at": now}},
    )
    doc = generation_job_items_col.find_one({"_id": item_id})
    if doc:
        refresh_generation_job(doc["job_id"])
    return get_generation_job_item(item_id)


def get_generation_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _decorate_runtime_fields(generation_jobs_col.find_one({"_id": job_id}))


def get_generation_job_item(item_id: str) -> Optional[Dict[str, Any]]:
    return _decorate_runtime_fields(generation_job_items_col.find_one({"_id": item_id}))


def list_generation_job_items(job_id: str) -> List[Dict[str, Any]]:
    cursor = generation_job_items_col.find({"job_id": job_id}, {"_id": 0}).sort("created_at", ASCENDING)
    return [_decorate_runtime_fields(doc) for doc in cursor]


def find_active_job_item_by_top_event(normalized_top_event: str) -> Optional[Dict[str, Any]]:
    doc = generation_job_items_col.find_one(
        {
            "normalized_top_event": normalized_top_event,
            "status": {"$in": ["pending", "running"]},
        },
        sort=[("updated_at", DESCENDING)],
    )
    return _decorate_runtime_fields(doc)


def refresh_generation_job(job_id: str) -> Optional[Dict[str, Any]]:
    items = list(
        generation_job_items_col.find(
            {"job_id": job_id},
            {"status": 1, "started_at": 1, "finished_at": 1},
        )
    )
    total = len(items)
    counts = {
        "success": 0,
        "failed": 0,
        "running": 0,
        "pending": 0,
    }
    for item in items:
        status = item.get("status")
        if status in counts:
            counts[status] += 1

    started_candidates = [item.get("started_at") for item in items if item.get("started_at")]
    finished_candidates = [item.get("finished_at") for item in items if item.get("finished_at")]
    started_at = min(started_candidates) if started_candidates else None

    if total == 0:
        status = "completed"
        finished_at = _now()
    elif counts["success"] == total:
        status = "completed"
        finished_at = _now()
    elif counts["failed"] == total:
        status = "failed"
        finished_at = _now()
    elif counts["success"] + counts["failed"] == total:
        status = "partial_failed" if counts["failed"] else "completed"
        finished_at = _now()
    elif counts["running"] > 0:
        status = "running"
        finished_at = None
    else:
        status = "pending"
        finished_at = None

    if finished_candidates and status in {"completed", "partial_failed", "failed"}:
        finished_at = max(finished_candidates)

    duration_seconds = _seconds_between(started_at, finished_at) if started_at else None

    generation_jobs_col.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": status,
                "total": total,
                "success": counts["success"],
                "failed": counts["failed"],
                "running": counts["running"],
                "pending": counts["pending"],
                "updated_at": _now(),
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": duration_seconds,
            },
            "$setOnInsert": {"created_at": _now()},
        },
    )

    return get_generation_job(job_id)


def try_mark_job_completion_logged(job_id: str) -> bool:
    result = generation_jobs_col.update_one(
        {
            "_id": job_id,
            "completion_logged_at": None,
            "status": {"$in": ["completed", "partial_failed", "failed"]},
        },
        {"$set": {"completion_logged_at": _now()}},
    )
    return result.modified_count > 0
