"""Merge cleaned assistant answers back into original-format SFT rows.

The cleaning agent/human outputs one JSON per input row:
  {"id": 0, "cleaned_assistant": "[ENTITY]\\n...\\n\\n[RELATION]\\n...\\n\\n[LOGIC_GROUP]\\n..."}

This script rebuilds full rows (system/user unchanged, assistant replaced) so the result
can be validated and then converted with build_two_stage_datasets.py.
Rows without a cleaned answer (partial cleaning, e.g. only style-issue rows) keep their
original assistant answer.

Usage:
  python 2stages/entity_v2_tools/merge_cleaned_answers.py
    --input data_raw\\combined_train_sft_messages.jsonl
    --cleaned cleaned_answers.jsonl
    --out 2stages\\data\\entity_v2_clean\\combined_train_entity_v2_clean_original.jsonl
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import SECTION_NAMES, read_jsonl, split_messages, write_jsonl


KNOWN_HEADERS = {f"[{name}]" for name in SECTION_NAMES}


def strip_unknown_sections(text: str) -> str:
    """Drop any trailing part that starts with a bracketed header outside the
    three known sections (e.g. an echoed [RELATION_ENDPOINTS] block)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        upper = line.strip().upper()
        if upper.startswith("[") and upper.endswith("]") and upper not in KNOWN_HEADERS:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cleaned", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = read_jsonl(args.input)
    cleaned_by_id: dict[int, str] = {}
    for cleaned_row in read_jsonl(args.cleaned):
        row_id = cleaned_row.get("id")
        answer = cleaned_row.get("cleaned_assistant")
        if not isinstance(row_id, int) or not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"Invalid cleaned row: {cleaned_row}")
        cleaned_by_id[row_id] = answer

    missing = sorted(set(range(len(rows))) - set(cleaned_by_id))

    outputs: list[dict[str, Any]] = []
    for row_id, row in enumerate(rows):
        prompt_messages, gold = split_messages(row)
        content = strip_unknown_sections(cleaned_by_id.get(row_id, gold))
        outputs.append(
            {
                "messages": prompt_messages
                + [{"role": "assistant", "content": content}]
            }
        )

    write_jsonl(args.out, outputs)
    print(
        f"merged={len(outputs)} rows (cleaned={len(cleaned_by_id)}, "
        f"kept_original={len(rows) - len(cleaned_by_id)}) -> {args.out}"
    )
    if missing:
        print(f"WARNING: no cleaned answer for row ids: {missing[:20]} (total {len(missing)}); kept original for those")


if __name__ == "__main__":
    main()
