from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from import_relations_to_neo4j import (
    GraphDatabase,
    clear_graph,
    ensure_constraints,
    import_rows,
    load_json,
)

ROOT_DIR = Path(__file__).parent.resolve()
RUN_SCRIPT = ROOT_DIR / "run.py"
DEFAULT_GENERATE_FTA_BASE_URL = os.getenv("GENERATE_FTA_BASE_URL", "http://127.0.0.1:8000")

app = FastAPI(title="知识库构建与关系抽取服务", version="1.2.0")

_job_lock = threading.Lock()
_jobs: Dict[str, Dict] = {}


class PipelineJobRequest(BaseModel):
    pdf_path: Optional[str] = Field(None, description="输入 PDF 文件路径；导入模式下可不填")
    pdf_stem: Optional[str] = Field(None, description="导入模式下显式指定产物前缀")
    import_only_dir: Optional[str] = Field(None, description="直接复用已有产物目录，跳过所有生成步骤")
    output_dir: str = Field("./output", description="输出目录")
    chunk_size: int = Field(800, ge=200, le=4000)
    skip_mineru: bool = False
    skip_entity: bool = False
    skip_relation: bool = False
    print_raw_text: bool = False
    sync_to_generate_fta: bool = True
    generate_fta_base_url: str = Field(
        DEFAULT_GENERATE_FTA_BASE_URL,
        description="故障树自动生成服务地址，例如 http://127.0.0.1:8000",
    )
    clear_graph_before_import: bool = True


class Neo4jImportRequest(BaseModel):
    file_path: str = Field(..., description="关系 JSON/JSONL 文件路径")
    uri: str = Field("bolt://localhost:7687", description="Neo4j URI")
    user: str = Field("neo4j", description="Neo4j 用户名")
    password: str = Field(..., description="Neo4j 密码")
    database: str = Field("neo4j", description="Neo4j 数据库")
    batch_size: int = Field(200, ge=1, le=5000)
    clear: bool = False



def _set_job(job_id: str, patch: Dict) -> Dict:
    with _job_lock:
        current = _jobs.setdefault(job_id, {})
        current.update(patch)
        return dict(current)



def _get_job(job_id: str) -> Dict:
    with _job_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(job)



def _decode_output(data: bytes) -> str:
    return (data or b"").decode("utf-8", errors="replace")



def _auto_detect_pdf_stem(import_only_dir: Path) -> str:
    chunk_files = sorted(import_only_dir.glob("*_chunks.json"))
    if len(chunk_files) == 1:
        return chunk_files[0].name[: -len("_chunks.json")]
    raise ValueError("导入模式下请提供 pdf_stem，或保证目录中只有一个 *_chunks.json 文件")



def _resolve_pdf_stem(pdf_path: Optional[str], pdf_stem: Optional[str], import_only_dir: Optional[str]) -> str:
    if pdf_stem:
        return pdf_stem
    if pdf_path:
        return Path(pdf_path).stem
    if import_only_dir:
        return _auto_detect_pdf_stem(Path(import_only_dir).resolve())
    raise ValueError("缺少 pdf_path 或 pdf_stem")



def _build_artifacts(pdf_stem: str, output_dir: str, import_only_dir: Optional[str] = None) -> Dict[str, str]:
    result_dir = Path(import_only_dir).resolve() if import_only_dir else Path(output_dir).resolve() / pdf_stem
    return {
        "result_dir": str(result_dir),
        "chunks_json": str(result_dir / f"{pdf_stem}_chunks.json"),
        "entities_jsonl": str(result_dir / f"{pdf_stem}_entities.jsonl"),
        "entities_merged_json": str(result_dir / f"{pdf_stem}_entities_merged.json"),
        "relations_jsonl": str(result_dir / f"{pdf_stem}_relations.jsonl"),
        "relations_csv": str(result_dir / f"{pdf_stem}_relations.csv"),
    }



def _build_generate_fta_contract(artifacts: Dict[str, str], base_url: str, clear_graph: bool) -> Dict[str, object]:
    endpoint = base_url.rstrip("/") + "/api/integration/import-knowledge-artifacts"
    payload = {
        "chunks_file": artifacts["chunks_json"],
        "entities_file": artifacts["entities_merged_json"],
        "relations_file": artifacts["relations_jsonl"],
        "clear_graph": clear_graph,
        "import_relations": True,
        "source": "knowledge_base_construction",
    }
    return {
        "generate_fta_endpoint": endpoint,
        "artifact_contract": [
            "chunks_json -> generate-fta 导入 MongoDB chunks",
            "entities_merged_json -> generate-fta 导入 entity_reverse_index",
            "relations_jsonl -> generate-fta 导入 Neo4j 图谱",
        ],
        "example_payload": payload,
    }



def _post_generate_fta_import(job_id: str, request: PipelineJobRequest, artifacts: Dict[str, str]) -> Dict[str, object]:
    endpoint = request.generate_fta_base_url.rstrip("/") + "/api/integration/import-knowledge-artifacts"
    payload = {
        "chunks_file": artifacts["chunks_json"],
        "entities_file": None if request.skip_entity else artifacts["entities_merged_json"],
        "relations_file": None if request.skip_relation else artifacts["relations_jsonl"],
        "clear_graph": request.clear_graph_before_import,
        "import_relations": not request.skip_relation,
        "source": f"knowledge_base_construction:{job_id}",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {"status": "success"}
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"generate-fta 导入接口返回 {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"无法连接 generate-fta 服务: {exc}") from exc



def _run_pipeline_job(job_id: str, request: PipelineJobRequest):
    pdf_stem = _resolve_pdf_stem(request.pdf_path, request.pdf_stem, request.import_only_dir)
    artifacts = _build_artifacts(pdf_stem, request.output_dir, request.import_only_dir)
    started_at = time.time()

    cmd = [sys.executable, str(RUN_SCRIPT)]
    if request.pdf_path:
        cmd.extend(["--pdf", request.pdf_path])
    if request.pdf_stem:
        cmd.extend(["--pdf-stem", request.pdf_stem])
    if request.import_only_dir:
        cmd.extend(["--import-only-dir", request.import_only_dir])
    else:
        cmd.extend(["--output-dir", request.output_dir, "--chunk-size", str(request.chunk_size)])
    if request.skip_mineru:
        cmd.append("--skip-mineru")
    if request.skip_entity:
        cmd.append("--skip-entity")
    if request.skip_relation:
        cmd.append("--skip-relation")
    if request.print_raw_text:
        cmd.append("--print-raw-text")

    _set_job(
        job_id,
        {
            "job_id": job_id,
            "type": "pipeline",
            "status": "running",
            "started_at": started_at,
            "request": request.model_dump(),
            "pdf_stem": pdf_stem,
            "artifacts": artifacts,
            "integration": _build_generate_fta_contract(artifacts, request.generate_fta_base_url, request.clear_graph_before_import),
            "command": cmd,
        },
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(
        cmd,
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=False,
        env=env,
    )

    finished_at = time.time()
    base_patch = {
        "finished_at": finished_at,
        "duration_seconds": round(finished_at - started_at, 3),
        "return_code": result.returncode,
        "stdout": _decode_output(result.stdout),
        "stderr": _decode_output(result.stderr),
    }

    if result.returncode != 0:
        _set_job(job_id, {**base_patch, "status": "failed", "sync_status": "skipped"})
        return

    if not request.sync_to_generate_fta:
        _set_job(job_id, {**base_patch, "status": "success", "sync_status": "skipped"})
        return

    _set_job(job_id, {**base_patch, "status": "syncing", "sync_status": "running"})
    try:
        sync_response = _post_generate_fta_import(job_id, request, artifacts)
        _set_job(
            job_id,
            {
                "status": "success",
                "sync_status": "success",
                "sync_response": sync_response,
            },
        )
    except Exception as exc:
        _set_job(
            job_id,
            {
                "status": "completed_with_sync_error",
                "sync_status": "failed",
                "sync_error": str(exc),
            },
        )


@app.get("/")
def root():
    return {
        "service": "knowledge_base_construction",
        "description": "知识库构建与关系抽取模块服务",
        "docs": "/docs",
        "endpoints": {
            "run_pipeline": "/api/kb/jobs/run",
            "get_job": "/api/kb/jobs/{job_id}",
            "import_relations_to_neo4j": "/api/kb/neo4j/import",
        },
        "downstream": {
            "generate_fta_repo": "D:\\fwwb\\fault_tree_system",
            "frontend_repo": "D:\\fwwb\\fault_tree_visual",
            "default_generate_fta_base_url": DEFAULT_GENERATE_FTA_BASE_URL,
            "artifact_flow": [
                "本服务输出 chunks / entities / relations 文件",
                "默认会在流水线完成后自动调用 generate-fta 的导入接口",
                "也支持 import_only_dir 模式直接复用已有产物后同步到 generate-fta",
            ],
        },
    }


@app.post("/api/kb/jobs/run")
def run_pipeline_job(request: PipelineJobRequest):
    if request.import_only_dir:
        import_only_dir = Path(request.import_only_dir).expanduser().resolve()
        if not import_only_dir.exists():
            raise HTTPException(status_code=400, detail=f"导入目录不存在: {import_only_dir}")
        try:
            pdf_stem = _resolve_pdf_stem(request.pdf_path, request.pdf_stem, str(import_only_dir))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        normalized_request = request.model_copy(
            update={
                "import_only_dir": str(import_only_dir),
                "pdf_stem": pdf_stem,
                "pdf_path": str(Path(request.pdf_path).expanduser().resolve()) if request.pdf_path else None,
            }
        )
    else:
        if not request.pdf_path:
            raise HTTPException(status_code=400, detail="生成模式下必须提供 pdf_path")
        pdf_path = Path(request.pdf_path).expanduser().resolve()
        if not pdf_path.exists():
            raise HTTPException(status_code=400, detail=f"PDF 文件不存在: {pdf_path}")
        normalized_request = request.model_copy(update={"pdf_path": str(pdf_path), "pdf_stem": Path(pdf_path).stem})

    if not RUN_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="找不到 run.py，无法启动知识库构建流水线")

    pdf_stem = _resolve_pdf_stem(normalized_request.pdf_path, normalized_request.pdf_stem, normalized_request.import_only_dir)
    artifacts = _build_artifacts(pdf_stem, normalized_request.output_dir, normalized_request.import_only_dir)
    job_id = f"kb_{uuid.uuid4().hex[:12]}"
    thread = threading.Thread(target=_run_pipeline_job, args=(job_id, normalized_request), daemon=True)
    thread.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "pdf_stem": pdf_stem,
        "artifacts": artifacts,
        "integration": _build_generate_fta_contract(artifacts, normalized_request.generate_fta_base_url, normalized_request.clear_graph_before_import),
    }


@app.get("/api/kb/jobs/{job_id}")
def get_pipeline_job(job_id: str):
    return _get_job(job_id)


@app.post("/api/kb/neo4j/import")
def import_relations_to_neo4j_api(request: Neo4jImportRequest):
    file_path = Path(request.file_path).expanduser().resolve()
    if not file_path.exists():
        raise HTTPException(status_code=400, detail=f"关系文件不存在: {file_path}")

    rows = load_json(file_path)
    if not rows:
        raise HTTPException(status_code=400, detail="关系文件中没有可导入的数据")

    driver = GraphDatabase.driver(request.uri, auth=(request.user, request.password))
    try:
        driver.verify_connectivity()
        ensure_constraints(driver, request.database)
        if request.clear:
            clear_graph(driver, request.database)
        relation_count = import_rows(driver, request.database, rows, request.batch_size)
    finally:
        driver.close()

    return {
        "file_path": str(file_path),
        "database": request.database,
        "rows": len(rows),
        "relations": relation_count,
        "status": "success",
    }
