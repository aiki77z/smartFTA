from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))

from cluster_entities import (  # noqa: E402
    build_embeddings,
    cluster_mentions,
    load_env,
    load_mentions_and_relations,
    normalize_name,
)


def pair_key(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))


def build_pairs(groups: dict[str, list[str]]) -> set[tuple[str, str]]:
    pairs = set()
    for mention_ids in groups.values():
        if len(mention_ids) < 2:
            continue
        for left, right in combinations(sorted(mention_ids), 2):
            pairs.add(pair_key(left, right))
    return pairs


def weak_gold_groups(mentions: list[Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for mention in mentions:
        key = "|".join(
            [
                mention.file_id,
                mention.entity_type,
                normalize_name(mention.normalized_name or mention.mention),
            ]
        )
        if key.endswith("|"):
            continue
        groups[key].append(mention.mention_id)
    return groups


def predicted_groups(mention_to_cluster: dict[str, str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for mention_id, cluster_id in mention_to_cluster.items():
        groups[cluster_id].append(mention_id)
    return groups


def pairwise_metrics(pred_pairs: set[tuple[str, str]], gold_pairs: set[tuple[str, str]]) -> dict[str, float | int]:
    tp = len(pred_pairs & gold_pairs)
    fp = len(pred_pairs - gold_pairs)
    fn = len(gold_pairs - pred_pairs)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def fragmentation_report(gold_groups: dict[str, list[str]], mention_to_cluster: dict[str, str], top_n: int) -> list[dict[str, Any]]:
    rows = []
    for gold_key, mention_ids in gold_groups.items():
        if len(mention_ids) < 2:
            continue
        cluster_ids = {mention_to_cluster.get(mention_id, "") for mention_id in mention_ids}
        cluster_ids.discard("")
        if len(cluster_ids) > 1:
            rows.append(
                {
                    "gold_key": gold_key,
                    "mention_count": len(mention_ids),
                    "pred_cluster_count": len(cluster_ids),
                    "pred_cluster_ids": ";".join(sorted(cluster_ids)),
                    "mention_ids": ";".join(sorted(mention_ids)),
                }
            )
    rows.sort(key=lambda item: (item["pred_cluster_count"], item["mention_count"]), reverse=True)
    return rows[:top_n]


def overmerge_report(
    pred_groups: dict[str, list[str]],
    mention_to_gold: dict[str, str],
    top_n: int,
) -> list[dict[str, Any]]:
    rows = []
    for cluster_id, mention_ids in pred_groups.items():
        gold_keys = {mention_to_gold.get(mention_id, "") for mention_id in mention_ids}
        gold_keys.discard("")
        if len(gold_keys) > 1:
            rows.append(
                {
                    "cluster_id": cluster_id,
                    "mention_count": len(mention_ids),
                    "gold_group_count": len(gold_keys),
                    "gold_keys": ";".join(sorted(gold_keys)),
                    "mention_ids": ";".join(sorted(mention_ids)),
                }
            )
    rows.sort(key=lambda item: (item["gold_group_count"], item["mention_count"]), reverse=True)
    return rows[:top_n]


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate entity clustering with weak normalized-name gold groups.")
    parser.add_argument("--env-file", default=str(CURRENT_DIR / ".env"))
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--auto-threshold", type=float, default=0.90)
    parser.add_argument("--review-threshold", type=float, default=0.72)
    parser.add_argument("--embedding-backend", choices=["none", "hash", "sentence-transformers", "openai-compatible"], default="none")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-api-key", default="")
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-batch-size", type=int, default=10)
    parser.add_argument("--embedding-dim", type=int, default=256)
    parser.add_argument("--reuse-embeddings-jsonl", default="")
    parser.add_argument("--name-weight", type=float, default=0.55)
    parser.add_argument("--embedding-weight", type=float, default=0.20)
    parser.add_argument("--neighbor-weight", type=float, default=0.20)
    parser.add_argument("--chunk-weight", type=float, default=0.05)
    parser.add_argument("--graph-boost-threshold", type=float, default=0.75)
    parser.add_argument("--graph-boost-min-name", type=float, default=0.45)
    parser.add_argument("--graph-boost-score", type=float, default=0.90)
    parser.add_argument("--semantic-boost-min-name", type=float, default=0.90)
    parser.add_argument("--semantic-boost-min-embedding", type=float, default=0.95)
    parser.add_argument("--semantic-boost-score", type=float, default=0.90)
    parser.add_argument("--disable-refinement", action="store_true")
    parser.add_argument("--top-n", type=int, default=50)
    args = parser.parse_args()

    load_env(Path(args.env_file))
    mentions, _relations = load_mentions_and_relations(Path(args.input_csv), args.file_id)
    reused_embeddings = 0
    if args.reuse_embeddings_jsonl:
        from cluster_entities import apply_existing_embeddings

        reused_embeddings = apply_existing_embeddings(mentions, Path(args.reuse_embeddings_jsonl))
    if reused_embeddings <= 0:
        build_embeddings(
            mentions,
            args.embedding_backend,
            args.embedding_model or os.getenv("EMBEDDING_MODEL", ""),
            args.embedding_dim,
            api_key=args.embedding_api_key or os.getenv("EMBEDDING_API_KEY", ""),
            base_url=args.embedding_base_url or os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            batch_size=args.embedding_batch_size or int(os.getenv("EMBEDDING_BATCH_SIZE", "10")),
        )
    clusters, mention_to_cluster, diagnostic_candidates = cluster_mentions(
        mentions,
        args.auto_threshold,
        args.review_threshold,
        name_weight=args.name_weight,
        embedding_weight=args.embedding_weight,
        neighbor_weight=args.neighbor_weight,
        chunk_weight=args.chunk_weight,
        graph_boost_threshold=args.graph_boost_threshold,
        graph_boost_min_name=args.graph_boost_min_name,
        graph_boost_score=args.graph_boost_score,
        semantic_boost_min_name=args.semantic_boost_min_name,
        semantic_boost_min_embedding=args.semantic_boost_min_embedding,
        semantic_boost_score=args.semantic_boost_score,
        enable_refinement=(not args.disable_refinement),
    )

    gold_groups = weak_gold_groups(mentions)
    pred_groups = predicted_groups(mention_to_cluster)
    gold_pairs = build_pairs(gold_groups)
    pred_pairs = build_pairs(pred_groups)
    metrics = pairwise_metrics(pred_pairs, gold_pairs)

    mention_to_gold = {}
    for gold_key, mention_ids in gold_groups.items():
        for mention_id in mention_ids:
            mention_to_gold[mention_id] = gold_key

    fragmentation = fragmentation_report(gold_groups, mention_to_cluster, args.top_n)
    overmerge = overmerge_report(pred_groups, mention_to_gold, args.top_n)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "file_id": args.file_id,
        "mentions": len(mentions),
        "clusters": len(clusters),
        "diagnostic_candidates": len(diagnostic_candidates),
        "gold_groups": len(gold_groups),
        "gold_pairs": len(gold_pairs),
        "pred_pairs": len(pred_pairs),
        "pairwise": metrics,
        "entity_type_mentions": dict(Counter(m.entity_type for m in mentions)),
        "entity_type_clusters": dict(Counter(c.entity_type for c in clusters)),
        "fragmentation_cases": len(fragmentation),
        "overmerge_cases": len(overmerge),
    }

    (output_dir / "cluster_eval_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(
        output_dir / "cluster_eval_fragmentation.csv",
        fragmentation,
        ["gold_key", "mention_count", "pred_cluster_count", "pred_cluster_ids", "mention_ids"],
    )
    write_csv(
        output_dir / "cluster_eval_overmerge.csv",
        overmerge,
        ["cluster_id", "mention_count", "gold_group_count", "gold_keys", "mention_ids"],
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Output dir: {output_dir}")


if __name__ == "__main__":
    main()
