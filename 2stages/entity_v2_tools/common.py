"""Shared parsing helpers for the entity_v2 cleanup toolchain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")

ALLOWED_ENTITY_TYPES = {"故障事件", "故障类别", "报警码", "维修方法", "触发规则"}

# Only these five relation types may appear inside [RELATION].
ALLOWED_RELATION_TYPES = {"故障触发", "故障表征", "故障分类", "故障处理", "规则触发"}

ENTITY_HEADER = "mentiontypenormalized_nameevidence"
RELATION_HEADER = "sourcerelation_typetargetcross_chunkinvolved_chunk_idsevidencepolaritycertainty"
LOGIC_HEADER = "logic_typemembersresultinvolved_chunk_idsevidence"


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
                raise ValueError(f"Invalid JSON on line {line_no} in {path}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


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


def is_header_line(section: str, line: str) -> bool:
    normalized = "".join(part.strip().lower() for part in line.split("|"))
    headers = {
        "ENTITY": ENTITY_HEADER,
        "RELATION": RELATION_HEADER,
        "LOGIC_GROUP": LOGIC_HEADER,
    }
    return normalized == headers[section]


def split_sections(text: str) -> dict[str, list[str]]:
    sections = {name: [] for name in SECTION_NAMES}
    known_headers = {f"[{name}]" for name in SECTION_NAMES}
    current: str | None = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper in known_headers:
            current = upper.strip("[]")
            continue
        if upper.startswith("[") and upper.endswith("]"):
            # Unknown bracketed header (e.g. [RELATION_ENDPOINTS] echoed by the
            # cleaning model): stop collecting into the current section.
            current = None
            continue
        if current is not None and not is_header_line(current, line):
            sections[current].append(line)
    return sections


def parse_entities(text: str) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    for line in split_sections(text)["ENTITY"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        mention, entity_type, normalized_name = fields[:3]
        evidence = fields[3] if len(fields) > 3 else ""
        entities.append(
            {
                "mention": mention,
                "type": entity_type,
                "normalized_name": normalized_name or mention,
                "evidence": evidence,
                "line": line,
            }
        )
    return entities


def parse_relations(text: str) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for line in split_sections(text)["RELATION"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        relations.append(
            {
                "source": fields[0],
                "relation_type": fields[1],
                "target": fields[2],
                "line": line,
            }
        )
    return relations


def parse_logic_groups(text: str) -> list[dict[str, str]]:
    groups: list[dict[str, str]] = []
    for line in split_sections(text)["LOGIC_GROUP"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        groups.append(
            {
                "logic_type": fields[0],
                "members": fields[1],
                "result": fields[2],
                "line": line,
            }
        )
    return groups


def entity_coverage_sets(entities: list[dict[str, str]]) -> tuple[set[str], set[str]]:
    normalized: set[str] = set()
    mentions: set[str] = set()
    for entity in entities:
        if entity["normalized_name"]:
            normalized.add(entity["normalized_name"])
        if entity["mention"]:
            mentions.add(entity["mention"])
    return normalized, mentions


def render_row(system: str, user: str, assistant: str) -> str:
    return (
        "[SFT_ROW]\n"
        "[system]\n"
        f"{system}\n\n"
        "[user]\n"
        f"{user}\n\n"
        "[assistant]\n"
        f"{assistant}"
    )
