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
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import (
    append_generation_job_item_event,
    claim_generation_job_item,
    create_generation_job,
    create_generation_job_item,
    create_tree,
    find_active_job_item_by_top_event,
    find_tree_by_top_event,
    get_chunk_by_id,
    get_generation_job,
    get_generation_job_item,
    get_tree_meta,
    get_version,
    get_version_list,
    list_all_chunks,
    list_entity_reverse_index,
    list_generation_job_items,
    refresh_generation_job,
    resolve_top_event_catalog,
    rollback_version,
    save_version,
    top_event_catalog_col,
    try_mark_job_completion_logged,
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
from validator import validate_full, validate_semantics

MAX_GENERATION_WORKERS = max(1, int(os.getenv("MAX_GENERATION_WORKERS", "2")))
RESERVED_SINGLE_WORKERS = 1 if MAX_GENERATION_WORKERS > 1 else 0
SHARED_WORKERS = max(1, MAX_GENERATION_WORKERS - RESERVED_SINGLE_WORKERS)
single_generation_queue: Queue[str] = Queue()
batch_generation_queue: Queue[str] = Queue()
generation_worker_threads: List[threading.Thread] = []

app = FastAPI(title="故障树智能生成系统", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _ensure_catalog_entry(name: str, aliases: Optional[List[str]] = None, source_chunk_ids: Optional[List[int]] = None):
    canonical_name = normalize_top_event_name(name)
    if not canonical_name:
        raise ValueError("顶事件不能为空")

    raw_aliases = _dedupe_keep_order((aliases or []) + [name])
    normalized_candidates = build_top_event_normalized_candidates(canonical_name, raw_aliases)
    existing = resolve_top_event_catalog(normalized_candidates=normalized_candidates)

    if existing:
        final_name = existing["name"]
        normalized_name = existing["normalized_name"]
        merged_aliases = _dedupe_keep_order((existing.get("aliases") or []) + raw_aliases)
        merged_normalized_aliases = _dedupe_keep_order(
            (existing.get("normalized_aliases") or [])
            + [candidate for candidate in normalized_candidates if candidate != normalized_name]
        )
        merged_source_chunk_ids = list(dict.fromkeys((existing.get("source_chunk_ids") or []) + (source_chunk_ids or [])))
        return upsert_top_event_catalog_entry(
            name=final_name,
            normalized_name=normalized_name,
            aliases=merged_aliases,
            normalized_aliases=merged_normalized_aliases,
            source_chunk_ids=merged_source_chunk_ids,
        )

    normalized_name = canonical_name
    normalized_aliases = [candidate for candidate in normalized_candidates if candidate != normalized_name]
    return upsert_top_event_catalog_entry(
        name=canonical_name,
        normalized_name=normalized_name,
        aliases=raw_aliases,
        normalized_aliases=normalized_aliases,
        source_chunk_ids=source_chunk_ids or [],
    )


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
            print(f"[scheduler] worker={worker_name} item={item_id} failed: {exc}")
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
    print(
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
    print(
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
    normalized_top_event = item["normalized_top_event"]
    aliases = _dedupe_keep_order(item.get("aliases") or [])
    requirements = item.get("requirements") or ""
    tree_id = None

    try:
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
        )
        if reused:
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
            top_event=top_event,
            catalog_name=top_event,
            normalized_top_event=normalized_top_event,
            aliases=aliases,
            source_chunk_ids=item.get("source_chunk_ids") or [],
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
            top_event=top_event,
            requirements=requirements,
            progress_callback=progress_callback,
            log_callback=log_callback,
        )

        version = save_version(
            tree_id=tree_id,
            tree_data=tree_data,
            editor="AI",
            description=f"AI initial generation for top event: {top_event}",
            is_ai=True,
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


def _queue_single_generation(prompt: str, parsed_top_event: str, requirements: str) -> Dict:
    catalog = _ensure_catalog_entry(parsed_top_event, aliases=[parsed_top_event])
    aliases = _dedupe_keep_order((catalog.get("aliases") or []) + [parsed_top_event])

    reused = find_tree_by_top_event(
        top_event=catalog["name"],
        normalized_top_event=catalog["normalized_name"],
        aliases=aliases,
        catalog_name=catalog["name"],
    )
    if reused:
        return {
            "mode": "reuse",
            "tree_id": reused["tree_id"],
            "version": reused["version"],
            "parsed_prompt": {
                "top_event": parsed_top_event,
                "catalog_top_event": catalog["name"],
                "requirements": requirements,
            },
            "tree_data": reused["tree_data"],
        }

    active_item = find_active_job_item_by_top_event(catalog["normalized_name"])
    if active_item:
        active_job = get_generation_job(active_item["job_id"])
        if active_item["status"] == "pending" and active_job and active_job.get("job_type") == "batch":
            batch_claim_owner = f"accelerated-batch:{uuid.uuid4().hex[:8]}"
            claimed_batch_item = claim_generation_job_item(
                active_item["item_id"],
                execution_owner=batch_claim_owner,
                allowed_statuses=["pending"],
                progress=1,
                stage="accelerated",
                message="Accelerated by single request",
            )
            if claimed_batch_item:
                job = create_generation_job(
                    job_type="single",
                    total=1,
                    top_event=catalog["name"],
                    metadata={
                        "requested_prompt": prompt,
                        "source": "/api/tree/generate",
                        "accelerated_batch_item_id": claimed_batch_item["item_id"],
                    },
                )
                item = create_generation_job_item(
                    job_id=job["job_id"],
                    top_event=catalog["name"],
                    normalized_top_event=catalog["normalized_name"],
                    aliases=aliases,
                    source_chunk_ids=catalog.get("source_chunk_ids") or [],
                    requirements=requirements,
                    metadata={
                        "requested_prompt": prompt,
                        "query_top_event": parsed_top_event,
                        "accelerated_batch_item_id": claimed_batch_item["item_id"],
                    },
                )
                _start_dedicated_generation_thread(item["item_id"], mirror_item_ids=[claimed_batch_item["item_id"]])
                return {
                    "mode": "queued",
                    "dispatch": "dedicated_worker",
                    "job_id": job["job_id"],
                    "item_id": item["item_id"],
                    "status": item["status"],
                    "progress": item["progress"],
                    "accelerated_batch_job_id": claimed_batch_item["job_id"],
                    "accelerated_batch_item_id": claimed_batch_item["item_id"],
                    "parsed_prompt": {
                        "top_event": parsed_top_event,
                        "catalog_top_event": catalog["name"],
                        "requirements": requirements,
                    },
                }

        return {
            "mode": "queued",
            "job_id": active_item["job_id"],
            "item_id": active_item["item_id"],
            "status": active_item["status"],
            "progress": active_item["progress"],
            "parsed_prompt": {
                "top_event": parsed_top_event,
                "catalog_top_event": catalog["name"],
                "requirements": requirements,
            },
        }

    job = create_generation_job(
        job_type="single",
        total=1,
        top_event=catalog["name"],
        metadata={"requested_prompt": prompt, "source": "/api/tree/generate"},
    )
    item = create_generation_job_item(
        job_id=job["job_id"],
        top_event=catalog["name"],
        normalized_top_event=catalog["normalized_name"],
        aliases=aliases,
        source_chunk_ids=catalog.get("source_chunk_ids") or [],
        requirements=requirements,
        metadata={"requested_prompt": prompt, "query_top_event": parsed_top_event},
    )
    _submit_generation_item(item["item_id"], "single")

    return {
        "mode": "queued",
        "job_id": job["job_id"],
        "item_id": item["item_id"],
        "status": item["status"],
        "progress": item["progress"],
        "parsed_prompt": {
            "top_event": parsed_top_event,
            "catalog_top_event": catalog["name"],
            "requirements": requirements,
        },
    }


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
    prompt: str


class SaveRequest(BaseModel):
    tree_data: dict
    editor: str = "专家"
    description: str = "手动修改"


class ValidateRequest(BaseModel):
    tree_data: dict


class SemanticValidateRequest(BaseModel):
    tree_data: dict


@app.post("/api/tree/generate")
def api_generate(req: GenerateRequest):
    try:
        parsed = parse_user_prompt(req.prompt)
        top_event = parsed["top_event"]
        requirements = parsed.get("requirements", "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Prompt parse failed: {exc}")

    try:
        return _queue_single_generation(req.prompt, top_event, requirements)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Queue generation failed: {exc}")


@app.post("/api/batch/generate-all")
def api_batch_generate_all():
    entity_index_entries = list_entity_reverse_index()
    if entity_index_entries:
        discovered = discover_top_events_from_entity_index(entity_index_entries)
        discovery_source = "entity_reverse_index"
    else:
        chunks = list_all_chunks()
        if not chunks:
            raise HTTPException(status_code=400, detail="No chunks found, please import knowledge chunks first")
        discovered = discover_top_events(chunks)
        discovery_source = "chunks"

    if not discovered:
        raise HTTPException(status_code=400, detail="No top events were discovered from chunks")

    catalog_entries = []
    for item in discovered:
        try:
            entry = _ensure_catalog_entry(
                item["name"],
                aliases=item.get("aliases") or [],
                source_chunk_ids=item.get("source_chunk_ids") or [],
            )
        except ValueError:
            continue
        catalog_entries.append(entry)

    existing_count = 0
    active_count = 0
    queued_entries = []
    for entry in catalog_entries:
        aliases = entry.get("aliases") or []
        reused = find_tree_by_top_event(
            top_event=entry["name"],
            normalized_top_event=entry["normalized_name"],
            aliases=aliases,
            catalog_name=entry["name"],
        )
        if reused:
            existing_count += 1
            continue

        active_item = find_active_job_item_by_top_event(entry["normalized_name"])
        if active_item:
            active_count += 1
            continue

        queued_entries.append(entry)

    job = create_generation_job(
        job_type="batch",
        total=len(queued_entries),
        metadata={
            "discovered_total": len(discovered),
            "catalog_total": len(catalog_entries),
            "existing_count": existing_count,
            "active_count": active_count,
            "discovery_source": discovery_source,
            "source": "/api/batch/generate-all",
        },
    )

    queued_item_ids = []
    for entry in queued_entries:
        item = create_generation_job_item(
            job_id=job["job_id"],
            top_event=entry["name"],
            normalized_top_event=entry["normalized_name"],
            aliases=entry.get("aliases") or [],
            source_chunk_ids=entry.get("source_chunk_ids") or [],
            requirements="",
            metadata={"source": "batch_generate_all"},
        )
        queued_item_ids.append(item["item_id"])
        _submit_generation_item(item["item_id"], "batch")

    refresh_generation_job(job["job_id"])
    return {
        "job_id": job["job_id"],
        "status": get_generation_job(job["job_id"])["status"],
        "discovered_total": len(discovered),
        "catalog_total": len(catalog_entries),
        "existing_count": existing_count,
        "active_count": active_count,
        "discovery_source": discovery_source,
        "queued_count": len(queued_entries),
        "queued_item_ids": queued_item_ids,
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

    new_version = save_version(
        tree_id=tree_id,
        tree_data=req.tree_data,
        editor=req.editor,
        description=description,
        is_ai=False,
    )

    learned_count = 0
    if prev_version_num and prev_ver and prev_ver.get("is_ai_generated"):
        learned_count = analyze_and_store(tree_id, prev_version_num, new_version)

    return {"success": True, "version": new_version, "learned_count": learned_count}


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


@app.get("/api/corrections/{tree_id}")
def api_get_corrections(tree_id: str):
    docs = list(corrections_col.find({"tree_id": tree_id}, {"_id": 0}).sort("created_at", -1))
    return docs


@app.get("/api/catalog/top-events")
def api_list_catalog_top_events():
    docs = list(top_event_catalog_col.find({}, {"_id": 0}).sort("name", 1))
    return {"items": docs, "total": len(docs)}


@app.get("/")
def root():
    return {
        "message": "Fault tree generation system is running",
        "docs": "/docs",
        "generate_input_example": {"prompt": "请分析控制单元过热，并生成故障树"},
        "batch_endpoint": "/api/batch/generate-all",
    }
