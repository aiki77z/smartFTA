from __future__ import annotations

import json
import logging
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

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
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

logging.basicConfig(
    level=os.getenv("KB_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("fta-kb")

# 允许前端（Vite dev server 等）跨域调用 KB 服务
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        prev_stage = current.get("stage")
        prev_status = current.get("status")
        prev_progress = current.get("progress")
        current.update(patch)
        next_stage = current.get("stage")
        next_status = current.get("status")
        next_progress = current.get("progress")
        next_message = current.get("message")

        # 仅在状态/阶段变化时打印，避免过度刷屏
        if (
            ("stage" in patch and next_stage != prev_stage)
            or ("status" in patch and next_status != prev_status)
            or ("progress" in patch and next_progress != prev_progress and next_stage != prev_stage)
        ):
            try:
                logger.info(
                    "job=%s status=%s stage=%s progress=%s message=%s",
                    job_id,
                    next_status,
                    next_stage,
                    next_progress,
                    (str(next_message)[:200] if next_message else ""),
                )
            except Exception:
                # ignore logging failures
                pass
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

    # 使用 -u / PYTHONUNBUFFERED 关闭子进程 stdout 缓冲，确保阶段识别与前端进度能实时推进
    cmd = [sys.executable, "-u", str(RUN_SCRIPT)]
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
    env["PYTHONUNBUFFERED"] = "1"

    # 实时输出：逐行读取 run.py 的 stdout，并将阶段/进度同步到 job（便于前端展示“进行到哪一步”）
    stage_progress_map = {
        "queued": 0,
        "prepare": 3,
        "parse": 22,
        "chunk": 40,
        "entity": 62,
        "relation": 78,
        "pipeline_done": 85,
        "syncing": 92,
        "success": 100,
        "failed": 100,
        "completed_with_sync_error": 100,
    }

    def _update_stage(stage: str, message: str):
        _set_job(
            job_id,
            {
                "stage": stage,
                "progress": stage_progress_map.get(stage, 0),
                "message": message,
            },
        )

    _update_stage("prepare", "准备启动流水线…")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
    except Exception as exc:
        # 启动子进程失败（例如找不到 python / 权限 / 路径问题）
        _set_job(
            job_id,
            {
                "status": "failed",
                "stage": "failed",
                "progress": 100,
                "sync_status": "skipped",
                "error": f"无法启动流水线进程: {exc}",
                "message": "无法启动流水线进程",
                "finished_at": time.time(),
            },
        )
        return
    out_lines = []
    if proc.stdout is not None:
        for line in proc.stdout:
            text = (line or "").rstrip("\n")
            out_lines.append(text)
            # 阶段识别：run.py 固定打印 “=== 步骤X: ... ===”
            if "=== 步骤1: PDF 转 Markdown" in text:
                _update_stage("parse", "解析文件：PDF 转 Markdown / 清理 Markdown")
            elif "=== 步骤1.5: 清理 Markdown" in text:
                _update_stage("parse", "解析文件：PDF 转 Markdown / 清理 Markdown")
            elif "=== 步骤2: Markdown 分块" in text:
                _update_stage("chunk", "步骤2：文本分块")
            elif "=== 步骤3: 实体提取" in text:
                _update_stage("entity", "步骤3：实体提取")
            elif "=== 步骤4: 关系提取" in text:
                _update_stage("relation", "步骤4：关系提取")
            elif "=== 流水线执行完成 ===" in text:
                _update_stage("pipeline_done", "流水线已完成，准备同步到故障树后端…")

    return_code = proc.wait()
    stdout_text = "\n".join(out_lines)

    finished_at = time.time()
    base_patch = {
        "finished_at": finished_at,
        "duration_seconds": round(finished_at - started_at, 3),
        "return_code": return_code,
        "stdout": stdout_text,
        "stderr": "",
    }

    if return_code != 0:
        tail = "\n".join(stdout_text.splitlines()[-20:]).strip()
        msg = f"流水线失败（return_code={return_code}）。请查看 stdout；末尾摘要：{tail[:600]}"
        _set_job(
            job_id,
            {
                **base_patch,
                "status": "failed",
                "stage": "failed",
                "progress": 100,
                "sync_status": "skipped",
                "error": msg,
                "message": msg,
            },
        )
        return

    if not request.sync_to_generate_fta:
        _set_job(job_id, {**base_patch, "status": "success", "stage": "success", "progress": 100, "sync_status": "skipped"})
        return

    _update_stage("syncing", "同步到故障树后端（导入 chunks/entities/relations）…")
    _set_job(job_id, {**base_patch, "status": "syncing", "sync_status": "running"})
    try:
        sync_response = _post_generate_fta_import(job_id, request, artifacts)
        _set_job(
            job_id,
            {
                "status": "success",
                "stage": "success",
                "progress": 100,
                "sync_status": "success",
                "sync_response": sync_response,
            },
        )
    except Exception as exc:
        _set_job(
            job_id,
            {
                "status": "completed_with_sync_error",
                "stage": "completed_with_sync_error",
                "progress": 100,
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
            "run_upload": "/api/kb/jobs/run-upload",
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

    # 先写入“已创建/排队中”的 job，前端无需等待轮询即可显示初始状态
    _set_job(
        job_id,
        {
            "job_id": job_id,
            "type": "pipeline",
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "message": "任务已创建，等待启动…",
            "created_at": time.time(),
            "request": normalized_request.model_dump(),
            "pdf_stem": pdf_stem,
            "artifacts": artifacts,
            "integration": _build_generate_fta_contract(
                artifacts,
                normalized_request.generate_fta_base_url,
                normalized_request.clear_graph_before_import,
            ),
        },
    )
    logger.info("enqueue job=%s pdf_stem=%s import_only_dir=%s", job_id, pdf_stem, normalized_request.import_only_dir or "")
    thread = threading.Thread(target=_run_pipeline_job, args=(job_id, normalized_request), daemon=True)
    thread.start()
    return _get_job(job_id)


@app.post("/api/kb/jobs/run-upload")
async def run_pipeline_job_upload(
    file: UploadFile = File(...),
    output_dir: str = Form("./output"),
    chunk_size: int = Form(800),
    skip_mineru: bool = Form(False),
    skip_entity: bool = Form(False),
    skip_relation: bool = Form(False),
    print_raw_text: bool = Form(False),
    sync_to_generate_fta: bool = Form(True),
    generate_fta_base_url: str = Form(DEFAULT_GENERATE_FTA_BASE_URL),
    clear_graph_before_import: bool = Form(True),
):
    """
    兼容前端上传模式（multipart/form-data）：
    - 接收浏览器上传的 PDF/TXT 等文件
    - 保存到 output_dir/_uploads 下
    - 复用 /api/kb/jobs/run 的流水线（pdf_path 指向保存后的本地路径）
    """
    name = (file.filename or "upload.bin").strip()
    safe_name = "".join([c for c in name if c not in '\\/:*?"<>|']) or "upload.bin"
    out_root = Path(output_dir).expanduser().resolve()
    upload_dir = out_root / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(safe_name).suffix or ""
    # 避免同名覆盖
    saved_path = upload_dir / f"{Path(safe_name).stem}_{uuid.uuid4().hex[:8]}{suffix}"

    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取上传文件失败: {exc}")

    try:
        saved_path.write_bytes(content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存上传文件失败: {exc}")

    logger.info("upload received name=%s bytes=%s saved=%s", safe_name, len(content or b""), saved_path)

    req = PipelineJobRequest(
        pdf_path=str(saved_path),
        output_dir=str(out_root),
        chunk_size=int(chunk_size),
        skip_mineru=bool(skip_mineru),
        skip_entity=bool(skip_entity),
        skip_relation=bool(skip_relation),
        print_raw_text=bool(print_raw_text),
        sync_to_generate_fta=bool(sync_to_generate_fta),
        generate_fta_base_url=str(generate_fta_base_url or DEFAULT_GENERATE_FTA_BASE_URL),
        clear_graph_before_import=bool(clear_graph_before_import),
    )
    resp = run_pipeline_job(req)
    try:
        job_id = str(resp.get("job_id") or "")
        if job_id:
            _set_job(job_id, {"uploaded_file_path": str(saved_path), "uploaded_file_name": safe_name})
    except Exception:
        pass
    return resp


@app.get("/api/kb/jobs/{job_id}/download")
def download_uploaded_file(job_id: str):
    job = _get_job(job_id)
    path = str(job.get("uploaded_file_path") or "").strip()
    if not path:
        raise HTTPException(status_code=404, detail="该任务未记录上传文件路径")
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"上传文件不存在: {p}")
    fname = str(job.get("uploaded_file_name") or p.name)
    return FileResponse(str(p), filename=fname)


@app.get("/api/kb/jobs/{job_id}")
def get_pipeline_job(job_id: str):
    # 这里会被前端高频轮询；默认不打 INFO，避免刷屏（需要可调 KB_LOG_LEVEL=DEBUG）
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("poll job=%s", job_id)
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
