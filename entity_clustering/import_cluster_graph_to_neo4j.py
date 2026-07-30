from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from neo4j import GraphDatabase
except ImportError as exc:
    raise SystemExit("Missing dependency: neo4j. Install with: pip install neo4j") from exc

CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))

from cluster_entities import (  # noqa: E402
    apply_existing_embeddings,
    build_embeddings,
    build_clustered_relations,
    cluster_mentions,
    export_results,
    json_dumps,
    load_mentions_and_relations,
)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def ensure_constraints(session: Any) -> None:
    statements = [
        "CREATE CONSTRAINT smartfta_chunk_key IF NOT EXISTS FOR (n:SmartFTAChunk) REQUIRE (n.file_id, n.chunk_id) IS UNIQUE",
        "CREATE CONSTRAINT smartfta_mention_id IF NOT EXISTS FOR (n:SmartFTAMention) REQUIRE n.mention_id IS UNIQUE",
        "CREATE CONSTRAINT smartfta_cluster_id IF NOT EXISTS FOR (n:SmartFTAEntityCluster) REQUIRE n.cluster_id IS UNIQUE",
    ]
    for statement in statements:
        session.run(statement)


def clear_file_graph(session: Any, file_id: str) -> None:
    session.run(
        """
        MATCH (n)
        WHERE n.file_id = $file_id
          AND (
            n:SmartFTAFile OR n:SmartFTAChunk OR n:SmartFTAMention OR
            n:SmartFTAEntityCluster OR n:SmartFTARawRelation OR n:SmartFTAClusteredRelation
          )
        DETACH DELETE n
        """,
        file_id=file_id,
    )


def evidence_chunks(evidence: list[dict[str, Any]]) -> list[str]:
    chunks = []
    for item in evidence:
        chunk_id = str(item.get("chunk_id") or "").strip()
        if chunk_id and chunk_id not in chunks:
            chunks.append(chunk_id)
    return chunks


def write_mentions(session: Any, file_id: str, mentions: list[Any]) -> None:
    rows = []
    chunk_rows = {}
    for mention in mentions:
        rows.append(
            {
                "mention_id": mention.mention_id,
                "file_id": mention.file_id,
                "sample_id": mention.sample_id,
                "chapter_id": mention.chapter_id,
                "source_type": mention.source_type,
                "entity_type": mention.entity_type,
                "mention": mention.mention,
                "normalized_name": mention.normalized_name,
                "chunk_ids": mention.chunk_ids,
                "evidence_json": json_dumps(mention.evidence),
                "neighbor_tokens": sorted(mention.neighbor_tokens),
                "embedding_dim": len(mention.embedding),
            }
        )
        for chunk_id in mention.chunk_ids:
            chunk_rows[(mention.file_id, chunk_id)] = {"file_id": mention.file_id, "chunk_id": chunk_id}

    if chunk_rows:
        session.run(
            """
            UNWIND $rows AS row
            MERGE (c:SmartFTAChunk {file_id: row.file_id, chunk_id: row.chunk_id})
            """,
            rows=list(chunk_rows.values()),
        )

    if rows:
        session.run(
            """
            UNWIND $rows AS row
            MERGE (m:SmartFTAMention {mention_id: row.mention_id})
            SET m.file_id = row.file_id,
                m.sample_id = row.sample_id,
                m.chapter_id = row.chapter_id,
                m.source_type = row.source_type,
                m.entity_type = row.entity_type,
                m.mention = row.mention,
                m.normalized_name = row.normalized_name,
                m.chunk_ids = row.chunk_ids,
                m.evidence_json = row.evidence_json,
                m.neighbor_tokens = row.neighbor_tokens,
                m.embedding_dim = row.embedding_dim
            """,
            rows=rows,
        )
        session.run(
            """
            UNWIND $rows AS row
            MATCH (m:SmartFTAMention {mention_id: row.mention_id})
            UNWIND row.chunk_ids AS chunk_id
            MATCH (c:SmartFTAChunk {file_id: row.file_id, chunk_id: chunk_id})
            MERGE (m)-[:EVIDENCED_IN]->(c)
            """,
            rows=rows,
        )


def write_raw_relations(session: Any, relations: list[Any]) -> None:
    rows = []
    for relation in relations:
        rows.append(
            {
                "relation_id": relation.relation_id,
                "file_id": relation.file_id,
                "sample_id": relation.sample_id,
                "source_mention_id": relation.source_mention_id,
                "target_mention_id": relation.target_mention_id,
                "source_text": relation.source_text,
                "target_text": relation.target_text,
                "relation_type": relation.relation_type,
                "cross_chunk": relation.cross_chunk,
                "involved_chunk_ids": relation.involved_chunk_ids,
                "evidence_json": json_dumps(relation.evidence),
                "polarity": relation.polarity,
                "certainty": relation.certainty,
            }
        )
    if not rows:
        return
    session.run(
        """
        UNWIND $rows AS row
        MATCH (source:SmartFTAMention {mention_id: row.source_mention_id})
        MATCH (target:SmartFTAMention {mention_id: row.target_mention_id})
        MERGE (source)-[rel:MENTION_RELATION {relation_id: row.relation_id}]->(target)
        SET rel.file_id = row.file_id,
            rel.sample_id = row.sample_id,
            rel.source_text = row.source_text,
            rel.target_text = row.target_text,
            rel.relation_type = row.relation_type,
            rel.cross_chunk = row.cross_chunk,
            rel.involved_chunk_ids = row.involved_chunk_ids,
            rel.evidence_json = row.evidence_json,
            rel.polarity = row.polarity,
            rel.certainty = row.certainty
        """,
        rows=rows,
    )


def write_clusters(session: Any, clusters: list[Any], mention_to_cluster: dict[str, str]) -> None:
    rows = []
    resolve_rows = []
    for cluster in clusters:
        rows.append(
            {
                "cluster_id": cluster.cluster_id,
                "file_id": cluster.file_id,
                "entity_type": cluster.entity_type,
                "canonical_name": cluster.canonical_name,
                "aliases": sorted(cluster.aliases),
                "mention_count": len(cluster.mention_ids),
                "evidence_json": json_dumps(cluster.evidence),
                "neighbor_tokens": sorted(cluster.neighbor_tokens),
                "merge_reasons": sorted(cluster.merge_reasons),
                "embedding_dim": len(cluster.embedding),
            }
        )
        for mention_id in cluster.mention_ids:
            resolve_rows.append({"mention_id": mention_id, "cluster_id": cluster.cluster_id})
    if rows:
        session.run(
            """
            UNWIND $rows AS row
            MERGE (c:SmartFTAEntityCluster {cluster_id: row.cluster_id})
            SET c.file_id = row.file_id,
                c.entity_type = row.entity_type,
                c.canonical_name = row.canonical_name,
                c.aliases = row.aliases,
                c.mention_count = row.mention_count,
                c.evidence_json = row.evidence_json,
                c.neighbor_tokens = row.neighbor_tokens,
                c.merge_reasons = row.merge_reasons,
                c.embedding_dim = row.embedding_dim
            """,
            rows=rows,
        )
    if resolve_rows:
        session.run(
            """
            UNWIND $rows AS row
            MATCH (m:SmartFTAMention {mention_id: row.mention_id})
            MATCH (c:SmartFTAEntityCluster {cluster_id: row.cluster_id})
            MERGE (m)-[:RESOLVED_TO]->(c)
            """,
            rows=resolve_rows,
        )


def write_clustered_relations(session: Any, clustered_relations: list[dict[str, Any]], file_id: str) -> None:
    rows = []
    for index, relation in enumerate(clustered_relations, 1):
        relation_id = f"{file_id}#CR{index:05d}"
        rows.append(
            {
                "relation_id": relation_id,
                "file_id": file_id,
                "source_cluster_id": relation["source_cluster_id"],
                "target_cluster_id": relation["target_cluster_id"],
                "relation_type": relation["relation_type"],
                "polarity": relation["polarity"],
                "certainty": relation["certainty"],
                "cross_chunk": relation["cross_chunk"],
                "involved_chunk_ids": relation["involved_chunk_ids"],
                "evidence_json": json_dumps(relation["evidence"]),
                "source_relation_ids": relation["source_relation_ids"],
            }
        )
    if not rows:
        return
    session.run(
        """
        UNWIND $rows AS row
        MATCH (source:SmartFTAEntityCluster {cluster_id: row.source_cluster_id})
        MATCH (target:SmartFTAEntityCluster {cluster_id: row.target_cluster_id})
        MERGE (source)-[rel:CLUSTERED_RELATION {relation_id: row.relation_id}]->(target)
        SET rel.file_id = row.file_id,
            rel.relation_type = row.relation_type,
            rel.polarity = row.polarity,
            rel.certainty = row.certainty,
            rel.cross_chunk = row.cross_chunk,
            rel.involved_chunk_ids = row.involved_chunk_ids,
            rel.evidence_json = row.evidence_json,
            rel.source_relation_ids = row.source_relation_ids
        """,
        rows=rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import mention-level and clustered entity graph to Neo4j.")
    parser.add_argument("--env-file", default=str(CURRENT_DIR / ".env"))
    parser.add_argument("--input-csv", default="")
    parser.add_argument("--file-id", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--auto-threshold", type=float, default=0.90)
    parser.add_argument("--review-threshold", type=float, default=0.72)
    parser.add_argument("--embedding-backend", choices=["none", "hash", "sentence-transformers", "openai-compatible"], default="")
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
    parser.add_argument("--candidate-top-k", type=int, default=20)
    parser.add_argument("--candidate-min-name", type=float, default=0.45)
    parser.add_argument("--candidate-min-embedding", type=float, default=0.88)
    parser.add_argument("--candidate-min-neighbor", type=float, default=0.35)
    parser.add_argument("--disable-refinement", action="store_true")
    parser.add_argument("--refinement-max-rounds", type=int, default=50)
    parser.add_argument("--refinement-merge-mode", choices=["single", "batch", "strong-batch"], default="single")
    parser.add_argument("--no-clear", action="store_true", help="Do not clear existing graph data for this file_id.")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    input_csv = args.input_csv or os.getenv("ENTITY_CLUSTER_INPUT_CSV", "")
    file_id = args.file_id or os.getenv("ENTITY_CLUSTER_FILE_ID", "")
    output_dir = args.output_dir or os.getenv("ENTITY_CLUSTER_OUTPUT_DIR", "output/entity_clustering")
    if not input_csv or not file_id:
        raise ValueError("Set --input-csv and --file-id, or configure ENTITY_CLUSTER_INPUT_CSV and ENTITY_CLUSTER_FILE_ID.")

    uri = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not password:
        raise ValueError("NEO4J_PASSWORD is required.")

    mentions, relations = load_mentions_and_relations(Path(input_csv), file_id)
    embedding_backend = args.embedding_backend or os.getenv("ENTITY_CLUSTER_EMBEDDING_BACKEND", "none")
    embedding_model = args.embedding_model or os.getenv("ENTITY_CLUSTER_EMBEDDING_MODEL", "") or os.getenv("EMBEDDING_MODEL", "")
    embedding_api_key = args.embedding_api_key or os.getenv("ENTITY_CLUSTER_EMBEDDING_API_KEY", "") or os.getenv("EMBEDDING_API_KEY", "")
    embedding_base_url = (
        args.embedding_base_url
        or os.getenv("ENTITY_CLUSTER_EMBEDDING_BASE_URL", "")
        or os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    embedding_batch_size = args.embedding_batch_size or int(os.getenv("ENTITY_CLUSTER_EMBEDDING_BATCH_SIZE", os.getenv("EMBEDDING_BATCH_SIZE", "10")))
    reuse_embeddings_jsonl = args.reuse_embeddings_jsonl or os.getenv("ENTITY_CLUSTER_REUSE_EMBEDDINGS_JSONL", "")
    reused_embeddings = apply_existing_embeddings(mentions, Path(reuse_embeddings_jsonl)) if reuse_embeddings_jsonl else 0
    if reused_embeddings <= 0:
        build_embeddings(
            mentions,
            embedding_backend,
            embedding_model,
            args.embedding_dim,
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            batch_size=embedding_batch_size,
        )
    clusters, mention_to_cluster, review_candidates = cluster_mentions(
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
        refinement_max_rounds=args.refinement_max_rounds,
        refinement_merge_mode=args.refinement_merge_mode,
        candidate_top_k=args.candidate_top_k,
        candidate_min_name=args.candidate_min_name,
        candidate_min_embedding=args.candidate_min_embedding,
        candidate_min_neighbor=args.candidate_min_neighbor,
    )
    clustered_relations = build_clustered_relations(relations, mention_to_cluster)
    export_results(Path(output_dir), mentions, clusters, relations, clustered_relations, review_candidates, mention_to_cluster)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            ensure_constraints(session)
            if not args.no_clear:
                clear_file_graph(session, file_id)
            write_mentions(session, file_id, mentions)
            write_raw_relations(session, relations)
            write_clusters(session, clusters, mention_to_cluster)
            write_clustered_relations(session, clustered_relations, file_id)
    finally:
        driver.close()

    print(f"Neo4j database: {database}")
    print(f"file_id: {file_id}")
    print(f"mentions: {len(mentions)}")
    print(f"raw_relations: {len(relations)}")
    print(f"clusters: {len(clusters)}")
    print(f"clustered_relations: {len(clustered_relations)}")
    print(f"diagnostic_candidates: {len(review_candidates)}")
    print(f"reused_embeddings: {reused_embeddings}")
    print(f"output_dir: {output_dir}")


if __name__ == "__main__":
    main()
