#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import dotenv_values
from neo4j import GraphDatabase
from pymongo import ASCENDING, MongoClient

ROOT = Path(__file__).resolve().parent
ENV = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT.parent / "generate-fta" / ".env")}
MONGO_URI = ENV.get("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = ENV.get("MONGO_DB_NAME") or "fault-tree-trial"
NEO4J_URI = ENV.get("NEO4J_URI") or "bolt://localhost:7687"
NEO4J_USER = ENV.get("NEO4J_USER") or "neo4j"
NEO4J_PASSWORD = ENV.get("NEO4J_PASSWORD") or "password"
NEO4J_DATABASE = ENV.get("NEO4J_DATABASE") or "neo4j"
SEP = "::"


def norm(value: Any) -> str:
    return str(value or "").strip()


def make_ref(file_version_id: Any, chunk_id: Any) -> str:
    version = norm(file_version_id)
    chunk = norm(chunk_id)
    return f"{version}{SEP}{chunk}" if version and chunk else chunk


def split_ref(value: Any) -> Tuple[str, str]:
    text = norm(value)
    if SEP not in text:
        return "", text
    version, chunk = text.split(SEP, 1)
    return norm(version), norm(chunk)


def ensure_catalog_unique_index(collection) -> None:
    keys = [("file_version_id", ASCENDING), ("normalized_name", ASCENDING)]
    duplicate_keys = []
    seen = set()
    for doc in collection.find({}, {"file_version_id": 1, "normalized_name": 1}):
        key = (norm(doc.get("file_version_id")), norm(doc.get("normalized_name")))
        if key in seen:
            duplicate_keys.append(key)
        seen.add(key)
    if duplicate_keys:
        raise RuntimeError(
            "top_event_catalog contains duplicate version/name keys: "
            + ", ".join(repr(key) for key in sorted(set(duplicate_keys))[:10])
        )

    for index in collection.list_indexes():
        if list(index["key"].items()) != keys:
            continue
        if index.get("unique"):
            return
        collection.drop_index(index["name"])
    collection.create_index(keys, unique=True)

def repair_catalog(db, apply: bool) -> Dict[str, int]:
    changed = 0
    for doc in db.top_event_catalog.find({}, {"file_version_id": 1, "source_chunk_ids": 1}):
        file_version_id = norm(doc.get("file_version_id"))
        refs = [make_ref(file_version_id, split_ref(v)[1]) for v in doc.get("source_chunk_ids") or [] if norm(v)]
        refs = sorted(set(r for r in refs if r))
        if refs != (doc.get("source_chunk_ids") or []):
            changed += 1
            if apply:
                db.top_event_catalog.update_one({"_id": doc["_id"]}, {"$set": {"source_chunk_ids": refs}})
    if apply:
        try:
            db.top_event_catalog.drop_index("name_1")
        except Exception:
            pass
        ensure_catalog_unique_index(db.top_event_catalog)
    return {"updated_source_refs": changed}


def repair_neo4j(apply: bool) -> Dict[str, Any]:
    if not apply:
        return {"status": "skipped_dry_run"}
    cypher = [
        "DROP CONSTRAINT entity_name_type_unique IF EXISTS",
        "DROP CONSTRAINT chunk_id_unique IF EXISTS",
        "CREATE CONSTRAINT entity_name_type_version_unique IF NOT EXISTS FOR (e:Entity) REQUIRE (e.name, e.entity_type, e.file_version_id) IS UNIQUE",
        "CREATE CONSTRAINT chunk_uid_unique IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_uid IS UNIQUE",
        "MATCH (c:Chunk) WHERE c.chunk_uid IS NULL AND c.file_version_id IS NOT NULL AND c.chunk_id IS NOT NULL SET c.chunk_uid = c.file_version_id + '::' + toString(c.chunk_id), c.is_active = coalesce(c.is_active, true)",
        "MATCH (e:Entity) WHERE e.file_version_id IS NOT NULL AND e.source_chunk_ids IS NOT NULL SET e.source_chunk_refs = [x IN e.source_chunk_ids | CASE WHEN toString(x) CONTAINS '::' THEN toString(x) ELSE e.file_version_id + '::' + toString(x) END], e.is_active = coalesce(e.is_active, true)",
        "MATCH ()-[r:RELATION]->() WHERE r.file_version_id IS NOT NULL AND r.chunk_id IS NOT NULL SET r.source_chunk_ids = coalesce(r.source_chunk_ids, [toString(r.chunk_id)]), r.source_chunk_refs = [x IN coalesce(r.source_chunk_ids, [toString(r.chunk_id)]) | CASE WHEN toString(x) CONTAINS '::' THEN toString(x) ELSE r.file_version_id + '::' + toString(x) END], r.is_active = coalesce(r.is_active, true)",
        "MATCH (e:Entity)-[r:MENTIONED_IN]->(c:Chunk) SET r.file_id = coalesce(r.file_id, e.file_id, c.file_id), r.file_version_id = coalesce(r.file_version_id, e.file_version_id, c.file_version_id), r.chunk_id = coalesce(r.chunk_id, c.chunk_id), r.is_active = coalesce(r.is_active, e.is_active, c.is_active, true) WITH r WHERE r.file_version_id IS NOT NULL AND r.chunk_id IS NOT NULL SET r.source_chunk_ids = coalesce(r.source_chunk_ids, [toString(r.chunk_id)]), r.source_chunk_refs = [x IN coalesce(r.source_chunk_ids, [toString(r.chunk_id)]) | CASE WHEN toString(x) CONTAINS '::' THEN toString(x) ELSE r.file_version_id + '::' + toString(x) END]",
    ]
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        with driver.session(database=NEO4J_DATABASE) as session:
            for stmt in cypher:
                session.run(stmt).consume()
    finally:
        driver.close()
    return {"status": "updated", "database": NEO4J_DATABASE}


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair versioned KB metadata in MongoDB and Neo4j")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    args = parser.parse_args()
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    result = {
        "mode": "apply" if args.apply else "dry-run",
        "mongo_db": MONGO_DB,
        "top_event_catalog": repair_catalog(db, args.apply),
        "neo4j": repair_neo4j(args.apply),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())