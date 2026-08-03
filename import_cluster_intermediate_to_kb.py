from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_local_env

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None  # type: ignore

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover
    MongoClient = None  # type: ignore


ENTITY_TYPE_MAP = {
    "故障事件": "FaultEvent",
    "故障类别": "FaultCategory",
    "报警码": "AlarmCode",
    "维修方法": "MaintenanceAction",
    "触发规则": "TriggerRule",
}
ENTITY_TYPE_ZH_MAP = {value: key for key, value in ENTITY_TYPE_MAP.items()}
RELATION_TYPE_MAP = {
    "故障触发": "CAUSES",
    "故障表征": "INDICATES",
    "故障分类": "BELONGS_TO_CATEGORY",
    "故障处理": "HANDLED_BY",
    "规则触发": "TRIGGERED_BY_RULE",
    "参与组合": "PARTICIPATES_IN",
    "组合导致": "COMBINATION_CAUSES",
}
RELATION_TYPE_ZH_MAP = {value: key for key, value in RELATION_TYPE_MAP.items()}


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def dedupe_keep_order(values: list[Any]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = clean_scalar(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def split_semicolon(value: Any) -> list[str]:
    if isinstance(value, list):
        return dedupe_keep_order(value)
    return dedupe_keep_order(str(value or "").split(";"))


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def cypher_ident(value: str) -> str:
    if "`" in value:
        raise ValueError(f"Invalid Neo4j identifier: {value}")
    return f"`{value}`"


def entity_label(entity_type: str) -> str:
    label = entity_type_code(entity_type)
    if not label:
        return ""
    return cypher_ident(label)


def entity_type_code(entity_type: str) -> str:
    normalized = clean_scalar(entity_type)
    return ENTITY_TYPE_MAP.get(normalized) or (normalized if normalized in ENTITY_TYPE_ZH_MAP else "")


def entity_type_zh(entity_type: str, explicit_zh: Any = "") -> str:
    explicit = clean_scalar(explicit_zh)
    if explicit:
        return explicit
    normalized = clean_scalar(entity_type)
    return ENTITY_TYPE_ZH_MAP.get(normalized) or normalized


def relation_type_name(level: str, relation_type: str) -> str:
    relation_code = relation_type_code(relation_type)
    if not relation_code:
        raise ValueError(f"Unsupported relation_type: {relation_type}")
    prefix = "MENTION" if level == "mention" else "CLUSTERED"
    return f"{prefix}_{relation_code}"


def relation_type_code(relation_type: str) -> str:
    normalized = clean_scalar(relation_type)
    return RELATION_TYPE_MAP.get(normalized) or (normalized if normalized in RELATION_TYPE_ZH_MAP else "")


def relation_type_zh(relation_type: str) -> str:
    normalized = clean_scalar(relation_type)
    return RELATION_TYPE_ZH_MAP.get(normalized) or normalized


def relation_ident(level: str, relation_type: str) -> str:
    return cypher_ident(relation_type_name(level, relation_type))


def infer_chunk_ids_from_evidence(evidence: list[dict[str, Any]]) -> list[str]:
    return dedupe_keep_order([item.get("chunk_id") for item in evidence if isinstance(item, dict)])


def neo4j_property_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
            return ["" if item is None else item for item in value]
        return json_dumps(value)
    if isinstance(value, dict):
        return json_dumps(value)
    return str(value)


def sanitize_neo4j_props(row: dict[str, Any]) -> dict[str, Any]:
    return {key: neo4j_property_value(value) for key, value in row.items()}


def normalize_chunk_row(chunk: dict[str, Any], *, file_id: str, file_version_id: str) -> dict[str, Any]:
    row = dict(chunk or {})
    chunk_id = clean_scalar(row.get("chunk_id") if row.get("chunk_id") not in (None, "") else row.get("id"))
    if not chunk_id:
        return {}
    row["id"] = clean_scalar(row.get("id")) or chunk_id
    row["chunk_id"] = chunk_id
    row["chunk_uid"] = clean_scalar(row.get("chunk_uid")) or f"{file_version_id}::{chunk_id}"
    row["file_id"] = file_id
    row["file_version_id"] = file_version_id

    body = ""
    for key in ("content", "text", "markdown", "raw_text", "page_content", "body"):
        value = row.get(key)
        if value not in (None, ""):
            body = str(value)
            break
    if body:
        row.setdefault("content", body)
        row.setdefault("text", body)
        row.setdefault("markdown", body)

    chunk_name = clean_scalar(row.get("chunk_name") or row.get("title") or row.get("heading") or row.get("chapter"))
    section_path = clean_scalar(row.get("section_path") or row.get("section") or row.get("chapter_id") or row.get("chapter"))
    chapter = clean_scalar(row.get("chapter") or row.get("section") or chunk_name)
    if chunk_name:
        row.setdefault("chunk_name", chunk_name)
        row.setdefault("chapter_title", clean_scalar(row.get("chapter_title")) or chunk_name)
    if section_path:
        row.setdefault("section_path", section_path)
        row.setdefault("chapter_id", clean_scalar(row.get("chapter_id")) or section_path)
    if chapter:
        row.setdefault("chapter", chapter)
    return sanitize_neo4j_props(row)


def collect_chunks(data: dict[str, Any], file_id: str, file_version_id: str) -> list[dict[str, Any]]:
    if isinstance(data.get("chunks"), list) and data.get("chunks"):
        rows = [
            row
            for row in (
                normalize_chunk_row(chunk, file_id=file_id, file_version_id=file_version_id)
                for chunk in data.get("chunks", [])
            )
            if row
        ]
        return rows

    chunk_ids: set[str] = set()
    for mention in data.get("mentions", []):
        chunk_ids.update(split_semicolon(mention.get("chunk_ids")))
        chunk_ids.update(infer_chunk_ids_from_evidence(mention.get("evidence") or []))
    for relation in data.get("raw_relations", []):
        chunk_ids.update(split_semicolon(relation.get("involved_chunk_ids")))
        chunk_ids.update(infer_chunk_ids_from_evidence(relation.get("evidence") or []))
    chunks = []
    for chunk_id in sorted(chunk_ids, key=lambda value: (not str(value).isdigit(), str(value))):
        chunks.append(
            {
                "chunk_uid": f"{file_version_id}::{chunk_id}",
                "file_id": file_id,
                "file_version_id": file_version_id,
                "chunk_id": chunk_id,
            }
        )
    return chunks


def merge_node_rows(session: Any, rows: list[dict[str, Any]], *, base_label: str, id_key: str, extra_label_key: str | None = None) -> None:
    if not rows:
        return
    if extra_label_key:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(clean_scalar(row.get(extra_label_key)), []).append(row)
        for label_value, label_rows in grouped.items():
            label = entity_label(label_value)
            if not label:
                label = ""
            session.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:{base_label}{':' + label if label else ''} {{{id_key}: row.{id_key}}})
                SET n += row.props
                """,
                rows=label_rows,
            ).consume()
        return
    session.run(
        f"""
        UNWIND $rows AS row
        MERGE (n:{base_label} {{{id_key}: row.{id_key}}})
        SET n += row.props
        """,
        rows=rows,
    ).consume()


def import_to_neo4j(data: dict[str, Any], *, file_id: str, file_version_id: str, file_name: str, clear_scope: bool) -> dict[str, int]:
    if GraphDatabase is None:
        raise RuntimeError("neo4j package is not installed")
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is empty")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    now = datetime.now(timezone.utc).isoformat()
    chunks = collect_chunks(data, file_id, file_version_id)
    mention_rows = []
    for mention in data.get("mentions", []):
        mention_id = clean_scalar(mention.get("mention_id"))
        if not mention_id:
            continue
        evidence = mention.get("evidence") or []
        chunk_ids = split_semicolon(mention.get("chunk_ids")) or infer_chunk_ids_from_evidence(evidence)
        mention_entity_type_code = entity_type_code(mention.get("entity_type"))
        mention_entity_type_zh = entity_type_zh(mention.get("entity_type"), mention.get("entity_type_zh"))
        mention_rows.append(
            {
                "mention_id": mention_id,
                "entity_type": mention_entity_type_code,
                "props": {
                    "mention_id": mention_id,
                    "file_id": file_id,
                    "file_version_id": file_version_id,
                    "source_file_ids": [file_id],
                    "source_file_version_ids": [file_version_id],
                    "sample_id": clean_scalar(mention.get("sample_id")),
                    "chapter_id": clean_scalar(mention.get("chapter_id")),
                    "source_type": clean_scalar(mention.get("source_type")),
                    "entity_type": mention_entity_type_code,
                    "entity_type_code": mention_entity_type_code,
                    "entity_type_zh": mention_entity_type_zh,
                    "mention": clean_scalar(mention.get("mention")),
                    "normalized_name": clean_scalar(mention.get("normalized_name")),
                    "chunk_ids": chunk_ids,
                    "neighbor_tokens": split_semicolon(mention.get("neighbor_tokens")) if isinstance(mention.get("neighbor_tokens"), str) else dedupe_keep_order(mention.get("neighbor_tokens") or []),
                    "evidence_json": json_dumps(evidence),
                    "cluster_id": clean_scalar(mention.get("cluster_id")),
                    "embedding_dim": int(mention.get("embedding_dim") or 0),
                    "updated_at": now,
                },
            }
        )
    cluster_rows = []
    for cluster in data.get("clusters", []):
        cluster_id = clean_scalar(cluster.get("cluster_id"))
        if not cluster_id:
            continue
        evidence = cluster.get("evidence") or []
        cluster_entity_type_code = entity_type_code(cluster.get("entity_type"))
        cluster_entity_type_zh = entity_type_zh(cluster.get("entity_type"), cluster.get("entity_type_zh"))
        cluster_rows.append(
            {
                "cluster_id": cluster_id,
                "entity_type": cluster_entity_type_code,
                "props": {
                    "cluster_id": cluster_id,
                    "source_file_ids": [file_id],
                    "source_file_version_ids": [file_version_id],
                    "entity_type": cluster_entity_type_code,
                    "entity_type_code": cluster_entity_type_code,
                    "entity_type_zh": cluster_entity_type_zh,
                    "canonical_name": clean_scalar(cluster.get("canonical_name")),
                    "name": clean_scalar(cluster.get("canonical_name")),
                    "aliases": dedupe_keep_order(cluster.get("aliases") or []),
                    "mention_ids": dedupe_keep_order(cluster.get("mention_ids") or []),
                    "mention_count": int(cluster.get("mention_count") or len(cluster.get("mention_ids") or [])),
                    "chunk_ids": split_semicolon(cluster.get("chunk_ids")) if isinstance(cluster.get("chunk_ids"), str) else dedupe_keep_order(cluster.get("chunk_ids") or infer_chunk_ids_from_evidence(evidence)),
                    "neighbor_tokens": split_semicolon(cluster.get("neighbor_tokens")) if isinstance(cluster.get("neighbor_tokens"), str) else dedupe_keep_order(cluster.get("neighbor_tokens") or []),
                    "merge_reasons": split_semicolon(cluster.get("merge_reasons")) if isinstance(cluster.get("merge_reasons"), str) else dedupe_keep_order(cluster.get("merge_reasons") or []),
                    "evidence_json": json_dumps(evidence),
                    "embedding_dim": int(cluster.get("embedding_dim") or 0),
                    "updated_at": now,
                },
            }
        )

    with driver.session(database=database) as session:
        session.run("DROP CONSTRAINT smartfta_file_version IF EXISTS").consume()
        session.run("DROP CONSTRAINT smartfta_chunk_uid IF EXISTS").consume()
        session.run("DROP CONSTRAINT smartfta_mention_id IF EXISTS").consume()
        session.run("DROP CONSTRAINT smartfta_cluster_id IF EXISTS").consume()
        session.run("CREATE CONSTRAINT fta_file_version IF NOT EXISTS FOR (n:File) REQUIRE n.file_version_id IS UNIQUE").consume()
        session.run("CREATE CONSTRAINT fta_chunk_uid IF NOT EXISTS FOR (n:Chunk) REQUIRE n.chunk_uid IS UNIQUE").consume()
        session.run("CREATE CONSTRAINT fta_mention_id IF NOT EXISTS FOR (n:Mention) REQUIRE n.mention_id IS UNIQUE").consume()
        session.run("CREATE CONSTRAINT fta_cluster_id IF NOT EXISTS FOR (n:EntityCluster) REQUIRE n.cluster_id IS UNIQUE").consume()
        if clear_scope:
            session.run(
                """
                MATCH (n)
                WHERE (
                    n.file_id = $file_id
                    AND coalesce(n.file_version_id, '') = $file_version_id
                )
                OR (
                    n:EntityCluster
                    AND $file_version_id IN (
                        CASE WHEN 'source_file_version_ids' IN keys(n) THEN n['source_file_version_ids']
                             WHEN 'file_version_id' IN keys(n) THEN [n['file_version_id']]
                             ELSE [] END
                    )
                )
                DETACH DELETE n
                """,
                file_id=file_id,
                file_version_id=file_version_id,
            ).consume()
        session.run(
            """
            MERGE (f:File {file_version_id: $file_version_id})
            SET f.file_id = $file_id,
                f.file_version_id = $file_version_id,
                f.file_name = $file_name,
                f.updated_at = $updated_at
            """,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            updated_at=now,
        ).consume()
        session.run(
            """
            UNWIND $rows AS row
            MERGE (c:Chunk {chunk_uid: row.chunk_uid})
            SET c += row
            WITH c
            MATCH (f:File {file_version_id: c.file_version_id})
            MERGE (f)-[:HAS_CHUNK]->(c)
            """,
            rows=chunks,
        ).consume()
        merge_node_rows(session, mention_rows, base_label="Mention", id_key="mention_id", extra_label_key="entity_type")
        merge_node_rows(session, cluster_rows, base_label="EntityCluster", id_key="cluster_id", extra_label_key="entity_type")
        session.run(
            """
            MATCH (e:EntityCluster)
            WHERE $file_version_id IN (
                CASE WHEN 'source_file_version_ids' IN keys(e) THEN e['source_file_version_ids']
                     WHEN 'file_version_id' IN keys(e) THEN [e['file_version_id']]
                     ELSE [] END
            )
            REMOVE e.file_id, e.file_version_id
            """,
            file_version_id=file_version_id,
        ).consume()
        session.run(
            """
            UNWIND $rows AS row
            MATCH (m:Mention {mention_id: row.mention_id})
            UNWIND row.chunk_ids AS chunk_id
            MATCH (c:Chunk {chunk_uid: $file_version_id + '::' + chunk_id})
            MERGE (m)-[r:EVIDENCED_IN]->(c)
            SET r.file_id = $file_id,
                r.file_version_id = $file_version_id,
                r.updated_at = $updated_at
            """,
            rows=[{"mention_id": row["mention_id"], "chunk_ids": row["props"]["chunk_ids"]} for row in mention_rows],
            file_id=file_id,
            file_version_id=file_version_id,
            updated_at=now,
        ).consume()
        session.run(
            """
            UNWIND $rows AS row
            MATCH (m:Mention {mention_id: row.mention_id})
            MATCH (e:EntityCluster {cluster_id: row.cluster_id})
            MERGE (m)-[r:RESOLVED_TO]->(e)
            SET r.file_id = $file_id,
                r.file_version_id = $file_version_id,
                r.updated_at = $updated_at
            """,
            rows=[
                {"mention_id": row["mention_id"], "cluster_id": row["props"]["cluster_id"]}
                for row in mention_rows
                if row["props"]["cluster_id"]
            ],
            file_id=file_id,
            file_version_id=file_version_id,
            updated_at=now,
        ).consume()

        raw_grouped: dict[str, list[dict[str, Any]]] = {}
        for rel in data.get("raw_relations", []):
            relation_type = clean_scalar(rel.get("relation_type"))
            relation_code = relation_type_code(relation_type)
            relation_zh = relation_type_zh(relation_type)
            if not relation_code:
                continue
            raw_grouped.setdefault(relation_type, []).append(
                {
                    "relation_id": clean_scalar(rel.get("relation_id")),
                    "source_id": clean_scalar(rel.get("source_mention_id")),
                    "target_id": clean_scalar(rel.get("target_mention_id")),
                    "props": {
                        "relation_id": clean_scalar(rel.get("relation_id")),
                        "file_id": file_id,
                        "file_version_id": file_version_id,
                        "source_file_ids": [file_id],
                        "source_file_version_ids": [file_version_id],
                        "relation_type": relation_code,
                        "relation_type_code": relation_code,
                        "relation_type_zh": relation_zh,
                        "relation_level": "mention",
                        "source_text": clean_scalar(rel.get("source_text")),
                        "target_text": clean_scalar(rel.get("target_text")),
                        "cross_chunk": clean_scalar(rel.get("cross_chunk")),
                        "involved_chunk_ids": split_semicolon(rel.get("involved_chunk_ids")),
                        "evidence_json": json_dumps(rel.get("evidence") or []),
                        "polarity": clean_scalar(rel.get("polarity")) or "positive",
                        "certainty": clean_scalar(rel.get("certainty")) or "certain",
                        "updated_at": now,
                    },
                }
            )
        for relation_type, rows in raw_grouped.items():
            session.run(
                f"""
                UNWIND $rows AS row
                MATCH (a:Mention {{mention_id: row.source_id}})
                MATCH (b:Mention {{mention_id: row.target_id}})
                MERGE (a)-[r:{relation_ident('mention', relation_type)} {{relation_id: row.relation_id}}]->(b)
                SET r += row.props
                """,
                rows=rows,
            ).consume()

        clustered_grouped: dict[str, list[dict[str, Any]]] = {}
        for index, rel in enumerate(data.get("clustered_relations", []), start=1):
            relation_type = clean_scalar(rel.get("relation_type"))
            relation_code = relation_type_code(relation_type)
            relation_zh = relation_type_zh(relation_type)
            if not relation_code:
                continue
            source_id = clean_scalar(rel.get("source_cluster_id"))
            target_id = clean_scalar(rel.get("target_cluster_id"))
            relation_id = f"{file_version_id}#clustered#{index}"
            clustered_grouped.setdefault(relation_type, []).append(
                {
                    "relation_id": relation_id,
                    "source_id": source_id,
                    "target_id": target_id,
                    "props": {
                        "relation_id": relation_id,
                        "file_id": file_id,
                        "file_version_id": file_version_id,
                        "source_file_ids": [file_id],
                        "source_file_version_ids": [file_version_id],
                        "relation_type": relation_code,
                        "relation_type_code": relation_code,
                        "relation_type_zh": relation_zh,
                        "relation_level": "clustered",
                        "cross_chunk": clean_scalar(rel.get("cross_chunk")),
                        "involved_chunk_ids": split_semicolon(rel.get("involved_chunk_ids")),
                        "evidence_json": json_dumps(rel.get("evidence") or []),
                        "source_relation_ids": dedupe_keep_order(rel.get("source_relation_ids") or []),
                        "polarity": clean_scalar(rel.get("polarity")) or "positive",
                        "certainty": clean_scalar(rel.get("certainty")) or "certain",
                        "updated_at": now,
                    },
                }
            )
        for relation_type, rows in clustered_grouped.items():
            session.run(
                f"""
                UNWIND $rows AS row
                MATCH (a:EntityCluster {{cluster_id: row.source_id}})
                MATCH (b:EntityCluster {{cluster_id: row.target_id}})
                MERGE (a)-[r:{relation_ident('clustered', relation_type)} {{relation_id: row.relation_id}}]->(b)
                SET r += row.props
                """,
                rows=rows,
            ).consume()
    driver.close()
    return {
        "files": 1,
        "chunks": len(chunks),
        "mentions": len(mention_rows),
        "clusters": len(cluster_rows),
        "raw_relations": len(data.get("raw_relations", [])),
        "clustered_relations": len(data.get("clustered_relations", [])),
    }


def top_event_doc_id(file_version_id: str, normalized_name: str) -> str:
    key = re.sub(r"\s+", "", normalized_name or "")
    return f"{file_version_id}::{key}"


def import_top_event_catalog(data: dict[str, Any], *, file_id: str, file_version_id: str, file_name: str, clear_scope: bool) -> dict[str, int]:
    if MongoClient is None:
        raise RuntimeError("pymongo package is not installed")
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    mongo_db_name = os.getenv("MONGO_DB_NAME", "fault-tree-trial")
    client = MongoClient(mongo_uri)
    db = client[mongo_db_name]
    now = datetime.now(timezone.utc)
    if clear_scope:
        db["top_event_catalog"].delete_many({"file_id": file_id, "file_version_id": file_version_id})
    clusters = [cluster for cluster in data.get("clusters", []) if entity_type_code(cluster.get("entity_type")) == "FaultEvent"]
    inserted_or_updated = 0
    for cluster in clusters:
        canonical_name = clean_scalar(cluster.get("canonical_name"))
        if not canonical_name:
            continue
        aliases = dedupe_keep_order(cluster.get("aliases") or [])
        source_chunk_ids = split_semicolon(cluster.get("chunk_ids")) if isinstance(cluster.get("chunk_ids"), str) else dedupe_keep_order(cluster.get("chunk_ids") or [])
        normalized_name = re.sub(r"\s+", "", canonical_name)
        doc = {
            "_id": top_event_doc_id(file_version_id, normalized_name),
            "file_id": file_id,
            "file_version_id": file_version_id,
            "file_name": file_name,
            "is_active": True,
            "name": canonical_name,
            "display_name": canonical_name,
            "normalized_name": normalized_name,
            "aliases": [alias for alias in aliases if alias != canonical_name],
            "normalized_aliases": [re.sub(r"\s+", "", alias) for alias in aliases if alias and alias != canonical_name],
            "source_chunk_ids": source_chunk_ids,
            "graph_node_id": clean_scalar(cluster.get("cluster_id")),
            "mention_count": int(cluster.get("mention_count") or len(cluster.get("mention_ids") or [])),
            "evidence_json": json_dumps(cluster.get("evidence") or []),
            "semantic_text": "；".join(dedupe_keep_order([canonical_name] + aliases)),
            "updated_at": now,
        }
        db["top_event_catalog"].update_one(
            {"_id": doc["_id"]},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        inserted_or_updated += 1
    client.close()
    return {"top_event_catalog": inserted_or_updated}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import SmartFTA clustered intermediate artifact into Neo4j and MongoDB.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--intermediate-json", required=True)
    parser.add_argument("--file-id", default="")
    parser.add_argument("--file-version-id", default="")
    parser.add_argument("--file-name", default="")
    parser.add_argument("--skip-neo4j", action="store_true")
    parser.add_argument("--skip-mongo", action="store_true")
    parser.add_argument("--no-clear-neo4j", action="store_true")
    parser.add_argument("--no-clear-mongo", action="store_true")
    args = parser.parse_args()

    load_local_env(args.env_file, override=True)
    path = Path(args.intermediate_json)
    data = json.loads(path.read_text(encoding="utf-8"))
    file_id = clean_scalar(args.file_id) or clean_scalar(data.get("file_id"))
    if not file_id:
        raise ValueError("file_id is required")
    file_version_id = clean_scalar(args.file_version_id) or f"{file_id}_v1"
    file_name = clean_scalar(args.file_name) or file_id

    stats: dict[str, Any] = {
        "file_id": file_id,
        "file_version_id": file_version_id,
        "file_name": file_name,
    }
    if not args.skip_neo4j:
        stats["neo4j"] = import_to_neo4j(
            data,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            clear_scope=not args.no_clear_neo4j,
        )
    if not args.skip_mongo:
        stats["mongo"] = import_top_event_catalog(
            data,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            clear_scope=not args.no_clear_mongo,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
