#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from neo4j import GraphDatabase
except ImportError as exc:
    raise SystemExit("Missing dependency: neo4j\nInstall it with: pip install neo4j") from exc

LABEL_MAP = {
    "\u6545\u969c\u73b0\u8c61\u4e0e\u62a5\u8b66": "FaultPhenomenon",
    "\u6545\u969c\u539f\u56e0\u4e0e\u73b0\u8c61": "FaultPhenomenon",
    "\u786c\u4ef6\u7ec4\u4ef6\u4e0e\u5143\u5668\u4ef6": "Component",
    "\u53c2\u6570\u4e0e\u6570\u636e": "Parameter",
    "\u6280\u672f\u7cfb\u7edf\u4e0e\u8bbe\u5907": "System",
    "\u6280\u672f\u6982\u5ff5\u4e0e\u65b9\u6cd5": "Method",
    "\u5de5\u5177\u4e0e\u4eea\u5668": "Tool",
}

def _make_chunk_ref(file_version_id: Any, chunk_id: Any) -> str:
    version = str(file_version_id or "").strip()
    chunk = str(chunk_id or "").strip()
    return f"{version}::{chunk}" if version and chunk else chunk


def load_json(
    path: Path,
    *,
    file_id: str = "",
    file_version_id: str = "",
    file_name: str = "",
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    content = path.read_text(encoding="utf-8").strip()
    data = []

    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            data = [parsed]
        elif isinstance(parsed, list):
            data = parsed
        else:
            raise ValueError("JSON root must be a list or object")
    except json.JSONDecodeError:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            data.append(json.loads(line))

    normalized = []
    for item in data:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id", "")).strip()
        embedded_file_id = str(item.get("file_id") or "").strip()
        embedded_version_id = str(item.get("file_version_id") or "").strip()
        if file_id and embedded_file_id and embedded_file_id != file_id:
            raise ValueError(f"Relation row '{chunk_id}' belongs to file_id '{embedded_file_id}', not '{file_id}'")
        if file_version_id and embedded_version_id and embedded_version_id != file_version_id:
            raise ValueError(
                f"Relation row '{chunk_id}' belongs to file_version_id '{embedded_version_id}', "
                f"not '{file_version_id}'"
            )
        relations = item.get("relations") or item.get("relation") or []
        if not isinstance(relations, list):
            continue
        cleaned = []
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            entity1 = str(rel.get("entity1", "")).strip()
            entity2 = str(rel.get("entity2", "")).strip()
            relation_type = str(rel.get("relation_type", "")).strip()
            entity1_type = str(rel.get("entity1_type", "")).strip()
            entity2_type = str(rel.get("entity2_type", "")).strip()
            if not entity1 or not entity2 or not relation_type:
                continue
            cleaned.append({
                "entity1": entity1,
                "entity2": entity2,
                "relation_type": relation_type,
                "entity1_type": entity1_type,
                "entity2_type": entity2_type,
                "entity1_label": LABEL_MAP.get(entity1_type, ""),
                "entity2_label": LABEL_MAP.get(entity2_type, ""),
            })
        if chunk_id and cleaned:
            chunk_ref = _make_chunk_ref(file_version_id, chunk_id)
            normalized.append({
                "chunk_id": chunk_id,
                "chunk_ref": chunk_ref,
                "file_id": file_id,
                "file_version_id": file_version_id,
                "file_name": file_name,
                "is_active": bool(is_active),
                "documents_json": json.dumps(
                    [
                        {"chunk_id": chunk_id, "chunk_uid": chunk_ref, "file_version_id": file_version_id}
                    ] if chunk_ref else [{"chunk_id": chunk_id}],
                    ensure_ascii=False,
                ),
                "source_chunk_ids": [chunk_id],
                "source_chunk_refs": [chunk_ref] if chunk_ref else [],
                "relations": cleaned,
            })

    return normalized

def load_entities(
    path: Path,
    *,
    file_id: str,
    file_version_id: str,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Entity JSON root must be a list")

    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("entity_name") or "").strip()
        entity_type = str(item.get("entity_type") or "").strip()
        if not name or not entity_type:
            continue
        embedded_file_id = str(item.get("file_id") or "").strip()
        embedded_version_id = str(item.get("file_version_id") or "").strip()
        if embedded_file_id and embedded_file_id != file_id:
            raise ValueError(f"Entity '{name}' belongs to file_id '{embedded_file_id}', not '{file_id}'")
        if embedded_version_id and embedded_version_id != file_version_id:
            raise ValueError(
                f"Entity '{name}' belongs to file_version_id '{embedded_version_id}', not '{file_version_id}'"
            )
        chunk_ids = [str(value).strip() for value in item.get("source_chunk_ids") or [] if str(value).strip()]
        rows.append(
            {
                "name": name,
                "normalized_name": str(item.get("normalized_name") or name).strip(),
                "entity_type": entity_type,
                "label": LABEL_MAP.get(entity_type, ""),
                "file_id": file_id,
                "file_version_id": file_version_id,
                "is_active": bool(is_active),
                "description": str(item.get("description") or ""),
                "error_level": str(item.get("errorLevel") or ""),
                "priority": item.get("priority"),
                "probability": item.get("probability"),
                "show_probability": item.get("showProbability"),
                "rule": str(item.get("rule") or ""),
                "investigate_method": str(item.get("investigateMethod") or ""),
                "repair_method": str(item.get("repairMethod") or ""),
                "support_count": int(item.get("support_count") or len(chunk_ids) or 1),
                "source_chunk_ids": chunk_ids,
                "source_chunk_refs": [_make_chunk_ref(file_version_id, value) for value in chunk_ids],
                "documents_json": json.dumps(item.get("documents") or [], ensure_ascii=False),
            }
        )
    return rows


IMPORT_ENTITIES_CYPHER = """
UNWIND $rows AS row
MERGE (e:Entity {name: row.name, entity_type: row.entity_type, file_version_id: row.file_version_id})
  ON CREATE SET e.created_at = datetime()
SET e.normalized_name = row.normalized_name,
    e.file_id = row.file_id,
    e.file_version_id = row.file_version_id,
    e.is_active = row.is_active,
    e.description = row.description,
    e.errorLevel = row.error_level,
    e.priority = row.priority,
    e.probability = row.probability,
    e.showProbability = row.show_probability,
    e.rule = row.rule,
    e.investigateMethod = row.investigate_method,
    e.repairMethod = row.repair_method,
    e.support_count = row.support_count,
    e.source_chunk_ids = row.source_chunk_ids,
    e.source_chunk_refs = row.source_chunk_refs,
    e.documents = row.documents_json,
    e.updated_at = datetime()
FOREACH (_ IN CASE WHEN row.label = 'FaultPhenomenon' THEN [1] ELSE [] END | SET e:FaultPhenomenon)
FOREACH (_ IN CASE WHEN row.entity_type = '\u903b\u8f91\u4e0e' THEN [1] ELSE [] END | SET e:LogicGate)
"""

CREATE_ENTITY_CONSTRAINT = """
CREATE CONSTRAINT entity_name_type_version_unique IF NOT EXISTS
FOR (e:Entity)
REQUIRE (e.name, e.entity_type, e.file_version_id) IS UNIQUE
"""

CREATE_CHUNK_CONSTRAINT = """
CREATE CONSTRAINT chunk_uid_unique IF NOT EXISTS
FOR (c:Chunk)
REQUIRE c.chunk_uid IS UNIQUE
"""

IMPORT_BATCH_CYPHER = """
UNWIND $rows AS row
MERGE (c:Chunk {chunk_uid: row.chunk_ref})
  ON CREATE SET c.created_at = datetime()
SET c.chunk_id = row.chunk_id,
    c.file_id = row.file_id,
    c.file_version_id = row.file_version_id,
    c.file_name = row.file_name,
    c.is_active = row.is_active,
    c.updated_at = datetime()
WITH c, row
UNWIND row.relations AS rel
MERGE (e1:Entity {name: rel.entity1, entity_type: rel.entity1_type, file_version_id: row.file_version_id})
  ON CREATE SET e1.created_at = datetime()
SET e1.file_id = row.file_id,
    e1.file_version_id = row.file_version_id,
    e1.is_active = row.is_active,
    e1.updated_at = datetime(),
    e1.source_chunk_ids = reduce(acc = [], value IN coalesce(e1.source_chunk_ids, []) + row.source_chunk_ids |
        CASE WHEN value IN acc THEN acc ELSE acc + value END),
    e1.source_chunk_refs = reduce(acc = [], value IN coalesce(e1.source_chunk_refs, []) + row.source_chunk_refs |
        CASE WHEN value IN acc THEN acc ELSE acc + value END)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'FaultPhenomenon' THEN [1] ELSE [] END | SET e1:FaultPhenomenon)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Component' THEN [1] ELSE [] END | SET e1:Component)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Parameter' THEN [1] ELSE [] END | SET e1:Parameter)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'System' THEN [1] ELSE [] END | SET e1:System)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Method' THEN [1] ELSE [] END | SET e1:Method)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Tool' THEN [1] ELSE [] END | SET e1:Tool)
MERGE (e2:Entity {name: rel.entity2, entity_type: rel.entity2_type, file_version_id: row.file_version_id})
  ON CREATE SET e2.created_at = datetime()
SET e2.file_id = row.file_id,
    e2.file_version_id = row.file_version_id,
    e2.is_active = row.is_active,
    e2.updated_at = datetime(),
    e2.source_chunk_ids = reduce(acc = [], value IN coalesce(e2.source_chunk_ids, []) + row.source_chunk_ids |
        CASE WHEN value IN acc THEN acc ELSE acc + value END),
    e2.source_chunk_refs = reduce(acc = [], value IN coalesce(e2.source_chunk_refs, []) + row.source_chunk_refs |
        CASE WHEN value IN acc THEN acc ELSE acc + value END)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'FaultPhenomenon' THEN [1] ELSE [] END | SET e2:FaultPhenomenon)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Component' THEN [1] ELSE [] END | SET e2:Component)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Parameter' THEN [1] ELSE [] END | SET e2:Parameter)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'System' THEN [1] ELSE [] END | SET e2:System)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Method' THEN [1] ELSE [] END | SET e2:Method)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Tool' THEN [1] ELSE [] END | SET e2:Tool)
MERGE (e1)-[r:RELATION {relation_type: rel.relation_type, chunk_ref: row.chunk_ref}]->(e2)
  ON CREATE SET r.created_at = datetime()
SET r.chunk_id = row.chunk_id,
    r.source_chunk_ids = row.source_chunk_ids,
    r.source_chunk_refs = row.source_chunk_refs,
    r.file_id = row.file_id,
    r.file_version_id = row.file_version_id,
    r.is_active = row.is_active,
    r.updated_at = datetime()
MERGE (e1)-[m1:MENTIONED_IN {chunk_ref: row.chunk_ref}]->(c)
  ON CREATE SET m1.created_at = datetime()
SET m1.chunk_id = row.chunk_id,
    m1.source_chunk_ids = row.source_chunk_ids,
    m1.source_chunk_refs = row.source_chunk_refs,
    m1.file_id = row.file_id,
    m1.file_version_id = row.file_version_id,
    m1.is_active = row.is_active,
    m1.updated_at = datetime()
MERGE (e2)-[m2:MENTIONED_IN {chunk_ref: row.chunk_ref}]->(c)
  ON CREATE SET m2.created_at = datetime()
SET m2.chunk_id = row.chunk_id,
    m2.source_chunk_ids = row.source_chunk_ids,
    m2.source_chunk_refs = row.source_chunk_refs,
    m2.file_id = row.file_id,
    m2.file_version_id = row.file_version_id,
    m2.is_active = row.is_active,
    m2.updated_at = datetime()
"""

DELETE_ALL_CYPHER = "MATCH (n) DETACH DELETE n"

def chunked(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def ensure_constraints(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        session.run("DROP CONSTRAINT entity_name_type_unique IF EXISTS").consume()
        session.run("DROP CONSTRAINT chunk_id_unique IF EXISTS").consume()
        session.run(CREATE_ENTITY_CONSTRAINT).consume()
        session.run(CREATE_CHUNK_CONSTRAINT).consume()


def clear_graph(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        session.run(DELETE_ALL_CYPHER).consume()


def import_entities(driver: Any, database: str, rows: List[Dict[str, Any]], batch_size: int = 200) -> int:
    with driver.session(database=database) as session:
        for batch in chunked(rows, batch_size):
            session.run(IMPORT_ENTITIES_CYPHER, rows=batch).consume()
    return len(rows)


def import_rows(driver: Any, database: str, rows: List[Dict[str, Any]], batch_size: int) -> int:
    total_relations = 0
    with driver.session(database=database) as session:
        for batch in chunked(rows, batch_size):
            session.run(IMPORT_BATCH_CYPHER, rows=batch).consume()
            total_relations += sum(len(row["relations"]) for row in batch)
    return total_relations


def print_summary(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        node_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        relation_breakdown = session.run(
            "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS type, count(*) AS cnt ORDER BY cnt DESC"
        ).data()
    print(f"Nodes: {node_count}")
    print(f"Relationships: {rel_count}")
    print("RELATION breakdown:")
    for item in relation_breakdown:
        print(f"  - {item['type']}: {item['cnt']}")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import relation JSON into Neo4j")
    parser.add_argument("--file", required=True, help="Path to relation JSON file")
    parser.add_argument("--uri", default="bolt://localhost:7687", help="Neo4j URI")
    parser.add_argument("--user", default="neo4j", help="Neo4j username")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Target Neo4j database")
    parser.add_argument("--batch-size", type=int, default=200, help="Rows per write batch")
    parser.add_argument("--clear", action="store_true", help="Delete all existing graph data before import")
    parser.add_argument("--file-id", required=True, help="Stable source file id")
    parser.add_argument("--file-version-id", required=True, help="Exact imported source file version id")
    parser.add_argument("--file-name", default="", help="Original source file name")
    parser.add_argument("--inactive", action="store_true", help="Mark imported graph rows as inactive")
    return parser

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    json_path = Path(args.file)
    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        return 1
    rows = load_json(
        json_path,
        file_id=args.file_id,
        file_version_id=args.file_version_id,
        file_name=args.file_name,
        is_active=not args.inactive,
    )
    if not rows:
        print("No valid relation rows found in JSON.", file=sys.stderr)
        return 1
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        driver.verify_connectivity()
        ensure_constraints(driver, args.database)
        if args.clear:
            clear_graph(driver, args.database)
        relation_count = import_rows(driver, args.database, rows, args.batch_size)
        print(f"Imported {len(rows)} chunk rows and {relation_count} RELATION edges into database '{args.database}'.")
        print_summary(driver, args.database)
    finally:
        driver.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())


