"""Generate per-row cleaning inputs for the entity_v2 label cleanup task.

Each output row contains the original SFT row (system/user/assistant gold), the parsed
gold relation endpoints, and a ready-to-use cleaning prompt following
entity_v2_annotation_plan.md. The output can be fed to a GPT cleaning agent or a human
annotator; the cleaned answers are then merged back with merge_cleaned_answers.py.

Usage:
  python 2stages/entity_v2_tools/generate_cleaning_inputs.py
    --input data_raw\\combined_train_sft_messages.jsonl
    --rules-file 2stages\\entity_v2_normalization_rules.md
    --out-jsonl 2stages\\data\\entity_v2_clean\\cleaning_inputs.jsonl
    --only-blocked
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import (
    parse_entities,
    parse_relations,
    read_jsonl,
    render_row,
    split_messages,
    write_jsonl,
)


PROMPT_TEMPLATE = """You are cleaning SmartFTA entity labels.

Read the normalization rules below.
Then read one SFT row containing system, user, and assistant gold answer.

Task:
- Rewrite only the [ENTITY] section in the assistant answer.
- Keep [RELATION] and [LOGIC_GROUP] lines exactly unchanged EXCEPT:
  if you change an entity's normalized_name and that name is used as a relation
  source/target, update only the corresponding source/target field to the new
  normalized_name. Do not modify any other relation/logic field.
- Ensure every relation source/target appears as an entity normalized_name.
- Preserve the original three-section format.
- Output only the cleaned assistant answer.

[NORMALIZATION_RULES]
{rules}

{row}

[RELATION_ENDPOINT_CHECKLIST]
{endpoints}
"""

AGGRESSIVE_NOTE = """[AGGRESSIVE_MODE]
Actively apply the normalization rules to EVERY entity line, not only obvious errors:
- If normalized_name contains measurement values/units, verbose filler, or is longer
  than mention, rewrite it to the compact canonical form consistent with the gold style.
- Keep object identity and meaningful qualifiers (device IDs, fault phenomenon, action
  + object). Do not over-abstract into generic categories.
- If you rewrite a normalized_name, update the corresponding relation source/target
  references to the new name (only those fields; keep all other fields unchanged).
"""


def build_endpoint_list(relations: list[dict[str, str]]) -> list[str]:
    endpoints: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for relation in relations:
        key = (relation["source"], relation["relation_type"], relation["target"])
        if key in seen:
            continue
        seen.add(key)
        endpoints.append(
            f"source: {relation['source']} | relation_type: {relation['relation_type']} | target: {relation['target']}"
        )
    return endpoints


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--rules-file", type=Path, required=True)
    parser.add_argument("--out-jsonl", type=Path, required=True)
    parser.add_argument("--only-blocked", action="store_true", help="Only emit rows that block gold relations")
    parser.add_argument(
        "--rows-file",
        type=Path,
        help="JSONL with row_id fields; only emit rows whose id is in this file "
        "(e.g. the style queue from analyze_label_style.py)",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Append aggressive-mode instructions that actively canonicalize every "
        "normalized_name instead of only fixing obvious issues.",
    )
    args = parser.parse_args()

    rules = args.rules_file.read_text(encoding="utf-8").strip()
    rows = read_jsonl(args.input)
    selected_ids: set[int] | None = None
    if args.rows_file:
        selected_ids = {
            row["row_id"] for row in read_jsonl(args.rows_file) if isinstance(row.get("row_id"), int)
        }

    outputs: list[dict[str, Any]] = []
    for row_id, row in enumerate(rows):
        if selected_ids is not None and row_id not in selected_ids:
            continue
        prompt_messages, gold = split_messages(row)
        system = next((m["content"] for m in prompt_messages if m["role"] == "system"), "")
        user = next((m["content"] for m in prompt_messages if m["role"] == "user"), "")
        entities = parse_entities(gold)
        relations = parse_relations(gold)
        normalized = {entity["normalized_name"] for entity in entities}
        mentions = {entity["mention"] for entity in entities}

        blocked_endpoints = []
        for relation in relations:
            status = {
                "source": relation["source"],
                "target": relation["target"],
                "missing_source": relation["source"] not in normalized and relation["source"] not in mentions,
                "missing_target": relation["target"] not in normalized and relation["target"] not in mentions,
                "source_normalized": relation["source"] in normalized,
                "target_normalized": relation["target"] in normalized,
            }
            if status["missing_source"] or status["missing_target"]:
                blocked_endpoints.append(
                    {
                        "relation_type": relation["relation_type"],
                        **status,
                    }
                )

        needs_cleaning = bool(blocked_endpoints)
        if args.only_blocked and not needs_cleaning:
            continue

        rendered_row = render_row(system, user, gold)
        endpoint_lines = build_endpoint_list(relations)
        endpoint_block = "\n".join(endpoint_lines) if endpoint_lines else "(no relations in this row)"
        prompt = PROMPT_TEMPLATE.format(rules=rules, row=rendered_row, endpoints=endpoint_block)
        if args.aggressive:
            prompt = prompt.rstrip() + "\n\n" + AGGRESSIVE_NOTE.strip()

        outputs.append(
            {
                "id": row_id,
                "needs_cleaning": needs_cleaning,
                "gold_entities": entities,
                "gold_relations": relations,
                "relation_endpoints": endpoint_lines,
                "blocked_endpoints": blocked_endpoints,
                "system": system,
                "user": user,
                "gold_assistant": gold,
                "prompt": prompt,
            }
        )

    write_jsonl(args.out_jsonl, outputs)
    mode = "aggressive" if args.aggressive else "gentle"
    print(
        f"rows={len(rows)} selected_ids={len(selected_ids) if selected_ids is not None else 'all'} "
        f"mode={mode} cleaning_inputs={len(outputs)} -> {args.out_jsonl}"
    )


if __name__ == "__main__":
    main()
