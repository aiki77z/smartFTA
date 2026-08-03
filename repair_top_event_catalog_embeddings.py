from __future__ import annotations

import argparse
import json
from typing import Any

from pymongo import MongoClient

from top_event_catalog_utils import (
    build_top_event_embedding_fields,
    build_top_event_semantic_text,
    load_top_event_embedding_env,
    normalize_catalog_name,
)


def clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def build_query(file_id: str, file_version_id: str, missing_only: bool) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    if file_id:
        clauses.append({"$or": [{"file_id": file_id}, {"source_file_ids": file_id}]})
    if file_version_id:
        clauses.append({"$or": [{"file_version_id": file_version_id}, {"source_file_version_ids": file_version_id}]})
    if missing_only:
        clauses.append(
            {
                "$or": [
                    {"embedding": {"$exists": False}},
                    {"embedding": None},
                    {"embedding": []},
                    {"embedding_model": {"$exists": False}},
                    {"semantic_text": {"$exists": False}},
                ]
            }
        )
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def repair_catalog_embeddings(*, env_file: str, file_id: str, file_version_id: str, missing_only: bool, dry_run: bool) -> dict[str, Any]:
    load_top_event_embedding_env(env_file)

    import os

    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
    db = client[os.getenv("MONGO_DB_NAME", "fault-tree-trial")]
    col = db["top_event_catalog"]

    query = build_query(file_id, file_version_id, missing_only)
    total = 0
    updated = 0
    failed = 0
    embedding_dims: dict[str, int] = {}
    for doc in col.find(query):
        total += 1
        name = str(doc.get("name") or doc.get("display_name") or "").strip()
        normalized_name = normalize_catalog_name(doc.get("normalized_name") or name)
        aliases = clean_list(doc.get("aliases")) + clean_list(doc.get("normalized_aliases"))
        semantic_text = build_top_event_semantic_text(name, normalized_name=normalized_name, aliases=aliases)
        payload = build_top_event_embedding_fields(semantic_text, existing=doc)
        embedding = payload.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            failed += 1
            continue
        embedding_dims[str(len(embedding))] = embedding_dims.get(str(len(embedding)), 0) + 1
        if dry_run:
            continue
        col.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "semantic_text": payload.get("semantic_text"),
                    "embedding": embedding,
                    "embedding_model": payload.get("embedding_model"),
                    "embedding_updated_at": payload.get("embedding_updated_at"),
                }
            },
        )
        updated += 1

    client.close()
    return {
        "mongo_db": db.name,
        "query": query,
        "total": total,
        "updated": updated,
        "failed": failed,
        "dry_run": dry_run,
        "embedding_dims": embedding_dims,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill MongoDB top_event_catalog semantic_text and embedding fields.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--file-id", default="")
    parser.add_argument("--file-version-id", default="")
    parser.add_argument("--all", action="store_true", help="Refresh every matched document, not only documents missing embedding.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = repair_catalog_embeddings(
        env_file=args.env_file,
        file_id=args.file_id,
        file_version_id=args.file_version_id,
        missing_only=not args.all,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

