"""Build entity-only and relation-only SFT datasets from the combined dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")


ENTITY_STAGE_NOTE = """

Two-stage extraction mode: ENTITY stage.
Only extract the [ENTITY] section. Keep [RELATION] and [LOGIC_GROUP] headers empty.
""".strip()


RELATION_STAGE_NOTE = """

Two-stage extraction mode: RELATION stage.
Use the provided [ENTITY_CANDIDATES] block as the closed entity set.
The source, target, members, and result fields must exactly copy a candidate normalized_name.
Do not create relations involving entities outside [ENTITY_CANDIDATES].
Keep [ENTITY] empty and output only [RELATION] and [LOGIC_GROUP] content.
""".strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_optional_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8").strip()


def split_messages(row: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    messages = row.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Each row must contain a messages list")

    prompt_messages: list[dict[str, str]] = []
    gold = ""
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role in {"system", "user"}:
            prompt_messages.append({"role": role, "content": content})
        elif role == "assistant" and not gold:
            gold = content
    if not gold:
        raise ValueError("Each row must contain an assistant gold answer")
    return prompt_messages, gold


def split_sections(text: str) -> dict[str, list[str]]:
    sections = {name: [] for name in SECTION_NAMES}
    current: str | None = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper in {f"[{name}]" for name in SECTION_NAMES}:
            current = upper.strip("[]")
            continue
        if current is not None and not is_header_line(current, line):
            sections[current].append(line)
    return sections


def is_header_line(section: str, line: str) -> bool:
    normalized = "".join(part.strip().lower() for part in line.split("|"))
    headers = {
        "ENTITY": "mentiontypenormalized_nameevidence",
        "RELATION": "sourcerelation_typetargetcross_chunkinvolved_chunk_idsevidencepolaritycertainty",
        "LOGIC_GROUP": "logic_typemembersresultinvolved_chunk_idsevidence",
    }
    return normalized == headers[section]


def render_answer(
    entity_lines: list[str] | None = None,
    relation_lines: list[str] | None = None,
    logic_lines: list[str] | None = None,
) -> str:
    entity = "\n".join(entity_lines or [])
    relation = "\n".join(relation_lines or [])
    logic = "\n".join(logic_lines or [])
    return f"[ENTITY]\n{entity}\n\n[RELATION]\n{relation}\n\n[LOGIC_GROUP]\n{logic}".strip()


def candidate_block(entity_lines: list[str]) -> str:
    rows = ["[ENTITY_CANDIDATES]", "normalized_name | type | mention"]
    for line in entity_lines:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        mention, entity_type, normalized_name = fields[:3]
        normalized_name = normalized_name or mention
        rows.append(f"{normalized_name} | {entity_type} | {mention}")
    return "\n".join(rows)


def add_system_note(messages: list[dict[str, str]], note: str) -> list[dict[str, str]]:
    updated: list[dict[str, str]] = []
    added = False
    for message in messages:
        if message["role"] == "system" and not added:
            updated.append({"role": "system", "content": message["content"].rstrip() + "\n\n" + note})
            added = True
        else:
            updated.append(dict(message))
    if not added:
        updated.insert(0, {"role": "system", "content": note})
    return updated


def add_user_suffix(messages: list[dict[str, str]], suffix: str) -> list[dict[str, str]]:
    updated = [dict(message) for message in messages]
    for idx in range(len(updated) - 1, -1, -1):
        if updated[idx]["role"] == "user":
            updated[idx]["content"] = updated[idx]["content"].rstrip() + "\n\n" + suffix
            return updated
    updated.append({"role": "user", "content": suffix})
    return updated


def build_rows(
    input_rows: list[dict[str, Any]],
    entity_rules: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entity_rows: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    entity_note = ENTITY_STAGE_NOTE if not entity_rules else ENTITY_STAGE_NOTE + "\n\n" + entity_rules

    for row in input_rows:
        prompt_messages, gold = split_messages(row)
        sections = split_sections(gold)

        entity_messages = add_system_note(prompt_messages, entity_note)
        entity_rows.append(
            {
                "messages": entity_messages
                + [{"role": "assistant", "content": render_answer(entity_lines=sections["ENTITY"])}]
            }
        )

        relation_messages = add_system_note(prompt_messages, RELATION_STAGE_NOTE)
        relation_messages = add_user_suffix(relation_messages, candidate_block(sections["ENTITY"]))
        relation_rows.append(
            {
                "messages": relation_messages
                + [
                    {
                        "role": "assistant",
                        "content": render_answer(
                            relation_lines=sections["RELATION"],
                            logic_lines=sections["LOGIC_GROUP"],
                        ),
                    }
                ]
            }
        )

    return entity_rows, relation_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-input", type=Path, required=True)
    parser.add_argument("--test-input", type=Path, required=True)
    parser.add_argument("--train-entity-output", type=Path, required=True)
    parser.add_argument("--train-relation-output", type=Path, required=True)
    parser.add_argument("--test-entity-output", type=Path, required=True)
    parser.add_argument("--test-relation-output", type=Path, required=True)
    parser.add_argument(
        "--entity-rules-file",
        type=Path,
        help="Optional prompt addendum for entity-stage normalization rules.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    entity_rules = read_optional_text(args.entity_rules_file)
    train_entity, train_relation = build_rows(read_jsonl(args.train_input), entity_rules=entity_rules)
    test_entity, test_relation = build_rows(read_jsonl(args.test_input), entity_rules=entity_rules)

    write_jsonl(args.train_entity_output, train_entity)
    write_jsonl(args.train_relation_output, train_relation)
    write_jsonl(args.test_entity_output, test_entity)
    write_jsonl(args.test_relation_output, test_relation)

    print(f"train_entity={len(train_entity)} -> {args.train_entity_output}")
    print(f"train_relation={len(train_relation)} -> {args.train_relation_output}")
    print(f"test_entity={len(test_entity)} -> {args.test_entity_output}")
    print(f"test_relation={len(test_relation)} -> {args.test_relation_output}")


if __name__ == "__main__":
    main()
