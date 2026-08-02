from __future__ import annotations

import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

CURRENT_DIR = Path(__file__).resolve().parent
KB_ROOT = CURRENT_DIR.parent
if str(KB_ROOT) not in sys.path:
    sys.path.insert(0, str(KB_ROOT))

from env_loader import load_local_env  # noqa: E402

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover
    MongoClient = None  # type: ignore


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def normalize_file_name(value: str) -> str:
    return re.sub(r"\s+", " ", clean_scalar(value)).strip().lower()


def parse_version_no(file_version_id: str, file_id: str) -> int:
    match = re.fullmatch(re.escape(file_id) + r"_v(\d+)", file_version_id)
    if match:
        return int(match.group(1))
    match = re.search(r"_v(\d+)$", file_version_id)
    return int(match.group(1)) if match else 1


def chunk_uid(file_version_id: str, chunk_id: str) -> str:
    return f"{file_version_id}::{chunk_id}"


def make_chunk_doc(
    *,
    file_id: str,
    file_version_id: str,
    file_name: str,
    chunk_id: str,
    text: str,
    row: dict[str, Any],
    text_field: str,
    now: datetime,
) -> dict[str, Any]:
    numeric_id = int(chunk_id) if str(chunk_id).isdigit() else chunk_id
    return {
        "_id": chunk_uid(file_version_id, chunk_id),
        "chunk_uid": chunk_uid(file_version_id, chunk_id),
        "file_id": file_id,
        "file_version_id": file_version_id,
        "file_name": file_name,
        "chunk_id": chunk_id,
        "id": numeric_id,
        "text": text,
        "markdown": text,
        "chapter_id": clean_scalar(row.get("chapter_id")),
        "chapter": clean_scalar(row.get("chapter_id")),
        "chapter_title": clean_scalar(row.get("chapter_id")),
        "source_type": clean_scalar(row.get("source_type")),
        "sample_id": clean_scalar(row.get("sample_id")),
        "text_field": text_field,
        "is_active": True,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "annotation_sample_id": clean_scalar(row.get("sample_id")),
            "import_source": "annotation_csv",
        },
    }


def load_chunks_from_csv(input_csv: Path, file_id: str, file_version_id: str, file_name: str) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    chunks_by_uid: dict[str, dict[str, Any]] = {}
    with input_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row_file_id = clean_scalar(row.get("file_id"))
            if file_id and row_file_id != file_id:
                continue
            target_chunk_id = clean_scalar(row.get("target_chunk_id"))
            target_text = clean_scalar(row.get("text"))
            if target_chunk_id and target_text:
                doc = make_chunk_doc(
                    file_id=file_id,
                    file_version_id=file_version_id,
                    file_name=file_name,
                    chunk_id=target_chunk_id,
                    text=target_text,
                    row=row,
                    text_field="text",
                    now=now,
                )
                chunks_by_uid[doc["_id"]] = doc
            context_chunk_id = clean_scalar(row.get("context_chunk_id"))
            context_text = clean_scalar(row.get("context_text"))
            if context_chunk_id and context_text:
                doc = make_chunk_doc(
                    file_id=file_id,
                    file_version_id=file_version_id,
                    file_name=file_name,
                    chunk_id=context_chunk_id,
                    text=context_text,
                    row=row,
                    text_field="context_text",
                    now=now,
                )
                chunks_by_uid.setdefault(doc["_id"], doc)
    return list(chunks_by_uid.values())


def import_to_mongo(chunks: list[dict[str, Any]], *, file_id: str, file_version_id: str, file_name: str, source_type: str, clear_scope: bool) -> dict[str, Any]:
    if MongoClient is None:
        raise RuntimeError("pymongo package is not installed")
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    mongo_db_name = os.getenv("MONGO_DB_NAME", "fault-tree-trial")
    client = MongoClient(mongo_uri)
    db = client[mongo_db_name]
    now = datetime.now(timezone.utc)
    version_no = parse_version_no(file_version_id, file_id)
    db["files"].update_one(
        {"_id": file_id},
        {
            "$set": {
                "file_id": file_id,
                "name": file_name,
                "normalized_name": normalize_file_name(file_name),
                "status": "active",
                "source": source_type or "annotation_csv",
                "current_file_version_id": file_version_id,
                "latest_version_no": version_no,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    db["file_versions"].update_one(
        {"_id": file_version_id},
        {
            "$set": {
                "file_version_id": file_version_id,
                "file_id": file_id,
                "file_name": file_name,
                "normalized_file_name": normalize_file_name(file_name),
                "version_no": version_no,
                "status": "active",
                "is_active": True,
                "source": source_type or "annotation_csv",
                "metadata": {"import_source": "annotation_csv"},
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    if clear_scope:
        db["chunks"].delete_many({"file_id": file_id, "file_version_id": file_version_id})
    if chunks:
        for chunk in chunks:
            db["chunks"].update_one({"_id": chunk["_id"]}, {"$set": chunk}, upsert=True)
    client.close()
    return {
        "mongo_db": mongo_db_name,
        "file_id": file_id,
        "file_version_id": file_version_id,
        "chunks": len(chunks),
        "clear_scope": clear_scope,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Temporary helper: import annotation CSV source chunks into MongoDB.")
    parser.add_argument("--env-file", default=str(KB_ROOT / ".env"))
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--file-version-id", default="")
    parser.add_argument("--file-name", default="")
    parser.add_argument("--source-type", default="annotation_csv")
    parser.add_argument("--no-clear", action="store_true")
    args = parser.parse_args()

    load_local_env(args.env_file, override=True)
    file_id = clean_scalar(args.file_id)
    file_version_id = clean_scalar(args.file_version_id) or f"{file_id}_v1"
    file_name = clean_scalar(args.file_name) or file_id
    chunks = load_chunks_from_csv(Path(args.input_csv), file_id, file_version_id, file_name)
    stats = import_to_mongo(
        chunks,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        source_type=clean_scalar(args.source_type),
        clear_scope=not args.no_clear,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
