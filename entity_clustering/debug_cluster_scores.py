from __future__ import annotations

import argparse
import csv
import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))

from cluster_entities import (  # noqa: E402
    Cluster,
    Mention,
    clean_scalar,
    cluster_score,
    cosine_similarity,
    dedupe_evidence,
    json_dumps,
    name_score,
    parse_json_cell,
    rank_candidate_clusters,
    soft_neighbor_similarity,
)


def split_semicolon(value: Any) -> list[str]:
    text = clean_scalar(value)
    if not text:
        return []
    return [item for item in text.split(";") if clean_scalar(item)]


def load_embeddings(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    embeddings: dict[str, list[float]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        mention_id = clean_scalar(item.get("mention_id"))
        vector = item.get("embedding")
        if mention_id and isinstance(vector, list):
            embeddings[mention_id] = [float(value) for value in vector]
    return embeddings


def load_mentions(output_dir: Path, embeddings: dict[str, list[float]]) -> dict[str, Mention]:
    mentions: dict[str, Mention] = {}
    with (output_dir / "entity_mentions.csv").open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            mention_id = clean_scalar(row.get("mention_id"))
            if not mention_id:
                continue
            evidence = dedupe_evidence(parse_json_cell(row.get("evidence")))
            mention = Mention(
                mention_id=mention_id,
                local_entity_id=mention_id.rsplit("#", 1)[-1],
                file_id=clean_scalar(row.get("file_id")),
                sample_id=clean_scalar(row.get("sample_id")),
                chapter_id="",
                source_type="",
                entity_type=clean_scalar(row.get("entity_type")),
                mention=clean_scalar(row.get("mention")),
                normalized_name=clean_scalar(row.get("normalized_name")),
                evidence=evidence,
                chunk_ids=split_semicolon(row.get("chunk_ids")),
                neighbor_tokens=set(split_semicolon(row.get("neighbor_tokens"))),
                embedding=embeddings.get(mention_id, []),
            )
            mentions[mention_id] = mention
    return mentions


def load_clusters(output_dir: Path, mentions: dict[str, Mention]) -> dict[str, Cluster]:
    clusters: dict[str, Cluster] = {}
    with (output_dir / "entity_clusters.csv").open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            cluster_id = clean_scalar(row.get("cluster_id"))
            if not cluster_id:
                continue
            mention_ids = split_semicolon(row.get("mention_ids"))
            cluster = Cluster(
                cluster_id=cluster_id,
                file_id=clean_scalar(row.get("file_id")),
                entity_type=clean_scalar(row.get("entity_type")),
                canonical_name=clean_scalar(row.get("canonical_name")),
                mention_ids=mention_ids,
                aliases=set(split_semicolon(row.get("aliases"))),
                evidence=dedupe_evidence(parse_json_cell(row.get("evidence"))),
                neighbor_tokens=set(split_semicolon(row.get("neighbor_tokens"))),
                merge_reasons=set(split_semicolon(row.get("merge_reasons"))),
            )
            vectors = [mentions[mid].embedding for mid in mention_ids if mid in mentions and mentions[mid].embedding]
            if vectors:
                dim = len(vectors[0])
                valid = [vec for vec in vectors if len(vec) == dim]
                if valid:
                    cluster.embedding = [sum(vec[index] for vec in valid) / len(valid) for index in range(dim)]
            clusters[cluster_id] = cluster
    return clusters


def score_mention_to_cluster(mention: Mention, cluster: Cluster, args: argparse.Namespace) -> dict[str, Any]:
    score, reasons = cluster_score(
        mention,
        cluster,
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
    )
    return {
        "score": round(score, 4),
        "name_score": round(name_score(mention, cluster), 4),
        "embedding_score": round(cosine_similarity(mention.embedding, cluster.embedding), 4),
        "neighbor_score": round(soft_neighbor_similarity(mention.neighbor_tokens, cluster.neighbor_tokens), 4),
        "chunk_score": 1.0
        if set(mention.chunk_ids)
        & {clean_scalar(item.get("chunk_id")) for item in cluster.evidence if isinstance(item, dict)}
        else 0.0,
        "reasons": ";".join(reasons),
    }


def mention_pair_score(left: Mention, right: Mention, args: argparse.Namespace) -> dict[str, Any]:
    local_cluster = cluster_from_mentions("pair", left, [])
    return score_mention_to_cluster(right, local_cluster, args)


def cluster_from_mentions(cluster_id: str, base: Mention, other_mentions: list[Mention]) -> Cluster:
    cluster = Cluster(
        cluster_id=cluster_id,
        file_id=base.file_id,
        entity_type=base.entity_type,
        canonical_name=base.normalized_name or base.mention,
        mention_ids=[base.mention_id],
        aliases={base.mention, base.normalized_name},
        evidence=list(base.evidence),
        neighbor_tokens=set(base.neighbor_tokens),
        embedding=list(base.embedding),
    )
    for mention in other_mentions:
        cluster.mention_ids.append(mention.mention_id)
        cluster.aliases.update([mention.mention, mention.normalized_name])
        cluster.evidence.extend(mention.evidence)
        cluster.neighbor_tokens.update(mention.neighbor_tokens)
        if mention.embedding and cluster.embedding and len(mention.embedding) == len(cluster.embedding):
            count = len(cluster.mention_ids)
            cluster.embedding = [
                (cluster.embedding[index] * (count - 1) + mention.embedding[index]) / count
                for index in range(len(cluster.embedding))
            ]
    return cluster


def scan_unmerged_pairs(
    mentions: dict[str, Mention],
    clusters: dict[str, Cluster],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    mention_to_cluster = {
        mention_id: cluster_id
        for cluster_id, cluster in clusters.items()
        for mention_id in cluster.mention_ids
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    active_clusters = [cluster for cluster in clusters.values() if cluster.mention_ids]
    for left in mentions.values():
        left_cluster_id = mention_to_cluster.get(left.mention_id, "")
        if not left_cluster_id:
            continue
        candidate_clusters = rank_candidate_clusters(
            left,
            active_clusters,
            exclude_cluster_id=left_cluster_id,
            top_k=args.scan_candidate_top_k,
            min_name=args.candidate_min_name,
            min_embedding=args.candidate_min_embedding,
            min_neighbor=args.candidate_min_neighbor,
        )
        for candidate_cluster, _candidate_scores in candidate_clusters:
            right_cluster_id = candidate_cluster.cluster_id
            if not right_cluster_id or left_cluster_id == right_cluster_id:
                continue
            pair_key = tuple(sorted([left_cluster_id, right_cluster_id])) + (left.mention_id,)
            if pair_key in seen:
                continue
            seen.add(pair_key)
            score_row = score_mention_to_cluster(left, candidate_cluster, args)
            if score_row["score"] < args.scan_min_score:
                continue
            right_mentions = [mentions[mid] for mid in candidate_cluster.mention_ids if mid in mentions]
            right = max(
                right_mentions,
                key=lambda item: mention_pair_score(left, item, args)["score"],
                default=None,
            )
            rows.append(
                {
                    "left": describe_mention(left),
                    "left_cluster_id": left_cluster_id,
                    "right": describe_mention(right) if right else candidate_cluster.canonical_name,
                    "right_cluster_id": right_cluster_id,
                    **score_row,
                    "candidate_canonical_name": candidate_cluster.canonical_name,
                }
            )
    rows.sort(key=lambda item: (float(item["score"]), float(item["embedding_score"]), float(item["name_score"])), reverse=True)
    return rows[: args.scan_limit]


def describe_mention(mention: Mention) -> str:
    chunks = ",".join(mention.chunk_ids)
    return f"{mention.normalized_name or mention.mention} [{mention.entity_type}] chunks={chunks}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug entity clustering score components from an existing output directory.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-file", default="", help="Write debug report as UTF-8 text instead of printing to stdout.")
    parser.add_argument("--merged-limit", type=int, default=8)
    parser.add_argument("--unmerged-limit", type=int, default=8)
    parser.add_argument("--scan-unmerged-pairs", action="store_true")
    parser.add_argument("--scan-limit", type=int, default=50)
    parser.add_argument("--scan-min-score", type=float, default=0.70)
    parser.add_argument("--scan-candidate-top-k", type=int, default=20)
    parser.add_argument("--candidate-min-name", type=float, default=0.45)
    parser.add_argument("--candidate-min-embedding", type=float, default=0.88)
    parser.add_argument("--candidate-min-neighbor", type=float, default=0.35)
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
    args = parser.parse_args()

    if args.output_file:
        output_file = Path(args.output_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = output_file.open("w", encoding="utf-8", newline="\n")

    output_dir = Path(args.output_dir)
    embeddings = load_embeddings(output_dir / "entity_embeddings.jsonl")
    mentions = load_mentions(output_dir, embeddings)
    clusters = load_clusters(output_dir, mentions)

    print(f"output_dir: {output_dir}")
    print(f"mentions: {len(mentions)} clusters: {len(clusters)} embeddings: {len(embeddings)}")
    print()

    print("[MERGED]")
    emitted = 0
    merged_clusters = sorted(
        [cluster for cluster in clusters.values() if len(cluster.mention_ids) > 1],
        key=lambda item: len(item.mention_ids),
        reverse=True,
    )
    for cluster in merged_clusters:
        ids = [mid for mid in cluster.mention_ids if mid in mentions]
        for left_id, right_id in combinations(ids, 2):
            left = mentions[left_id]
            right = mentions[right_id]
            local_cluster = cluster_from_mentions("pair", left, [])
            row = score_mention_to_cluster(right, local_cluster, args)
            print(json.dumps(
                {
                    "cluster_id": cluster.cluster_id,
                    "left": describe_mention(left),
                    "right": describe_mention(right),
                    **row,
                    "cluster_merge_reasons": sorted(cluster.merge_reasons),
                },
                ensure_ascii=False,
            ))
            emitted += 1
            if emitted >= args.merged_limit:
                break
        if emitted >= args.merged_limit:
            break
    if emitted == 0:
        print("(none)")

    if args.scan_unmerged_pairs:
        print()
        print("[SCANNED_UNMERGED_PAIRS]")
        scanned_rows = scan_unmerged_pairs(mentions, clusters, args)
        if not scanned_rows:
            print("(none)")
        for row in scanned_rows:
            print(json.dumps(row, ensure_ascii=False))
    print()

    print("[UNMERGED_DIAGNOSTIC_CANDIDATES]")
    diag_path = output_dir / "diagnostics" / "cluster_candidate_diagnostics.csv"
    emitted = 0
    if diag_path.exists():
        with diag_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                mention_id = clean_scalar(row.get("mention_id"))
                cluster_id = clean_scalar(row.get("candidate_cluster_id"))
                mention = mentions.get(mention_id)
                cluster = clusters.get(cluster_id)
                if not mention or not cluster:
                    continue
                score_row = score_mention_to_cluster(mention, cluster, args)
                print(json.dumps(
                    {
                        "mention": describe_mention(mention),
                        "candidate_cluster_id": cluster.cluster_id,
                        "candidate_canonical_name": cluster.canonical_name,
                        **score_row,
                        "candidate_aliases": sorted(cluster.aliases),
                    },
                    ensure_ascii=False,
                ))
                emitted += 1
                if emitted >= args.unmerged_limit:
                    break
    if emitted == 0:
        print("(none)")


if __name__ == "__main__":
    main()
