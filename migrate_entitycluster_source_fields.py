from __future__ import annotations

import argparse
import json
import os
from typing import Any

from env_loader import load_local_env

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None  # type: ignore


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def build_scope_filter(file_id: str, file_version_id: str) -> tuple[str, dict[str, str]]:
    clauses = []
    params: dict[str, str] = {}
    if file_id:
        clauses.append(
            """
            (
              ('file_id' IN keys(e) AND coalesce(e['file_id'], '') = $file_id)
              OR ('source_file_ids' IN keys(e) AND $file_id IN e['source_file_ids'])
            )
            """
        )
        params["file_id"] = file_id
    if file_version_id:
        clauses.append(
            """
            (
              ('file_version_id' IN keys(e) AND coalesce(e['file_version_id'], '') = $file_version_id)
              OR ('source_file_version_ids' IN keys(e) AND $file_version_id IN e['source_file_version_ids'])
            )
            """
        )
        params["file_version_id"] = file_version_id
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def count_candidates(session: Any, *, file_id: str, file_version_id: str) -> int:
    scope_filter, params = build_scope_filter(file_id, file_version_id)
    record = session.run(
        f"""
        MATCH (e:EntityCluster)
        WHERE (
          'file_id' IN keys(e)
          OR 'file_version_id' IN keys(e)
          OR NOT 'source_file_ids' IN keys(e)
          OR NOT 'source_file_version_ids' IN keys(e)
        ){scope_filter}
        RETURN count(e) AS count
        """,
        **params,
    ).single()
    return int(record["count"]) if record else 0


def sample_candidates(session: Any, *, file_id: str, file_version_id: str, limit: int) -> list[dict[str, Any]]:
    scope_filter, params = build_scope_filter(file_id, file_version_id)
    params["limit"] = max(1, int(limit or 20))
    rows = session.run(
        f"""
        MATCH (e:EntityCluster)
        WHERE (
          'file_id' IN keys(e)
          OR 'file_version_id' IN keys(e)
          OR NOT 'source_file_ids' IN keys(e)
          OR NOT 'source_file_version_ids' IN keys(e)
        ){scope_filter}
        RETURN
          e.cluster_id AS cluster_id,
          coalesce(e.canonical_name, e.name, '') AS name,
          CASE WHEN 'file_id' IN keys(e) THEN e['file_id'] ELSE null END AS old_file_id,
          CASE WHEN 'file_version_id' IN keys(e) THEN e['file_version_id'] ELSE null END AS old_file_version_id,
          CASE WHEN 'source_file_ids' IN keys(e) THEN e['source_file_ids'] ELSE [] END AS source_file_ids,
          CASE WHEN 'source_file_version_ids' IN keys(e) THEN e['source_file_version_ids'] ELSE [] END AS source_file_version_ids
        LIMIT $limit
        """,
        **params,
    ).data()
    return rows


def migrate(session: Any, *, file_id: str, file_version_id: str) -> int:
    scope_filter, params = build_scope_filter(file_id, file_version_id)
    record = session.run(
        f"""
        MATCH (e:EntityCluster)
        WHERE (
          'file_id' IN keys(e)
          OR 'file_version_id' IN keys(e)
          OR NOT 'source_file_ids' IN keys(e)
          OR NOT 'source_file_version_ids' IN keys(e)
        ){scope_filter}
        WITH e,
          CASE WHEN 'source_file_ids' IN keys(e) THEN e['source_file_ids'] ELSE [] END AS existing_file_ids,
          CASE WHEN 'source_file_version_ids' IN keys(e) THEN e['source_file_version_ids'] ELSE [] END AS existing_version_ids,
          CASE WHEN 'file_id' IN keys(e) AND e['file_id'] IS NOT NULL THEN [e['file_id']] ELSE [] END AS old_file_ids,
          CASE WHEN 'file_version_id' IN keys(e) AND e['file_version_id'] IS NOT NULL THEN [e['file_version_id']] ELSE [] END AS old_version_ids
        WITH e,
          [x IN existing_file_ids + old_file_ids WHERE x IS NOT NULL AND trim(toString(x)) <> ''] AS raw_file_ids,
          [x IN existing_version_ids + old_version_ids WHERE x IS NOT NULL AND trim(toString(x)) <> ''] AS raw_version_ids
        WITH e,
          reduce(acc = [], x IN raw_file_ids | CASE WHEN x IN acc THEN acc ELSE acc + x END) AS merged_file_ids,
          reduce(acc = [], x IN raw_version_ids | CASE WHEN x IN acc THEN acc ELSE acc + x END) AS merged_version_ids
        SET e.source_file_ids = merged_file_ids,
            e.source_file_version_ids = merged_version_ids,
            e.updated_at = datetime()
        REMOVE e.file_id, e.file_version_id
        RETURN count(e) AS migrated
        """,
        **params,
    ).single()
    return int(record["migrated"]) if record else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate EntityCluster.file_id/file_version_id into source_file_ids/source_file_version_ids."
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--file-id", default="", help="Optional scope filter.")
    parser.add_argument("--file-version-id", default="", help="Optional scope filter.")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true", help="Actually modify Neo4j. Without this flag it only reports.")
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

    file_id = clean_scalar(args.file_id)
    file_version_id = clean_scalar(args.file_version_id)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as session:
        before = count_candidates(session, file_id=file_id, file_version_id=file_version_id)
        samples_before = sample_candidates(
            session,
            file_id=file_id,
            file_version_id=file_version_id,
            limit=args.sample_limit,
        )
        migrated = 0
        if args.apply:
            migrated = migrate(session, file_id=file_id, file_version_id=file_version_id)
        after = count_candidates(session, file_id=file_id, file_version_id=file_version_id)
        samples_after = sample_candidates(
            session,
            file_id=file_id,
            file_version_id=file_version_id,
            limit=args.sample_limit,
        )
    driver.close()

    print(
        json.dumps(
            {
                "neo4j_database": database,
                "file_id": file_id,
                "file_version_id": file_version_id,
                "apply": bool(args.apply),
                "candidate_count_before": before,
                "migrated": migrated,
                "candidate_count_after": after,
                "samples_before": samples_before,
                "samples_after": samples_after,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
