"""
Import chunk data into MongoDB.

Supports:
- standard JSON array/object files
- JSON Lines files where each line is a chunk object
"""

import argparse
import json

from database import import_chunks


def _load_chunks(file_path: str):
    with open(file_path, "r", encoding="utf-8") as f:
        raw_text = f.read().strip()

    if not raw_text:
        return []

    try:
        loaded = json.loads(raw_text)
        if isinstance(loaded, list):
            chunks = loaded
        elif isinstance(loaded, dict):
            chunks = [loaded]
        else:
            raise ValueError("Unsupported JSON root type")
    except json.JSONDecodeError:
        chunks = []
        for line_no, line in enumerate(raw_text.splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            try:
                chunks.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Failed to parse line {line_no} as JSON: {exc}") from exc

    normalized_chunks = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        normalized_chunk = dict(chunk)
        if "entities" not in normalized_chunk and isinstance(normalized_chunk.get("entity"), list):
            normalized_chunk["entities"] = normalized_chunk["entity"]
        normalized_chunks.append(normalized_chunk)

    return normalized_chunks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import chunks into MongoDB")
    parser.add_argument("--file", required=True, help="Path to the chunk JSON or JSONL file")
    args = parser.parse_args()

    chunks = _load_chunks(args.file)
    import_chunks(chunks)
    print(f"Imported {len(chunks)} chunks successfully")
