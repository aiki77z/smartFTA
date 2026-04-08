"""
Import aggregated entity reverse-index data into MongoDB.
"""

import argparse
import json

from database import import_entity_reverse_index


def _load_entries(file_path: str):
    with open(file_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    if isinstance(loaded, list):
        entries = loaded
    elif isinstance(loaded, dict):
        entries = [loaded]
    else:
        raise ValueError("Unsupported JSON root type")

    normalized_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        normalized_entry = dict(entry)
        chunk_ids = normalized_entry.get("chunk_ids") or []
        normalized_entry["chunk_ids"] = [chunk_id for chunk_id in chunk_ids if chunk_id not in (None, "")]
        normalized_entries.append(normalized_entry)

    return normalized_entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import aggregated entity reverse-index into MongoDB")
    parser.add_argument("--file", required=True, help="Path to the aggregated entity JSON file")
    args = parser.parse_args()

    entries = _load_entries(args.file)
    import_entity_reverse_index(entries)
    print(f"Imported {len(entries)} reverse-index entries successfully")
