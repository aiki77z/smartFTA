"""Evaluate remote LLM extraction predictions against gold answers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _split_sections(text: str) -> dict[str, list[str]]:
    sections = {name: [] for name in SECTION_NAMES}
    current: str | None = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        matched = None
        for name in SECTION_NAMES:
            if upper == f"[{name}]":
                matched = name
                break
        if matched:
            current = matched
            continue
        if current is None:
            continue
        if _is_header_line(current, line):
            continue
        sections[current].append(line)
    return sections


def _is_header_line(section: str, line: str) -> bool:
    normalized = "".join(part.strip().lower() for part in line.split("|"))
    headers = {
        "ENTITY": "mentiontypenormalized_nameevidence",
        "RELATION": "sourcerelation_typetargetcross_chunkinvolved_chunk_idsevidencepolaritycertainty",
        "LOGIC_GROUP": "logic_typemembersresultinvolved_chunk_idsevidence",
    }
    return normalized == headers[section]


def _fields(line: str) -> list[str]:
    return [part.strip() for part in line.split("|")]


def _entity_key(line: str, strict_evidence: bool) -> tuple[str, ...] | None:
    fields = _fields(line)
    if len(fields) < 3:
        return None
    key = (fields[1], fields[2] or fields[0])
    if strict_evidence and len(fields) >= 4:
        key = key + (fields[3],)
    return key


def _relation_key(line: str, strict_evidence: bool) -> tuple[str, ...] | None:
    fields = _fields(line)
    if len(fields) < 3:
        return None
    key = (fields[0], fields[1], fields[2])
    if len(fields) >= 8:
        key = key + (fields[6], fields[7])
    if strict_evidence and len(fields) >= 6:
        key = key + (fields[5],)
    return key


def _logic_group_key(line: str, strict_evidence: bool) -> tuple[str, ...] | None:
    fields = _fields(line)
    if len(fields) < 3:
        return None
    members = tuple(sorted(part.strip() for part in fields[1].split(";") if part.strip()))
    key = (fields[0], ";".join(members), fields[2])
    if strict_evidence and len(fields) >= 5:
        key = key + (fields[4],)
    return key


def _key_set(section: str, lines: list[str], strict_evidence: bool) -> set[tuple[str, ...]]:
    key_fn = {
        "ENTITY": _entity_key,
        "RELATION": _relation_key,
        "LOGIC_GROUP": _logic_group_key,
    }[section]
    keys = set()
    for line in lines:
        key = key_fn(line, strict_evidence)
        if key is not None:
            keys.add(key)
    return keys


def _score(pred: set[tuple[str, ...]], gold: set[tuple[str, ...]]) -> dict[str, float]:
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def evaluate(args: argparse.Namespace) -> None:
    rows = _read_jsonl(args.input)
    totals = {name: {"pred": set(), "gold": set()} for name in SECTION_NAMES}
    bad_rows = 0

    for row in rows:
        prediction = row.get("prediction", "")
        gold = row.get("gold", "")
        if row.get("error"):
            bad_rows += 1
        pred_sections = _split_sections(prediction)
        gold_sections = _split_sections(gold)
        row_id = str(row.get("id", len(totals["ENTITY"]["pred"])))
        for section in SECTION_NAMES:
            pred_keys = _key_set(section, pred_sections[section], args.strict_evidence)
            gold_keys = _key_set(section, gold_sections[section], args.strict_evidence)
            totals[section]["pred"].update((row_id,) + key for key in pred_keys)
            totals[section]["gold"].update((row_id,) + key for key in gold_keys)

    report: dict[str, Any] = {"rows": len(rows), "error_rows": bad_rows, "strict_evidence": args.strict_evidence}
    combined_pred: set[tuple[str, ...]] = set()
    combined_gold: set[tuple[str, ...]] = set()
    for section in SECTION_NAMES:
        pred = totals[section]["pred"]
        gold = totals[section]["gold"]
        report[section.lower()] = _score(pred, gold)
        combined_pred.update((section,) + item for item in pred)
        combined_gold.update((section,) + item for item in gold)
    report["overall"] = _score(combined_pred, combined_gold)

    print(json.dumps(report, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Prediction JSONL from run_dataset.py")
    parser.add_argument(
        "--strict-evidence",
        action="store_true",
        help="Include evidence text/offsets in match keys. Default ignores evidence for a more tolerant score.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    evaluate(parse_args())

