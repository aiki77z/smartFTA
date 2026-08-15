"""Analyze gold-relation endpoint coverage against gold entities in original SFT data.

This measures the label-level ceiling before cleaning: how many gold relations have
both source and target present in the gold [ENTITY] normalized_name set. Rows that
block relations are the cleaning work queue (Pass 1 order).

Usage:
  python 2stages/entity_v2_tools/analyze_endpoint_coverage.py
    --input data_raw\\combined_train_sft_messages.jsonl
    --out-json outputs\\entity_v2_clean\\coverage_report.json
    --out-md outputs\\entity_v2_clean\\coverage_report.md
    --queue outputs\\entity_v2_clean\\cleaning_queue.jsonl
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

from common import (
    entity_coverage_sets,
    parse_entities,
    parse_relations,
    read_jsonl,
    split_messages,
    write_jsonl,
)


def endpoint_status(source: str, target: str, normalized: set[str], mentions: set[str]) -> dict[str, Any]:
    source_normalized = source in normalized
    target_normalized = target in normalized
    source_mention = not source_normalized and source in mentions
    target_mention = not target_normalized and target in mentions
    return {
        "source": source,
        "target": target,
        "missing_source": not (source_normalized or source_mention),
        "missing_target": not (target_normalized or target_mention),
        "source_normalized": source_normalized,
        "target_normalized": target_normalized,
        "source_mention_only": source_mention,
        "target_mention_only": target_mention,
    }


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_relations = 0
    covered_normalized = 0
    covered_loose = 0
    row_details: list[dict[str, Any]] = []
    blocked_counts: Counter = Counter()

    for row_id, row in enumerate(rows):
        _, gold = split_messages(row)
        entities = parse_entities(gold)
        relations = parse_relations(gold)
        normalized, mentions = entity_coverage_sets(entities)

        blocked: list[dict[str, Any]] = []
        for relation in relations:
            total_relations += 1
            status = endpoint_status(relation["source"], relation["target"], normalized, mentions)
            strict_ok = status["source_normalized"] and status["target_normalized"]
            loose_ok = not (status["missing_source"] or status["missing_target"])
            if strict_ok:
                covered_normalized += 1
            if loose_ok:
                covered_loose += 1
            if not loose_ok:
                blocked.append(
                    {
                        "relation_type": relation["relation_type"],
                        **status,
                    }
                )
        if blocked:
            blocked_counts[row_id] = len(blocked)
        row_details.append(
            {
                "row_id": row_id,
                "gold_entities": len(entities),
                "gold_relations": len(relations),
                "blocked_relations": blocked,
                "needs_cleaning": bool(blocked),
            }
        )

    summary = {
        "rows": len(rows),
        "gold_relations": total_relations,
        "relation_endpoint_possible_normalized": covered_normalized,
        "relation_endpoint_possible_loose": covered_loose,
        "relation_endpoint_blocked": total_relations - covered_loose,
        "relation_endpoint_possible_recall_ceiling_normalized": (
            covered_normalized / total_relations if total_relations else 0.0
        ),
        "rows_needing_cleaning": sum(1 for detail in row_details if detail["needs_cleaning"]),
        "rows_blocking_most_relations": [
            {"row_id": row_id, "blocked_gold_relations": count}
            for row_id, count in blocked_counts.most_common(20)
        ],
    }
    return {"summary": summary, "rows": row_details}


def markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    rows = report["rows"]
    lines = [
        "# Gold Relation Endpoint Coverage (Label-Level Baseline)",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| rows | {summary['rows']} |",
        f"| gold_relations | {summary['gold_relations']} |",
        f"| relation_endpoint_possible (normalized) | {summary['relation_endpoint_possible_normalized']} |",
        f"| relation_endpoint_possible (loose, mention fallback) | {summary['relation_endpoint_possible_loose']} |",
        f"| relation_endpoint_blocked | {summary['relation_endpoint_blocked']} |",
        f"| recall ceiling (normalized) | {summary['relation_endpoint_possible_recall_ceiling_normalized']:.4f} |",
        f"| rows_needing_cleaning | {summary['rows_needing_cleaning']} |",
        "",
        "## Rows Blocking The Most Gold Relations",
        "",
        "| row_id | blocked_gold_relations |",
        "|---|---:|",
    ]
    for item in summary["rows_blocking_most_relations"]:
        lines.append(f"| {item['row_id']} | {item['blocked_gold_relations']} |")
    lines += [
        "",
        "## Blocked Relation Examples (top 30)",
        "",
        "| row_id | source | relation_type | target | missing_source | missing_target |",
        "|---|---|---|---|---|---|",
    ]
    shown = 0
    for detail in rows:
        for blocked in detail["blocked_relations"]:
            lines.append(
                "| {row_id} | {source} | {relation_type} | {target} | {ms} | {mt} |".format(
                    row_id=detail["row_id"],
                    source=blocked["source"].replace("|", "/"),
                    relation_type=blocked["relation_type"],
                    target=blocked["target"].replace("|", "/"),
                    ms="Y" if blocked["missing_source"] else "",
                    mt="Y" if blocked["missing_target"] else "",
                )
            )
            shown += 1
            if shown >= 30:
                break
        if shown >= 30:
            break
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args()

    rows = read_jsonl(args.input)
    report = analyze(rows)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(markdown_report(report), encoding="utf-8")

    queue = [detail for detail in report["rows"] if detail["needs_cleaning"]]
    queue.sort(key=lambda item: len(item["blocked_relations"]), reverse=True)
    write_jsonl(args.queue, queue)

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"queue={len(queue)} rows -> {args.queue}")


if __name__ == "__main__":
    main()
