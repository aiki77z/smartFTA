"""Evaluate extraction predictions with span/entity and directed relation metrics.

Main metrics follow the planned protocol:
- strict entity span + type precision/recall/F1
- loose entity span + type precision/recall/F1
- normalized entity F1
- mention + type F1
- exact evidence F1
- strict directed relation triple F1: source, relation_type, target

Optional semantic normalized-entity metrics can be enabled with
``--semantic-normalized``. The exact metrics are always preserved.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple, Protocol


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")


class Span(NamedTuple):
    row_id: int
    chunk_id: str
    text_field: str
    start: int
    end: int
    entity_type: str
    name: str


class Entity(NamedTuple):
    row_id: int
    entity_type: str
    mention: str
    normalized_name: str
    evidence_texts: tuple[str, ...]


class ParsedAnswer(NamedTuple):
    entities: list[Entity]
    entity_spans: list[Span]
    normalized_entities: set[tuple[int, str, str]]
    mention_entities: set[tuple[int, str, str]]
    evidence_exact: set[tuple[int, str, str, int, int, str, str]]
    evidence_text_type: set[tuple[int, str, str]]
    relation_triples: set[tuple[int, str, str, str]]
    relation_triples_with_attrs: set[tuple[int, str, str, str, str, str]]
    logic_groups: set[tuple[int, str, str, str]]


class Embedder(Protocol):
    def encode(self, texts: list[str], batch_size: int) -> list[list[float]]:
        ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name_or_path: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Semantic normalized metrics require sentence-transformers. "
                "Install it with: pip install sentence-transformers"
            ) from exc
        self.model = SentenceTransformer(model_name_or_path)
        self.cache: dict[str, list[float]] = {}

    def encode(self, texts: list[str], batch_size: int) -> list[list[float]]:
        missing = [text for text in texts if text not in self.cache]
        if missing:
            embeddings = self.model.encode(
                missing,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            for text, embedding in zip(missing, embeddings):
                self.cache[text] = embedding.tolist()
        return [self.cache[text] for text in texts]


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
    entities: list[Entity] = []
    entity_spans: list[Span] = []
    normalized_entities: set[tuple[int, str, str]] = set()
    mention_entities: set[tuple[int, str, str]] = set()
    evidence_exact: set[tuple[int, str, str, int, int, str, str]] = set()
    evidence_text_type: set[tuple[int, str, str]] = set()
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
        mention_entities.add((row_id, entity_type, mention))

        evidence = fields[3] if len(fields) >= 4 else ""
        evidence_texts: list[str] = []
        for chunk_id, text_field, start, end, quote in _parse_evidence(evidence):
            evidence_texts.append(quote)
            evidence_exact.add((row_id, chunk_id, text_field, start, end, entity_type, quote))
            evidence_text_type.add((row_id, entity_type, quote))
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
        entities.append(
            Entity(
                row_id=row_id,
                entity_type=entity_type,
                mention=mention,
                normalized_name=normalized_name,
                evidence_texts=tuple(evidence_texts),
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
        entities=entities,
        entity_spans=entity_spans,
        normalized_entities=normalized_entities,
        mention_entities=mention_entities,
        evidence_exact=evidence_exact,
        evidence_text_type=evidence_text_type,
        relation_triples=relation_triples,
        relation_triples_with_attrs=relation_triples_with_attrs,
        logic_groups=logic_groups,
    )


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _soft_prf(score: float, pred_count: int, gold_count: int) -> dict[str, float | int]:
    precision = score / pred_count if pred_count else 0.0
    recall = score / gold_count if gold_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "soft_tp": score,
        "pred": pred_count,
        "gold": gold_count,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


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


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _entity_is_extractable(entity: Entity) -> bool:
    normalized = _normalized_text(entity.normalized_name)
    if not normalized:
        return False
    if normalized == _normalized_text(entity.mention):
        return True
    return any(normalized and normalized in _normalized_text(evidence) for evidence in entity.evidence_texts)


def _number_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _has_semantic_conflict(pred: Entity, gold: Entity) -> bool:
    pred_name = _normalized_text(pred.normalized_name)
    gold_name = _normalized_text(gold.normalized_name)

    if pred.entity_type == "报警码" or gold.entity_type == "报警码":
        return pred_name != gold_name

    gold_nums = _number_tokens(gold.normalized_name)
    pred_nums = _number_tokens(pred.normalized_name)
    if gold_nums and not gold_nums.issubset(pred_nums):
        return True

    negation_tokens = ("无", "未", "不", "否", "非")
    if _has_any(pred_name, negation_tokens) != _has_any(gold_name, negation_tokens):
        return True

    antonym_pairs = (("过高", "过低"), ("高于", "低于"), ("大于", "小于"), ("升高", "降低"), ("打开", "关闭"))
    for left, right in antonym_pairs:
        if (left in pred_name and right in gold_name) or (right in pred_name and left in gold_name):
            return True

    return False


def _cosine(a: list[float], b: list[float]) -> float:
    # SentenceTransformerEmbedder already returns normalized embeddings, but keep this robust for custom embedders.
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _collect_text_embeddings(texts: list[str], embedder: Embedder, batch_size: int) -> dict[str, list[float]]:
    unique_texts = sorted({text for text in texts if text.strip()})
    if not unique_texts:
        return {}
    embeddings = embedder.encode(unique_texts, batch_size=batch_size)
    return dict(zip(unique_texts, embeddings))


def _collect_embeddings(entities: list[Entity], embedder: Embedder, batch_size: int) -> dict[str, list[float]]:
    return _collect_text_embeddings([entity.normalized_name for entity in entities], embedder, batch_size)


def _semantic_pair_score(
    pred: Entity,
    gold: Entity,
    embeddings: dict[str, list[float]],
    threshold: float,
    soft_floor: float,
    soft_ceiling: float,
    require_extractable_exact: bool = True,
) -> tuple[bool, float, str]:
    if pred.row_id != gold.row_id or pred.entity_type != gold.entity_type:
        return False, 0.0, "incompatible"

    pred_name = _normalized_text(pred.normalized_name)
    gold_name = _normalized_text(gold.normalized_name)
    if pred_name and pred_name == gold_name:
        return True, 1.0, "exact"

    if require_extractable_exact and _entity_is_extractable(gold):
        return False, 0.0, "extractable_exact_required"

    if _has_semantic_conflict(pred, gold):
        return False, 0.0, "hard_conflict"

    pred_embedding = embeddings.get(pred.normalized_name)
    gold_embedding = embeddings.get(gold.normalized_name)
    if pred_embedding is None or gold_embedding is None:
        return False, 0.0, "missing_embedding"

    similarity = _cosine(pred_embedding, gold_embedding)
    if similarity >= threshold:
        return True, similarity, "semantic"
    if similarity <= soft_floor:
        return False, 0.0, "below_floor"
    if soft_ceiling <= soft_floor:
        soft_score = similarity
    else:
        soft_score = min(1.0, max(0.0, (similarity - soft_floor) / (soft_ceiling - soft_floor)))
    return False, soft_score, "soft_partial"


def _semantic_normalized_scores(
    pred: list[Entity],
    gold: list[Entity],
    embedder: Embedder,
    threshold: float,
    soft_floor: float,
    soft_ceiling: float,
    batch_size: int,
    require_extractable_exact: bool = True,
) -> dict[str, Any]:
    embeddings = _collect_embeddings(pred + gold, embedder, batch_size)
    groups: dict[tuple[int, str], tuple[list[Entity], list[Entity]]] = {}
    for entity in pred:
        groups.setdefault((entity.row_id, entity.entity_type), ([], []))[0].append(entity)
    for entity in gold:
        groups.setdefault((entity.row_id, entity.entity_type), ([], []))[1].append(entity)

    hard_tp = 0
    soft_tp = 0.0
    exact_matches = 0
    semantic_matches = 0
    extractable_gold = sum(1 for entity in gold if _entity_is_extractable(entity))
    abstractive_gold = len(gold) - extractable_gold

    for pred_group, gold_group in groups.values():
        candidates: list[tuple[float, float, str, int, int]] = []
        for pred_idx, pred_entity in enumerate(pred_group):
            for gold_idx, gold_entity in enumerate(gold_group):
                hard_match, soft_score, reason = _semantic_pair_score(
                    pred_entity,
                    gold_entity,
                    embeddings,
                    threshold,
                    soft_floor,
                    soft_ceiling,
                    require_extractable_exact=require_extractable_exact,
                )
                if hard_match or soft_score > 0:
                    hard_priority = 1.0 if hard_match else 0.0
                    candidates.append((hard_priority, soft_score, reason, pred_idx, gold_idx))

        used_pred: set[int] = set()
        used_gold: set[int] = set()
        for hard_priority, soft_score, reason, pred_idx, gold_idx in sorted(candidates, reverse=True):
            if pred_idx in used_pred or gold_idx in used_gold:
                continue
            used_pred.add(pred_idx)
            used_gold.add(gold_idx)
            soft_tp += soft_score
            if hard_priority:
                hard_tp += 1
                if reason == "exact":
                    exact_matches += 1
                elif reason == "semantic":
                    semantic_matches += 1

    return {
        "hybrid": _prf(tp=hard_tp, fp=len(pred) - hard_tp, fn=len(gold) - hard_tp),
        "soft": _soft_prf(score=soft_tp, pred_count=len(pred), gold_count=len(gold)),
        "details": {
            "threshold": threshold,
            "soft_floor": soft_floor,
            "soft_ceiling": soft_ceiling,
            "extractable_gold": extractable_gold,
            "abstractive_gold": abstractive_gold,
            "exact_matches": exact_matches,
            "semantic_matches": semantic_matches,
        },
    }



def _relation_component_score(
    pred_name: str,
    gold_name: str,
    row_id: int,
    embeddings: dict[str, list[float]],
    threshold: float,
    soft_floor: float,
    soft_ceiling: float,
) -> tuple[bool, float, str]:
    pred_entity = Entity(row_id=row_id, entity_type="RELATION_ARG", mention=pred_name, normalized_name=pred_name, evidence_texts=())
    gold_entity = Entity(row_id=row_id, entity_type="RELATION_ARG", mention=gold_name, normalized_name=gold_name, evidence_texts=())
    return _semantic_pair_score(
        pred_entity,
        gold_entity,
        embeddings,
        threshold,
        soft_floor,
        soft_ceiling,
        require_extractable_exact=False,
    )


def _relation_semantic_scores(
    pred: set[tuple[Any, ...]],
    gold: set[tuple[Any, ...]],
    embedder: Embedder,
    threshold: float,
    soft_floor: float,
    soft_ceiling: float,
    batch_size: int,
    with_attrs: bool = False,
) -> dict[str, Any]:
    names: list[str] = []
    for item in pred | gold:
        names.extend([str(item[1]), str(item[3])])
    embeddings = _collect_text_embeddings(names, embedder, batch_size)

    groups: dict[tuple[Any, ...], tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]] = {}
    for item in pred:
        key = (item[0], item[2], item[4], item[5]) if with_attrs else (item[0], item[2])
        groups.setdefault(key, ([], []))[0].append(item)
    for item in gold:
        key = (item[0], item[2], item[4], item[5]) if with_attrs else (item[0], item[2])
        groups.setdefault(key, ([], []))[1].append(item)

    hard_tp = 0
    soft_tp = 0.0
    exact_matches = 0
    semantic_matches = 0
    for pred_group, gold_group in groups.values():
        candidates: list[tuple[float, float, str, int, int]] = []
        for pred_idx, pred_item in enumerate(pred_group):
            for gold_idx, gold_item in enumerate(gold_group):
                source_hard, source_soft, source_reason = _relation_component_score(
                    str(pred_item[1]), str(gold_item[1]), int(gold_item[0]), embeddings, threshold, soft_floor, soft_ceiling
                )
                target_hard, target_soft, target_reason = _relation_component_score(
                    str(pred_item[3]), str(gold_item[3]), int(gold_item[0]), embeddings, threshold, soft_floor, soft_ceiling
                )
                soft_score = (source_soft + target_soft) / 2
                hard_match = source_hard and target_hard
                if hard_match or soft_score > 0:
                    reason = "exact" if source_reason == "exact" and target_reason == "exact" else "semantic"
                    candidates.append((1.0 if hard_match else 0.0, soft_score, reason, pred_idx, gold_idx))

        used_pred: set[int] = set()
        used_gold: set[int] = set()
        for hard_priority, soft_score, reason, pred_idx, gold_idx in sorted(candidates, reverse=True):
            if pred_idx in used_pred or gold_idx in used_gold:
                continue
            used_pred.add(pred_idx)
            used_gold.add(gold_idx)
            soft_tp += soft_score
            if hard_priority:
                hard_tp += 1
                if reason == "exact":
                    exact_matches += 1
                else:
                    semantic_matches += 1

    return {
        "hybrid": _prf(tp=hard_tp, fp=len(pred) - hard_tp, fn=len(gold) - hard_tp),
        "soft": _soft_prf(score=soft_tp, pred_count=len(pred), gold_count=len(gold)),
        "details": {
            "threshold": threshold,
            "soft_floor": soft_floor,
            "soft_ceiling": soft_ceiling,
            "exact_matches": exact_matches,
            "semantic_matches": semantic_matches,
            "with_attrs": with_attrs,
        },
    }
def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    rows = _read_jsonl(args.input)
    error_rows = sum(1 for row in rows if row.get("error"))

    pred_entities: list[Entity] = []
    gold_entities: list[Entity] = []
    pred_spans: list[Span] = []
    gold_spans: list[Span] = []
    pred_normalized: set[tuple[int, str, str]] = set()
    gold_normalized: set[tuple[int, str, str]] = set()
    pred_mentions: set[tuple[int, str, str]] = set()
    gold_mentions: set[tuple[int, str, str]] = set()
    pred_evidence_exact: set[tuple[int, str, str, int, int, str, str]] = set()
    gold_evidence_exact: set[tuple[int, str, str, int, int, str, str]] = set()
    pred_evidence_text_type: set[tuple[int, str, str]] = set()
    gold_evidence_text_type: set[tuple[int, str, str]] = set()
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

        pred_entities.extend(pred.entities)
        gold_entities.extend(gold.entities)
        pred_spans.extend(pred.entity_spans)
        gold_spans.extend(gold.entity_spans)
        pred_normalized.update(pred.normalized_entities)
        gold_normalized.update(gold.normalized_entities)
        pred_mentions.update(pred.mention_entities)
        gold_mentions.update(gold.mention_entities)
        pred_evidence_exact.update(pred.evidence_exact)
        gold_evidence_exact.update(gold.evidence_exact)
        pred_evidence_text_type.update(pred.evidence_text_type)
        gold_evidence_text_type.update(gold.evidence_text_type)
        pred_relations.update(pred.relation_triples)
        gold_relations.update(gold.relation_triples)
        pred_relations_attrs.update(pred.relation_triples_with_attrs)
        gold_relations_attrs.update(gold.relation_triples_with_attrs)
        pred_logic.update(pred.logic_groups)
        gold_logic.update(gold.logic_groups)

    pred_span_exact = {_span_exact_key(span) for span in pred_spans}
    gold_span_exact = {_span_exact_key(span) for span in gold_spans}

    secondary_metrics: dict[str, Any] = {
        "relation_triple_with_polarity_certainty": _set_score(pred_relations_attrs, gold_relations_attrs),
        "logic_group": _set_score(pred_logic, gold_logic),
    }
    if args.semantic_normalized:
        embedder = SentenceTransformerEmbedder(args.embedding_model)
        semantic_scores = _semantic_normalized_scores(
            pred=pred_entities,
            gold=gold_entities,
            embedder=embedder,
            threshold=args.semantic_threshold,
            soft_floor=args.soft_floor,
            soft_ceiling=args.soft_ceiling,
            batch_size=args.embedding_batch_size,
            require_extractable_exact=True,
        )
        semantic_all_scores = _semantic_normalized_scores(
            pred=pred_entities,
            gold=gold_entities,
            embedder=embedder,
            threshold=args.semantic_threshold,
            soft_floor=args.soft_floor,
            soft_ceiling=args.soft_ceiling,
            batch_size=args.embedding_batch_size,
            require_extractable_exact=False,
        )
        relation_semantic_scores = _relation_semantic_scores(
            pred=pred_relations,
            gold=gold_relations,
            embedder=embedder,
            threshold=args.semantic_threshold,
            soft_floor=args.soft_floor,
            soft_ceiling=args.soft_ceiling,
            batch_size=args.embedding_batch_size,
            with_attrs=False,
        )
        relation_semantic_attr_scores = _relation_semantic_scores(
            pred=pred_relations_attrs,
            gold=gold_relations_attrs,
            embedder=embedder,
            threshold=args.semantic_threshold,
            soft_floor=args.soft_floor,
            soft_ceiling=args.soft_ceiling,
            batch_size=args.embedding_batch_size,
            with_attrs=True,
        )
        secondary_metrics["entity_normalized_hybrid"] = semantic_scores["hybrid"]
        secondary_metrics["entity_normalized_soft"] = semantic_scores["soft"]
        secondary_metrics["entity_normalized_semantic_details"] = semantic_scores["details"]
        secondary_metrics["entity_normalized_semantic_all"] = semantic_all_scores["hybrid"]
        secondary_metrics["entity_normalized_semantic_all_soft"] = semantic_all_scores["soft"]
        secondary_metrics["entity_normalized_semantic_all_details"] = semantic_all_scores["details"]
        secondary_metrics["relation_semantic_triple"] = relation_semantic_scores["hybrid"]
        secondary_metrics["relation_semantic_triple_soft"] = relation_semantic_scores["soft"]
        secondary_metrics["relation_semantic_triple_details"] = relation_semantic_scores["details"]
        secondary_metrics["relation_semantic_triple_with_polarity_certainty"] = relation_semantic_attr_scores["hybrid"]
        secondary_metrics["relation_semantic_triple_with_polarity_certainty_soft"] = relation_semantic_attr_scores["soft"]
        secondary_metrics["relation_semantic_triple_with_polarity_certainty_details"] = relation_semantic_attr_scores["details"]

    report: dict[str, Any] = {
        "rows": len(rows),
        "error_rows": error_rows,
        "main_metrics": {
            "entity_strict_span_type": _set_score(pred_span_exact, gold_span_exact),
            "entity_loose_span_type": _loose_span_score(pred_spans, gold_spans),
            "entity_normalized": _set_score(pred_normalized, gold_normalized),
            "entity_mention_type": _set_score(pred_mentions, gold_mentions),
            "entity_evidence_exact": _set_score(pred_evidence_exact, gold_evidence_exact),
            "entity_evidence_text_type": _set_score(pred_evidence_text_type, gold_evidence_text_type),
            "relation_strict_triple": _set_score(pred_relations, gold_relations),
        },
        "secondary_metrics": secondary_metrics,
        "counts": {
            "pred_entity_spans": len(pred_spans),
            "gold_entity_spans": len(gold_spans),
            "pred_normalized_entities": len(pred_normalized),
            "gold_normalized_entities": len(gold_normalized),
            "pred_mentions": len(pred_mentions),
            "gold_mentions": len(gold_mentions),
            "pred_evidence_exact": len(pred_evidence_exact),
            "gold_evidence_exact": len(gold_evidence_exact),
            "pred_evidence_text_type": len(pred_evidence_text_type),
            "gold_evidence_text_type": len(gold_evidence_text_type),
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
    parser.add_argument(
        "--semantic-normalized",
        action="store_true",
        help="Also compute hybrid/soft semantic normalized-entity metrics with sentence-transformers.",
    )
    parser.add_argument("--embedding-model", default="BAAI/bge-m3", help="SentenceTransformer model name or local path")
    parser.add_argument("--semantic-threshold", type=float, default=0.85, help="Cosine threshold for semantic hard match")
    parser.add_argument("--soft-floor", type=float, default=0.70, help="Cosine score below this gets 0 soft credit")
    parser.add_argument("--soft-ceiling", type=float, default=0.90, help="Cosine score at/above this gets full soft credit")
    parser.add_argument("--embedding-batch-size", type=int, default=64)
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
