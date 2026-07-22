import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "ai_annotation_draft.csv"
DEFAULT_OUTPUT = BASE_DIR / "ai_annotation_draft_offsets_fixed.csv"
DEFAULT_REPORT = BASE_DIR / "evidence_offset_report.json"

JSON_COLUMNS = ["entities_json", "context_entities_json", "target_entities_json", "relations_json", "logic_groups_json"]


def parse_json_cell(value: Any) -> list[dict[str, Any]]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def parse_position(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def normalize_text_field(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"context", "context_text", "previous", "prev"}:
        return "context_text"
    return "text"


def parse_legacy_evidence(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    text = str(value or "").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass

    items = []
    for piece in text.split(";;"):
        piece = piece.strip()
        if not piece:
            continue
        parts = piece.split("::", 4)
        if len(parts) == 5:
            items.append(
                {
                    "chunk_id": parts[0],
                    "text_field": parts[1],
                    "start": parse_position(parts[2]),
                    "end": parse_position(parts[3]),
                    "text": parts[4],
                }
            )
        elif len(parts) == 3:
            items.append({"chunk_id": parts[0], "text_field": parts[1], "start": None, "end": None, "text": parts[2]})
        else:
            items.append({"chunk_id": "", "text_field": "", "start": None, "end": None, "text": piece})
    return items


def find_span(source_text: str, evidence_text: str) -> tuple[int | None, int | None, int]:
    if not evidence_text:
        return None, None, 0
    first = source_text.find(evidence_text)
    if first < 0:
        return None, None, 0
    count = source_text.count(evidence_text)
    return first, first + len(evidence_text), count


def check_existing_span(source_text: str, evidence_text: str, start: int | None, end: int | None) -> bool:
    if start is None or end is None or start < 0 or end < start or end > len(source_text):
        return False
    return source_text[start:end] == evidence_text


def fix_evidence_list(
    evidence_items: list[dict[str, Any]],
    context_text: str,
    target_text: str,
    stats: dict[str, Any],
    sample_id: str,
    owner: str,
) -> list[dict[str, Any]]:
    fixed_items = []
    for index, item in enumerate(evidence_items):
        fixed = dict(item)
        evidence_text = str(fixed.get("text") or "")
        field = normalize_text_field(fixed.get("text_field"))
        source_text = context_text if field == "context_text" else target_text
        start = parse_position(fixed.get("start"))
        end = parse_position(fixed.get("end"))

        stats["total"] += 1
        stats["by_field"].setdefault(field, {"total": 0, "correct": 0, "fixed": 0, "not_found": 0})
        stats["by_field"][field]["total"] += 1

        if check_existing_span(source_text, evidence_text, start, end):
            fixed["start"] = start
            fixed["end"] = end
            stats["correct"] += 1
            stats["by_field"][field]["correct"] += 1
            fixed_items.append(fixed)
            continue

        new_start, new_end, match_count = find_span(source_text, evidence_text)
        if new_start is not None and new_end is not None:
            fixed["start"] = new_start
            fixed["end"] = new_end
            stats["fixed"] += 1
            stats["by_field"][field]["fixed"] += 1
            if match_count > 1:
                stats["ambiguous"] += 1
            stats["errors"].append(
                {
                    "sample_id": sample_id,
                    "owner": owner,
                    "evidence_index": index,
                    "status": "fixed",
                    "old_start": start,
                    "old_end": end,
                    "new_start": new_start,
                    "new_end": new_end,
                    "match_count": match_count,
                    "text": evidence_text,
                }
            )
        else:
            fixed["start"] = None
            fixed["end"] = None
            stats["not_found"] += 1
            stats["by_field"][field]["not_found"] += 1
            stats["errors"].append(
                {
                    "sample_id": sample_id,
                    "owner": owner,
                    "evidence_index": index,
                    "status": "not_found",
                    "old_start": start,
                    "old_end": end,
                    "text": evidence_text,
                }
            )
        fixed["text_field"] = field
        fixed_items.append(fixed)
    return fixed_items


def fix_records(records: list[dict[str, Any]], context_text: str, target_text: str, stats: dict[str, Any], sample_id: str, column: str) -> list[dict[str, Any]]:
    fixed_records = []
    for record_index, record in enumerate(records):
        fixed_record = dict(record)
        evidence = parse_legacy_evidence(fixed_record.get("evidence"))
        if evidence:
            fixed_record["evidence"] = fix_evidence_list(
                evidence,
                context_text,
                target_text,
                stats,
                sample_id,
                f"{column}[{record_index}]",
            )
        fixed_records.append(fixed_record)
    return fixed_records


def build_empty_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "correct": 0,
        "fixed": 0,
        "not_found": 0,
        "ambiguous": 0,
        "by_field": {},
        "errors": [],
    }


def add_rates(stats: dict[str, Any]) -> dict[str, Any]:
    total = stats["total"]
    stats["accuracy_before_fix"] = stats["correct"] / total if total else None
    stats["error_rate_before_fix"] = (total - stats["correct"]) / total if total else None
    stats["repairable_rate"] = stats["fixed"] / total if total else None
    stats["not_found_rate"] = stats["not_found"] / total if total else None
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and fix evidence start/end offsets in annotation CSV.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-json", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    df = pd.read_csv(args.input_csv, dtype=str, keep_default_na=False)
    stats = build_empty_stats()

    for row_index, row in df.iterrows():
        sample_id = str(row.get("sample_id") or row_index)
        context_text = str(row.get("context_text") or "")
        target_text = str(row.get("text") or "")
        for column in JSON_COLUMNS:
            if column not in df.columns:
                continue
            records = parse_json_cell(row.get(column, "[]"))
            fixed_records = fix_records(records, context_text, target_text, stats, sample_id, column)
            df.at[row_index, column] = json.dumps(fixed_records, ensure_ascii=False)

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")

    report = add_rates(stats)
    report_path = Path(args.report_json)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Input CSV: {args.input_csv}")
    print(f"Output CSV: {output_path}")
    print(f"Report JSON: {report_path}")
    print(f"Evidence total: {report['total']}")
    print(f"Correct before fix: {report['correct']}")
    print(f"Fixed: {report['fixed']}")
    print(f"Not found: {report['not_found']}")
    if report["accuracy_before_fix"] is not None:
        print(f"Accuracy before fix: {report['accuracy_before_fix']:.2%}")
        print(f"Error rate before fix: {report['error_rate_before_fix']:.2%}")


if __name__ == "__main__":
    main()
