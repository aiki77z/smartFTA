#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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

REQUIRED_RELATION_FIELDS = (
    "chunk_id",
    "entity1",
    "entity2",
    "relation_type",
    "entity1_type",
    "entity2_type",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _entity_label(entity_type: str) -> str:
    return LABEL_MAP.get(_clean(entity_type), "")


def _is_neo4j_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _sanitize_property_value(value: Any) -> Any:
    if value is None:
        return None
    if _is_neo4j_scalar(value):
        return value
    if isinstance(value, list):
        if all(_is_neo4j_scalar(item) for item in value):
            return value
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _sanitize_documents(value: Any, fallback_chunk_id: Any = "") -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    chunk_candidates: List[Any] = []

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("chunk_id") not in (None, ""):
                chunk_candidates.append(item.get("chunk_id"))

    if fallback_chunk_id not in (None, ""):
        chunk_candidates.append(fallback_chunk_id)

    seen = set()
    for chunk_id in chunk_candidates:
        key = _clean(chunk_id)
        if not key or key in seen:
            continue
        seen.add(key)
        documents.append({"chunk_id": key})
    return documents


def _sanitize_entity_props(props: Any, fallback_chunk_id: Any = "") -> Dict[str, Any]:
    if not isinstance(props, dict):
        return {}

    sanitized: Dict[str, Any] = {}
    for key, value in props.items():
        clean_key = _clean(key)
        if not clean_key:
            continue
        if clean_key == "documents":
            sanitized_documents = _sanitize_documents(value, fallback_chunk_id=fallback_chunk_id)
            if sanitized_documents:
                sanitized[clean_key] = json.dumps(sanitized_documents, ensure_ascii=False)
            continue
        sanitized_value = _sanitize_property_value(value)
        if sanitized_value is None:
            continue
        sanitized[clean_key] = sanitized_value
    return sanitized


def _normalize_relation(
    rel: Dict[str, Any],
    default_chunk_id: Any = "",
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    is_active: bool = True,
) -> Optional[Dict[str, Any]]:
    entity1 = _clean(rel.get("entity1"))
    entity2 = _clean(rel.get("entity2"))
    relation_type = _clean(rel.get("relation_type"))
    entity1_type = _clean(rel.get("entity1_type"))
    entity2_type = _clean(rel.get("entity2_type"))
    chunk_id = _clean(rel.get("chunk_id", default_chunk_id))

    if not chunk_id or not entity1 or not entity2 or not relation_type:
        return None

    return {
        "chunk_id": chunk_id,
        "file_id": _clean(file_id) or _clean(rel.get("file_id")),
        "file_version_id": _clean(file_version_id) or _clean(rel.get("file_version_id")),
        "file_name": _clean(file_name) or _clean(rel.get("file_name")),
        "is_active": bool(rel.get("is_active", is_active)),
        "entity1": entity1,
        "entity2": entity2,
        "relation_type": relation_type,
        "entity1_type": entity1_type,
        "entity2_type": entity2_type,
        "entity1_label": _entity_label(entity1_type),
        "entity2_label": _entity_label(entity2_type),
        "entity1_props": _sanitize_entity_props(rel.get("entity1_props"), fallback_chunk_id=chunk_id),
        "entity2_props": _sanitize_entity_props(rel.get("entity2_props"), fallback_chunk_id=chunk_id),
    }


def _group_flat_relations(
    relations: Iterable[Dict[str, Any]],
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    seen = set()

    for rel in relations:
        normalized = _normalize_relation(
            rel,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            is_active=is_active,
        )
        if not normalized:
            continue

        relation_key = (
            normalized["file_version_id"],
            normalized["chunk_id"],
            normalized["entity1"],
            normalized["entity2"],
            normalized["relation_type"],
            normalized["entity1_type"],
            normalized["entity2_type"],
        )
        if relation_key in seen:
            continue
        seen.add(relation_key)

        chunk_id = normalized.pop("chunk_id")
        bucket_key = f"{normalized.get('file_version_id', '')}::{chunk_id}"
        bucket = grouped.setdefault(
            bucket_key,
            {
                "chunk_id": chunk_id,
                "file_id": normalized.get("file_id") or _clean(file_id),
                "file_version_id": normalized.get("file_version_id") or _clean(file_version_id),
                "file_name": normalized.get("file_name") or _clean(file_name),
                "is_active": bool(normalized.get("is_active", is_active)),
                "relations": [],
            },
        )
        bucket["relations"].append(normalized)

    return list(grouped.values())


def _load_csv(
    path: Path,
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [field for field in REQUIRED_RELATION_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(missing)}")
        return _group_flat_relations(
            reader,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            is_active=is_active,
        )


def _load_json_or_jsonl(
    path: Path,
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    content = path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return []

    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            data = [parsed]
        elif isinstance(parsed, list):
            data = parsed
        else:
            raise ValueError("JSON root must be a list or object")
    except json.JSONDecodeError:
        data = []
        for line_no, line in enumerate(content.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc

    flat_relations = []
    for item in data:
        if not isinstance(item, dict):
            continue

        chunk_id = _clean(item.get("chunk_id"))
        nested_relations = item.get("relations") or item.get("relation")
        if isinstance(nested_relations, list):
            for rel in nested_relations:
                if isinstance(rel, dict):
                    copied = dict(rel)
                    copied.setdefault("chunk_id", chunk_id)
                    flat_relations.append(copied)
            continue

        if all(field in item for field in REQUIRED_RELATION_FIELDS):
            flat_relations.append(item)

    return _group_flat_relations(
        flat_relations,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        is_active=is_active,
    )


def load_relations(
    path: Path | str,
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    relation_path = Path(path)
    suffix = relation_path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(
            relation_path,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            is_active=is_active,
        )
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return _load_json_or_jsonl(
            relation_path,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            is_active=is_active,
        )
    raise ValueError(f"Unsupported relation file type: {relation_path.suffix}")


def load_json(
    path: Path | str,
    *,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    """Backward-compatible name used by main.py."""
    return load_relations(
        path,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        is_active=is_active,
    )


DROP_LEGACY_ENTITY_CONSTRAINT = "DROP CONSTRAINT entity_name_type_unique IF EXISTS"
DROP_LEGACY_CHUNK_CONSTRAINT = "DROP CONSTRAINT chunk_id_unique IF EXISTS"

CREATE_ENTITY_CONSTRAINT = """
CREATE CONSTRAINT entity_name_type_version_unique IF NOT EXISTS
FOR (e:Entity)
REQUIRE (e.name, e.entity_type, e.file_version_id) IS UNIQUE
"""

CREATE_CHUNK_CONSTRAINT = """
CREATE CONSTRAINT chunk_version_chunk_id_unique IF NOT EXISTS
FOR (c:Chunk)
REQUIRE (c.file_version_id, c.chunk_id) IS UNIQUE
"""

IMPORT_BATCH_CYPHER = """
UNWIND $rows AS row
MERGE (c:Chunk {file_version_id: row.file_version_id, chunk_id: row.chunk_id})
  ON CREATE SET c.created_at = datetime()
SET c.file_id = row.file_id,
    c.file_name = row.file_name,
    c.file_version_id = row.file_version_id,
    c.is_active = coalesce(row.is_active, true),
    c.updated_at = datetime()
WITH c, row
UNWIND row.relations AS rel
MERGE (e1:Entity {name: rel.entity1, entity_type: rel.entity1_type, file_version_id: row.file_version_id})
  ON CREATE SET e1.created_at = datetime()
SET e1.file_id = row.file_id,
    e1.file_version_id = row.file_version_id,
    e1.is_active = coalesce(row.is_active, true),
    e1 += rel.entity1_props,
    e1.updated_at = datetime()
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
    e2.is_active = coalesce(row.is_active, true),
    e2 += rel.entity2_props,
    e2.updated_at = datetime()
FOREACH (_ IN CASE WHEN rel.entity2_label = 'FaultPhenomenon' THEN [1] ELSE [] END | SET e2:FaultPhenomenon)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Component' THEN [1] ELSE [] END | SET e2:Component)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Parameter' THEN [1] ELSE [] END | SET e2:Parameter)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'System' THEN [1] ELSE [] END | SET e2:System)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Method' THEN [1] ELSE [] END | SET e2:Method)
FOREACH (_ IN CASE WHEN rel.entity2_label = 'Tool' THEN [1] ELSE [] END | SET e2:Tool)
MERGE (e1)-[r:RELATION {
  relation_type: rel.relation_type,
  chunk_id: row.chunk_id,
  entity1_type: rel.entity1_type,
  entity2_type: rel.entity2_type,
  file_version_id: row.file_version_id
}]->(e2)
  ON CREATE SET r.created_at = datetime()
SET r.file_id = row.file_id,
    r.file_version_id = row.file_version_id,
    r.is_active = coalesce(row.is_active, true),
    r.updated_at = datetime()
MERGE (e1)-[:MENTIONED_IN {file_version_id: row.file_version_id}]->(c)
MERGE (e2)-[:MENTIONED_IN {file_version_id: row.file_version_id}]->(c)
"""

DELETE_ALL_CYPHER = "MATCH (n) DETACH DELETE n"


def chunked(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def ensure_constraints(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        session.run(DROP_LEGACY_ENTITY_CONSTRAINT).consume()
        session.run(DROP_LEGACY_CHUNK_CONSTRAINT).consume()
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


def summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    relation_counts: Dict[str, int] = {}
    label_counts: Dict[str, int] = {}
    entity_keys = set()
    edge_count = 0

    for row in rows:
        for rel in row.get("relations", []):
            edge_count += 1
            relation_counts[rel["relation_type"]] = relation_counts.get(rel["relation_type"], 0) + 1
            for side in ("entity1", "entity2"):
                entity_keys.add((rel[side], rel.get(f"{side}_type", "")))
                label = rel.get(f"{side}_label") or "Entity"
                label_counts[label] = label_counts.get(label, 0) + 1

    return {
        "chunk_rows": len(rows),
        "entities": len(entity_keys),
        "relations": edge_count,
        "relation_counts": dict(sorted(relation_counts.items(), key=lambda item: (-item[1], item[0]))),
        "label_mentions": dict(sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))),
    }


def print_summary(driver: Any, database: str) -> None:
    with driver.session(database=database) as session:
        node_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        relation_breakdown = session.run(
            """
            MATCH ()-[r:RELATION]->()
            RETURN r.relation_type AS type, count(*) AS cnt
            ORDER BY cnt DESC, type ASC
            """
        ).data()
        label_breakdown = session.run(
            """
            MATCH (e:Entity)
            RETURN labels(e) AS labels, count(*) AS cnt
            ORDER BY cnt DESC
            """
        ).data()

    print(f"Nodes: {node_count}")
    print(f"Relationships: {rel_count}")
    print("RELATION breakdown:")
    for item in relation_breakdown:
        print(f"  - {item['type']}: {item['cnt']}")
    print("Entity label breakdown:")
    for item in label_breakdown:
        labels = [label for label in item["labels"] if label != "Entity"]
        print(f"  - {'/'.join(labels) or 'Entity'}: {item['cnt']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import knowledge graph relations into Neo4j")
    parser.add_argument("--file", required=True, help="Path to relation CSV/JSON/JSONL file")
    parser.add_argument("--uri", default="bolt://localhost:7687", help="Neo4j URI")
    parser.add_argument("--user", default="neo4j", help="Neo4j username")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Target Neo4j database")
    parser.add_argument("--batch-size", type=int, default=200, help="Chunk rows per write batch")
    parser.add_argument("--clear", action="store_true", help="Delete all existing graph data before import")
    parser.add_argument("--dry-run", action="store_true", help="Parse the file and print a summary without writing")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    relation_path = Path(args.file)
    if not relation_path.exists():
        print(f"File not found: {relation_path}", file=sys.stderr)
        return 1

    rows = load_relations(relation_path)
    if not rows:
        print("No valid relation rows found.", file=sys.stderr)
        return 1

    local_summary = summarize_rows(rows)
    print(json.dumps(local_summary, ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0

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
