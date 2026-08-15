"""Validate cleaned entity_v2 rows before building SFT data and training.

Checks:
  - JSONL is valid; every row has system/user/assistant
  - [ENTITY] lines have at least 4 fields
  - entity type is one of the 5 allowed types
  - [RELATION] lines have at least 3 fields and a valid relation type
  - every relation source/target appears in entity normalized_name
    (mention-only coverage is reported as a warning unless --allow-mention)
  - [LOGIC_GROUP] logic_type is AND; members/result are covered by entities

Exit code is 1 when strict violations exist, so it can gate training.

Usage:
  python 2stages/entity_v2_tools/validate_entity_v2_clean.py
    --input 2stages\\data\\entity_v2_clean\\combined_train_entity_v2_clean_original.jsonl
    --out-json outputs\\entity_v2_clean\\validation_report.json
    --out-md outputs\\entity_v2_clean\\validation_report.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    ALLOWED_ENTITY_TYPES,
    ALLOWED_RELATION_TYPES,
    entity_coverage_sets,
    parse_entities,
    parse_logic_groups,
    parse_relations,
    read_jsonl,
    split_messages,
    write_jsonl,
)


def validate_row(row_id: int, gold: str, allow_mention: bool) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    entities = parse_entities(gold)
    relations = parse_relations(gold)
    logic_groups = parse_logic_groups(gold)
    normalized, mentions = entity_coverage_sets(entities)

    for entity in entities:
        if len(entity["line"].split("|")) < 4:
            issues.append(
                {
                    "row_id": row_id,
                    "level": "error",
                    "section": "ENTITY",
                    "message": f"entity line has < 4 fields: {entity['line'][:120]}",
                }
            )
        if entity["type"] not in ALLOWED_ENTITY_TYPES:
            issues.append(
                {
                    "row_id": row_id,
                    "level": "error",
                    "section": "ENTITY",
                    "message": f"invalid entity type '{entity['type']}': {entity['line'][:120]}",
                }
            )

    for relation in relations:
        if len(relation["line"].split("|")) < 3:
            issues.append(
                {
                    "row_id": row_id,
                    "level": "error",
                    "section": "RELATION",
                    "message": f"relation line has < 3 fields: {relation['line'][:120]}",
                }
            )
        if relation["relation_type"] not in ALLOWED_RELATION_TYPES:
            issues.append(
                {
                    "row_id": row_id,
                    "level": "error",
                    "section": "RELATION",
                    "message": f"invalid relation type '{relation['relation_type']}': {relation['line'][:120]}",
                }
            )
        for role, endpoint in (("source", relation["source"]), ("target", relation["target"])):
            covered = endpoint in normalized or (allow_mention and endpoint in mentions)
            if not covered:
                issues.append(
                    {
                        "row_id": row_id,
                        "level": "error",
                        "section": "RELATION",
                        "message": f"relation {role} '{endpoint}' not in entity normalized_name: {relation['line'][:160]}",
                    }
                )
            elif endpoint not in normalized and endpoint in mentions:
                issues.append(
                    {
                        "row_id": row_id,
                        "level": "warning",
                        "section": "RELATION",
                        "message": f"relation {role} '{endpoint}' covered via mention only (not normalized_name)",
                    }
                )

    for group in logic_groups:
        if group["logic_type"] != "AND":
            issues.append(
                {
                    "row_id": row_id,
                    "level": "error",
                    "section": "LOGIC_GROUP",
                    "message": f"invalid logic_type '{group['logic_type']}' (expected AND): {group['line'][:120]}",
                }
            )
        members = [item.strip() for item in group["members"].split(";") if item.strip()]
        for member in members:
            covered = member in normalized or (allow_mention and member in mentions)
            if not covered:
                issues.append(
                    {
                        "row_id": row_id,
                        "level": "error",
                        "section": "LOGIC_GROUP",
                        "message": f"logic member '{member}' not in entity normalized_name: {group['line'][:160]}",
                    }
                )
        result = group["result"]
        covered = result in normalized or (allow_mention and result in mentions)
        if not covered:
            issues.append(
                {
                    "row_id": row_id,
                    "level": "error",
                    "section": "LOGIC_GROUP",
                    "message": f"logic result '{result}' not in entity normalized_name: {group['line'][:160]}",
                }
            )

    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--allow-mention", action="store_true", help="Accept relation endpoints covered via mention only")
    args = parser.parse_args()

    rows = read_jsonl(args.input)
    all_issues: list[dict[str, Any]] = []
    error_rows: list[int] = []
    total_relations = 0
    covered_relations = 0

    for row_id, row in enumerate(rows):
        _, gold = split_messages(row)
        issues = validate_row(row_id, gold, allow_mention=args.allow_mention)
        all_issues.extend(issues)
        errors = [issue for issue in issues if issue["level"] == "error"]
        if errors:
            error_rows.append(row_id)

        entities = parse_entities(gold)
        relations = parse_relations(gold)
        normalized, mentions = entity_coverage_sets(entities)
        for relation in relations:
            total_relations += 1
            if relation["source"] in normalized and relation["target"] in normalized:
                covered_relations += 1
            elif args.allow_mention and relation["source"] in normalized | mentions and relation["target"] in normalized | mentions:
                covered_relations += 1

    errors = [issue for issue in all_issues if issue["level"] == "error"]
    warnings = [issue for issue in all_issues if issue["level"] == "warning"]
    report = {
        "rows": len(rows),
        "error_rows": len(error_rows),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "gold_relations": total_relations,
        "relation_endpoint_possible": covered_relations,
        "passed": len(errors) == 0,
        "issues": all_issues,
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# Entity V2 Clean Validation",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| rows | {len(rows)} |",
        f"| error_rows | {len(error_rows)} |",
        f"| error_count | {len(errors)} |",
        f"| warning_count | {len(warnings)} |",
        f"| gold_relations | {total_relations} |",
        f"| relation_endpoint_possible | {covered_relations} |",
        f"| passed | {report['passed']} |",
        "",
        "## Errors (first 50)",
        "",
    ]
    for issue in errors[:50]:
        md_lines.append(f"- row {issue['row_id']} [{issue['section']}] {issue['message']}")
    md_lines += ["", "## Warnings (first 50)", ""]
    for issue in warnings[:50]:
        md_lines.append(f"- row {issue['row_id']} [{issue['section']}] {issue['message']}")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "issues"}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        print("VALIDATION FAILED: fix errors before building SFT data.")
        raise SystemExit(1)
    print("VALIDATION PASSED.")


if __name__ == "__main__":
    main()
