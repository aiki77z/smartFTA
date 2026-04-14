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
    "故障现象与报警": "FaultPhenomenon",
    "硬件组件与元器件": "Component",
    "参数与数据": "Parameter",
    "技术系统与设备": "System",
    "技术概念与方法": "Method",
    "工具与仪器": "Tool",
}

def load_json(path: Path) -> List[Dict[str, Any]]:
    content = path.read_text(encoding="utf-8").strip()
    data = []
    
    try:
        # 首先尝试按标准单体 JSON 解析
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            data = [parsed]
        elif isinstance(parsed, list):
            data = parsed
        else:
            raise ValueError("JSON root must be a list or object")
    except json.JSONDecodeError:
        # 如果标准解析失败（触发 Extra data 错误），则按 JSONL 格式逐行解析
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            data.append(item)

    # 下方的逻辑保持不变，处理 normalized 提取
    normalized = []
    for item in data:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id", "")).strip()
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
            normalized.append({"chunk_id": chunk_id, "relations": cleaned})
            
    return normalized

CREATE_ENTITY_CONSTRAINT = """
CREATE CONSTRAINT entity_name_type_unique IF NOT EXISTS
FOR (e:Entity)
REQUIRE (e.name, e.entity_type) IS UNIQUE
"""

CREATE_CHUNK_CONSTRAINT = """
CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS
FOR (c:Chunk)
REQUIRE c.chunk_id IS UNIQUE
"""

IMPORT_BATCH_CYPHER = """
UNWIND $rows AS row
MERGE (c:Chunk {chunk_id: row.chunk_id})
WITH c, row
UNWIND row.relations AS rel
MERGE (e1:Entity {name: rel.entity1, entity_type: rel.entity1_type})
  ON CREATE SET e1.created_at = datetime()
SET e1.updated_at = datetime()
FOREACH (_ IN CASE WHEN rel.entity1_label = 'FaultPhenomenon' THEN [1] ELSE [] END | SET e1:FaultPhenomenon)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Component' THEN [1] ELSE [] END | SET e1:Component)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Parameter' THEN [1] ELSE [] END | SET e1:Parameter)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'System' THEN [1] ELSE [] END | SET e1:System)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Method' THEN [1] ELSE [] END | SET e1:Method)
FOREACH (_ IN CASE WHEN rel.entity1_label = 'Tool' THEN [1] ELSE [] END | SET e1:Tool)
MERGE (e2:Entity {name: rel.entity2, entity_type: rel.entity2_type})
  ON CREATE SET e2.created_at = datetime()
SET e2.updated_at = datetime()
FOREACH (_ IN CASE WHEN rel.entity2_label = 'FaultPhenomenon' THEN [1] ELSE [] END | SET e2:FaultPhenomenon)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Component' THEN [1] ELSE [] END | SET e2:Component)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Parameter' THEN [1] ELSE [] END | SET e2:Parameter)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'System' THEN [1] ELSE [] END | SET e2:System)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Method' THEN [1] ELSE [] END | SET e2:Method)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Tool' THEN [1] ELSE [] END | SET e2:Tool)
MERGE (e1)-[r:RELATION {relation_type: rel.relation_type, chunk_id: row.chunk_id}]->(e2)
  ON CREATE SET r.created_at = datetime()
SET r.updated_at = datetime()
MERGE (e1)-[:MENTIONED_IN]->(c)
MERGE (e2)-[:MENTIONED_IN]->(c)
"""

DELETE_ALL_CYPHER = "MATCH (n) DETACH DELETE n"

def chunked(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]

def ensure_constraints(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        session.run(CREATE_ENTITY_CONSTRAINT).consume()
        session.run(CREATE_CHUNK_CONSTRAINT).consume()

def clear_graph(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        session.run(DELETE_ALL_CYPHER).consume()

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
    return parser

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    json_path = Path(args.file)
    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        return 1
    rows = load_json(json_path)
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
