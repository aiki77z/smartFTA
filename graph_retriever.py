from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from config import (
    ENABLE_GRAPH_RETRIEVAL,
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)
from database import fetch_chunks_by_ids

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None


FAULT_TRIGGER_RELATION = "\u6545\u969c\u89e6\u53d1"
GRAPH_TOP_EVENT_NOISE_TERMS = (
    "\u9a8c\u6536\u6d4b\u8bd5",
    "\u9700\u4fdd\u5b58",
    "\u5e94\u7b54/\u4fdd\u5b58",
    "\u5e94\u7b54\u4fdd\u5b58",
    "\u66f4\u6362\u9700\u4fdd\u5b58",
)
GRAPH_EXPAND_RELATION_TYPES = [
    "\u6545\u969c\u89e6\u53d1",
    "\u7ec4\u6210\u5173\u7cfb",
    "\u4f9d\u8d56\u5173\u7cfb",
    "\u901a\u8baf\u8fde\u63a5",
    "\u53c2\u6570\u914d\u7f6e",
    "\u529f\u80fd\u652f\u6301",
    "\u6545\u969c\u5904\u7406",
]
GRAPH_SUPPORT_RELATION_TYPES = [
    "\u7ec4\u6210\u5173\u7cfb",
    "\u4f9d\u8d56\u5173\u7cfb",
    "\u901a\u8baf\u8fde\u63a5",
    "\u53c2\u6570\u914d\u7f6e",
]
GRAPH_LAYER_PRIORITY = {"A": 0, "B": 1, "C": 2}
GRAPH_LAYER_SCORES = {
    "A_top_chunk": 120,
    "A_direct_cause": 95,
    "B_second_cause": 70,
}
GRAPH_SUPPORT_RELATION_SCORES = {
    "\u7ec4\u6210\u5173\u7cfb": 55,
    "\u4f9d\u8d56\u5173\u7cfb": 50,
    "\u901a\u8baf\u8fde\u63a5": 48,
    "\u53c2\u6570\u914d\u7f6e": 45,
}


def _dedupe_keep_order(values: List[Any]) -> List[Any]:
    result = []
    for value in values or []:
        if value in (None, ""):
            continue
        if value not in result:
            result.append(value)
    return result


def _normalize_graph_top_event_name(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = value.replace("\uff08", "(").replace("\uff09", ")")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _normalize_chunk_key(chunk_id: Any) -> str:
    return str(chunk_id).strip()


def _is_valid_fault_phenomenon_top_event(name: str) -> bool:
    candidate = _normalize_graph_top_event_name(name)
    if not candidate or len(candidate) < 2 or len(candidate) > 60:
        return False

    if re.fullmatch(r"[FA]\d{5}(?:\([A-Z]\))?", candidate, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"[0-9A-F]{3,4}", candidate, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"\d+", candidate):
        return False
    if re.fullmatch(r"SI\s*[A-Z0-9]+", candidate, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"SIP\s*\d+", candidate, flags=re.IGNORECASE):
        return False

    if not re.search(r"[\u4e00-\u9fff]", candidate):
        if candidate not in {"STOP A", "STOP F"}:
            return False

    if any(term in candidate for term in GRAPH_TOP_EVENT_NOISE_TERMS):
        return False

    return True


def _is_meaningful_graph_entity(name: str, family: str) -> bool:
    candidate = _normalize_graph_top_event_name(name)
    if not candidate:
        return False
    if re.fullmatch(r"[FA]\d{5}(?:\([A-Z]\))?", candidate, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"[0-9A-F]{3,4}", candidate, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"\d+", candidate):
        return False
    if family == "parameter":
        return bool(re.search(r"[A-Za-z]\d|\u53c2\u6570|[\u4e00-\u9fff]", candidate))
    if family == "system":
        return bool(re.search(r"[\u4e00-\u9fff]|PROFI|DRIVE-CLIQ|CAN|BUS", candidate, flags=re.IGNORECASE))
    if family == "component":
        return bool(re.search(r"[\u4e00-\u9fff]", candidate))
    if family == "fault":
        return _is_valid_fault_phenomenon_top_event(candidate)
    return True


def _score_support_relation(relation_type: str, family: str) -> int:
    score = GRAPH_SUPPORT_RELATION_SCORES.get(relation_type, 35)
    if family == "component":
        score += 6
    elif family == "system":
        score += 4
    elif family == "parameter":
        score += 2
    return score


def _sort_trace(trace: Dict[str, Any]):
    return (
        -int(trace.get("score") or 0),
        GRAPH_LAYER_PRIORITY.get(trace.get("source_layer"), 99),
        len(trace.get("path") or []),
        str(trace.get("matched_entity") or ""),
    )


def _merge_chunk_trace_buckets(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for trace in traces:
        chunk_id = trace.get("chunk_id")
        if chunk_id in (None, ""):
            continue
        chunk_key = _normalize_chunk_key(chunk_id)
        bucket = buckets.setdefault(
            chunk_key,
            {
                "chunk_id": chunk_id,
                "score": 0,
                "evidences": [],
            },
        )
        bucket["score"] += int(trace.get("score") or 0)
        bucket["evidences"].append(trace)

    merged = []
    for bucket in buckets.values():
        evidences = sorted(bucket["evidences"], key=_sort_trace)
        primary = evidences[0]
        merged.append(
            {
                "chunk_id": bucket["chunk_id"],
                "score": bucket["score"],
                "source_layer": primary.get("source_layer"),
                "matched_entity": primary.get("matched_entity"),
                "path": primary.get("path") or [],
                "path_relations": primary.get("path_relations") or [],
                "evidences": evidences,
            }
        )

    merged.sort(
        key=lambda item: (
            -int(item.get("score") or 0),
            GRAPH_LAYER_PRIORITY.get(item.get("source_layer"), 99),
            str(item.get("matched_entity") or ""),
            _normalize_chunk_key(item.get("chunk_id")),
        )
    )
    return merged


def _fetch_ordered_chunks(chunk_ids: List[Any], limit: int) -> List[Dict[str, Any]]:
    ordered_ids = _dedupe_keep_order(chunk_ids)[:limit]
    docs = fetch_chunks_by_ids(ordered_ids, limit=max(limit, len(ordered_ids)))
    doc_map = {_normalize_chunk_key(doc.get("id", doc.get("chunk_id"))): doc for doc in docs}
    ordered_docs = []
    for chunk_id in ordered_ids:
        doc = doc_map.get(_normalize_chunk_key(chunk_id))
        if doc:
            ordered_docs.append(doc)
    return ordered_docs[:limit]


def is_graph_available() -> bool:
    return bool(ENABLE_GRAPH_RETRIEVAL and GraphDatabase and NEO4J_PASSWORD)


@lru_cache(maxsize=1)
def _get_driver():
    if not is_graph_available():
        return None
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def list_fault_phenomenon_top_events(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    driver = _get_driver()
    if driver is None:
        return []

    cypher = """
    MATCH (top:Entity:FaultPhenomenon)
    OPTIONAL MATCH (top)-[:MENTIONED_IN]->(c:Chunk)
    RETURN top.name AS name, collect(DISTINCT c.chunk_id) AS source_chunk_ids
    ORDER BY size(source_chunk_ids) DESC, name ASC
    """
    if limit:
        cypher += "\nLIMIT $limit"

    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            rows = session.run(cypher, limit=limit).data()
    except Exception:
        return []

    results = []
    for row in rows:
        name = _normalize_graph_top_event_name(row.get("name"))
        if not _is_valid_fault_phenomenon_top_event(name):
            continue
        results.append(
            {
                "name": name,
                "aliases": [],
                "source_chunk_ids": _dedupe_keep_order(row.get("source_chunk_ids") or []),
            }
        )
    return results


def _entity_type_family(entity_type: str) -> str:
    value = str(entity_type or "").strip()
    if "\u6545\u969c\u73b0\u8c61" in value or "\u62a5\u8b66" in value:
        return "fault"
    if "\u786c\u4ef6\u7ec4\u4ef6" in value:
        return "component"
    if "\u53c2\u6570" in value or "\u6570\u636e" in value:
        return "parameter"
    if "\u7cfb\u7edf" in value or "\u8bbe\u5907" in value:
        return "system"
    if "\u65b9\u6cd5" in value or "\u6982\u5ff5" in value:
        return "method"
    if "\u5de5\u5177" in value or "\u4eea\u5668" in value:
        return "tool"
    return "other"


def _map_entity_to_tree_event(entity_name: str, entity_type: str, *, has_children: bool) -> Optional[Dict[str, Any]]:
    name = _normalize_graph_top_event_name(entity_name)
    if not name:
        return None

    family = _entity_type_family(entity_type)
    mapped_name = name
    suggested_type = "basic_event"
    role = "event"

    if family == "fault":
        suggested_type = "intermediate_event" if has_children else "basic_event"
    elif family == "component":
        mapped_name = f"{name}\u6545\u969c"
        suggested_type = "intermediate_event" if has_children else "basic_event"
    elif family == "parameter":
        mapped_name = f"{name}\u53c2\u6570\u5f02\u5e38"
        suggested_type = "basic_event"
    elif family == "system":
        if "PROFI" in name.upper() or "DRIVE-CLIQ" in name.upper() or "\u901a\u8baf" in name:
            mapped_name = f"{name}\u901a\u8baf\u5f02\u5e38"
        else:
            mapped_name = f"{name}\u7cfb\u7edf\u5f02\u5e38"
        suggested_type = "intermediate_event" if has_children else "basic_event"
    elif family in {"method", "tool"}:
        role = "investigate_method"
        suggested_type = "reference"
    else:
        mapped_name = f"{name}\u5f02\u5e38"
        suggested_type = "basic_event"

    return {
        "entity_name": name,
        "entity_type": entity_type,
        "mapped_name": mapped_name,
        "suggested_type": suggested_type,
        "role": role,
    }


def search_graph_related_chunks(top_event: str, aliases: Optional[List[str]] = None, limit: int = 8) -> Dict[str, Any]:
    driver = _get_driver()
    names = _dedupe_keep_order([top_event] + list(aliases or []))
    empty_result = {
        "chunks": [],
        "chunk_ids": [],
        "chunk_traces": [],
        "cause_names": [],
        "matched_names": names,
        "layer_counts": {"A": 0, "B": 0, "C": 0},
    }
    if driver is None or not names:
        return empty_result

    layer_a_top_rows: List[Dict[str, Any]] = []
    layer_a_direct_rows: List[Dict[str, Any]] = []
    layer_b_rows: List[Dict[str, Any]] = []
    layer_c_rows: List[Dict[str, Any]] = []

    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            layer_a_top_rows = session.run(
                """
                UNWIND $names AS name
                MATCH (top:Entity:FaultPhenomenon {name: name})-[:MENTIONED_IN]->(c:Chunk)
                RETURN DISTINCT c.chunk_id AS chunk_id, top.name AS top_name
                """,
                names=names,
            ).data()

            layer_a_direct_rows = session.run(
                """
                UNWIND $names AS name
                MATCH (cause1:Entity)-[r1:RELATION]->(top:Entity:FaultPhenomenon {name: name})
                WHERE r1.relation_type = $fault_trigger_relation
                MATCH (cause1)-[:MENTIONED_IN]->(c:Chunk)
                RETURN DISTINCT
                  c.chunk_id AS chunk_id,
                  top.name AS top_name,
                  cause1.name AS cause_name,
                  cause1.entity_type AS cause_type
                """,
                names=names,
                fault_trigger_relation=FAULT_TRIGGER_RELATION,
            ).data()

            layer_b_rows = session.run(
                """
                UNWIND $names AS name
                MATCH (cause1:Entity)-[r1:RELATION]->(top:Entity:FaultPhenomenon {name: name})
                WHERE r1.relation_type = $fault_trigger_relation
                MATCH (cause2:Entity)-[r2:RELATION]->(cause1)
                WHERE r2.relation_type = $fault_trigger_relation
                MATCH (cause2)-[:MENTIONED_IN]->(c:Chunk)
                RETURN DISTINCT
                  c.chunk_id AS chunk_id,
                  top.name AS top_name,
                  cause1.name AS cause1_name,
                  cause2.name AS cause2_name,
                  cause2.entity_type AS cause2_type
                """,
                names=names,
                fault_trigger_relation=FAULT_TRIGGER_RELATION,
            ).data()

            layer_c_rows = session.run(
                """
                UNWIND $names AS name
                MATCH (cause1:Entity)-[r1:RELATION]->(top:Entity:FaultPhenomenon {name: name})
                WHERE r1.relation_type = $fault_trigger_relation
                MATCH (cause1)-[r2:RELATION]-(support:Entity)
                WHERE r2.relation_type IN $support_relation_types
                MATCH (support)-[:MENTIONED_IN]->(c:Chunk)
                RETURN DISTINCT
                  c.chunk_id AS chunk_id,
                  top.name AS top_name,
                  cause1.name AS cause1_name,
                  support.name AS support_name,
                  support.entity_type AS support_type,
                  r2.relation_type AS support_relation
                """,
                names=names,
                fault_trigger_relation=FAULT_TRIGGER_RELATION,
                support_relation_types=GRAPH_SUPPORT_RELATION_TYPES,
            ).data()
    except Exception:
        return empty_result

    direct_cause_names = []
    traces: List[Dict[str, Any]] = []
    layer_counts = {"A": 0, "B": 0, "C": 0}

    for row in layer_a_top_rows:
        top_name = _normalize_graph_top_event_name(row.get("top_name"))
        chunk_id = row.get("chunk_id")
        if not top_name or chunk_id in (None, ""):
            continue
        traces.append(
            {
                "chunk_id": chunk_id,
                "score": GRAPH_LAYER_SCORES["A_top_chunk"],
                "source_layer": "A",
                "matched_entity": top_name,
                "path": [top_name],
                "path_relations": [],
            }
        )
        layer_counts["A"] += 1

    for row in layer_a_direct_rows:
        top_name = _normalize_graph_top_event_name(row.get("top_name"))
        cause_name = _normalize_graph_top_event_name(row.get("cause_name"))
        cause_family = _entity_type_family(str(row.get("cause_type") or ""))
        chunk_id = row.get("chunk_id")
        if not top_name or not cause_name or chunk_id in (None, ""):
            continue
        if not _is_meaningful_graph_entity(cause_name, cause_family):
            continue
        if cause_name not in direct_cause_names:
            direct_cause_names.append(cause_name)
        traces.append(
            {
                "chunk_id": chunk_id,
                "score": GRAPH_LAYER_SCORES["A_direct_cause"],
                "source_layer": "A",
                "matched_entity": cause_name,
                "path": [top_name, cause_name],
                "path_relations": [FAULT_TRIGGER_RELATION],
            }
        )
        layer_counts["A"] += 1

    for row in layer_b_rows:
        top_name = _normalize_graph_top_event_name(row.get("top_name"))
        cause1_name = _normalize_graph_top_event_name(row.get("cause1_name"))
        cause2_name = _normalize_graph_top_event_name(row.get("cause2_name"))
        cause2_family = _entity_type_family(str(row.get("cause2_type") or ""))
        chunk_id = row.get("chunk_id")
        if not top_name or not cause1_name or not cause2_name or chunk_id in (None, ""):
            continue
        if not _is_meaningful_graph_entity(cause2_name, cause2_family):
            continue
        traces.append(
            {
                "chunk_id": chunk_id,
                "score": GRAPH_LAYER_SCORES["B_second_cause"],
                "source_layer": "B",
                "matched_entity": cause2_name,
                "path": [top_name, cause1_name, cause2_name],
                "path_relations": [FAULT_TRIGGER_RELATION, FAULT_TRIGGER_RELATION],
            }
        )
        layer_counts["B"] += 1

    for row in layer_c_rows:
        top_name = _normalize_graph_top_event_name(row.get("top_name"))
        cause1_name = _normalize_graph_top_event_name(row.get("cause1_name"))
        support_name = _normalize_graph_top_event_name(row.get("support_name"))
        support_type = str(row.get("support_type") or "")
        support_family = _entity_type_family(support_type)
        relation_type = str(row.get("support_relation") or "")
        chunk_id = row.get("chunk_id")
        if not top_name or not cause1_name or not support_name or chunk_id in (None, ""):
            continue
        if support_family not in {"component", "system", "parameter"}:
            continue
        if not _is_meaningful_graph_entity(support_name, support_family):
            continue
        traces.append(
            {
                "chunk_id": chunk_id,
                "score": _score_support_relation(relation_type, support_family),
                "source_layer": "C",
                "matched_entity": support_name,
                "path": [top_name, cause1_name, support_name],
                "path_relations": [FAULT_TRIGGER_RELATION, relation_type],
            }
        )
        layer_counts["C"] += 1

    chunk_traces = _merge_chunk_trace_buckets(traces)[:limit]
    chunk_ids = [item["chunk_id"] for item in chunk_traces]
    chunks = _fetch_ordered_chunks(chunk_ids, limit=limit)
    trace_map = {_normalize_chunk_key(item["chunk_id"]): item for item in chunk_traces}

    annotated_chunks = []
    for chunk in chunks:
        chunk_copy = dict(chunk)
        trace = trace_map.get(_normalize_chunk_key(chunk.get("id", chunk.get("chunk_id"))))
        if trace:
            chunk_copy["retrieval_trace"] = trace
        annotated_chunks.append(chunk_copy)

    return {
        "chunks": annotated_chunks,
        "chunk_ids": chunk_ids,
        "chunk_traces": chunk_traces,
        "cause_names": direct_cause_names,
        "matched_names": names,
        "layer_counts": layer_counts,
    }


def build_graph_draft_candidates(top_event: str, aliases: Optional[List[str]] = None, limit: int = 24) -> Dict[str, Any]:
    driver = _get_driver()
    names = _dedupe_keep_order([top_event] + list(aliases or []))
    if driver is None or not names:
        return {
            "top_event": top_event,
            "matched_names": names,
            "direct_causes": [],
            "expanded_nodes": [],
            "draft": {"nodes": [], "relations": []},
            "investigate_methods": [],
        }

    cypher = """
    UNWIND $names AS name
    MATCH (cause:Entity)-[r:RELATION]->(top:Entity:FaultPhenomenon {name: name})
    WHERE r.relation_type = $fault_trigger_relation
    OPTIONAL MATCH (cause)-[r2:RELATION]->(neighbor:Entity)
    WHERE r2.relation_type IN $expand_relation_types
    RETURN
      top.name AS top_name,
      cause.name AS cause_name,
      cause.entity_type AS cause_type,
      collect(DISTINCT {
        name: neighbor.name,
        entity_type: neighbor.entity_type,
        relation_type: r2.relation_type
      }) AS expansions
    LIMIT $limit
    """

    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            rows = session.run(
                cypher,
                names=names,
                limit=limit,
                fault_trigger_relation=FAULT_TRIGGER_RELATION,
                expand_relation_types=GRAPH_EXPAND_RELATION_TYPES,
            ).data()
    except Exception:
        rows = []

    direct_causes = []
    expanded_nodes = []
    investigate_methods = []
    draft_nodes = []
    draft_relations = []
    seen_nodes = set()
    seen_relations = set()

    def add_node(name: str, node_type: str, source: str):
        if not name or name in seen_nodes:
            return
        seen_nodes.add(name)
        draft_nodes.append({"name": name, "type": node_type, "source": source})

    add_node(top_event, "top_event", "graph_top")

    for row in rows:
        cause_name = _normalize_graph_top_event_name(row.get("cause_name"))
        cause_type = str(row.get("cause_type") or "")
        expansions = [
            item
            for item in (row.get("expansions") or [])
            if isinstance(item, dict) and item.get("name")
        ]

        cause_mapped = _map_entity_to_tree_event(cause_name, cause_type, has_children=bool(expansions))
        if not cause_mapped or cause_mapped["role"] != "event":
            continue
        if cause_mapped["mapped_name"] == top_event:
            continue

        direct_causes.append(
            {
                **cause_mapped,
                "source_relation": FAULT_TRIGGER_RELATION,
            }
        )
        add_node(cause_mapped["mapped_name"], cause_mapped["suggested_type"], "graph_direct_cause")

        direct_relation = (cause_mapped["mapped_name"], top_event)
        if direct_relation not in seen_relations:
            seen_relations.add(direct_relation)
            draft_relations.append(
                {
                    "parent": top_event,
                    "child": cause_mapped["mapped_name"],
                    "gate": "OR",
                    "source_relation": FAULT_TRIGGER_RELATION,
                    "source_entity": cause_name,
                }
            )

        for expansion in expansions:
            neighbor_name = _normalize_graph_top_event_name(expansion.get("name"))
            neighbor_type = str(expansion.get("entity_type") or "")
            relation_type = str(expansion.get("relation_type") or "")
            mapped_neighbor = _map_entity_to_tree_event(neighbor_name, neighbor_type, has_children=False)
            if not mapped_neighbor:
                continue

            if mapped_neighbor["role"] == "investigate_method":
                if mapped_neighbor["mapped_name"] not in investigate_methods:
                    investigate_methods.append(mapped_neighbor["mapped_name"])
                continue

            if mapped_neighbor["mapped_name"] in {top_event, cause_mapped["mapped_name"]}:
                continue

            expanded_nodes.append(
                {
                    **mapped_neighbor,
                    "parent_cause": cause_mapped["mapped_name"],
                    "source_relation": relation_type,
                }
            )
            add_node(mapped_neighbor["mapped_name"], mapped_neighbor["suggested_type"], "graph_expansion")

            nested_relation = (mapped_neighbor["mapped_name"], cause_mapped["mapped_name"])
            if nested_relation not in seen_relations:
                seen_relations.add(nested_relation)
                draft_relations.append(
                    {
                        "parent": cause_mapped["mapped_name"],
                        "child": mapped_neighbor["mapped_name"],
                        "gate": "OR",
                        "source_relation": relation_type,
                        "source_entity": neighbor_name,
                    }
                )

    return {
        "top_event": top_event,
        "matched_names": names,
        "direct_causes": direct_causes,
        "expanded_nodes": expanded_nodes,
        "draft": {"nodes": draft_nodes, "relations": draft_relations},
        "investigate_methods": investigate_methods,
    }
