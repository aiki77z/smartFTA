from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_local_env
from import_cluster_intermediate_to_kb import (
    ENTITY_TYPE_MAP,
    RELATION_TYPE_MAP,
    clean_scalar,
    cypher_ident,
    dedupe_keep_order,
    entity_label,
    entity_type_code,
    entity_type_zh,
    json_dumps,
    relation_ident,
    relation_type_code,
    relation_type_zh,
    split_semicolon,
)
from top_event_catalog_utils import (
    build_top_event_embedding_fields,
    build_top_event_semantic_text,
    load_top_event_embedding_env,
    normalize_catalog_name,
)

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None  # type: ignore

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover
    MongoClient = None  # type: ignore


CURRENT_DIR = Path(__file__).resolve().parent
ENTITY_CLUSTERING_DIR = CURRENT_DIR / "entity_clustering"
if str(ENTITY_CLUSTERING_DIR) not in sys.path:
    sys.path.insert(0, str(ENTITY_CLUSTERING_DIR))

from cluster_entities import (  # noqa: E402
    Cluster,
    Mention,
    cosine_similarity,
    dedupe_evidence,
    evidence_qualifier_conflict,
    hard_conflict,
    normalize_name,
    normalized_text_similarity,
    soft_neighbor_similarity,
)


def to_zh_entity_type(value: Any, explicit_zh: Any = "") -> str:
    zh = entity_type_zh(value, explicit_zh)
    if zh in ENTITY_TYPE_MAP:
        return zh
    code = entity_type_code(value)
    reverse = {v: k for k, v in ENTITY_TYPE_MAP.items()}
    return reverse.get(code, "")


def file_scope(file_id: str, file_version_id: str) -> str:
    return f"{file_id}::{file_version_id}"


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    text = clean_scalar(value)
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def merge_json_evidence(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    existing_items = parse_json_list(existing)
    incoming_items = incoming if isinstance(incoming, list) else parse_json_list(incoming)
    return dedupe_evidence([item for item in [*existing_items, *incoming_items] if isinstance(item, dict)])


def enrich_evidence(evidence: list[dict[str, Any]], *, file_id: str, file_version_id: str) -> list[dict[str, Any]]:
    enriched = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        next_item.setdefault("file_id", file_id)
        next_item.setdefault("file_version_id", file_version_id)
        chunk_id = clean_scalar(next_item.get("chunk_id"))
        if chunk_id:
            next_item.setdefault("chunk_ref", f"{file_version_id}::{chunk_id}")
        enriched.append(next_item)
    return dedupe_evidence(enriched)


def evidence_chunk_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return dedupe_keep_order([item.get("chunk_id") for item in evidence if isinstance(item, dict)])


def evidence_chunk_refs(evidence: list[dict[str, Any]], file_version_id: str) -> list[str]:
    refs = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        chunk_ref = clean_scalar(item.get("chunk_ref"))
        if chunk_ref:
            refs.append(chunk_ref)
            continue
        chunk_id = clean_scalar(item.get("chunk_id"))
        if chunk_id:
            refs.append(f"{file_version_id}::{chunk_id}")
    return dedupe_keep_order(refs)


def neo4j_property_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
            return ["" if item is None else item for item in value]
        return json_dumps(value)
    if isinstance(value, dict):
        return json_dumps(value)
    return str(value)


def sanitize_neo4j_props(row: dict[str, Any]) -> dict[str, Any]:
    return {key: neo4j_property_value(value) for key, value in row.items()}


def normalize_chunk_row(chunk: dict[str, Any], *, file_id: str, file_version_id: str) -> dict[str, Any]:
    row = dict(chunk or {})
    chunk_id = clean_scalar(row.get("chunk_id") if row.get("chunk_id") not in (None, "") else row.get("id"))
    if not chunk_id:
        return {}
    row["id"] = clean_scalar(row.get("id")) or chunk_id
    row["chunk_id"] = chunk_id
    row["chunk_uid"] = clean_scalar(row.get("chunk_uid")) or f"{file_version_id}::{chunk_id}"
    row["file_id"] = file_id
    row["file_version_id"] = file_version_id

    body = ""
    for key in ("content", "text", "markdown", "raw_text", "page_content", "body"):
        value = row.get(key)
        if value not in (None, ""):
            body = str(value)
            break
    if body:
        row.setdefault("content", body)
        row.setdefault("text", body)
        row.setdefault("markdown", body)

    chunk_name = clean_scalar(row.get("chunk_name") or row.get("title") or row.get("heading") or row.get("chapter"))
    section_path = clean_scalar(row.get("section_path") or row.get("section") or row.get("chapter_id") or row.get("chapter"))
    chapter = clean_scalar(row.get("chapter") or row.get("section") or chunk_name)
    if chunk_name:
        row.setdefault("chunk_name", chunk_name)
        row.setdefault("chapter_title", clean_scalar(row.get("chapter_title")) or chunk_name)
    if section_path:
        row.setdefault("section_path", section_path)
        row.setdefault("chapter_id", clean_scalar(row.get("chapter_id")) or section_path)
    if chapter:
        row.setdefault("chapter", chapter)
    row["source_file_ids"] = [file_id]
    row["source_file_version_ids"] = [file_version_id]
    return sanitize_neo4j_props(row)


def cluster_to_pseudo_mention(cluster: dict[str, Any], *, file_id: str, file_version_id: str) -> Mention:
    evidence = enrich_evidence(cluster.get("evidence") or [], file_id=file_id, file_version_id=file_version_id)
    canonical = clean_scalar(cluster.get("canonical_name"))
    aliases = dedupe_keep_order(cluster.get("aliases") or [])
    normalized_name = canonical or (aliases[0] if aliases else "")
    return Mention(
        mention_id=clean_scalar(cluster.get("cluster_id")),
        local_entity_id=clean_scalar(cluster.get("cluster_id")),
        file_id=file_id,
        sample_id=f"{file_version_id}#cluster",
        chapter_id="",
        source_type="cluster_intermediate",
        entity_type=to_zh_entity_type(cluster.get("entity_type"), cluster.get("entity_type_zh")),
        mention=canonical,
        normalized_name=normalized_name,
        evidence=evidence,
        chunk_ids=evidence_chunk_ids(evidence),
        neighbor_tokens=set(cluster.get("neighbor_tokens") or []),
        embedding=[],
    )


def existing_row_to_cluster(row: dict[str, Any]) -> Cluster:
    evidence = parse_json_list(row.get("evidence_json"))
    aliases = set(dedupe_keep_order(row.get("aliases") or []))
    mention_ids = dedupe_keep_order(row.get("mention_ids") or [])
    return Cluster(
        cluster_id=clean_scalar(row.get("cluster_id")),
        file_id=clean_scalar(row.get("file_id")),
        entity_type=to_zh_entity_type(row.get("entity_type"), row.get("entity_type_zh")),
        canonical_name=clean_scalar(row.get("canonical_name") or row.get("name")),
        mention_ids=mention_ids or [clean_scalar(row.get("cluster_id"))],
        aliases=aliases,
        evidence=[item for item in evidence if isinstance(item, dict)],
        neighbor_tokens=set(dedupe_keep_order(row.get("neighbor_tokens") or [])),
        merge_reasons=set(dedupe_keep_order(row.get("merge_reasons") or [])),
        embedding=[],
    )


def load_existing_clusters(session: Any, *, exclude_file_version_id: str = "") -> list[dict[str, Any]]:
    rows = session.run(
        """
        MATCH (e:EntityCluster)
        WITH e,
          CASE WHEN 'source_file_ids' IN keys(e) THEN e['source_file_ids']
               WHEN 'file_id' IN keys(e) THEN [e['file_id']]
               ELSE [] END AS source_file_ids,
          CASE WHEN 'source_file_version_ids' IN keys(e) THEN e['source_file_version_ids']
               WHEN 'file_version_id' IN keys(e) THEN [e['file_version_id']]
               ELSE [] END AS source_file_version_ids
        WHERE $exclude_file_version_id = ''
           OR NOT source_file_version_ids = [$exclude_file_version_id]
        RETURN
          e.cluster_id AS cluster_id,
          source_file_ids[0] AS file_id,
          source_file_version_ids[0] AS file_version_id,
          e.entity_type AS entity_type,
          e.entity_type_zh AS entity_type_zh,
          e.canonical_name AS canonical_name,
          e.name AS name,
          coalesce(e.aliases, []) AS aliases,
          coalesce(e.mention_ids, []) AS mention_ids,
          coalesce(e.neighbor_tokens, []) AS neighbor_tokens,
          coalesce(e.merge_reasons, []) AS merge_reasons,
          source_file_ids AS source_file_ids,
          source_file_version_ids AS source_file_version_ids,
          coalesce(e.source_file_scopes, []) AS source_file_scopes,
          coalesce(e.chunk_refs, []) AS chunk_refs,
          coalesce(e.evidence_json, '[]') AS evidence_json
        """,
        exclude_file_version_id=exclude_file_version_id,
    ).data()
    return rows


def best_name_similarity(new_cluster: dict[str, Any], existing: Cluster) -> float:
    names = dedupe_keep_order(
        [
            new_cluster.get("canonical_name"),
            *(new_cluster.get("aliases") or []),
        ]
    )
    existing_names = dedupe_keep_order([existing.canonical_name, *sorted(existing.aliases)])
    best = 0.0
    for left in names:
        for right in existing_names:
            best = max(best, normalized_text_similarity(left, right))
    return best


def type_specific_score(new_mention: Mention, existing: Cluster, name_score: float, neighbor_score: float) -> float:
    if new_mention.entity_type == "报警码":
        return 1.0 if name_score >= 1.0 else 0.0
    if new_mention.entity_type == "触发规则":
        return 1.0 if name_score >= 0.92 else 0.0
    if new_mention.entity_type == "维修方法":
        return 0.8 if name_score >= 0.88 else 0.5 * neighbor_score
    if new_mention.entity_type == "故障事件":
        return 0.7 if name_score >= 0.90 else 0.5 * neighbor_score
    return 0.0


def score_cross_file_candidate(new_cluster: dict[str, Any], existing_row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    file_id = clean_scalar(new_cluster.get("_file_id"))
    file_version_id = clean_scalar(new_cluster.get("_file_version_id"))
    new_mention = cluster_to_pseudo_mention(new_cluster, file_id=file_id, file_version_id=file_version_id)
    existing = existing_row_to_cluster(existing_row)
    if not new_mention.entity_type or not existing.entity_type or new_mention.entity_type != existing.entity_type:
        return 0.0, {"blocked": "entity_type_mismatch"}
    if hard_conflict(new_mention, existing):
        return 0.0, {"blocked": "hard_conflict"}
    if evidence_qualifier_conflict(new_mention, existing):
        return 0.0, {"blocked": "evidence_qualifier_conflict"}

    n_score = best_name_similarity(new_cluster, existing)
    neighbor_score = soft_neighbor_similarity(new_mention.neighbor_tokens, existing.neighbor_tokens)
    embedding_score = cosine_similarity(new_mention.embedding, existing.embedding)
    specific_score = type_specific_score(new_mention, existing, n_score, neighbor_score)
    score = 0.45 * n_score + 0.35 * embedding_score + 0.15 * neighbor_score + 0.05 * specific_score
    semantic_boost = False
    if n_score >= 0.92:
        score = max(score, 0.93)
        semantic_boost = True
    if n_score >= 0.86 and neighbor_score >= 0.65:
        score = max(score, 0.91)
    if new_mention.entity_type == "报警码" and n_score >= 1.0:
        score = 1.0
    return score, {
        "name_score": round(n_score, 4),
        "embedding_score": round(embedding_score, 4),
        "neighbor_score": round(neighbor_score, 4),
        "type_specific_score": round(specific_score, 4),
        "semantic_boost": semantic_boost,
    }


def rank_cross_file_candidates(
    new_cluster: dict[str, Any],
    existing_rows: list[dict[str, Any]],
    *,
    min_name: float,
    min_neighbor: float,
    top_k: int,
) -> list[dict[str, Any]]:
    ranked = []
    for row in existing_rows:
        if entity_type_code(row.get("entity_type")) != entity_type_code(new_cluster.get("entity_type")):
            continue
        score, detail = score_cross_file_candidate(new_cluster, row)
        if score <= 0:
            continue
        if detail.get("name_score", 0.0) < min_name and detail.get("neighbor_score", 0.0) < min_neighbor:
            continue
        ranked.append(
            {
                "score": round(score, 4),
                "candidate_cluster_id": clean_scalar(row.get("cluster_id")),
                "candidate_canonical_name": clean_scalar(row.get("canonical_name") or row.get("name")),
                "detail": detail,
                "row": row,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:top_k] if top_k > 0 else ranked


def choose_cluster_mapping(
    data: dict[str, Any],
    existing_rows: list[dict[str, Any]],
    *,
    file_id: str,
    file_version_id: str,
    auto_threshold: float,
    min_name: float,
    min_neighbor: float,
    top_k: int,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    mapping: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    for cluster in data.get("clusters", []):
        local_cluster_id = clean_scalar(cluster.get("cluster_id"))
        if not local_cluster_id:
            continue
        cluster["_file_id"] = file_id
        cluster["_file_version_id"] = file_version_id
        candidates = rank_cross_file_candidates(
            cluster,
            existing_rows,
            min_name=min_name,
            min_neighbor=min_neighbor,
            top_k=top_k,
        )
        best = candidates[0] if candidates else None
        if best and best["score"] >= auto_threshold:
            mapping[local_cluster_id] = best["candidate_cluster_id"]
            action = "merge_existing"
        else:
            mapping[local_cluster_id] = local_cluster_id
            action = "create_new"
        diagnostics.append(
            {
                "local_cluster_id": local_cluster_id,
                "local_canonical_name": clean_scalar(cluster.get("canonical_name")),
                "entity_type": entity_type_code(cluster.get("entity_type")),
                "action": action,
                "mapped_cluster_id": mapping[local_cluster_id],
                "best_candidate": {
                    key: value
                    for key, value in (best or {}).items()
                    if key != "row"
                },
                "candidate_count": len(candidates),
            }
        )
    return mapping, diagnostics


def collect_chunks(data: dict[str, Any], *, file_id: str, file_version_id: str) -> list[dict[str, Any]]:
    if isinstance(data.get("chunks"), list) and data.get("chunks"):
        return [
            row
            for row in (
                normalize_chunk_row(chunk, file_id=file_id, file_version_id=file_version_id)
                for chunk in data.get("chunks", [])
            )
            if row
        ]

    chunk_ids: set[str] = set()
    for mention in data.get("mentions", []):
        chunk_ids.update(split_semicolon(mention.get("chunk_ids")))
        chunk_ids.update(evidence_chunk_ids(mention.get("evidence") or []))
    for relation in data.get("raw_relations", []):
        chunk_ids.update(split_semicolon(relation.get("involved_chunk_ids")))
        chunk_ids.update(evidence_chunk_ids(relation.get("evidence") or []))
    return [
        {
            "chunk_uid": f"{file_version_id}::{chunk_id}",
            "file_id": file_id,
            "file_version_id": file_version_id,
            "chunk_id": chunk_id,
            "source_file_ids": [file_id],
            "source_file_version_ids": [file_version_id],
        }
        for chunk_id in sorted(chunk_ids, key=lambda value: (not str(value).isdigit(), str(value)))
    ]


def merge_list(existing: Any, incoming: list[Any]) -> list[str]:
    return dedupe_keep_order([*(existing or []), *incoming])


def parse_existing_evidence_json(value: Any) -> list[dict[str, Any]]:
    return [item for item in parse_json_list(value) if isinstance(item, dict)]


def upsert_entity_cluster(
    session: Any,
    *,
    cluster: dict[str, Any],
    mapped_cluster_id: str,
    file_id: str,
    file_version_id: str,
) -> None:
    label = entity_label(cluster.get("entity_type"))
    if not label:
        return
    code = entity_type_code(cluster.get("entity_type"))
    zh = entity_type_zh(cluster.get("entity_type"), cluster.get("entity_type_zh"))
    evidence = enrich_evidence(cluster.get("evidence") or [], file_id=file_id, file_version_id=file_version_id)
    aliases = dedupe_keep_order([cluster.get("canonical_name"), *(cluster.get("aliases") or [])])
    mention_ids = dedupe_keep_order(cluster.get("mention_ids") or [])
    existing = session.run(
        "MATCH (e:EntityCluster {cluster_id: $cluster_id}) RETURN e LIMIT 1",
        cluster_id=mapped_cluster_id,
    ).single()
    props = {
        "cluster_id": mapped_cluster_id,
        "entity_type": code,
        "entity_type_code": code,
        "entity_type_zh": zh,
        "canonical_name": clean_scalar(cluster.get("canonical_name")),
        "name": clean_scalar(cluster.get("canonical_name")),
        "aliases": aliases,
        "mention_ids": mention_ids,
        "mention_count": len(mention_ids),
        "chunk_ids": evidence_chunk_ids(evidence),
        "source_file_ids": [file_id],
        "source_file_version_ids": [file_version_id],
        "neighbor_tokens": dedupe_keep_order(cluster.get("neighbor_tokens") or []),
        "merge_reasons": dedupe_keep_order(cluster.get("merge_reasons") or []),
        "evidence_json": json_dumps(evidence),
        "embedding_dim": int(cluster.get("embedding_dim") or 0),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        node = dict(existing["e"])
        merged_evidence = merge_json_evidence(node.get("evidence_json"), evidence)
        props["canonical_name"] = clean_scalar(node.get("canonical_name")) or props["canonical_name"]
        props["name"] = props["canonical_name"]
        props["aliases"] = merge_list(node.get("aliases"), aliases)
        props["mention_ids"] = merge_list(node.get("mention_ids"), mention_ids)
        props["mention_count"] = len(props["mention_ids"])
        props["chunk_ids"] = merge_list(node.get("chunk_ids"), evidence_chunk_ids(evidence))
        props["source_file_ids"] = merge_list(node.get("source_file_ids") or ([node.get("file_id")] if node.get("file_id") else []), [file_id])
        props["source_file_version_ids"] = merge_list(
            node.get("source_file_version_ids") or ([node.get("file_version_id")] if node.get("file_version_id") else []),
            [file_version_id],
        )
        props["neighbor_tokens"] = merge_list(node.get("neighbor_tokens"), props["neighbor_tokens"])
        props["merge_reasons"] = merge_list(node.get("merge_reasons"), props["merge_reasons"] + [f"cross_file:{file_version_id}"])
        props["evidence_json"] = json_dumps(merged_evidence)
    session.run(
        f"""
        MERGE (e:EntityCluster:{label} {{cluster_id: $cluster_id}})
        SET e += $props
        REMOVE e.file_id, e.file_version_id, e.chunk_refs, e.source_file_scopes
        """,
        cluster_id=mapped_cluster_id,
        props=props,
    ).consume()


def upsert_file_and_chunks(session: Any, *, chunks: list[dict[str, Any]], file_id: str, file_version_id: str, file_name: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    session.run(
        """
        MERGE (f:File {file_version_id: $file_version_id})
        SET f.file_id = $file_id,
            f.file_version_id = $file_version_id,
            f.file_name = $file_name,
            f.source_file_ids = [$file_id],
            f.source_file_version_ids = [$file_version_id],
            f.updated_at = $updated_at
        """,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        updated_at=now,
    ).consume()
    session.run(
        """
        UNWIND $rows AS row
        MERGE (c:Chunk {chunk_uid: row.chunk_uid})
        SET c += row,
            c.updated_at = $updated_at
        WITH c
        MATCH (f:File {file_version_id: c.file_version_id})
        MERGE (f)-[:HAS_CHUNK]->(c)
        REMOVE c.source_file_scopes
        """,
        rows=chunks,
        updated_at=now,
    ).consume()


def upsert_mentions(session: Any, *, data: dict[str, Any], cluster_mapping: dict[str, str], file_id: str, file_version_id: str) -> None:
    rows_by_type: dict[str, list[dict[str, Any]]] = {}
    mention_to_cluster = data.get("mention_to_cluster") or {}
    now = datetime.now(timezone.utc).isoformat()
    for mention in data.get("mentions", []):
        mention_id = clean_scalar(mention.get("mention_id"))
        if not mention_id:
            continue
        local_cluster_id = clean_scalar(mention.get("cluster_id")) or clean_scalar(mention_to_cluster.get(mention_id))
        mapped_cluster_id = cluster_mapping.get(local_cluster_id, local_cluster_id)
        code = entity_type_code(mention.get("entity_type"))
        zh = entity_type_zh(mention.get("entity_type"), mention.get("entity_type_zh"))
        if not code:
            continue
        evidence = enrich_evidence(mention.get("evidence") or [], file_id=file_id, file_version_id=file_version_id)
        chunk_ids = split_semicolon(mention.get("chunk_ids")) or evidence_chunk_ids(evidence)
        rows_by_type.setdefault(code, []).append(
            {
                "mention_id": mention_id,
                "cluster_id": mapped_cluster_id,
                "props": {
                    "mention_id": mention_id,
                    "file_id": file_id,
                    "file_version_id": file_version_id,
                    "sample_id": clean_scalar(mention.get("sample_id")),
                    "chapter_id": clean_scalar(mention.get("chapter_id")),
                    "source_type": clean_scalar(mention.get("source_type")),
                    "entity_type": code,
                    "entity_type_code": code,
                    "entity_type_zh": zh,
                    "mention": clean_scalar(mention.get("mention")),
                    "normalized_name": clean_scalar(mention.get("normalized_name")),
                    "chunk_ids": chunk_ids,
                    "source_file_ids": [file_id],
                    "source_file_version_ids": [file_version_id],
                    "neighbor_tokens": dedupe_keep_order(mention.get("neighbor_tokens") or []),
                    "evidence_json": json_dumps(evidence),
                    "cluster_id": mapped_cluster_id,
                    "embedding_dim": int(mention.get("embedding_dim") or 0),
                    "updated_at": now,
                },
            }
        )
    for code, rows in rows_by_type.items():
        session.run(
            f"""
            UNWIND $rows AS row
            MERGE (m:Mention:{cypher_ident(code)} {{mention_id: row.mention_id}})
            SET m += row.props
            WITH m, row
            MATCH (e:EntityCluster {{cluster_id: row.cluster_id}})
            MERGE (m)-[rr:RESOLVED_TO]->(e)
            SET rr.file_id = $file_id,
                rr.file_version_id = $file_version_id,
                rr.source_file_ids = [$file_id],
                rr.source_file_version_ids = [$file_version_id],
                rr.updated_at = $updated_at
            REMOVE rr.source_file_scopes
            WITH m, row
            UNWIND row.props.chunk_ids AS chunk_id
            MATCH (c:Chunk {{chunk_uid: $file_version_id + '::' + chunk_id}})
            MERGE (m)-[ei:EVIDENCED_IN]->(c)
            SET ei.file_id = $file_id,
                ei.file_version_id = $file_version_id,
                ei.source_file_ids = [$file_id],
                ei.source_file_version_ids = [$file_version_id],
                ei.updated_at = $updated_at
            REMOVE ei.source_file_scopes
            """,
            rows=rows,
            file_id=file_id,
            file_version_id=file_version_id,
            updated_at=now,
        ).consume()


def upsert_mention_relations(session: Any, *, data: dict[str, Any], file_id: str, file_version_id: str) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    now = datetime.now(timezone.utc).isoformat()
    for rel in data.get("raw_relations", []):
        code = relation_type_code(rel.get("relation_type"))
        if not code:
            continue
        relation_id = clean_scalar(rel.get("relation_id"))
        evidence = enrich_evidence(rel.get("evidence") or [], file_id=file_id, file_version_id=file_version_id)
        grouped.setdefault(code, []).append(
            {
                "relation_id": relation_id,
                "source_id": clean_scalar(rel.get("source_mention_id")),
                "target_id": clean_scalar(rel.get("target_mention_id")),
                "props": {
                    "relation_id": relation_id,
                    "file_id": file_id,
                    "file_version_id": file_version_id,
                    "source_file_ids": [file_id],
                    "source_file_version_ids": [file_version_id],
                    "relation_type": code,
                    "relation_type_code": code,
                    "relation_type_zh": relation_type_zh(rel.get("relation_type")),
                    "relation_level": "mention",
                    "cross_chunk": clean_scalar(rel.get("cross_chunk")),
                    "involved_chunk_ids": split_semicolon(rel.get("involved_chunk_ids")),
                    "evidence_json": json_dumps(evidence),
                    "polarity": clean_scalar(rel.get("polarity")) or "positive",
                    "certainty": clean_scalar(rel.get("certainty")) or "certain",
                    "updated_at": now,
                },
            }
        )
    for code, rows in grouped.items():
        session.run(
            f"""
            UNWIND $rows AS row
            MATCH (a:Mention {{mention_id: row.source_id}})
            MATCH (b:Mention {{mention_id: row.target_id}})
            MERGE (a)-[r:{cypher_ident('MENTION_' + code)} {{relation_id: row.relation_id}}]->(b)
            SET r += row.props
            REMOVE r.source_file_scopes, r.involved_chunk_refs
            """,
            rows=rows,
        ).consume()
    return sum(len(rows) for rows in grouped.values())


def upsert_cluster_relation(
    session: Any,
    *,
    relation_type: str,
    source_id: str,
    target_id: str,
    relation_id: str,
    props: dict[str, Any],
) -> None:
    rel_type = cypher_ident("CLUSTERED_" + relation_type)
    existing = session.run(
        f"""
        MATCH (:EntityCluster {{cluster_id: $source_id}})-[r:{rel_type} {{relation_id: $relation_id}}]->(:EntityCluster {{cluster_id: $target_id}})
        RETURN r LIMIT 1
        """,
        source_id=source_id,
        target_id=target_id,
        relation_id=relation_id,
    ).single()
    if existing:
        current = dict(existing["r"])
        props["source_file_ids"] = merge_list(current.get("source_file_ids"), props["source_file_ids"])
        props["source_file_version_ids"] = merge_list(current.get("source_file_version_ids"), props["source_file_version_ids"])
        props["involved_chunk_ids"] = merge_list(current.get("involved_chunk_ids"), props["involved_chunk_ids"])
        props["source_relation_ids"] = merge_list(current.get("source_relation_ids"), props["source_relation_ids"])
        props["evidence_json"] = json_dumps(merge_json_evidence(current.get("evidence_json"), parse_json_list(props["evidence_json"])))
    session.run(
        f"""
        MATCH (a:EntityCluster {{cluster_id: $source_id}})
        MATCH (b:EntityCluster {{cluster_id: $target_id}})
        MERGE (a)-[r:{rel_type} {{relation_id: $relation_id}}]->(b)
        SET r += $props
        REMOVE r.relation_key, r.source_file_scopes, r.involved_chunk_refs
        """,
        source_id=source_id,
        target_id=target_id,
        relation_id=relation_id,
        props=props,
    ).consume()


def upsert_cluster_relations(
    session: Any,
    *,
    data: dict[str, Any],
    cluster_mapping: dict[str, str],
    file_id: str,
    file_version_id: str,
    skip_self_loops: bool,
) -> int:
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    for index, rel in enumerate(data.get("clustered_relations", []), start=1):
        code = relation_type_code(rel.get("relation_type"))
        if not code:
            continue
        source_id = cluster_mapping.get(clean_scalar(rel.get("source_cluster_id")), clean_scalar(rel.get("source_cluster_id")))
        target_id = cluster_mapping.get(clean_scalar(rel.get("target_cluster_id")), clean_scalar(rel.get("target_cluster_id")))
        if skip_self_loops and source_id == target_id:
            continue
        polarity = clean_scalar(rel.get("polarity")) or "positive"
        certainty = clean_scalar(rel.get("certainty")) or "certain"
        relation_id = f"{source_id}|{code}|{target_id}|{polarity}|{certainty}"
        involved_chunk_ids = split_semicolon(rel.get("involved_chunk_ids"))
        evidence = enrich_evidence(rel.get("evidence") or [], file_id=file_id, file_version_id=file_version_id)
        props = {
            "relation_id": relation_id,
            "file_id": file_id,
            "file_version_id": file_version_id,
            "source_file_ids": [file_id],
            "source_file_version_ids": [file_version_id],
            "relation_type": code,
            "relation_type_code": code,
            "relation_type_zh": relation_type_zh(rel.get("relation_type")),
            "relation_level": "clustered",
            "cross_chunk": clean_scalar(rel.get("cross_chunk")),
            "involved_chunk_ids": involved_chunk_ids,
            "evidence_json": json_dumps(evidence),
            "source_relation_ids": dedupe_keep_order(rel.get("source_relation_ids") or [f"{file_version_id}#clustered#{index}"]),
            "polarity": polarity,
            "certainty": certainty,
            "updated_at": now,
        }
        upsert_cluster_relation(
            session,
            relation_type=code,
            source_id=source_id,
            target_id=target_id,
            relation_id=relation_id,
            props=props,
        )
        count += 1
    return count


def upsert_top_event_catalog(
    *,
    data: dict[str, Any],
    cluster_mapping: dict[str, str],
    file_id: str,
    file_version_id: str,
    file_name: str,
) -> int:
    if MongoClient is None:
        return 0
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
    db = client[os.getenv("MONGO_DB_NAME", "fault-tree-trial")]
    now = datetime.now(timezone.utc)
    count = 0
    for cluster in data.get("clusters", []):
        if entity_type_code(cluster.get("entity_type")) != "FaultEvent":
            continue
        mapped_cluster_id = cluster_mapping.get(clean_scalar(cluster.get("cluster_id")), clean_scalar(cluster.get("cluster_id")))
        canonical = clean_scalar(cluster.get("canonical_name"))
        aliases = dedupe_keep_order(cluster.get("aliases") or [])
        evidence = enrich_evidence(cluster.get("evidence") or [], file_id=file_id, file_version_id=file_version_id)
        existing = db.top_event_catalog.find_one({"_id": f"cluster::{mapped_cluster_id}"}) or {}
        source_file_ids = merge_list(existing.get("source_file_ids") or ([existing.get("file_id")] if existing.get("file_id") else []), [file_id])
        source_file_version_ids = merge_list(
            existing.get("source_file_version_ids") or ([existing.get("file_version_id")] if existing.get("file_version_id") else []),
            [file_version_id],
        )
        normalized_name = normalize_catalog_name(clean_scalar(existing.get("normalized_name")) or canonical)
        normalized_aliases = merge_list(existing.get("normalized_aliases"), [normalize_catalog_name(alias) for alias in aliases])
        semantic_text = build_top_event_semantic_text(
            clean_scalar(existing.get("name")) or canonical,
            normalized_name=normalized_name,
            aliases=merge_list(existing.get("aliases"), aliases) + normalized_aliases,
        )
        embedding_payload = build_top_event_embedding_fields(semantic_text, existing=existing)
        doc = {
            "_id": f"cluster::{mapped_cluster_id}",
            "graph_node_id": mapped_cluster_id,
            "file_id": source_file_ids[0] if source_file_ids else file_id,
            "file_version_id": source_file_version_ids[0] if source_file_version_ids else file_version_id,
            "file_name": file_name,
            "source_file_ids": source_file_ids,
            "source_file_version_ids": source_file_version_ids,
            "source_file_scopes": merge_list(existing.get("source_file_scopes"), [file_scope(file_id, file_version_id)]),
            "is_active": True,
            "name": clean_scalar(existing.get("name")) or canonical,
            "display_name": clean_scalar(existing.get("display_name")) or canonical,
            "normalized_name": normalized_name,
            "aliases": merge_list(existing.get("aliases"), aliases),
            "normalized_aliases": normalized_aliases,
            "source_chunk_ids": merge_list(existing.get("source_chunk_ids"), evidence_chunk_ids(evidence)),
            "chunk_refs": merge_list(existing.get("chunk_refs"), evidence_chunk_refs(evidence, file_version_id)),
            "evidence_json": json_dumps(merge_json_evidence(existing.get("evidence_json"), evidence)),
            **embedding_payload,
            "updated_at": now,
        }
        db.top_event_catalog.update_one(
            {"_id": doc["_id"]},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        count += 1
    client.close()
    return count


def ensure_constraints(session: Any) -> None:
    session.run("CREATE CONSTRAINT fta_file_version IF NOT EXISTS FOR (n:File) REQUIRE n.file_version_id IS UNIQUE").consume()
    session.run("CREATE CONSTRAINT fta_chunk_uid IF NOT EXISTS FOR (n:Chunk) REQUIRE n.chunk_uid IS UNIQUE").consume()
    session.run("CREATE CONSTRAINT fta_mention_id IF NOT EXISTS FOR (n:Mention) REQUIRE n.mention_id IS UNIQUE").consume()
    session.run("CREATE CONSTRAINT fta_cluster_id IF NOT EXISTS FOR (n:EntityCluster) REQUIRE n.cluster_id IS UNIQUE").consume()


def write_diagnostics(path: Path | None, diagnostics: list[dict[str, Any]]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in diagnostics),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-file conservative EntityCluster merge/import step for SmartFTA KB.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--intermediate-json", required=True)
    parser.add_argument("--file-id", default="")
    parser.add_argument("--file-version-id", default="")
    parser.add_argument("--file-name", default="")
    parser.add_argument("--auto-threshold", type=float, default=0.93)
    parser.add_argument("--candidate-top-k", type=int, default=10)
    parser.add_argument("--candidate-min-name", type=float, default=0.70)
    parser.add_argument("--candidate-min-neighbor", type=float, default=0.60)
    parser.add_argument("--skip-mongo", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--diagnostics-jsonl", default="")
    parser.add_argument("--allow-self-loops", action="store_true")
    args = parser.parse_args()

    if GraphDatabase is None:
        raise RuntimeError("neo4j package is not installed")

    load_top_event_embedding_env(args.env_file)
    data = json.loads(Path(args.intermediate_json).read_text(encoding="utf-8"))
    file_id = clean_scalar(args.file_id) or clean_scalar(data.get("file_id"))
    if not file_id:
        raise ValueError("file_id is required")
    file_version_id = clean_scalar(args.file_version_id) or f"{file_id}_v1"
    file_name = clean_scalar(args.file_name) or file_id

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is empty")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as session:
        ensure_constraints(session)
        existing_rows = load_existing_clusters(session)
        cluster_mapping, diagnostics = choose_cluster_mapping(
            data,
            existing_rows,
            file_id=file_id,
            file_version_id=file_version_id,
            auto_threshold=args.auto_threshold,
            min_name=args.candidate_min_name,
            min_neighbor=args.candidate_min_neighbor,
            top_k=args.candidate_top_k,
        )
        if not args.dry_run:
            chunks = collect_chunks(data, file_id=file_id, file_version_id=file_version_id)
            upsert_file_and_chunks(session, chunks=chunks, file_id=file_id, file_version_id=file_version_id, file_name=file_name)
            for cluster in data.get("clusters", []):
                local_cluster_id = clean_scalar(cluster.get("cluster_id"))
                mapped_cluster_id = cluster_mapping.get(local_cluster_id, local_cluster_id)
                upsert_entity_cluster(
                    session,
                    cluster=cluster,
                    mapped_cluster_id=mapped_cluster_id,
                    file_id=file_id,
                    file_version_id=file_version_id,
                )
            upsert_mentions(session, data=data, cluster_mapping=cluster_mapping, file_id=file_id, file_version_id=file_version_id)
            mention_relations = upsert_mention_relations(session, data=data, file_id=file_id, file_version_id=file_version_id)
            clustered_relations = upsert_cluster_relations(
                session,
                data=data,
                cluster_mapping=cluster_mapping,
                file_id=file_id,
                file_version_id=file_version_id,
                skip_self_loops=not args.allow_self_loops,
            )
        else:
            mention_relations = 0
            clustered_relations = 0
    driver.close()

    mongo_top_events = 0
    if not args.dry_run and not args.skip_mongo:
        mongo_top_events = upsert_top_event_catalog(
            data=data,
            cluster_mapping=cluster_mapping,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
        )

    diagnostics_path = Path(args.diagnostics_jsonl) if args.diagnostics_jsonl else None
    write_diagnostics(diagnostics_path, diagnostics)
    merged = sum(1 for item in diagnostics if item["action"] == "merge_existing")
    created = sum(1 for item in diagnostics if item["action"] == "create_new")
    print(
        json.dumps(
            {
                "file_id": file_id,
                "file_version_id": file_version_id,
                "dry_run": bool(args.dry_run),
                "existing_clusters_scanned": len(existing_rows),
                "input_clusters": len(data.get("clusters", [])),
                "cross_file_merged_clusters": merged,
                "new_clusters": created,
                "mentions": len(data.get("mentions", [])),
                "mention_relations": mention_relations,
                "clustered_relations": clustered_relations,
                "mongo_top_event_catalog": mongo_top_events,
                "diagnostics_jsonl": str(diagnostics_path) if diagnostics_path else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
