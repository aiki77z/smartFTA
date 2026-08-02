from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from env_loader import load_local_env

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None  # type: ignore


ALLOWED_NODE_LABELS = {"EntityCluster", "Mention"}


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def cypher_label(value: str) -> str:
    if value not in ALLOWED_NODE_LABELS:
        raise ValueError(f"Unsupported node label: {value}. Allowed: {sorted(ALLOWED_NODE_LABELS)}")
    return f"`{value}`"


def build_scope_filter(file_id: str, file_version_id: str) -> tuple[str, dict[str, str]]:
    clauses = []
    params = {}
    if file_id:
        clauses.append("coalesce(r.file_id, n.file_id, '') = $file_id")
        params["file_id"] = file_id
    if file_version_id:
        clauses.append("coalesce(r.file_version_id, n.file_version_id, '') = $file_version_id")
        params["file_version_id"] = file_version_id
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def count_self_loops(session: Any, *, node_label: str, relation_level: str, file_id: str, file_version_id: str) -> list[dict[str, Any]]:
    label = cypher_label(node_label)
    scope_filter, params = build_scope_filter(file_id, file_version_id)
    level_filter = ""
    if relation_level:
        level_filter = " AND coalesce(r.relation_level, '') = $relation_level"
        params["relation_level"] = relation_level
    query = f"""
    MATCH (n:{label})-[r]->(n)
    WHERE true{scope_filter}{level_filter}
    RETURN type(r) AS relation_type, count(r) AS count
    ORDER BY relation_type
    """
    return session.run(query, **params).data()


def sample_self_loops(session: Any, *, node_label: str, relation_level: str, file_id: str, file_version_id: str, limit: int) -> list[dict[str, Any]]:
    label = cypher_label(node_label)
    scope_filter, params = build_scope_filter(file_id, file_version_id)
    params["limit"] = limit
    level_filter = ""
    if relation_level:
        level_filter = " AND coalesce(r.relation_level, '') = $relation_level"
        params["relation_level"] = relation_level
    query = f"""
    MATCH (n:{label})-[r]->(n)
    WHERE true{scope_filter}{level_filter}
    RETURN
      coalesce(n.cluster_id, n.mention_id, id(n)) AS node_id,
      coalesce(n.canonical_name, n.mention, n.name, '') AS node_name,
      labels(n) AS labels,
      type(r) AS relation_type,
      properties(r) AS relation_props
    LIMIT $limit
    """
    return session.run(query, **params).data()


def delete_self_loops(session: Any, *, node_label: str, relation_level: str, file_id: str, file_version_id: str) -> int:
    label = cypher_label(node_label)
    scope_filter, params = build_scope_filter(file_id, file_version_id)
    level_filter = ""
    if relation_level:
        level_filter = " AND coalesce(r.relation_level, '') = $relation_level"
        params["relation_level"] = relation_level
    query = f"""
    MATCH (n:{label})-[r]->(n)
    WHERE true{scope_filter}{level_filter}
    WITH collect(r) AS rels
    FOREACH (rel IN rels | DELETE rel)
    RETURN size(rels) AS deleted
    """
    record = session.run(query, **params).single()
    return int(record["deleted"]) if record else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean Neo4j self-loop relationships in SmartFTA graph.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--file-id", default="")
    parser.add_argument("--file-version-id", default="")
    parser.add_argument("--node-label", choices=sorted(ALLOWED_NODE_LABELS), default="EntityCluster")
    parser.add_argument(
        "--relation-level",
        default="clustered",
        help="Default only deletes clustered semantic self-loops. Use empty string to include all relation levels.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()

    if GraphDatabase is None:
        raise RuntimeError("neo4j package is not installed")

    load_local_env(args.env_file, override=True)
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is empty")

    relation_level = clean_scalar(args.relation_level)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as session:
        before = count_self_loops(
            session,
            node_label=args.node_label,
            relation_level=relation_level,
            file_id=clean_scalar(args.file_id),
            file_version_id=clean_scalar(args.file_version_id),
        )
        samples = sample_self_loops(
            session,
            node_label=args.node_label,
            relation_level=relation_level,
            file_id=clean_scalar(args.file_id),
            file_version_id=clean_scalar(args.file_version_id),
            limit=args.sample_limit,
        )
        deleted = 0
        after = before
        if not args.dry_run:
            deleted = delete_self_loops(
                session,
                node_label=args.node_label,
                relation_level=relation_level,
                file_id=clean_scalar(args.file_id),
                file_version_id=clean_scalar(args.file_version_id),
            )
            after = count_self_loops(
                session,
                node_label=args.node_label,
                relation_level=relation_level,
                file_id=clean_scalar(args.file_id),
                file_version_id=clean_scalar(args.file_version_id),
            )
    driver.close()

    print(
        json.dumps(
            {
                "database": database,
                "file_id": clean_scalar(args.file_id),
                "file_version_id": clean_scalar(args.file_version_id),
                "node_label": args.node_label,
                "relation_level": relation_level,
                "dry_run": bool(args.dry_run),
                "before": before,
                "sample_self_loops": samples,
                "deleted": deleted,
                "after": after,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
