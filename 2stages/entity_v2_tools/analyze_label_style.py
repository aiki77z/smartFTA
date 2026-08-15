"""Scan gold labels for style issues that entity_v2 normalization rules target.

Endpoint coverage is usually already satisfied in gold labels; this script finds the
remaining label-level work: entity lines with missing fields, normalized_name carrying
measurements/alarm values, overlong descriptions, and duplicate normalized_names.

Usage:
  python 2stages/entity_v2_tools/analyze_label_style.py
    --input data_raw\\combined_train_sft_messages.jsonl
    --out-json outputs\\entity_v2_clean\\train_style_report.json
    --out-md outputs\\entity_v2_clean\\train_style_report.md
    --queue outputs\\entity_v2_clean\\train_style_queue.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    parse_entities,
    parse_logic_groups,
    parse_relations,
    read_jsonl,
    split_messages,
    write_jsonl,
)


UNIT = r"(mm|cm|m|kg|kg/s|kgf|MPa|kPa|Pa|bar|kV|V|A|Hz|rpm|℃|°C|度|秒|s|min|分钟|小时|%)"


def scan_row(row_id: int, gold: str) -> dict[str, Any]:
    entities = parse_entities(gold)
    relations = parse_relations(gold)
    logic_groups = parse_logic_groups(gold)

    issues: list[dict[str, str]] = []
    seen_normalized: set[str] = set()
    entity_count = 0

    for entity in entities:
        entity_count += 1
        n = entity["normalized_name"]
        field_count = len(entity["line"].split("|"))
        if field_count < 4:
            issues.append({"kind": "short_fields", "detail": entity["line"][:120]})
        if "=" in n:
            issues.append({"kind": "alarm_code_with_value", "detail": n[:120]})
        if has_measure(n):
            issues.append({"kind": "measurement_in_normalized", "detail": n[:120]})
        if len(n) > 24:
            issues.append({"kind": "overlong_normalized", "detail": n[:120]})
        if n in seen_normalized:
            issues.append({"kind": "duplicate_normalized", "detail": n[:120]})
        seen_normalized.add(n)

    return {
        "row_id": row_id,
        "gold_entities": entity_count,
        "gold_relations": len(relations),
        "gold_logic_groups": len(logic_groups),
        "issues": issues,
        "needs_attention": bool(issues),
    }


def has_measure(text: str) -> bool:
    import re

    pattern = re.compile(r"\d+\s*" + UNIT, re.I)
    return pattern.search(text) is not None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args()

    rows = read_jsonl(args.input)
    details: list[dict[str, Any]] = []
    totals = {
        "rows": len(rows),
        "entities": 0,
        "short_fields": 0,
        "alarm_code_with_value": 0,
        "measurement_in_normalized": 0,
        "overlong_normalized": 0,
        "duplicate_normalized": 0,
        "rows_needing_attention": 0,
    }

    for row_id, row in enumerate(rows):
        _, gold = split_messages(row)
        detail = scan_row(row_id, gold)
        details.append(detail)
        totals["entities"] += detail["gold_entities"]
        if detail["needs_attention"]:
            totals["rows_needing_attention"] += 1
        for issue in detail["issues"]:
            totals[issue["kind"]] = totals.get(issue["kind"], 0) + 1

    report = {"summary": totals, "rows": details}
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# Entity V2 Label Style Scan",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for key, value in totals.items():
        md.append(f"| {key} | {value} |")
    md += ["", "## Rows With Most Style Issues (top 20)", "", "| row_id | entities | relations | issues |", "|---|---:|---:|---:|"]
    top = sorted([d for d in details if d["needs_attention"]], key=lambda d: len(d["issues"]), reverse=True)[:20]
    for d in top:
        md.append(f"| {d['row_id']} | {d['gold_entities']} | {d['gold_relations']} | {len(d['issues'])} |")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(md) + "\n", encoding="utf-8")

    queue = [d for d in details if d["needs_attention"]]
    queue.sort(key=lambda d: len(d["issues"]), reverse=True)
    write_jsonl(args.queue, queue)

    print(json.dumps(totals, ensure_ascii=False, indent=2))
    print(f"queue={len(queue)} rows -> {args.queue}")


if __name__ == "__main__":
    main()
