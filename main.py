from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict

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

app = FastAPI(title="知识库构建与关系抽取服务", version="1.0.0")

_job_lock = threading.Lock()
_jobs: Dict[str, Dict] = {}


class PipelineJobRequest(BaseModel):
    pdf_path: str = Field(..., description="输入 PDF 文件路径")
    output_dir: str = Field("./output", description="输出目录")
    chunk_size: int = Field(800, ge=200, le=4000)
    skip_mineru: bool = False
    skip_entity: bool = False
    skip_relation: bool = False
    print_raw_text: bool = False


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


def _build_artifacts(pdf_path: str, output_dir: str) -> Dict[str, str]:
    pdf_stem = Path(pdf_path).stem
    result_dir = Path(output_dir).resolve() / pdf_stem
    return {
        "result_dir": str(result_dir),
        "chunks_json": str(result_dir / f"{pdf_stem}_chunks.json"),
        "entities_jsonl": str(result_dir / f"{pdf_stem}_entities.jsonl"),
        "entities_merged_json": str(result_dir / f"{pdf_stem}_entities_merged.json"),
        "relations_jsonl": str(result_dir / f"{pdf_stem}_relations.jsonl"),
        "relations_csv": str(result_dir / f"{pdf_stem}_relations.csv"),
    }


def _build_generate_fta_contract(artifacts: Dict[str, str]) -> Dict[str, list[str]]:
    return {
        "generate_fta_commands": [
            "cd D:\\fwwb\\fault_tree_system",
            f"python import_chunks.py --file \"{artifacts['chunks_json']}\"",
            f"python import_entity_index.py --file \"{artifacts['entities_merged_json']}\"",
            (
                "python import_relations_to_neo4j.py "
                f"--file \"{artifacts['relations_jsonl']}\" "
                "--uri bolt://localhost:7687 --user neo4j --password <password> --database neo4j"
            ),
        ],
        "artifact_contract": [
            "chunks_json -> generate-fta 分支导入 MongoDB chunks",
            "entities_merged_json -> generate-fta 分支导入 entity_reverse_index",
            "relations_jsonl -> generate-fta 分支导入 Neo4j 图谱",
        ],
    }


def _run_pipeline_job(job_id: str, request: PipelineJobRequest):
    started_at = time.time()
    artifacts = _build_artifacts(request.pdf_path, request.output_dir)

    cmd = [
        sys.executable,
        str(RUN_SCRIPT),
        "--pdf",
        request.pdf_path,
        "--output-dir",
        request.output_dir,
        "--chunk-size",
        str(request.chunk_size),
    ]
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
            "artifacts": artifacts,
            "integration": _build_generate_fta_contract(artifacts),
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
    _set_job(
        job_id,
        {
            "status": "success" if result.returncode == 0 else "failed",
            "finished_at": finished_at,
            "duration_seconds": round(finished_at - started_at, 3),
            "return_code": result.returncode,
            "stdout": _decode_output(result.stdout),
            "stderr": _decode_output(result.stderr),
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
            "artifact_flow": [
                "本服务输出 chunks / entities / relations 文件",
                "generate-fta 分支负责导入这些产物并完成故障树生成",
            ],
        },
    }


@app.post("/api/kb/jobs/run")
def run_pipeline_job(request: PipelineJobRequest):
    pdf_path = Path(request.pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        raise HTTPException(status_code=400, detail=f"PDF 文件不存在: {pdf_path}")
    if not RUN_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="找不到 run.py，无法启动知识库构建流水线")

    normalized_request = request.model_copy(update={"pdf_path": str(pdf_path)})
    job_id = f"kb_{uuid.uuid4().hex[:12]}"
    artifacts = _build_artifacts(str(pdf_path), normalized_request.output_dir)
    thread = threading.Thread(target=_run_pipeline_job, args=(job_id, normalized_request), daemon=True)
    thread.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "artifacts": artifacts,
        "integration": _build_generate_fta_contract(artifacts),
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
