"""Evaluate extraction predictions with span/entity and directed relation metrics.

Main metrics follow the planned protocol:
- strict entity span + type precision/recall/F1
- loose entity span + type precision/recall/F1
- normalized entity F1
- strict directed relation triple F1: source, relation_type, target
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, NamedTuple


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")


class Span(NamedTuple):
    row_id: int
    chunk_id: str
    text_field: str
    start: int
    end: int
    entity_type: str
    name: str


class ParsedAnswer(NamedTuple):
    entity_spans: list[Span]
    normalized_entities: set[tuple[int, str, str]]
    relation_triples: set[tuple[int, str, str, str]]
    relation_triples_with_attrs: set[tuple[int, str, str, str, str, str]]
    logic_groups: set[tuple[int, str, str, str]]


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
        if current is None or _is_header_line(current, line):
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


def _parse_evidence(evidence: str) -> list[tuple[str, str, int, int, str]]:
    spans: list[tuple[str, str, int, int, str]] = []
    for item in evidence.split(";;"):
        item = item.strip()
        if not item:
            continue
        parts = item.split("::", 4)
        if len(parts) != 5:
            continue
        chunk_id, text_field, start_text, end_text, quote = parts
        try:
            start = int(start_text)
            end = int(end_text)
        except ValueError:
            continue
        if start < 0 or end < start:
            continue
        spans.append((chunk_id.strip(), text_field.strip(), start, end, quote.strip()))
    return spans


def _canonical_name(name: str, alias_map: dict[str, str]) -> str:
    name = name.strip()
    return alias_map.get(name, name)


def _parse_answer(text: str, row_id: int) -> ParsedAnswer:
    sections = _split_sections(text)
    entity_spans: list[Span] = []
    normalized_entities: set[tuple[int, str, str]] = set()
    alias_map: dict[str, str] = {}

    for line in sections["ENTITY"]:
        fields = _fields(line)
        if len(fields) < 3:
            continue
        mention = fields[0]
        entity_type = fields[1]
        normalized_name = fields[2] or mention
        alias_map[mention] = normalized_name
        alias_map[normalized_name] = normalized_name
        normalized_entities.add((row_id, entity_type, normalized_name))

        evidence = fields[3] if len(fields) >= 4 else ""
        for chunk_id, text_field, start, end, _quote in _parse_evidence(evidence):
            entity_spans.append(
                Span(
                    row_id=row_id,
                    chunk_id=chunk_id,
                    text_field=text_field,
                    start=start,
                    end=end,
                    entity_type=entity_type,
                    name=normalized_name,
                )
            )

    relation_triples: set[tuple[int, str, str, str]] = set()
    relation_triples_with_attrs: set[tuple[int, str, str, str, str, str]] = set()
    for line in sections["RELATION"]:
        fields = _fields(line)
        if len(fields) < 3:
            continue
        source = _canonical_name(fields[0], alias_map)
        relation_type = fields[1]
        target = _canonical_name(fields[2], alias_map)
        triple = (row_id, source, relation_type, target)
        relation_triples.add(triple)
        polarity = fields[6] if len(fields) >= 7 else ""
        certainty = fields[7] if len(fields) >= 8 else ""
        relation_triples_with_attrs.add(triple + (polarity, certainty))

    logic_groups: set[tuple[int, str, str, str]] = set()
    for line in sections["LOGIC_GROUP"]:
        fields = _fields(line)
        if len(fields) < 3:
            continue
        logic_type = fields[0]
        members = sorted(_canonical_name(member, alias_map) for member in fields[1].split(";") if member.strip())
        result = _canonical_name(fields[2], alias_map)
        logic_groups.add((row_id, logic_type, ";".join(members), result))

    return ParsedAnswer(
        entity_spans=entity_spans,
        normalized_entities=normalized_entities,
        relation_triples=relation_triples,
        relation_triples_with_attrs=relation_triples_with_attrs,
        logic_groups=logic_groups,
    )


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _set_score(pred: set[tuple[Any, ...]], gold: set[tuple[Any, ...]]) -> dict[str, float | int]:
    tp = len(pred & gold)
    return _prf(tp=tp, fp=len(pred - gold), fn=len(gold - pred))


def _span_exact_key(span: Span) -> tuple[Any, ...]:
    return (span.row_id, span.chunk_id, span.text_field, span.start, span.end, span.entity_type)


def _span_overlap(pred: Span, gold: Span) -> bool:
    if pred.row_id != gold.row_id:
        return False
    if pred.chunk_id != gold.chunk_id or pred.text_field != gold.text_field:
        return False
    if pred.entity_type != gold.entity_type:
        return False
    return max(pred.start, gold.start) < min(pred.end, gold.end)


def _loose_span_score(pred: list[Span], gold: list[Span]) -> dict[str, float | int]:
    matched_gold: set[int] = set()
    tp = 0
    # Prefer longer overlaps first so broad spans do not always steal short exact matches.
    candidates: list[tuple[int, int, int]] = []
    for pred_idx, pred_span in enumerate(pred):
        for gold_idx, gold_span in enumerate(gold):
            if _span_overlap(pred_span, gold_span):
                overlap = min(pred_span.end, gold_span.end) - max(pred_span.start, gold_span.start)
                candidates.append((overlap, pred_idx, gold_idx))
    used_pred: set[int] = set()
    for _overlap, pred_idx, gold_idx in sorted(candidates, reverse=True):
        if pred_idx in used_pred or gold_idx in matched_gold:
            continue
        used_pred.add(pred_idx)
        matched_gold.add(gold_idx)
        tp += 1
    return _prf(tp=tp, fp=len(pred) - tp, fn=len(gold) - tp)


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    rows = _read_jsonl(args.input)
    error_rows = sum(1 for row in rows if row.get("error"))

    pred_spans: list[Span] = []
    gold_spans: list[Span] = []
    pred_normalized: set[tuple[int, str, str]] = set()
    gold_normalized: set[tuple[int, str, str]] = set()
    pred_relations: set[tuple[int, str, str, str]] = set()
    gold_relations: set[tuple[int, str, str, str]] = set()
    pred_relations_attrs: set[tuple[int, str, str, str, str, str]] = set()
    gold_relations_attrs: set[tuple[int, str, str, str, str, str]] = set()
    pred_logic: set[tuple[int, str, str, str]] = set()
    gold_logic: set[tuple[int, str, str, str]] = set()

    for ordinal, row in enumerate(rows):
        row_id = row.get("id", ordinal)
        if not isinstance(row_id, int):
            row_id = ordinal
        pred = _parse_answer(row.get("prediction", ""), row_id)
        gold = _parse_answer(row.get("gold", ""), row_id)

        pred_spans.extend(pred.entity_spans)
        gold_spans.extend(gold.entity_spans)
        pred_normalized.update(pred.normalized_entities)
        gold_normalized.update(gold.normalized_entities)
        pred_relations.update(pred.relation_triples)
        gold_relations.update(gold.relation_triples)
        pred_relations_attrs.update(pred.relation_triples_with_attrs)
        gold_relations_attrs.update(gold.relation_triples_with_attrs)
        pred_logic.update(pred.logic_groups)
        gold_logic.update(gold.logic_groups)

    pred_span_exact = {_span_exact_key(span) for span in pred_spans}
    gold_span_exact = {_span_exact_key(span) for span in gold_spans}

    report: dict[str, Any] = {
        "rows": len(rows),
        "error_rows": error_rows,
        "main_metrics": {
            "entity_strict_span_type": _set_score(pred_span_exact, gold_span_exact),
            "entity_loose_span_type": _loose_span_score(pred_spans, gold_spans),
            "entity_normalized": _set_score(pred_normalized, gold_normalized),
            "relation_strict_triple": _set_score(pred_relations, gold_relations),
        },
        "secondary_metrics": {
            "relation_triple_with_polarity_certainty": _set_score(pred_relations_attrs, gold_relations_attrs),
            "logic_group": _set_score(pred_logic, gold_logic),
        },
        "counts": {
            "pred_entity_spans": len(pred_spans),
            "gold_entity_spans": len(gold_spans),
            "pred_normalized_entities": len(pred_normalized),
            "gold_normalized_entities": len(gold_normalized),
            "pred_relations": len(pred_relations),
            "gold_relations": len(gold_relations),
            "pred_logic_groups": len(pred_logic),
            "gold_logic_groups": len(gold_logic),
        },
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Prediction JSONL from run_dataset.py")
    parser.add_argument("--output", type=Path, default=None, help="Optional report JSON path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = evaluate(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
