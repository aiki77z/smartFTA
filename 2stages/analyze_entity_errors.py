"""Analyze entity-stage errors and relation bottlenecks for two-stage runs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
from typing import Any


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")


@dataclass(frozen=True)
class Entity:
    row_id: int
    mention: str
    entity_type: str
    normalized_name: str
    evidence: str


@dataclass(frozen=True)
class Relation:
    row_id: int
    source: str
    relation_type: str
    target: str
    polarity: str = ""
    certainty: str = ""


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


def parse_entities(text: str, row_id: int) -> list[Entity]:
    entities: list[Entity] = []
    for line in split_sections(text)["ENTITY"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        mention, entity_type, normalized_name = fields[:3]
        evidence = fields[3] if len(fields) > 3 else ""
        entities.append(
            Entity(
                row_id=row_id,
                mention=mention,
                entity_type=entity_type,
                normalized_name=normalized_name or mention,
                evidence=evidence,
            )
        )
    return entities


def parse_relations(text: str, row_id: int) -> list[Relation]:
    relations: list[Relation] = []
    for line in split_sections(text)["RELATION"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        polarity = fields[6] if len(fields) > 6 else ""
        certainty = fields[7] if len(fields) > 7 else ""
        relations.append(
            Relation(
                row_id=row_id,
                source=fields[0],
                relation_type=fields[1],
                target=fields[2],
                polarity=polarity,
                certainty=certainty,
            )
        )
    return relations


def prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def score_sets(pred: set[tuple[Any, ...]], gold: set[tuple[Any, ...]]) -> dict[str, float | int]:
    tp = len(pred & gold)
    return prf(tp=tp, fp=len(pred - gold), fn=len(gold - pred))


def similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def truncate(text: str, limit: int = 120) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def entity_key(entity: Entity) -> tuple[int, str, str]:
    return (entity.row_id, entity.entity_type, entity.normalized_name)


def mention_key(entity: Entity) -> tuple[int, str, str]:
    return (entity.row_id, entity.entity_type, entity.mention)


def relation_key(relation: Relation) -> tuple[int, str, str, str]:
    return (relation.row_id, relation.source, relation.relation_type, relation.target)


def best_match(
    item: Entity,
    candidates: list[Entity],
    used: set[int],
    min_score: float,
) -> tuple[int, Entity, float] | None:
    best: tuple[int, Entity, float] | None = None
    for idx, candidate in enumerate(candidates):
        if idx in used or item.row_id != candidate.row_id or item.entity_type != candidate.entity_type:
            continue
        score = max(
            similarity(item.normalized_name, candidate.normalized_name),
            similarity(item.mention, candidate.mention),
        )
        if score >= min_score and (best is None or score > best[2]):
            best = (idx, candidate, score)
    return best


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    entity_rows_by_id = {}
    for row in read_jsonl(args.entity_output):
        row_id = row.get("id")
        if isinstance(row_id, int):
            entity_rows_by_id[row_id] = row
    entity_rows = [entity_rows_by_id[row_id] for row_id in sorted(entity_rows_by_id)]
    final_by_id = {}
    if args.final_output and args.final_output.exists():
        final_by_id = {row["id"]: row for row in read_jsonl(args.final_output) if isinstance(row.get("id"), int)}

    pred_entities: list[Entity] = []
    gold_entities: list[Entity] = []
    pred_relations: list[Relation] = []
    gold_relations: list[Relation] = []
    rows_with_errors: list[int] = []
    row_details: dict[int, dict[str, Any]] = {}

    for row in entity_rows:
        row_id = row.get("id")
        if not isinstance(row_id, int):
            continue
        if row.get("error"):
            rows_with_errors.append(row_id)
        pred = parse_entities(row.get("prediction", ""), row_id)
        gold = parse_entities(row.get("gold", ""), row_id)
        pred_entities.extend(pred)
        gold_entities.extend(gold)

        final_row = final_by_id.get(row_id, row)
        pred_rels = parse_relations(final_row.get("prediction", ""), row_id)
        gold_rels = parse_relations(row.get("gold", ""), row_id)
        pred_relations.extend(pred_rels)
        gold_relations.extend(gold_rels)

        row_details[row_id] = {
            "pred_entities": pred,
            "gold_entities": gold,
            "pred_relations": pred_rels,
            "gold_relations": gold_rels,
        }

    pred_entity_keys = {entity_key(entity) for entity in pred_entities}
    gold_entity_keys = {entity_key(entity) for entity in gold_entities}
    pred_mention_keys = {mention_key(entity) for entity in pred_entities}
    gold_mention_keys = {mention_key(entity) for entity in gold_entities}
    pred_relation_keys = {relation_key(relation) for relation in pred_relations}
    gold_relation_keys = {relation_key(relation) for relation in gold_relations}

    pred_by_key = defaultdict(list)
    gold_by_key = defaultdict(list)
    for entity in pred_entities:
        pred_by_key[entity_key(entity)].append(entity)
    for entity in gold_entities:
        gold_by_key[entity_key(entity)].append(entity)

    missing_entities = [gold_by_key[key][0] for key in sorted(gold_entity_keys - pred_entity_keys)]
    extra_entities = [pred_by_key[key][0] for key in sorted(pred_entity_keys - gold_entity_keys)]

    type_breakdown: dict[str, dict[str, int]] = {}
    entity_types = sorted({entity.entity_type for entity in pred_entities + gold_entities})
    for entity_type in entity_types:
        pred_type = {key for key in pred_entity_keys if key[1] == entity_type}
        gold_type = {key for key in gold_entity_keys if key[1] == entity_type}
        type_breakdown[entity_type] = {
            "gold": len(gold_type),
            "pred": len(pred_type),
            "tp": len(pred_type & gold_type),
            "missing": len(gold_type - pred_type),
            "extra": len(pred_type - gold_type),
        }

    normalized_mismatches = []
    for row_id, detail in row_details.items():
        for pred in detail["pred_entities"]:
            for gold in detail["gold_entities"]:
                if pred.entity_type == gold.entity_type and pred.mention == gold.mention:
                    if pred.normalized_name != gold.normalized_name:
                        normalized_mismatches.append(
                            {
                                "row_id": row_id,
                                "type": pred.entity_type,
                                "mention": pred.mention,
                                "pred_normalized": pred.normalized_name,
                                "gold_normalized": gold.normalized_name,
                                "similarity": similarity(pred.normalized_name, gold.normalized_name),
                            }
                        )

    near_misses = []
    used_extra: set[int] = set()
    for missing in missing_entities:
        match = best_match(missing, extra_entities, used_extra, args.near_threshold)
        if match is None:
            continue
        idx, extra, score = match
        used_extra.add(idx)
        near_misses.append(
            {
                "row_id": missing.row_id,
                "type": missing.entity_type,
                "gold_mention": missing.mention,
                "gold_normalized": missing.normalized_name,
                "pred_mention": extra.mention,
                "pred_normalized": extra.normalized_name,
                "similarity": score,
            }
        )

    overlong_predictions = []
    for entity in extra_entities:
        norm_len = len(entity.normalized_name)
        mention_len = len(entity.mention)
        if norm_len >= max(mention_len + args.long_delta, args.long_min):
            overlong_predictions.append(
                {
                    "row_id": entity.row_id,
                    "type": entity.entity_type,
                    "mention": entity.mention,
                    "normalized_name": entity.normalized_name,
                    "mention_len": mention_len,
                    "normalized_len": norm_len,
                }
            )
    overlong_predictions.sort(key=lambda item: item["normalized_len"] - item["mention_len"], reverse=True)

    relation_blocked = []
    relation_possible = []
    row_relation_blocked_counts = Counter()
    for relation in gold_relations:
        pred_norm_names = {
            entity.normalized_name for entity in row_details.get(relation.row_id, {}).get("pred_entities", [])
        }
        source_ok = relation.source in pred_norm_names
        target_ok = relation.target in pred_norm_names
        item = {
            "row_id": relation.row_id,
            "source": relation.source,
            "relation_type": relation.relation_type,
            "target": relation.target,
            "missing_source": not source_ok,
            "missing_target": not target_ok,
        }
        if source_ok and target_ok:
            relation_possible.append(item)
        else:
            relation_blocked.append(item)
            row_relation_blocked_counts[relation.row_id] += 1

    possible_relation_keys = {
        (item["row_id"], item["source"], item["relation_type"], item["target"]) for item in relation_possible
    }
    possible_but_missed = sorted(possible_relation_keys - pred_relation_keys)
    blocked_relation_keys = {
        (item["row_id"], item["source"], item["relation_type"], item["target"]) for item in relation_blocked
    }
    blocked_matched = blocked_relation_keys & pred_relation_keys

    summary = {
        "rows": len(row_details),
        "error_rows": len(rows_with_errors),
        "entity_normalized": score_sets(pred_entity_keys, gold_entity_keys),
        "entity_mention_type": score_sets(pred_mention_keys, gold_mention_keys),
        "relation_strict": score_sets(pred_relation_keys, gold_relation_keys),
        "gold_relations": len(gold_relation_keys),
        "relation_endpoint_possible": len(possible_relation_keys),
        "relation_endpoint_blocked": len(blocked_relation_keys),
        "relation_endpoint_possible_recall_ceiling": (
            len(possible_relation_keys) / len(gold_relation_keys) if gold_relation_keys else 0.0
        ),
        "possible_relation_matched": len(possible_relation_keys & pred_relation_keys),
        "possible_relation_missed": len(possible_but_missed),
        "blocked_relation_matched": len(blocked_matched),
        "normalized_mismatch_same_mention": len(normalized_mismatches),
        "near_miss_pairs": len(near_misses),
        "overlong_extra_predictions": len(overlong_predictions),
    }

    return {
        "summary": summary,
        "type_breakdown": type_breakdown,
        "rows_with_errors": rows_with_errors,
        "top_missing_entities": [asdict(entity) for entity in missing_entities[: args.top_k]],
        "top_extra_entities": [asdict(entity) for entity in extra_entities[: args.top_k]],
        "normalized_mismatches": sorted(
            normalized_mismatches, key=lambda item: item["similarity"], reverse=True
        )[: args.top_k],
        "near_misses": sorted(near_misses, key=lambda item: item["similarity"], reverse=True)[: args.top_k],
        "overlong_predictions": overlong_predictions[: args.top_k],
        "relation_blocked_examples": relation_blocked[: args.top_k],
        "possible_relation_missed_examples": [
            {
                "row_id": item[0],
                "source": item[1],
                "relation_type": item[2],
                "target": item[3],
            }
            for item in possible_but_missed[: args.top_k]
        ],
        "rows_by_blocked_relation_count": [
            {"row_id": row_id, "blocked_gold_relations": count}
            for row_id, count in row_relation_blocked_counts.most_common(args.top_k)
        ],
    }


def fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(fmt(cell).replace("\n", " ") for cell in row) + " |")
    return "\n".join(lines)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    type_rows = [
        [entity_type, data["gold"], data["pred"], data["tp"], data["missing"], data["extra"]]
        for entity_type, data in sorted(report["type_breakdown"].items())
    ]
    blocked_rows = [
        [
            item["row_id"],
            item["blocked_gold_relations"],
        ]
        for item in report["rows_by_blocked_relation_count"]
    ]
    near_rows = [
        [
            item["row_id"],
            item["type"],
            truncate(item["gold_normalized"], 60),
            truncate(item["pred_normalized"], 60),
            item["similarity"],
        ]
        for item in report["near_misses"][:15]
    ]
    overlong_rows = [
        [
            item["row_id"],
            item["type"],
            truncate(item["mention"], 50),
            truncate(item["normalized_name"], 70),
            item["normalized_len"] - item["mention_len"],
        ]
        for item in report["overlong_predictions"][:15]
    ]
    blocked_examples = [
        [
            item["row_id"],
            truncate(item["source"], 45),
            item["relation_type"],
            truncate(item["target"], 45),
            "Y" if item["missing_source"] else "",
            "Y" if item["missing_target"] else "",
        ]
        for item in report["relation_blocked_examples"][:15]
    ]

    lines = [
        "# Entity Error Analysis",
        "",
        "## Summary",
        "",
        markdown_table(
            ["metric", "value"],
            [
                ["rows", summary["rows"]],
                ["error_rows", summary["error_rows"]],
                ["entity_normalized_f1", summary["entity_normalized"]["f1"]],
                ["entity_mention_type_f1", summary["entity_mention_type"]["f1"]],
                ["relation_strict_f1", summary["relation_strict"]["f1"]],
                ["gold_relations", summary["gold_relations"]],
                ["relation_endpoint_possible", summary["relation_endpoint_possible"]],
                ["relation_endpoint_blocked", summary["relation_endpoint_blocked"]],
                [
                    "relation_endpoint_possible_recall_ceiling",
                    summary["relation_endpoint_possible_recall_ceiling"],
                ],
                ["possible_relation_matched", summary["possible_relation_matched"]],
                ["possible_relation_missed", summary["possible_relation_missed"]],
                ["near_miss_pairs", summary["near_miss_pairs"]],
                ["overlong_extra_predictions", summary["overlong_extra_predictions"]],
            ],
        ),
        "",
        "## Type Breakdown",
        "",
        markdown_table(["type", "gold", "pred", "tp", "missing", "extra"], type_rows),
        "",
        "## Rows With Most Gold Relations Blocked By Entity Candidates",
        "",
        markdown_table(["row_id", "blocked_gold_relations"], blocked_rows),
        "",
        "## Near-Miss Normalized Entity Pairs",
        "",
        markdown_table(["row_id", "type", "gold_normalized", "pred_normalized", "similarity"], near_rows),
        "",
        "## Overlong Extra Predictions",
        "",
        markdown_table(["row_id", "type", "mention", "normalized_name", "extra_len"], overlong_rows),
        "",
        "## Blocked Relation Examples",
        "",
        markdown_table(
            ["row_id", "source", "relation_type", "target", "missing_source", "missing_target"],
            blocked_examples,
        ),
        "",
        "## Suggested Direction",
        "",
        "- Prioritize entity_v2 data cleanup because many gold relations are blocked before relation inference.",
        "- Focus first on entity types with high missing and extra counts.",
        "- Review near-miss normalized pairs to write normalization rules and rewrite training targets.",
        "- Keep relation_e35 as the relation stage until entity_v2 improves candidate recall.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-output", type=Path, required=True)
    parser.add_argument("--final-output", type=Path)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--near-threshold", type=float, default=0.55)
    parser.add_argument("--long-delta", type=int, default=8)
    parser.add_argument("--long-min", type=int, default=24)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = analyze(args)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.out_md)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"json -> {args.out_json}")
    print(f"markdown -> {args.out_md}")


if __name__ == "__main__":
    main()
