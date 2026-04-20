from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from database import (
    activate_file_version,
    archive_file,
    append_generation_job_item_event,
    assert_chunk_artifacts_align_with_file_version,
    assert_relation_artifacts_align_with_file_version,
    claim_generation_job_item,
    collect_subgraph_chunks,
    create_file_version_record,
    create_generation_job,
    create_generation_job_item,
    create_tree,
    expand_scoped_local_fault_subgraph,
    ensure_top_event_catalog_for_scope,
    ensure_top_event_catalog_embeddings,
    find_active_job_item_by_top_event,
    find_active_job_item_by_top_event_and_scope,
    find_latest_generation_job_by_scope,
    find_tree_by_top_event,
    get_top_event_catalog_by_graph_node_id,
    get_chunks_by_ids,
    import_chunks as import_chunks_to_db,
    import_entity_reverse_index as import_entity_reverse_index_to_db,
    get_chunk_by_id,
    list_all_chunks,
    get_generation_job,
    get_generation_job_item,
    get_tree_meta,
    get_version,
    get_version_list,
    list_all_chunks,
    list_graph_top_event_candidates,
    list_entity_reverse_index,
    list_generation_job_items,
    list_top_event_catalog,
    mark_file_version_import_failed,
    match_top_event_from_graph,
    rebuild_top_event_catalog_for_file_version,
    refresh_generation_job,
    prepare_generation_job_for_continue,
    resolve_selected_file_version_ids,
    resolve_top_event_catalog,
    rollback_version,
    save_version,
    search_top_event_catalog_semantic,
    top_event_catalog_col,
    try_mark_job_completion_logged,
    update_graph_node_properties,
    update_generation_job_item,
    update_tree_status,
    upsert_top_event_catalog_entry,
    versions_col,
)
from diff_analyzer import analyze_and_store, corrections_col, generate_change_description
from generator import (
    build_top_event_normalized_candidates,
    discover_top_events,
    discover_top_events_from_entity_index,
    generate_fault_tree_with_progress,
    normalize_top_event_name,
    parse_user_prompt,
)
from import_chunks import _load_chunks
from import_entity_index import _load_entries
from import_relations_to_neo4j import GraphDatabase, clear_graph, ensure_constraints, import_rows, load_json
from validator import validate_full, validate_semantics

MAX_GENERATION_WORKERS = max(1, int(os.getenv("MAX_GENERATION_WORKERS", "2")))
RESERVED_SINGLE_WORKERS = 1 if MAX_GENERATION_WORKERS > 1 else 0
SHARED_WORKERS = max(1, MAX_GENERATION_WORKERS - RESERVED_SINGLE_WORKERS)
DEFAULT_TOP_EVENT_CANDIDATE_LIMIT = 10
MAX_TOP_EVENT_CANDIDATE_LIMIT = 15
single_generation_queue: Queue[str] = Queue()
batch_generation_queue: Queue[str] = Queue()
generation_worker_threads: List[threading.Thread] = []


class GraphCypherQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cypher: str
    params: Optional[Dict[str, Any]] = None
    database: Optional[str] = None
    limit: int = 200


def _ensure_readonly_cypher(cypher: str) -> str:
    text = (cypher or "").strip()
    if not text:
        raise ValueError("Cypher 为空")
    lowered = re.sub(r"\s+", " ", text).lower()
    # very defensive: block writes & dangerous procedures.
    blocked = [
        " create ",
        " merge ",
        " set ",
        " delete ",
        " detach delete ",
        " remove ",
        " drop ",
        " call dbms",
        " call apoc",
        " load csv",
    ]
    padded = f" {lowered} "
    for token in blocked:
        if token in padded:
            raise ValueError(f"仅允许只读 Cypher（检测到禁用语句片段：{token.strip()}）")
    if ";" in text:
        raise ValueError("不支持多语句查询（请去掉分号）")
    return text


def _graph_cypher_available() -> bool:
    return bool(GraphDatabase and NEO4J_PASSWORD)


def _normalize_graph_bundle(nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _extract_graph_items(value: Any, nodes_by_id: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]]):
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _extract_graph_items(item, nodes_by_id, edges)
        return
    if isinstance(value, dict):
        for item in value.values():
            _extract_graph_items(item, nodes_by_id, edges)
        return

    # neo4j driver graph types
    try:
        from neo4j.graph import Node as Neo4jNode
        from neo4j.graph import Relationship as Neo4jRel
        from neo4j.graph import Path as Neo4jPath
    except Exception:
        Neo4jNode = None
        Neo4jRel = None
        Neo4jPath = None

    if Neo4jPath is not None and isinstance(value, Neo4jPath):
        for n in value.nodes:
            _extract_graph_items(n, nodes_by_id, edges)
        for r in value.relationships:
            _extract_graph_items(r, nodes_by_id, edges)
        return

    if Neo4jNode is not None and isinstance(value, Neo4jNode):
        node_id = value.element_id
        nodes_by_id.setdefault(
            node_id,
            {
                "graph_node_id": node_id,
                "labels": list(value.labels),
                "props": dict(value),
            },
        )
        return

    if Neo4jRel is not None and isinstance(value, Neo4jRel):
        start_id = value.start_node.element_id
        end_id = value.end_node.element_id
        nodes_by_id.setdefault(
            start_id,
            {
                "graph_node_id": start_id,
                "labels": list(value.start_node.labels),
                "props": dict(value.start_node),
            },
        )
        nodes_by_id.setdefault(
            end_id,
            {
                "graph_node_id": end_id,
                "labels": list(value.end_node.labels),
                "props": dict(value.end_node),
            },
        )
        edges.append(
            {
                "source_graph_node_id": start_id,
                "target_graph_node_id": end_id,
                "rel_type": getattr(value, "type", None) or getattr(value, "__class__", type("X", (), {})).__name__,
                "rel_props": dict(value),
                "graph_rel_id": getattr(value, "element_id", None),
            }
        )
        return

app = FastAPI(title="故障树智能生成系统", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _console_log(message: str):
    print(message, flush=True)


def _load_py_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {module_name} ({file_path})")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dedupe_keep_order(values: Optional[List[str]]) -> List[str]:
    result: List[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


@app.post("/api/graph/cypher")
def api_graph_cypher_query(req: GraphCypherQueryRequest):
    if not _graph_cypher_available():
        raise HTTPException(status_code=400, detail="Neo4j 未配置。请设置 NEO4J_PASSWORD 并安装 neo4j 包。")
    try:
        cypher = _ensure_readonly_cypher(req.cypher)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    limit = max(1, min(int(req.limit or 200), 1000))
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        db = str(req.database or "").strip() or NEO4J_DATABASE
        params = dict(req.params or {})
        # If query doesn't include LIMIT, add a safe one.
        if not re.search(r"(?is)\blimit\b", cypher):
            cypher = f"{cypher}\nLIMIT $limit"
            params["limit"] = limit
        with driver.session(database=db) as session:
            result = session.run(cypher, **params)
            nodes_by_id: Dict[str, Dict[str, Any]] = {}
            edges: List[Dict[str, Any]] = []
            row_count = 0
            for record in result:
                row_count += 1
                # record.values() may contain nodes, rels, paths, lists/dicts etc.
                for v in record.values():
                    _extract_graph_items(v, nodes_by_id, edges)
                if len(nodes_by_id) >= limit * 5:
                    break
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cypher 查询失败: {exc}")
    finally:
        driver.close()

    # de-dupe edges by (source,target,type,rel_id)
    seen = set()
    deduped_edges = []
    for e in edges:
        key = (
            e.get("source_graph_node_id"),
            e.get("target_graph_node_id"),
            e.get("rel_type"),
            e.get("graph_rel_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped_edges.append(e)

    return {
        "query": {"database": db, "limit": limit},
        "graph": _normalize_graph_bundle(nodes_by_id, deduped_edges),
        "stats": {"rows_scanned": row_count},
    }


def _ensure_catalog_entry(
    name: str,
    aliases: Optional[List[str]] = None,
    source_chunk_ids: Optional[List[int]] = None,
    selected_file_version_ids: Optional[List[str]] = None,
):
    canonical_name = normalize_top_event_name(name)
    if not canonical_name:
        raise ValueError("顶事件不能为空")

    scoped_file_version_ids = resolve_selected_file_version_ids(selected_file_version_ids, fallback_to_active=True)
    raw_aliases = _dedupe_keep_order((aliases or []) + [name])
    normalized_candidates = build_top_event_normalized_candidates(canonical_name, raw_aliases)
    existing = resolve_top_event_catalog(
        normalized_candidates=normalized_candidates,
        selected_file_version_ids=scoped_file_version_ids,
    )

    if existing:
        return existing

    graph_candidates = list_graph_top_event_candidates(selected_file_version_ids=scoped_file_version_ids)
    candidate_keys = set(normalized_candidates)
    for item in graph_candidates:
        item_name = normalize_top_event_name(item.get("name"))
        item_normalized_name = normalize_top_event_name(item.get("normalized_name") or item_name)
        if not item_name or not item_normalized_name:
            continue
        if item_normalized_name not in candidate_keys and item_name not in candidate_keys:
            continue
        upsert_top_event_catalog_entry(
            name=item_name,
            normalized_name=item_normalized_name,
            file_id=item.get("file_id"),
            file_version_id=item.get("file_version_id"),
            aliases=_dedupe_keep_order(raw_aliases + [item_name]),
            normalized_aliases=[candidate for candidate in normalized_candidates if candidate != item_normalized_name],
            source_chunk_ids=_dedupe_keep_order((item.get("source_chunk_refs") or item.get("source_chunk_ids") or []) + (source_chunk_ids or [])),
            graph_node_id=item.get("graph_node_id"),
        )

    resolved = resolve_top_event_catalog(
        normalized_candidates=normalized_candidates,
        selected_file_version_ids=scoped_file_version_ids,
    )
    if resolved:
        return resolved

    raise ValueError(f"当前选源范围内未找到顶事件: {canonical_name}")


def _graph_node_id_from_catalog(entry: Optional[Dict[str, Any]]) -> Optional[str]:
    if not entry:
        return None
    return entry.get("graph_node_id") or ((entry.get("graph_node_ids") or [None])[0])


def _normalize_candidate_limit(value: Optional[int]) -> int:
    return max(1, min(int(value or DEFAULT_TOP_EVENT_CANDIDATE_LIMIT), MAX_TOP_EVENT_CANDIDATE_LIMIT))


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"string", "null", "none", "undefined"}:
        return None
    return text


def _build_top_event_resolution_payload(
    *,
    status: str,
    requested_top_event: str,
    requirements: str,
    selected_file_version_ids: List[str],
    catalog_entry: Optional[Dict[str, Any]] = None,
    candidates: Optional[List[Dict[str, Any]]] = None,
    catalog_generated: bool = False,
    source: str = "prompt",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": status,
        "requested_top_event": requested_top_event,
        "requirements": requirements,
        "selected_file_version_ids": selected_file_version_ids,
        "catalog_generated": bool(catalog_generated),
        "source": source,
    }
    if catalog_entry:
        payload.update(
            {
                "resolved_top_event": catalog_entry["name"],
                "normalized_top_event": catalog_entry["normalized_name"],
                "graph_node_id": _graph_node_id_from_catalog(catalog_entry),
                "catalog_entry": {
                    "name": catalog_entry["name"],
                    "display_name": catalog_entry.get("display_name") or catalog_entry["name"],
                    "normalized_name": catalog_entry["normalized_name"],
                    "aliases": catalog_entry.get("aliases") or [],
                    "graph_node_id": _graph_node_id_from_catalog(catalog_entry),
                    "graph_node_ids": catalog_entry.get("graph_node_ids") or [],
                    "file_version_ids": catalog_entry.get("file_version_ids") or [],
                    "source_chunk_ids": catalog_entry.get("source_chunk_ids") or [],
                },
            }
        )
    if candidates is not None:
        payload["candidate_count"] = len(candidates)
        payload["candidates"] = candidates
    return payload


def _resolve_prompt_top_event(
    *,
    prompt: str,
    selected_file_version_ids: Optional[List[str]] = None,
    candidate_limit: Optional[int] = None,
) -> Dict[str, Any]:
    parsed_prompt = parse_user_prompt(prompt)
    requested_top_event = parsed_prompt["top_event"]
    requirements = parsed_prompt.get("requirements", "")
    scoped_file_version_ids = _resolve_selected_scope(selected_file_version_ids)
    ensured_catalog = ensure_top_event_catalog_for_scope(selected_file_version_ids=scoped_file_version_ids)
    existing_catalog = ensured_catalog.get("catalog") or []
    catalog_generated = bool(ensured_catalog.get("rebuilt_file_version_ids") or [])
    if not existing_catalog:
        raise ValueError("当前选源范围内没有可用顶事件")

    normalized_candidates = build_top_event_normalized_candidates(requested_top_event, [requested_top_event])
    exact_entry = resolve_top_event_catalog(
        normalized_candidates=normalized_candidates,
        selected_file_version_ids=scoped_file_version_ids,
    )
    if exact_entry:
        payload = _build_top_event_resolution_payload(
            status="exact_match",
            requested_top_event=requested_top_event,
            requirements=requirements,
            selected_file_version_ids=scoped_file_version_ids,
            catalog_entry=exact_entry,
            candidates=[],
            catalog_generated=catalog_generated,
        )
        payload["parsed_prompt"] = parsed_prompt
        return payload

    candidates = search_top_event_catalog_semantic(
        requested_top_event,
        selected_file_version_ids=scoped_file_version_ids,
        limit=_normalize_candidate_limit(candidate_limit),
    )
    if not candidates:
        raise ValueError("当前选源范围内没有找到可供确认的顶事件候选")
    payload = _build_top_event_resolution_payload(
        status="need_user_confirmation",
        requested_top_event=requested_top_event,
        requirements=requirements,
        selected_file_version_ids=scoped_file_version_ids,
        candidates=[
            {
                "rank": index + 1,
                "display_name": item.get("display_name") or item.get("name"),
                "name": item.get("name"),
                "normalized_top_event": item.get("normalized_name"),
                "graph_node_id": item.get("graph_node_id") or ((item.get("graph_node_ids") or [None])[0]),
                "graph_node_ids": item.get("graph_node_ids") or [],
                "aliases": item.get("aliases") or [],
                "file_version_ids": item.get("file_version_ids") or [],
                "score": round(float(item.get("score") or 0.0), 4),
                "match_type": item.get("match_type") or "vector",
            }
            for index, item in enumerate(candidates)
        ],
        catalog_generated=catalog_generated,
    )
    payload["parsed_prompt"] = parsed_prompt
    return payload


def _resolve_confirmed_top_event(
    *,
    confirmed_top_event: Optional[str],
    confirmed_normalized_top_event: Optional[str],
    confirmed_graph_node_id: Optional[str],
    selected_file_version_ids: List[str],
) -> Dict[str, Any]:
    if confirmed_graph_node_id:
        entry = get_top_event_catalog_by_graph_node_id(
            confirmed_graph_node_id,
            selected_file_version_ids=selected_file_version_ids,
        )
        if entry:
            return entry

    candidate_values = _dedupe_keep_order(
        [
            confirmed_top_event,
            confirmed_normalized_top_event,
        ]
    )
    normalized_candidates = []
    for value in candidate_values:
        normalized_candidates.extend(build_top_event_normalized_candidates(value, [value]))
    entry = resolve_top_event_catalog(
        normalized_candidates=_dedupe_keep_order(normalized_candidates),
        selected_file_version_ids=selected_file_version_ids,
    )
    if entry:
        return entry
    raise ValueError("当前选源范围内未找到用户确认的顶事件")


def _merge_discovered_top_events(
    *,
    graph_top_events: Optional[List[Dict[str, Any]]] = None,
    catalog_top_events: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    def _ensure_bucket(name: str, normalized_name: str) -> Dict[str, Any]:
        bucket = merged.get(normalized_name)
        if bucket:
            return bucket

        bucket = {
            "name": name,
            "normalized_name": normalized_name,
            "aliases": [],
            "graph_node_ids": [],
            "file_ids": [],
            "file_version_ids": [],
            "source_chunk_ids": [],
            "support_count": 0,
            "catalog_hit": False,
            "catalog_name": None,
            "discovery_sources": [],
            "graph_candidate_count": 0,
        }
        merged[normalized_name] = bucket
        return bucket

    def _merge_item(item: Dict[str, Any], *, source: str):
        raw_name = normalize_top_event_name(item.get("name"))
        normalized_name = normalize_top_event_name(item.get("normalized_name") or raw_name)
        if not raw_name or not normalized_name:
            return

        bucket = _ensure_bucket(raw_name, normalized_name)
        item_support = int(item.get("support_count") or 0)
        bucket_support = int(bucket.get("support_count") or 0)
        if not bucket.get("name") or item_support > bucket_support:
            bucket["name"] = raw_name

        aliases = item.get("aliases") or []
        bucket["aliases"] = _dedupe_keep_order((bucket.get("aliases") or []) + aliases + [raw_name])

        graph_node_ids = []
        if item.get("graph_node_id"):
            graph_node_ids.append(item.get("graph_node_id"))
        graph_node_ids.extend(item.get("graph_node_ids") or [])
        bucket["graph_node_ids"] = _dedupe_keep_order((bucket.get("graph_node_ids") or []) + graph_node_ids)

        file_ids = []
        if item.get("file_id"):
            file_ids.append(item.get("file_id"))
        file_ids.extend(item.get("file_ids") or [])
        bucket["file_ids"] = _dedupe_keep_order((bucket.get("file_ids") or []) + file_ids)

        file_version_ids = []
        if item.get("file_version_id"):
            file_version_ids.append(item.get("file_version_id"))
        file_version_ids.extend(item.get("file_version_ids") or [])
        bucket["file_version_ids"] = _dedupe_keep_order((bucket.get("file_version_ids") or []) + file_version_ids)

        source_chunk_ids = list(item.get("source_chunk_refs") or item.get("source_chunk_ids") or [])
        bucket["source_chunk_ids"] = _dedupe_keep_order((bucket.get("source_chunk_ids") or []) + source_chunk_ids)

        bucket["support_count"] = max(bucket_support, item_support)
        if source not in bucket["discovery_sources"]:
            bucket["discovery_sources"] = (bucket.get("discovery_sources") or []) + [source]
        if source == "graph":
            bucket["graph_candidate_count"] = int(bucket.get("graph_candidate_count") or 0) + 1
        if source == "catalog":
            bucket["catalog_hit"] = True
            bucket["catalog_name"] = item.get("name") or bucket.get("catalog_name") or raw_name

    for item in graph_top_events or []:
        if isinstance(item, dict):
            _merge_item(item, source="graph")

    for item in catalog_top_events or []:
        if isinstance(item, dict):
            _merge_item(item, source="catalog")

    items = []
    for normalized_name, bucket in merged.items():
        name = normalize_top_event_name(bucket.get("name") or normalized_name)
        aliases = _dedupe_keep_order(
            [
                alias
                for alias in (bucket.get("aliases") or [])
                if normalize_top_event_name(alias) and normalize_top_event_name(alias) != name
            ]
        )
        items.append(
            {
                "name": name,
                "normalized_name": normalized_name,
                "aliases": aliases,
                "graph_node_id": ((bucket.get("graph_node_ids") or [None])[0]),
                "graph_node_ids": bucket.get("graph_node_ids") or [],
                "file_ids": bucket.get("file_ids") or [],
                "file_version_ids": bucket.get("file_version_ids") or [],
                "source_chunk_ids": bucket.get("source_chunk_ids") or [],
                "support_count": int(bucket.get("support_count") or 0),
                "catalog_hit": bool(bucket.get("catalog_hit")),
                "catalog_name": bucket.get("catalog_name"),
                "discovery_sources": bucket.get("discovery_sources") or [],
                "graph_candidate_count": int(bucket.get("graph_candidate_count") or 0),
            }
        )

    items.sort(
        key=lambda item: (
            0 if "graph" in (item.get("discovery_sources") or []) else 1,
            -(int(item.get("support_count") or 0)),
            item.get("name") or "",
        )
    )
    return items


def _serialize_job(job_id: str) -> Dict:
    refresh_generation_job(job_id)
    job = get_generation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def _worker_loop(worker_name: str, allow_batch: bool):
    while True:
        queue_ref = None
        item_id = None

        try:
            item_id = single_generation_queue.get(timeout=0.5)
            queue_ref = single_generation_queue
        except Empty:
            if allow_batch:
                try:
                    item_id = batch_generation_queue.get(timeout=0.5)
                    queue_ref = batch_generation_queue
                except Empty:
                    continue
            else:
                continue

        try:
            _run_generation_item(item_id, execution_owner=f"queue:{worker_name}:{item_id}")
        except Exception as exc:
            _console_log(f"[scheduler] worker={worker_name} item={item_id} failed: {exc}")
        finally:
            if queue_ref is not None:
                queue_ref.task_done()


def _start_generation_workers():
    if generation_worker_threads:
        return

    for index in range(RESERVED_SINGLE_WORKERS):
        thread = threading.Thread(
            target=_worker_loop,
            args=(f"single-{index + 1}", False),
            daemon=True,
        )
        thread.start()
        generation_worker_threads.append(thread)

    for index in range(SHARED_WORKERS):
        thread = threading.Thread(
            target=_worker_loop,
            args=(f"shared-{index + 1}", True),
            daemon=True,
        )
        thread.start()
        generation_worker_threads.append(thread)


def _submit_generation_item(item_id: str, queue_type: str):
    if queue_type == "single":
        single_generation_queue.put(item_id)
    else:
        batch_generation_queue.put(item_id)
    _console_log(f"[scheduler] queued item={item_id} queue={queue_type}")


def _start_dedicated_generation_thread(item_id: str, mirror_item_ids: Optional[List[str]] = None):
    thread = threading.Thread(
        target=_run_generation_item,
        kwargs={
            "item_id": item_id,
            "execution_owner": f"dedicated:{item_id}:{uuid.uuid4().hex[:6]}",
            "mirror_item_ids": mirror_item_ids or [],
        },
        daemon=True,
    )
    thread.start()
    return thread


def _log_item_duration(item_id: str):
    item = get_generation_job_item(item_id)
    if not item:
        return

    duration = item.get("duration_seconds")
    duration_text = f"{duration:.3f}s" if isinstance(duration, (int, float)) else "unknown"
    _console_log(
        f"[timing] item={item_id} top_event={item.get('top_event')} "
        f"status={item.get('status')} duration={duration_text}"
    )


def _log_job_duration_once(job_id: str):
    job = refresh_generation_job(job_id)
    if not job or job.get("status") not in {"completed", "partial_failed", "failed"}:
        return
    if not try_mark_job_completion_logged(job_id):
        return

    duration = job.get("duration_seconds")
    duration_text = f"{duration:.3f}s" if isinstance(duration, (int, float)) else "unknown"
    _console_log(
        f"[timing] job={job_id} type={job.get('job_type')} status={job.get('status')} "
        f"total={job.get('total')} success={job.get('success')} failed={job.get('failed')} "
        f"duration={duration_text}"
    )


_start_generation_workers()


def _agent_from_log_line(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return "Agent"
    if text.startswith("[召回]"):
        return "召回智能体"
    if text.startswith("[修复]"):
        return "修复智能体"
    if text.startswith("[生成]"):
        m = re.search(r"LLM#\d+", text)
        if m:
            return m.group(0)
        if "校验" in text:
            return "结构校验智能体"
        return "生成智能体"
    if text.startswith("[scheduler]"):
        return "调度器"
    if text.startswith("[timing]"):
        return "计时器"
    if text.startswith("[graph-draft]"):
        return "草稿生成"
    if text.startswith("[graph-validate]"):
        return "结构校验"
    if text.startswith("[graph-regenerate]"):
        return "重新生成"
    if text.startswith("[history-repair]"):
        return "历史修正"
    if text.startswith("[graph-persist]"):
        return "版本持久化"
    return "Agent"


def _append_event(
    item_id: str,
    *,
    agent: str,
    text: str,
    level: str = "INFO",
    stage: Optional[str] = None,
    progress: Optional[int] = None,
):
    append_generation_job_item_event(
        item_id,
        agent=agent,
        text=text,
        level=level,
        stage=stage,
        progress=progress,
        kind="log",
    )


def _sync_mirror_items(mirror_item_ids: Optional[List[str]], **kwargs):
    for mirror_item_id in mirror_item_ids or []:
        update_generation_job_item(mirror_item_id, **kwargs)


def _build_single_generate_result(
    *,
    mode: str,
    tree_id: str,
    version: int,
    tree_data: Dict[str, Any],
    selected_file_version_ids: List[str],
    requested_top_event: str,
    resolved_top_event: str,
    normalized_top_event: str,
    requirements: str,
    graph_node_id: Optional[str] = None,
    job_id: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "mode": mode,
        "tree_id": tree_id,
        "version": version,
        "selected_file_version_ids": selected_file_version_ids,
        "parsed_prompt": {
            "requested_top_event": requested_top_event,
            "resolved_top_event": resolved_top_event,
            "normalized_top_event": normalized_top_event,
            "requirements": requirements,
        },
        "tree_data": tree_data,
    }
    if graph_node_id:
        payload["graph_node_id"] = graph_node_id
    if job_id:
        payload["job_id"] = job_id
    if item_id:
        payload["item_id"] = item_id
    return payload


def _wait_for_single_item_result(
    *,
    item_id: str,
    selected_file_version_ids: List[str],
    requested_top_event: str,
    resolved_top_event: str,
    normalized_top_event: str,
    requirements: str,
    graph_node_id: Optional[str] = None,
    job_id: Optional[str] = None,
    execute_if_pending: bool = False,
) -> Dict[str, Any]:
    execution_owner = f"single-sync:{uuid.uuid4().hex[:8]}"
    if execute_if_pending:
        _run_generation_item(item_id, execution_owner=execution_owner)

    while True:
        item = get_generation_job_item(item_id)
        if not item:
            raise ValueError(f"Generation item not found: {item_id}")

        status = item.get("status")
        if status == "success":
            tree_id = item.get("tree_id")
            if not tree_id:
                raise ValueError(f"Generation item succeeded without tree_id: {item_id}")
            version_doc = get_version(tree_id)
            if not version_doc:
                raise ValueError(f"Tree version not found for generated tree: {tree_id}")
            return _build_single_generate_result(
                mode="generated",
                tree_id=tree_id,
                version=int(version_doc.get("version") or 1),
                tree_data=version_doc.get("tree_data") or {},
                selected_file_version_ids=selected_file_version_ids,
                requested_top_event=requested_top_event,
                resolved_top_event=resolved_top_event,
                normalized_top_event=normalized_top_event,
                requirements=requirements,
                graph_node_id=graph_node_id,
                job_id=job_id or item.get("job_id"),
                item_id=item_id,
            )

        if status == "failed":
            raise ValueError(item.get("error") or "Generation failed")

        time.sleep(0.2)


def _run_generation_item(item_id: str, execution_owner: Optional[str] = None, mirror_item_ids: Optional[List[str]] = None):
    item = get_generation_job_item(item_id)
    if not item:
        return

    if execution_owner:
        claimed = claim_generation_job_item(
            item_id,
            execution_owner=execution_owner,
            allowed_statuses=["pending"],
            progress=5,
            stage="prepare",
            message="Preparing generation task",
        )
        if claimed:
            item = claimed
        else:
            latest_item = get_generation_job_item(item_id)
            if not latest_item:
                return
            if latest_item.get("status") in {"success", "failed"}:
                return
            if latest_item.get("execution_owner") != execution_owner:
                return
            item = latest_item
    else:
        if item.get("status") not in {"pending", "running"}:
            return

    wall_started = time.perf_counter()
    job_id = item["job_id"]
    top_event = item["top_event"]
    requested_top_event = item.get("requested_top_event") or top_event
    resolved_top_event = item.get("resolved_top_event") or top_event
    normalized_top_event = item["normalized_top_event"]
    graph_node_id = item.get("graph_node_id")
    aliases = _dedupe_keep_order(item.get("aliases") or [])
    requirements = item.get("requirements") or ""
    selected_file_version_ids = _resolve_selected_scope(item.get("source_file_version_ids") or [])
    tree_id = None

    try:
        _console_log(
            f"[scheduler] start item={item_id} job={job_id} top_event={top_event} "
            f"owner={execution_owner or item.get('execution_owner') or 'direct'}"
        )
        # 任务级事件流：用于前端对话栏实时展示执行进度（引用到该 task）
        _append_event(
            item_id,
            agent="调度器",
            text="任务开始执行。",
            stage=item.get("stage") or "prepare",
            progress=item.get("progress") or 0,
        )
        for mid in mirror_item_ids or []:
            _append_event(
                mid,
                agent="调度器",
                text="任务开始执行。",
                stage=item.get("stage") or "prepare",
                progress=item.get("progress") or 0,
            )

        reused = find_tree_by_top_event(
            top_event=top_event,
            normalized_top_event=normalized_top_event,
            aliases=aliases,
            catalog_name=top_event,
            source_file_version_ids=selected_file_version_ids,
        )
        if reused:
            _console_log(
                f"[reuse] item={item_id} job={job_id} top_event={top_event} "
                f"tree_id={reused['tree_id']} version={reused['version']}"
            )
            update_generation_job_item(
                item_id,
                status="success",
                progress=100,
                stage="reuse",
                message="Reused existing tree",
                tree_id=reused["tree_id"],
                reused=True,
                version=reused["version"],
            )
            _append_event(item_id, agent="生成智能体", text="已复用已有故障树。", stage="reuse", progress=100)
            _sync_mirror_items(
                mirror_item_ids,
                status="success",
                progress=100,
                stage="reuse",
                message="Reused existing tree",
                tree_id=reused["tree_id"],
                reused=True,
                version=reused["version"],
                error=None,
            )
            for mid in mirror_item_ids or []:
                _append_event(mid, agent="生成智能体", text="已复用已有故障树。", stage="reuse", progress=100)
            _log_item_duration(item_id)
            for mirror_item_id in mirror_item_ids or []:
                _log_item_duration(mirror_item_id)
            _log_job_duration_once(job_id)
            for mirror_item_id in mirror_item_ids or []:
                mirror_item = get_generation_job_item(mirror_item_id)
                if mirror_item:
                    _log_job_duration_once(mirror_item["job_id"])
            return

        tree_id = f"ft_{uuid.uuid4().hex[:8]}"
        create_tree(
            tree_id=tree_id,
            top_event=resolved_top_event,
            requested_top_event=requested_top_event,
            resolved_top_event=resolved_top_event,
            catalog_name=resolved_top_event,
            normalized_top_event=normalized_top_event,
            graph_node_id=graph_node_id,
            aliases=aliases,
            source_chunk_ids=item.get("source_chunk_ids") or [],
            source_file_version_ids=selected_file_version_ids,
            source_scope_key=item.get("source_scope_key"),
            job_id=job_id,
            job_item_id=item_id,
        )

        update_generation_job_item(
            item_id,
            status="running",
            progress=15,
            stage="tree_record",
            message="Tree record created",
            tree_id=tree_id,
        )
        _append_event(item_id, agent="生成智能体", text="已创建故障树记录。", stage="tree_record", progress=15)
        _sync_mirror_items(
            mirror_item_ids,
            tree_id=tree_id,
            progress=15,
            stage="tree_record",
            message="Tree record created",
        )
        for mid in mirror_item_ids or []:
            _append_event(mid, agent="生成智能体", text="已创建故障树记录。", stage="tree_record", progress=15)

        def progress_callback(progress: int, stage: str, message: str):
            _console_log(
                f"[progress] item={item_id} top_event={top_event} "
                f"stage={stage} progress={progress} message={message}"
            )
            update_generation_job_item(
                item_id,
                status="running",
                progress=progress,
                stage=stage,
                message=message,
                tree_id=tree_id,
            )
            _append_event(item_id, agent="流程控制", text=message, stage=stage, progress=progress)
            _sync_mirror_items(
                mirror_item_ids,
                status="running",
                progress=progress,
                stage=stage,
                message=message,
                tree_id=tree_id,
            )
            for mid in mirror_item_ids or []:
                _append_event(mid, agent="流程控制", text=message, stage=stage, progress=progress)

        def log_callback(line: str):
            agent = _agent_from_log_line(line)
            _append_event(item_id, agent=agent, text=line)
            for mid in mirror_item_ids or []:
                _append_event(mid, agent=agent, text=line)

        tree_data = generate_fault_tree_with_progress(
            top_event=resolved_top_event,
            requirements=requirements,
            selected_file_version_ids=selected_file_version_ids,
            root_graph_node_id=graph_node_id,
            progress_callback=progress_callback,
            log_callback=log_callback,
        )

        retrieval = tree_data.get("retrieval") or {}
        tree_data["resolution"] = {
            "requested_top_event": requested_top_event,
            "resolved_top_event": resolved_top_event,
            "normalized_top_event": normalized_top_event,
            "graph_node_id": graph_node_id or retrieval.get("matched_node_id"),
        }
        evidence_chunk_ids = retrieval.get("evidence_chunk_ids") or retrieval.get("chunk_ids") or []
        subgraph_node_ids = retrieval.get("subgraph_node_ids") or []
        resolved_source_file_version_ids = retrieval.get("source_file_version_ids") or selected_file_version_ids

        _append_event(
            item_id,
            agent="版本持久化",
            text=f"[graph-persist] saving tree tree_id={tree_id}",
            stage="persistence",
            progress=90,
        )
        for mid in mirror_item_ids or []:
            _append_event(
                mid,
                agent="版本持久化",
                text=f"[graph-persist] saving tree tree_id={tree_id}",
                stage="persistence",
                progress=90,
            )
        version = save_version(
            tree_id=tree_id,
            tree_data=tree_data,
            editor="AI",
            description=f"AI initial generation for top event: {resolved_top_event}",
            is_ai=True,
            requested_top_event=requested_top_event,
            resolved_top_event=resolved_top_event,
            normalized_top_event=normalized_top_event,
            source_file_version_ids=resolved_source_file_version_ids,
            evidence_chunk_ids=evidence_chunk_ids,
            subgraph_node_ids=subgraph_node_ids,
        )
        _append_event(
            item_id,
            agent="版本持久化",
            text=f"[graph-persist] saved tree tree_id={tree_id} version={version}",
            stage="persistence",
            progress=95,
        )
        for mid in mirror_item_ids or []:
            _append_event(
                mid,
                agent="版本持久化",
                text=f"[graph-persist] saved tree tree_id={tree_id} version={version}",
                stage="persistence",
                progress=95,
            )

        update_generation_job_item(
            item_id,
            status="success",
            progress=100,
            stage="completed",
            message="Tree generated",
            tree_id=tree_id,
            version=version,
            worker_duration_seconds=round(time.perf_counter() - wall_started, 3),
        )
        _console_log(
            f"[scheduler] success item={item_id} job={job_id} top_event={top_event} "
            f"tree_id={tree_id} version={version}"
        )
        _append_event(item_id, agent="生成智能体", text="生成完成。", stage="completed", progress=100)
        _sync_mirror_items(
            mirror_item_ids,
            status="success",
            progress=100,
            stage="completed",
            message="Tree generated",
            tree_id=tree_id,
            version=version,
            error=None,
            worker_duration_seconds=round(time.perf_counter() - wall_started, 3),
        )
        for mid in mirror_item_ids or []:
            _append_event(mid, agent="生成智能体", text="生成完成。", stage="completed", progress=100)
        _log_item_duration(item_id)
        for mirror_item_id in mirror_item_ids or []:
            _log_item_duration(mirror_item_id)
        _log_job_duration_once(job_id)
        for mirror_item_id in mirror_item_ids or []:
            mirror_item = get_generation_job_item(mirror_item_id)
            if mirror_item:
                _log_job_duration_once(mirror_item["job_id"])
    except ValueError as exc:
        _console_log(f"[scheduler] failed item={item_id} job={job_id} top_event={top_event} error={exc}")
        if tree_id:
            update_tree_status(tree_id, "failed", error=str(exc))
        update_generation_job_item(
            item_id,
            status="failed",
            progress=100,
            stage="failed",
            message="Generation failed",
            tree_id=tree_id,
            error=str(exc),
            worker_duration_seconds=round(time.perf_counter() - wall_started, 3),
        )
        _append_event(item_id, agent="生成智能体", text=f"生成失败：{exc}", level="ERROR", stage="failed", progress=100)
        _sync_mirror_items(
            mirror_item_ids,
            status="failed",
            progress=100,
            stage="failed",
            message="Generation failed",
            tree_id=tree_id,
            error=str(exc),
            worker_duration_seconds=round(time.perf_counter() - wall_started, 3),
        )
        for mid in mirror_item_ids or []:
            _append_event(mid, agent="生成智能体", text=f"生成失败：{exc}", level="ERROR", stage="failed", progress=100)
        _log_item_duration(item_id)
        for mirror_item_id in mirror_item_ids or []:
            _log_item_duration(mirror_item_id)
        _log_job_duration_once(job_id)
        for mirror_item_id in mirror_item_ids or []:
            mirror_item = get_generation_job_item(mirror_item_id)
            if mirror_item:
                _log_job_duration_once(mirror_item["job_id"])
    except Exception as exc:
        _console_log(f"[scheduler] failed item={item_id} job={job_id} top_event={top_event} error={exc}")
        if tree_id:
            update_tree_status(tree_id, "failed", error=str(exc))
        update_generation_job_item(
            item_id,
            status="failed",
            progress=100,
            stage="failed",
            message="Generation failed",
            tree_id=tree_id,
            error=str(exc),
            worker_duration_seconds=round(time.perf_counter() - wall_started, 3),
        )
        _append_event(item_id, agent="生成智能体", text=f"生成失败：{exc}", level="ERROR", stage="failed", progress=100)
        _sync_mirror_items(
            mirror_item_ids,
            status="failed",
            progress=100,
            stage="failed",
            message="Generation failed",
            tree_id=tree_id,
            error=str(exc),
            worker_duration_seconds=round(time.perf_counter() - wall_started, 3),
        )
        for mid in mirror_item_ids or []:
            _append_event(mid, agent="生成智能体", text=f"生成失败：{exc}", level="ERROR", stage="failed", progress=100)
        _log_item_duration(item_id)
        for mirror_item_id in mirror_item_ids or []:
            _log_item_duration(mirror_item_id)
        _log_job_duration_once(job_id)
        for mirror_item_id in mirror_item_ids or []:
            mirror_item = get_generation_job_item(mirror_item_id)
            if mirror_item:
                _log_job_duration_once(mirror_item["job_id"])


def _queue_single_generation(
    prompt: str,
    requested_top_event: str,
    requirements: str,
    catalog: Dict[str, Any],
    graph_node_id_override: Optional[str] = None,
    selected_file_version_ids: Optional[List[str]] = None,
    async_mode: bool = False,
) -> Dict:
    scoped_file_version_ids = _resolve_selected_scope(selected_file_version_ids)
    resolved_top_event = catalog["name"]
    normalized_top_event = catalog["normalized_name"]
    graph_node_id = graph_node_id_override
    aliases = _dedupe_keep_order((catalog.get("aliases") or []) + [requested_top_event, resolved_top_event])

    reused = find_tree_by_top_event(
        top_event=resolved_top_event,
        normalized_top_event=normalized_top_event,
        aliases=aliases,
        catalog_name=resolved_top_event,
        source_file_version_ids=scoped_file_version_ids,
    )
    if reused:
        return _build_single_generate_result(
            mode="reuse",
            tree_id=reused["tree_id"],
            version=reused["version"],
            tree_data=reused["tree_data"],
            selected_file_version_ids=scoped_file_version_ids,
            requested_top_event=requested_top_event,
            resolved_top_event=resolved_top_event,
            normalized_top_event=normalized_top_event,
            requirements=requirements,
            graph_node_id=graph_node_id,
        )

    active_item = find_active_job_item_by_top_event_and_scope(normalized_top_event, scoped_file_version_ids)
    if active_item:
        return _wait_for_single_item_result(
            item_id=active_item["item_id"],
            selected_file_version_ids=scoped_file_version_ids,
            requested_top_event=requested_top_event,
            resolved_top_event=resolved_top_event,
            normalized_top_event=normalized_top_event,
            requirements=requirements,
            graph_node_id=graph_node_id,
            job_id=active_item.get("job_id"),
            execute_if_pending=(active_item.get("status") == "pending"),
        )

    job = create_generation_job(
        job_type="single",
        total=1,
        top_event=resolved_top_event,
        source_file_version_ids=scoped_file_version_ids,
        metadata={
            "requested_prompt": prompt,
            "source": "/api/tree/generate",
            "requested_top_event": requested_top_event,
            "resolved_top_event": resolved_top_event,
            "normalized_top_event": normalized_top_event,
            "graph_node_id": graph_node_id,
            "selected_file_version_ids": scoped_file_version_ids,
        },
    )
    item = create_generation_job_item(
        job_id=job["job_id"],
        top_event=resolved_top_event,
        requested_top_event=requested_top_event,
        resolved_top_event=resolved_top_event,
        normalized_top_event=normalized_top_event,
        graph_node_id=graph_node_id,
        aliases=aliases,
        source_chunk_ids=catalog.get("source_chunk_ids") or [],
        source_file_version_ids=scoped_file_version_ids,
        requirements=requirements,
        metadata={
            "requested_prompt": prompt,
            "query_top_event": requested_top_event,
            "resolved_top_event": resolved_top_event,
            "normalized_top_event": normalized_top_event,
            "graph_node_id": graph_node_id,
            "selected_file_version_ids": scoped_file_version_ids,
        },
    )

    if async_mode:
        _start_dedicated_generation_thread(item["item_id"])
        return {
            "mode": "queued",
            "job_id": job["job_id"],
            "item_id": item["item_id"],
            "status": "queued",
            "selected_file_version_ids": scoped_file_version_ids,
            "parsed_prompt": {
                "requested_top_event": requested_top_event,
                "resolved_top_event": resolved_top_event,
                "normalized_top_event": normalized_top_event,
                "requirements": requirements,
            },
        }

    return _wait_for_single_item_result(
        item_id=item["item_id"],
        selected_file_version_ids=scoped_file_version_ids,
        requested_top_event=requested_top_event,
        resolved_top_event=resolved_top_event,
        normalized_top_event=normalized_top_event,
        requirements=requirements,
        graph_node_id=graph_node_id,
        job_id=job["job_id"],
        execute_if_pending=True,
    )


# ---- Optional: merge validator-service into this backend (same uvicorn port) ----
_VALIDATOR_DIR = Path(__file__).resolve().parent / "validator-service"
if _VALIDATOR_DIR.exists():
    try:
        _validator_dir_str = str(_VALIDATOR_DIR)
        _had_validator_path = _validator_dir_str in sys.path
        if not _had_validator_path:
            sys.path.insert(0, _validator_dir_str)

        _validator_main = _load_py_module("validator_service_main", _VALIDATOR_DIR / "main.py")

        @app.post("/validate-fault-tree")
        def validate_fault_tree(payload: _validator_main.ValidateRequest):  # type: ignore[name-defined]
            return _validator_main.validate_fault_tree(payload)  # type: ignore[attr-defined]

        @app.post("/export-fault-tree-image")
        async def export_fault_tree_image(payload: _validator_main.ExportImageRequest):  # type: ignore[name-defined]
            try:
                return await _validator_main.export_fault_tree_image(payload)  # type: ignore[attr-defined]
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

    except Exception as exc:
        print(f"[warn] validator-service merge failed: {exc}")
    finally:
        try:
            if "_validator_dir_str" in locals() and not locals().get("_had_validator_path", True):
                sys.path = [path for path in sys.path if path != locals()["_validator_dir_str"]]
        except Exception:
            pass


class GenerateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prompt": "生成我一个顶事件为核心组件故障的故障树",
                "selected_file_version_ids": ["fv_test_part1_cleaned_v1", "fv_test_part2_cleaned_v1"],
                "candidate_limit": 10,
            }
        }
    )
    prompt: str
    selected_file_version_ids: Optional[List[str]] = None
    candidate_limit: int = DEFAULT_TOP_EVENT_CANDIDATE_LIMIT
    confirmed_top_event: Optional[str] = None
    confirmed_normalized_top_event: Optional[str] = None
    confirmed_graph_node_id: Optional[str] = None
    # 若为 True：同步等待生成完成并返回 tree_data（默认兼容旧行为）
    # 若为 False：异步排队，立即返回 mode=queued + job_id/item_id，供前端轮询 events 实时展示进度
    sync: bool = True


class ResolveTopEventRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prompt": "生成我一个顶事件为核心组件故障的故障树",
                "selected_file_version_ids": ["fv_test_part1_cleaned_v1", "fv_test_part2_cleaned_v1"],
                "candidate_limit": 10,
            }
        }
    )
    prompt: str
    selected_file_version_ids: Optional[List[str]] = None
    candidate_limit: int = DEFAULT_TOP_EVENT_CANDIDATE_LIMIT


class GraphRecallDebugRequest(BaseModel):
    top_event: Optional[str] = None
    prompt: Optional[str] = None
    limit: int = 12
    selected_file_version_ids: Optional[List[str]] = None


class TopEventsPreviewRequest(BaseModel):
    limit: int = 200
    selected_file_version_ids: Optional[List[str]] = None


class GenerateAllRequest(BaseModel):
    selected_file_version_ids: Optional[List[str]] = None


class ContinueBatchJobRequest(BaseModel):
    stale_after_seconds: int = 300


class KnowledgeArtifactsImportRequest(BaseModel):
    chunks_file: str
    entities_file: Optional[str] = None
    relations_file: Optional[str] = None
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    file_version_id: Optional[str] = None
    chunks_import_mode: str = "replace"
    clear_graph: bool = False
    import_relations: bool = True
    source: str = "knowledge_base_construction"


class SaveRequest(BaseModel):
    tree_data: dict
    editor: str = "专家"
    description: str = "手动修改"


class ValidateRequest(BaseModel):
    tree_data: dict


class SemanticValidateRequest(BaseModel):
    tree_data: dict


def _resolve_debug_top_event(req: GraphRecallDebugRequest) -> Dict[str, object]:
    parsed_prompt = None
    if req.prompt:
        parsed_prompt = parse_user_prompt(req.prompt)
        top_event = parsed_prompt["top_event"]
    else:
        top_event = str(req.top_event or "").strip()

    if not top_event:
        raise ValueError("top_event 或 prompt 至少需要提供一个")

    scoped_file_version_ids = _resolve_selected_scope(req.selected_file_version_ids)
    normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
    catalog_entry = resolve_top_event_catalog(
        normalized_candidates=normalized_candidates,
        selected_file_version_ids=scoped_file_version_ids,
    )

    resolved_top_event = top_event
    aliases = [top_event]
    if catalog_entry:
        resolved_top_event = catalog_entry["name"]
        aliases = _dedupe_keep_order((catalog_entry.get("aliases") or []) + [resolved_top_event, top_event])
    else:
        aliases = _dedupe_keep_order(normalized_candidates + [top_event])

    return {
        "parsed_prompt": parsed_prompt,
        "top_event": top_event,
        "resolved_top_event": resolved_top_event,
        "aliases": aliases,
        "catalog_entry": catalog_entry,
        "selected_file_version_ids": scoped_file_version_ids,
    }


def _discover_batch_top_events(selected_file_version_ids: Optional[List[str]] = None) -> Dict[str, object]:
    scoped_file_version_ids = _resolve_selected_scope(selected_file_version_ids)
    ensured_catalog = ensure_top_event_catalog_for_scope(selected_file_version_ids=scoped_file_version_ids)
    catalog_top_events = ensured_catalog.get("catalog") or []
    if catalog_top_events:
        return {
            "discovered": catalog_top_events,
            "discovery_source": "scoped_top_event_catalog",
            "embedding_result": ensured_catalog.get("embedding_result") or {},
        }

    graph_top_events = list_graph_top_event_candidates(selected_file_version_ids=scoped_file_version_ids)
    if graph_top_events:
        return {
            "discovered": graph_top_events,
            "discovery_source": "graph_top_event_candidates",
        }

    raise HTTPException(status_code=400, detail="当前选源范围内没有可用顶事件")


def _discover_batch_top_events_v2(selected_file_version_ids: Optional[List[str]] = None) -> Dict[str, object]:
    scoped_file_version_ids = _resolve_selected_scope(selected_file_version_ids)
    graph_top_events = list_graph_top_event_candidates(selected_file_version_ids=scoped_file_version_ids)
    ensured_catalog = ensure_top_event_catalog_for_scope(selected_file_version_ids=scoped_file_version_ids)
    catalog_top_events = ensured_catalog.get("catalog") or []
    discovered = _merge_discovered_top_events(
        graph_top_events=graph_top_events,
        catalog_top_events=catalog_top_events,
    )
    if not discovered:
        raise HTTPException(status_code=400, detail="褰撳墠閫夋簮鑼冨洿鍐呮病鏈夊彲鐢ㄩ《浜嬩欢")

    if graph_top_events and catalog_top_events:
        discovery_source = "graph_candidates_merged_with_catalog"
    elif graph_top_events:
        discovery_source = "graph_top_event_candidates"
    else:
        discovery_source = "scoped_top_event_catalog"

    return {
        "discovered": discovered,
        "discovery_source": discovery_source,
        "graph_total": len(graph_top_events),
        "catalog_total": len(catalog_top_events),
        "embedding_result": ensured_catalog.get("embedding_result") or {},
    }


def _resolve_selected_scope(selected_file_version_ids: Optional[List[str]]) -> List[str]:
    return resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )


def _derive_file_name_from_artifacts(
    *,
    explicit_file_name: Optional[str],
    chunks: Optional[List[Dict[str, Any]]] = None,
    chunks_path: Optional[Path] = None,
    relations_path: Optional[Path] = None,
) -> str:
    if explicit_file_name:
        return explicit_file_name

    first_chunk = (chunks or [{}])[0] if chunks else {}
    chunk_file = str(first_chunk.get("file") or "").strip()
    if chunk_file:
        return chunk_file

    if relations_path:
        relation_name = relations_path.name
        if relation_name.endswith("_relations.jsonl"):
            return relation_name.replace("_relations.jsonl", "_cleaned.md")
        if relation_name.endswith("_relations.json"):
            return relation_name.replace("_relations.json", "_cleaned.md")

    if chunks_path:
        return chunks_path.stem + ".md"

    raise ValueError("无法推断 file_name，请显式传入 file_name")


def _import_relations_from_file(
    relations_file: Path,
    clear_existing_graph: bool,
    *,
    file_id: str,
    file_version_id: str,
    file_name: str,
) -> Dict[str, object]:
    if not relations_file.exists():
        raise ValueError(f"relations_file 不存在: {relations_file}")
    if not NEO4J_PASSWORD:
        raise ValueError("未配置 NEO4J_PASSWORD，无法导入图谱关系")

    raw_rows = load_json(relations_file, is_active=True)
    assert_relation_artifacts_align_with_file_version(
        raw_rows,
        file_id=file_id,
        file_version_id=file_version_id,
    )
    rows = load_json(
        relations_file,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        is_active=True,
    )
    if not rows:
        return {
            "rows": 0,
            "relations": 0,
            "database": NEO4J_DATABASE,
            "cleared": clear_existing_graph,
        }

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        ensure_constraints(driver, NEO4J_DATABASE)
        if clear_existing_graph:
            clear_graph(driver, NEO4J_DATABASE)
        relation_count = import_rows(driver, NEO4J_DATABASE, rows, batch_size=200)
    finally:
        driver.close()

    return {
        "rows": len(rows),
        "relations": relation_count,
        "database": NEO4J_DATABASE,
        "cleared": clear_existing_graph,
    }


@app.post("/api/tree/resolve-top-event")
def api_resolve_top_event(req: ResolveTopEventRequest):
    try:
        return _resolve_prompt_top_event(
            prompt=req.prompt,
            selected_file_version_ids=req.selected_file_version_ids,
            candidate_limit=req.candidate_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Resolve top event failed: {exc}")


@app.post("/api/tree/generate")
def api_generate(req: GenerateRequest):
    try:
        resolution = _resolve_prompt_top_event(
            prompt=req.prompt,
            selected_file_version_ids=req.selected_file_version_ids,
            candidate_limit=req.candidate_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Top event resolution failed: {exc}")

    requested_top_event = resolution["requested_top_event"]
    requirements = resolution.get("requirements", "")
    scoped_file_version_ids = resolution["selected_file_version_ids"]
    exact_catalog = resolution.get("catalog_entry")
    confirmed_top_event = _clean_optional_text(req.confirmed_top_event)
    confirmed_normalized_top_event = _clean_optional_text(req.confirmed_normalized_top_event)
    confirmed_graph_node_id = _clean_optional_text(req.confirmed_graph_node_id)

    if not any([confirmed_top_event, confirmed_normalized_top_event, confirmed_graph_node_id]):
        if resolution["status"] != "exact_match":
            return {
                "mode": "need_confirmation",
                **resolution,
            }
        catalog = exact_catalog
    else:
        try:
            catalog = _resolve_confirmed_top_event(
                confirmed_top_event=confirmed_top_event,
                confirmed_normalized_top_event=confirmed_normalized_top_event,
                confirmed_graph_node_id=confirmed_graph_node_id,
                selected_file_version_ids=scoped_file_version_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Confirm top event failed: {exc}")

    try:
        graph_node_id_override = (
            (confirmed_graph_node_id or _graph_node_id_from_catalog(catalog))
            if any([confirmed_top_event, confirmed_normalized_top_event, confirmed_graph_node_id])
            else None
        )
        if not bool(req.sync):
            # async: create item and run in background, return queued result for polling
            result = _queue_single_generation(
                req.prompt,
                requested_top_event,
                requirements,
                catalog,
                graph_node_id_override=graph_node_id_override,
                selected_file_version_ids=scoped_file_version_ids,
                async_mode=True,
            )
            return result

        # sync (default): block until finished and return tree_data
        return _queue_single_generation(
            req.prompt,
            requested_top_event,
            requirements,
            catalog,
            graph_node_id_override=graph_node_id_override,
            selected_file_version_ids=scoped_file_version_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Queue generation failed: {exc}")


@app.post("/api/integration/import-knowledge-artifacts")
def api_import_knowledge_artifacts(req: KnowledgeArtifactsImportRequest):
    chunks_path = Path(req.chunks_file).expanduser().resolve()
    entities_path = Path(req.entities_file).expanduser().resolve() if req.entities_file else None
    relations_path = Path(req.relations_file).expanduser().resolve() if req.relations_file else None

    if not chunks_path.exists():
        raise HTTPException(status_code=400, detail=f"chunks_file 不存在: {chunks_path}")
    if entities_path and not entities_path.exists():
        raise HTTPException(status_code=400, detail=f"entities_file 不存在: {entities_path}")
    if req.import_relations and relations_path and not relations_path.exists():
        raise HTTPException(status_code=400, detail=f"relations_file 不存在: {relations_path}")
    if req.clear_graph:
        raise HTTPException(status_code=400, detail="版本化知识库模式下禁止 clear_graph，请通过 file_version 失活旧版本。")

    chunks_import_mode = str(req.chunks_import_mode or "replace").strip().lower()
    if chunks_import_mode not in {"replace", "append"}:
        raise HTTPException(status_code=400, detail="chunks_import_mode 必须是 replace 或 append")

    file_version = None
    try:
        chunks = _load_chunks(str(chunks_path))
        file_name = _derive_file_name_from_artifacts(
            explicit_file_name=req.file_name,
            chunks=chunks,
            chunks_path=chunks_path,
            relations_path=relations_path,
        )
        file_version = create_file_version_record(
            file_name=file_name,
            file_id=req.file_id,
            file_version_id=req.file_version_id,
            source=req.source,
            metadata={
                "chunks_file": str(chunks_path),
                "entities_file": str(entities_path) if entities_path else None,
                "relations_file": str(relations_path) if relations_path else None,
            },
        )
        assert_chunk_artifacts_align_with_file_version(
            chunks,
            file_id=file_version["file_id"],
            file_version_id=file_version["file_version_id"],
        )

        chunk_import_result = import_chunks_to_db(
            chunks,
            mode=chunks_import_mode,
            file_id=file_version["file_id"],
            file_version_id=file_version["file_version_id"],
            is_active=True,
        )

        entity_count = 0
        if entities_path:
            entries = _load_entries(str(entities_path))
            import_entity_reverse_index_to_db(
                entries,
                file_id=file_version["file_id"],
                file_version_id=file_version["file_version_id"],
                is_active=True,
            )
            entity_count = len(entries)

        relation_result = None
        if req.import_relations and relations_path:
            relation_result = _import_relations_from_file(
                relations_path,
                clear_existing_graph=req.clear_graph,
                file_id=file_version["file_id"],
                file_version_id=file_version["file_version_id"],
                file_name=file_version["file_name"],
            )

        catalog_entries = rebuild_top_event_catalog_for_file_version(file_version["file_version_id"])
        activate_file_version(file_version["file_id"], file_version["file_version_id"])

        return {
            "status": "success",
            "source": req.source,
            "file": {
                "file_id": file_version["file_id"],
                "file_name": file_version["file_name"],
                "file_version_id": file_version["file_version_id"],
                "version_no": file_version["version_no"],
                "is_active": True,
            },
            "imported": {
                "chunks": chunk_import_result,
                "entity_reverse_index": entity_count,
                "relations": relation_result,
                "top_event_catalog": len(catalog_entries),
            },
            "files": {
                "chunks_file": str(chunks_path),
                "entities_file": str(entities_path) if entities_path else None,
                "relations_file": str(relations_path) if relations_path else None,
            },
            "next_steps": {
                "resolve_top_event": "/api/tree/resolve-top-event",
                "preview_top_events": "/api/batch/preview-top-events",
                "generate_all": "/api/batch/generate-all",
            },
        }
    except ValueError as exc:
        if file_version:
            mark_file_version_import_failed(file_version["file_version_id"], str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if file_version:
            mark_file_version_import_failed(file_version["file_version_id"], str(exc))
        raise HTTPException(status_code=500, detail=f"导入知识库产物失败: {exc}")


@app.post("/api/debug/graph-recall")
def api_debug_graph_recall(req: GraphRecallDebugRequest):
    try:
        resolved = _resolve_debug_top_event(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Debug top event parse failed: {exc}")

    limit = max(1, min(int(req.limit or 12), 30))
    top_event = resolved["resolved_top_event"]
    aliases = resolved["aliases"] or []
    scoped_file_version_ids = resolved["selected_file_version_ids"]

    matched = match_top_event_from_graph(
        top_event,
        build_top_event_normalized_candidates(top_event, aliases),
        selected_file_version_ids=scoped_file_version_ids,
    )
    root_node_ids = [item.get("graph_node_id") for item in (matched.get("matched_nodes") or []) if item.get("graph_node_id")]
    subgraph_bundle = expand_scoped_local_fault_subgraph(
        root_node_ids or [matched["matched_node_id"]],
        max_depth=3,
        max_nodes=20,
        selected_file_version_ids=scoped_file_version_ids,
    )
    chunk_ids = collect_subgraph_chunks(subgraph_bundle, chunk_limit=limit)
    chunk_items = get_chunks_by_ids(chunk_ids, limit=limit, selected_file_version_ids=scoped_file_version_ids)

    return {
        "query": {
            "top_event": resolved["top_event"],
            "resolved_top_event": matched["matched_name"],
            "aliases": aliases,
            "limit": limit,
            "parsed_prompt": resolved["parsed_prompt"],
            "selected_file_version_ids": scoped_file_version_ids,
        },
        "catalog_entry": resolved["catalog_entry"],
        "graph_match": {
            "matched_node_id": matched["matched_node_id"],
            "matched_name": matched["matched_name"],
            "matched_nodes": matched.get("matched_nodes") or [],
            "alternatives": matched.get("alternatives") or [],
        },
        "subgraph": {
            "root": subgraph_bundle.get("root"),
            "node_count": len(subgraph_bundle.get("nodes") or []),
            "edge_count": len(subgraph_bundle.get("edges") or []),
            "nodes": subgraph_bundle.get("nodes") or [],
            "edges": subgraph_bundle.get("edges") or [],
            "gate_groups": subgraph_bundle.get("gate_groups") or [],
        },
        "chunk_recall": {
            "chunk_ids": chunk_ids,
            "chunks": chunk_items,
        },
    }


@app.post("/api/batch/preview-top-events")
def api_preview_top_events(req: TopEventsPreviewRequest):
    scoped_file_version_ids = _resolve_selected_scope(req.selected_file_version_ids)
    discovery = _discover_batch_top_events_v2(scoped_file_version_ids)
    discovered = discovery["discovered"] or []
    discovery_source = discovery["discovery_source"]

    if not discovered:
        raise HTTPException(status_code=400, detail="No top events were discovered from graph")

    limit = max(1, min(int(req.limit or 200), 1000))
    preview_items = []
    for item in discovered[:limit]:
        canonical_name = normalize_top_event_name(item["name"])
        aliases = _dedupe_keep_order(item.get("aliases") or [])
        normalized_candidates = build_top_event_normalized_candidates(canonical_name, aliases + [item["name"]])
        preview_items.append(
            {
                "name": canonical_name,
                "graph_node_id": item.get("graph_node_id") or ((item.get("graph_node_ids") or [None])[0]),
                "normalized_name": item.get("normalized_name") or canonical_name,
                "aliases": aliases,
                "file_version_ids": item.get("file_version_ids") or ([item.get("file_version_id")] if item.get("file_version_id") else []),
                "support_count": item.get("support_count"),
                "source_chunk_ids": item.get("source_chunk_ids") or [],
                "normalized_candidates": normalized_candidates,
                "catalog_hit": bool(item.get("catalog_hit")),
                "catalog_name": item.get("catalog_name"),
                "discovery_sources": item.get("discovery_sources") or [],
                "graph_candidate_count": int(item.get("graph_candidate_count") or 0),
            }
        )

    return {
        "discovery_source": discovery_source,
        "selected_file_version_ids": scoped_file_version_ids,
        "graph_total": int(discovery.get("graph_total") or 0),
        "catalog_total": int(discovery.get("catalog_total") or 0),
        "total": len(discovered),
        "returned": len(preview_items),
        "items": preview_items,
    }


@app.post("/api/batch/generate-all")
def api_batch_generate_all(req: Optional[GenerateAllRequest] = None):
    scoped_file_version_ids = _resolve_selected_scope((req.selected_file_version_ids if req else None))
    existing_job = find_latest_generation_job_by_scope(
        job_type="batch",
        source_file_version_ids=scoped_file_version_ids,
        statuses=["pending", "running", "partial_failed", "failed"],
    )
    if existing_job:
        existing_job = refresh_generation_job(existing_job["job_id"]) or existing_job
        return {
            "mode": "existing_job",
            "job_id": existing_job["job_id"],
            "status": existing_job.get("status"),
            "selected_file_version_ids": scoped_file_version_ids,
            "message": "Existing batch job found for the same scope. Continue that job instead of creating a new one.",
            "continue_endpoint": f"/api/batch/job/{existing_job['job_id']}/continue",
        }

    discovery = _discover_batch_top_events_v2(scoped_file_version_ids)
    discovered = discovery["discovered"] or []
    discovery_source = discovery["discovery_source"]

    if not discovered:
        raise HTTPException(status_code=400, detail="No top events were discovered from graph")

    catalog_entries = []
    for item in discovered:
        try:
            entry = _ensure_catalog_entry(
                item["name"],
                aliases=item.get("aliases") or [],
                source_chunk_ids=item.get("source_chunk_ids") or [],
                selected_file_version_ids=scoped_file_version_ids,
            )
        except ValueError:
            continue
        catalog_entries.append(entry)

    existing_count = 0
    active_count = 0
    queued_entries = []
    entry_statuses = []
    for entry in catalog_entries:
        aliases = entry.get("aliases") or []
        reused = find_tree_by_top_event(
            top_event=entry["name"],
            normalized_top_event=entry["normalized_name"],
            aliases=aliases,
            catalog_name=entry["name"],
            source_file_version_ids=scoped_file_version_ids,
        )
        if reused:
            existing_count += 1
            entry_statuses.append(
                {
                    "top_event": entry["name"],
                    "normalized_name": entry["normalized_name"],
                    "status": "reused_in_same_scope",
                    "reason": "existing_tree_in_same_scope",
                    "tree_id": reused.get("tree_id"),
                    "version": reused.get("version"),
                    "source_file_version_ids": scoped_file_version_ids,
                }
            )
            continue

        active_item = find_active_job_item_by_top_event_and_scope(entry["normalized_name"], scoped_file_version_ids)
        if active_item:
            active_count += 1
            entry_statuses.append(
                {
                    "top_event": entry["name"],
                    "normalized_name": entry["normalized_name"],
                    "status": "already_running_in_same_scope",
                    "reason": "active_job_item_in_same_scope",
                    "job_id": active_item.get("job_id"),
                    "item_id": active_item.get("item_id"),
                    "source_file_version_ids": scoped_file_version_ids,
                }
            )
            continue

        queued_entries.append(entry)
        entry_statuses.append(
            {
                "top_event": entry["name"],
                "normalized_name": entry["normalized_name"],
                "status": "queued_for_generation",
                "reason": "no_existing_tree_in_same_scope",
                "source_file_version_ids": scoped_file_version_ids,
            }
        )

    job = create_generation_job(
        job_type="batch",
        total=len(queued_entries),
        source_file_version_ids=scoped_file_version_ids,
        metadata={
            "discovered_total": len(discovered),
            "catalog_total": len(catalog_entries),
            "graph_total": int(discovery.get("graph_total") or 0),
            "existing_count": existing_count,
            "active_count": active_count,
            "discovery_source": discovery_source,
            "source": "/api/batch/generate-all",
            "selected_file_version_ids": scoped_file_version_ids,
        },
    )
    _console_log(
        f"[batch] created job={job['job_id']} discovery={discovery_source} "
        f"discovered_total={len(discovered)} catalog_total={len(catalog_entries)} "
        f"existing={existing_count} active={active_count} queued={len(queued_entries)}"
    )

    queued_item_ids = []
    for entry in queued_entries:
        item = create_generation_job_item(
            job_id=job["job_id"],
            top_event=entry["name"],
            normalized_top_event=entry["normalized_name"],
            aliases=entry.get("aliases") or [],
            source_chunk_ids=entry.get("source_chunk_ids") or [],
            source_file_version_ids=scoped_file_version_ids,
            requirements="",
            metadata={"source": "batch_generate_all", "selected_file_version_ids": scoped_file_version_ids},
        )
        queued_item_ids.append(item["item_id"])
        for status_item in entry_statuses:
            if (
                status_item.get("normalized_name") == entry.get("normalized_name")
                and status_item.get("status") == "queued_for_generation"
                and not status_item.get("item_id")
            ):
                status_item["job_id"] = job["job_id"]
                status_item["item_id"] = item["item_id"]
                break
        _submit_generation_item(item["item_id"], "batch")
        _console_log(
            f"[batch] job={job['job_id']} queued item={item['item_id']} top_event={entry['name']}"
        )

    refresh_generation_job(job["job_id"])
    return {
        "job_id": job["job_id"],
        "status": get_generation_job(job["job_id"])["status"],
        "discovered_total": len(discovered),
        "graph_total": int(discovery.get("graph_total") or 0),
        "catalog_total": len(catalog_entries),
        "existing_count": existing_count,
        "active_count": active_count,
        "discovery_source": discovery_source,
        "selected_file_version_ids": scoped_file_version_ids,
        "queued_count": len(queued_entries),
        "queued_item_ids": queued_item_ids,
        "items": entry_statuses,
    }


@app.post("/api/batch/job/{job_id}/continue")
def api_continue_batch_job(job_id: str, req: Optional[ContinueBatchJobRequest] = None):
    job = get_generation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Batch job not found")
    if job.get("job_type") != "batch":
        raise HTTPException(status_code=400, detail="Only batch jobs can be continued")

    stale_after_seconds = max(30, min(int((req.stale_after_seconds if req else 300) or 300), 86400))
    try:
        prepared = prepare_generation_job_for_continue(job_id, stale_after_seconds=stale_after_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    queue_item_ids = prepared.get("queue_item_ids") or []
    for item_id in queue_item_ids:
        _submit_generation_item(item_id, "batch")

    refreshed_job = refresh_generation_job(job_id) or job
    return {
        "job_id": job_id,
        "status": refreshed_job.get("status"),
        "selected_file_version_ids": refreshed_job.get("source_file_version_ids") or [],
        "queued_item_ids": queue_item_ids,
        "queued_count": len(queue_item_ids),
        "stale_requeued_item_ids": prepared.get("stale_requeued_item_ids") or [],
        "active_running_item_ids": prepared.get("active_running_item_ids") or [],
        "message": "Batch job continued",
    }


@app.get("/api/batch/job/{job_id}")
def api_get_batch_job(job_id: str):
    job = _serialize_job(job_id)
    items = list_generation_job_items(job_id)
    return {"job": job, "items": items}


@app.get("/api/batch/job-item/{item_id}")
def api_get_batch_job_item(item_id: str):
    item = get_generation_job_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="任务项不存在")
    return item


@app.get("/api/tree/{tree_id}")
def api_get_tree(tree_id: str):
    ver = get_version(tree_id)
    if not ver:
        raise HTTPException(status_code=404, detail="故障树不存在")
    return ver


@app.get("/api/tree/{tree_id}/version/{version}")
def api_get_version(tree_id: str, version: int):
    ver = get_version(tree_id, version)
    if not ver:
        raise HTTPException(status_code=404, detail=f"版本 {version} 不存在")
    return ver


def _nodes_by_id(tree_data: Optional[dict]) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for node in (tree_data or {}).get("nodeList", []) or []:
        node_id = node.get("id")
        if node_id:
            result[node_id] = node
    return result


def _extract_graph_property_updates(previous_tree: Optional[dict], current_tree: dict) -> List[Dict[str, object]]:
    previous_nodes = _nodes_by_id(previous_tree)
    current_nodes = _nodes_by_id(current_tree)
    updates: List[Dict[str, object]] = []

    for node_id, current_node in current_nodes.items():
        previous_node = previous_nodes.get(node_id)
        if not previous_node:
            continue

        graph_node_id = current_node.get("graphNodeId") or current_node.get("kg_key")
        if not graph_node_id:
            continue

        prev_event = previous_node.get("event") or {}
        curr_event = current_node.get("event") or {}
        changed: Dict[str, object] = {}
        for field in ("description", "errorLevel", "priority", "probability", "showProbability", "investigateMethod"):
            if prev_event.get(field) != curr_event.get(field):
                changed[field] = curr_event.get(field)

        prev_rule = str(prev_event.get("rule") or "")
        curr_rule = str(curr_event.get("rule") or "")
        prev_rules = prev_event.get("rules") or []
        curr_rules = curr_event.get("rules") or []
        if not prev_rule and isinstance(prev_rules, list) and prev_rules and isinstance(prev_rules[0], dict):
            prev_rule = str(prev_rules[0].get("measurePointName") or "")
        if not curr_rule and isinstance(curr_rules, list) and curr_rules and isinstance(curr_rules[0], dict):
            curr_rule = str(curr_rules[0].get("measurePointName") or "")
        if prev_rule != curr_rule and curr_rule:
            changed["rule"] = curr_rule

        if changed:
            updates.append(
                {
                    "graph_node_id": graph_node_id,
                    "node_id": node_id,
                    "node_name": current_node.get("name"),
                    "properties": changed,
                }
            )
    return updates


@app.post("/api/tree/{tree_id}/save")
def api_save(tree_id: str, req: SaveRequest):
    meta = get_tree_meta(tree_id)
    if not meta:
        raise HTTPException(status_code=404, detail="故障树不存在")

    validation = validate_full(req.tree_data, skip_semantic=True)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "故障树存在结构错误，无法保存，请修正后重试",
                "issues": [issue for issue in validation["issues"] if issue["level"] == "ERROR"],
            },
        )

    prev_version_num = meta.get("current_version")
    prev_tree_data = None
    prev_ver = None
    if prev_version_num:
        prev_ver = versions_col.find_one({"tree_id": tree_id, "version": prev_version_num})
        if prev_ver:
            prev_tree_data = prev_ver.get("tree_data")

    description = (req.description or "").strip()
    if description in ("", "手动修改") and prev_tree_data:
        description = generate_change_description(prev_tree_data, req.tree_data)
    elif not description:
        description = "手动修改"

    graph_property_updates = _extract_graph_property_updates(prev_tree_data, req.tree_data)
    graph_updates_applied = 0
    graph_update_errors = []
    for item in graph_property_updates:
        try:
            updated = update_graph_node_properties(item["graph_node_id"], item["properties"])
            if updated:
                graph_updates_applied += 1
        except Exception as exc:
            graph_update_errors.append({"node_id": item["node_id"], "error": str(exc)})

    new_version = save_version(
        tree_id=tree_id,
        tree_data=req.tree_data,
        editor=req.editor,
        description=description,
        is_ai=False,
        requested_top_event=(prev_ver or {}).get("requested_top_event") or meta.get("requested_top_event"),
        resolved_top_event=(prev_ver or {}).get("resolved_top_event") or meta.get("resolved_top_event") or meta.get("top_event"),
        normalized_top_event=(prev_ver or {}).get("normalized_top_event") or meta.get("normalized_top_event"),
        source_file_version_ids=(prev_ver or {}).get("source_file_version_ids") or meta.get("source_file_version_ids") or [],
        evidence_chunk_ids=(prev_ver or {}).get("evidence_chunk_ids") or (prev_ver or {}).get("source_chunk_ids") or meta.get("source_chunk_ids") or [],
        subgraph_node_ids=(prev_ver or {}).get("subgraph_node_ids") or [],
    )

    learned_count = 0
    if prev_version_num and prev_ver and prev_ver.get("is_ai_generated"):
        learned_count = analyze_and_store(tree_id, prev_version_num, new_version)

    return {
        "success": True,
        "version": new_version,
        "learned_count": learned_count,
        "graph_property_updates": {
            "attempted": len(graph_property_updates),
            "applied": graph_updates_applied,
            "errors": graph_update_errors,
        },
    }


@app.post("/api/tree/{tree_id}/rollback/{target_version}")
def api_rollback(tree_id: str, target_version: int):
    try:
        rollback_version(tree_id, target_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ver = get_version(tree_id, target_version)
    return {"success": True, "version": target_version, "tree_data": ver["tree_data"]}


@app.get("/api/tree/{tree_id}/history")
def api_history(tree_id: str):
    return get_version_list(tree_id)


@app.post("/api/tree/validate")
def api_validate(req: ValidateRequest):
    return validate_full(req.tree_data)


@app.post("/api/tree/validate/semantic")
def api_validate_semantic(req: SemanticValidateRequest):
    try:
        issues = validate_semantics(req.tree_data) or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI semantic validation failed: {exc}")

    error_count = sum(1 for issue in issues if getattr(issue, "level", "") == "ERROR")
    warning_count = sum(1 for issue in issues if getattr(issue, "level", "") == "WARNING")
    info_count = sum(1 for issue in issues if getattr(issue, "level", "") == "INFO")
    return {
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "issues": [issue.to_dict() for issue in issues],
    }


@app.get("/api/chunk/{chunk_id}")
def api_get_chunk(chunk_id: str):
    chunk = get_chunk_by_id(chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")
    chunk.pop("_id", None)
    return chunk


@app.get("/api/chunks")
def api_list_chunks(
    file_names: Optional[List[str]] = None,
    file_version_ids: Optional[List[str]] = None,
    limit: int = 200,
):
    """
    供前端「按文件名预览 chunks」使用：
    - file_names 可重复传参（?file_names=a.pdf&file_names=b.txt）
    - 后端按 chunk 的 file/source/path/doc_name 等字段做 basename 子串匹配
    """
    safe_limit = max(1, min(int(limit or 200), 1000))
    names = [str(n or "").strip() for n in (file_names or []) if str(n or "").strip()]
    fvs = [str(v or "").strip() for v in (file_version_ids or []) if str(v or "").strip()]

    # 防止误返回“全库 chunks”：必须给出过滤条件（文件名或 file_version_id）
    if not names and not fvs:
        return {"chunks": [], "total": 0}

    chunks = list_all_chunks(selected_file_version_ids=fvs or None)
    if not names:
        return {"chunks": chunks[:safe_limit], "total": len(chunks)}

    lowered = []
    for n in names:
        base = n.replace("\\\\", "/").split("/")[-1].lower()
        lowered.append(base)

    def _hit(doc: Dict[str, Any]) -> bool:
        blob = " ".join(
            [
                str(doc.get("file") or ""),
                str(doc.get("source") or ""),
                str(doc.get("path") or ""),
                str(doc.get("doc_name") or ""),
                str(doc.get("chunk_name") or ""),
                str(doc.get("section_path") or ""),
            ]
        ).lower()
        return any(b and b in blob for b in lowered)

    filtered = [c for c in chunks if _hit(c)]
    return {"chunks": filtered[:safe_limit], "total": len(filtered)}


@app.get("/api/corrections/{tree_id}")
def api_get_corrections(tree_id: str):
    docs = list(corrections_col.find({"tree_id": tree_id}, {"_id": 0}).sort("created_at", -1))
    return docs


@app.post("/api/files/{file_id}/archive")
def api_archive_file(file_id: str):
    try:
        doc = archive_file(file_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not doc:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"success": True, "file": doc}


@app.get("/api/catalog/top-events")
def api_list_catalog_top_events(selected_file_version_ids: Optional[str] = None):
    scope = [item.strip() for item in str(selected_file_version_ids or "").split(",") if item.strip()] or None
    try:
        discovery = _discover_batch_top_events_v2(scope)
    except HTTPException:
        discovery = {"discovered": [], "discovery_source": "empty", "graph_total": 0, "catalog_total": 0}
    docs = discovery["discovered"] or []
    return {
        "items": docs,
        "total": len(docs),
        "selected_file_version_ids": _resolve_selected_scope(scope),
        "discovery_source": discovery["discovery_source"],
        "graph_total": int(discovery.get("graph_total") or 0),
        "catalog_total": int(discovery.get("catalog_total") or 0),
    }


@app.get("/api/top-events")
def api_list_graph_top_events(limit: int = 200, selected_file_version_ids: Optional[str] = None):
    safe_limit = max(1, min(int(limit or 200), 1000))
    scope = [item.strip() for item in str(selected_file_version_ids or "").split(",") if item.strip()] or None
    try:
        discovery = _discover_batch_top_events_v2(scope)
    except HTTPException:
        discovery = {"discovered": [], "discovery_source": "empty", "graph_total": 0, "catalog_total": 0}
    items = (discovery["discovered"] or [])[:safe_limit]
    return {
        "items": items,
        "total": len(discovery["discovered"] or []),
        "returned": len(items),
        "selected_file_version_ids": _resolve_selected_scope(scope),
        "discovery_source": discovery["discovery_source"],
        "graph_total": int(discovery.get("graph_total") or 0),
        "catalog_total": int(discovery.get("catalog_total") or 0),
    }


@app.get("/")
def root():
    return {
        "message": "Fault tree generation system is running",
        "docs": "/docs",
        "generate_input_example": {"prompt": "请分析控制单元过热，并生成故障树"},
        "batch_endpoint": "/api/batch/generate-all",
        "integration_endpoint": "/api/integration/import-knowledge-artifacts",
    }
