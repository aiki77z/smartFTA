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

from env_loader import load_local_env
from import_relations_to_neo4j import (
    GraphDatabase,
    clear_graph,
    ensure_constraints,
    import_rows,
    load_json,
)
from maintenance_cases import import_maintenance_cases
from work_order_import import import_work_order_file, profile_work_order_file

ROOT_DIR = Path(__file__).parent.resolve()
load_local_env(ROOT_DIR / ".env")
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
    skip_clean: bool = False
    skip_entity: bool = False
    skip_relation: bool = False
    print_raw_text: bool = False
    sync_to_generate_fta: bool = True
    generate_fta_base_url: str = Field(
        DEFAULT_GENERATE_FTA_BASE_URL,
        description="故障树自动生成服务地址，例如 http://127.0.0.1:8000",
    )
    # 下游（FTA-GNR）版本化知识库模式禁止 clear_graph=true
    clear_graph_before_import: bool = False
    # 同步到 FTA-GNR 时显式传入，需与 chunks/relations 产物中的 file_id 一致（通常为 pdf_stem）
    file_name: Optional[str] = Field(
        None,
        description="展示用原始文件名；缺省时用 pdf_path 的文件名或 {pdf_stem}.pdf",
    )
    # 与 run.py 版本目录名一致（如 {pdf_stem}_v1）；缺省为同步时 result_dir 的目录名
    file_version_id: Optional[str] = Field(
        None,
        description="显式传给 GNR 的 file_version_id，需与 chunks/relations 产物一致",
    )
    source_type: str = Field(
        "manual_document",
        description="来源业务类型，如 manual_document / standard_document",
    )
    file_format: Optional[str] = Field(
        None,
        description="原始输入文件格式；不传时由 run.py 根据输入文件推断",
    )
    chunk_type: str = Field(
        "document_section",
        description="chunk 内容类型；阶段0文档链路默认 document_section",
    )
    source_record_type: Optional[str] = Field(
        None,
        description="原始记录类型，文档类通常为空",
    )
    source_record_id: Optional[str] = Field(
        None,
        description="原始记录 ID，文档类通常为空",
    )


class Neo4jImportRequest(BaseModel):
    file_path: str = Field(..., description="关系 JSON/JSONL 文件路径")
    uri: str = Field("bolt://localhost:7687", description="Neo4j URI")
    user: str = Field("neo4j", description="Neo4j 用户名")
    password: str = Field(..., description="Neo4j 密码")
    database: str = Field("neo4j", description="Neo4j 数据库")
    batch_size: int = Field(200, ge=1, le=5000)
    clear: bool = False


class MaintenanceImportRequest(BaseModel):
    input_path: str = Field(..., description="维修记录文件路径，支持 md/txt/docx/pdf/csv")
    output_dir: str = Field("./output", description="输出目录")
    case_id_prefix: str = Field("case", description="维修案例编号前缀")
    max_summary_chars: int = Field(800, ge=120, le=4000)
    skip_entity: bool = False
    skip_relation: bool = False
    print_raw_text: bool = False



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


def _extract_version_dir_from_stdout(stdout_text: str) -> Optional[Path]:
    """
    run.py 会打印一行：VERSION_DIR=<abs path>
    在版本化产物模式下，chunks/entities/relations 会写到该版本目录内。
    """
    if not stdout_text:
        return None
    version_dir: Optional[Path] = None
    for line in stdout_text.splitlines():
        if not line.startswith("VERSION_DIR="):
            continue
        raw = line.split("=", 1)[1].strip()
        if raw:
            version_dir = Path(raw).expanduser().resolve()
    return version_dir if (version_dir and version_dir.exists()) else None


def _build_artifacts_for_dir(pdf_stem: str, result_dir: Path) -> Dict[str, str]:
    rd = Path(result_dir).expanduser().resolve()
    return {
        "result_dir": str(rd),
        "chunks_json": str(rd / f"{pdf_stem}_chunks.json"),
        "entities_jsonl": str(rd / f"{pdf_stem}_entities.jsonl"),
        "entities_merged_json": str(rd / f"{pdf_stem}_entities_merged.json"),
        "relations_jsonl": str(rd / f"{pdf_stem}_relations.jsonl"),
        "relations_csv": str(rd / f"{pdf_stem}_relations.csv"),
    }


def _next_version_dir(output_root: Path, file_id: str) -> Path:
    base_dir = output_root / file_id
    max_version = 0
    if base_dir.exists():
        prefix = f"{file_id}_v"
        for item in base_dir.iterdir():
            if not item.is_dir() or not item.name.startswith(prefix):
                continue
            suffix = item.name[len(prefix):]
            if suffix.isdigit():
                max_version = max(max_version, int(suffix))
    return base_dir / f"{file_id}_v{max_version + 1}"


def _latest_version_dir(output_root: Path, file_id: str) -> Optional[Path]:
    base_dir = output_root / file_id
    if not base_dir.exists():
        return None
    latest: Optional[Path] = None
    max_version = 0
    prefix = f"{file_id}_v"
    for item in base_dir.iterdir():
        if not item.is_dir() or not item.name.startswith(prefix):
            continue
        suffix = item.name[len(prefix):]
        if suffix.isdigit() and int(suffix) > max_version:
            max_version = int(suffix)
            latest = item
    return latest.resolve() if latest else None



def _resolve_import_display_file_name(request: PipelineJobRequest, pdf_stem: str) -> str:
    explicit = (request.file_name or "").strip()
    if explicit:
        return explicit
    if request.pdf_path:
        return Path(request.pdf_path).name
    return f"{pdf_stem}.pdf"


def _resolve_sync_file_version_id(request: PipelineJobRequest, artifacts: Dict[str, str]) -> str:
    explicit = (request.file_version_id or "").strip()
    if explicit:
        return explicit
    return Path(artifacts["result_dir"]).expanduser().resolve().name


def _build_generate_fta_contract(
    artifacts: Dict[str, str],
    base_url: str,
    clear_graph: bool,
    *,
    pdf_stem: str,
    file_name: str,
    file_version_id: Optional[str] = None,
) -> Dict[str, object]:
    endpoint = base_url.rstrip("/") + "/api/integration/import-knowledge-artifacts"
    resolved_fv = file_version_id or Path(artifacts["result_dir"]).expanduser().resolve().name
    payload = {
        "chunks_file": artifacts["chunks_json"],
        "entities_file": artifacts["entities_merged_json"],
        "relations_file": artifacts["relations_jsonl"],
        "clear_graph": clear_graph,
        "import_relations": True,
        "source": "knowledge_base_construction",
        "file_id": pdf_stem,
        "file_name": file_name,
        "file_version_id": resolved_fv,
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



def _post_generate_fta_import(
    job_id: str,
    request: PipelineJobRequest,
    artifacts: Dict[str, str],
    pdf_stem: str,
) -> Dict[str, object]:
    endpoint = request.generate_fta_base_url.rstrip("/") + "/api/integration/import-knowledge-artifacts"
    display_name = _resolve_import_display_file_name(request, pdf_stem)
    sync_fv = _resolve_sync_file_version_id(request, artifacts)
    payload = {
        "chunks_file": artifacts["chunks_json"],
        "entities_file": None if request.skip_entity else artifacts["entities_merged_json"],
        "relations_file": None if request.skip_relation else artifacts["relations_jsonl"],
        # 由下游（FTA-GNR）执行版本化约束：若 clear_graph=true 会返回 400
        "clear_graph": request.clear_graph_before_import,
        "import_relations": not request.skip_relation,
        "source": f"knowledge_base_construction:{job_id}",
        # 与产物内 file_id 对齐，避免 GNR 自建 file_* 导致 Mongo/Neo4j 与 chunks 元数据分裂
        "file_id": pdf_stem,
        "file_name": display_name,
        "file_version_id": sync_fv,
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


def _post_generate_fta_artifacts(
    *,
    base_url: str,
    payload: Dict[str, object],
) -> Dict[str, object]:
    endpoint = base_url.rstrip("/") + "/api/integration/import-knowledge-artifacts"
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
    display_file_name = _resolve_import_display_file_name(request, pdf_stem)
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
    if request.skip_clean:
        cmd.append("--skip-clean")
    if request.skip_entity:
        cmd.append("--skip-entity")
    if request.skip_relation:
        cmd.append("--skip-relation")
    if request.print_raw_text:
        cmd.append("--print-raw-text")
    if request.source_type:
        cmd.extend(["--source-type", request.source_type])
    if request.file_format:
        cmd.extend(["--file-format", request.file_format])
    if request.chunk_type:
        cmd.extend(["--chunk-type", request.chunk_type])
    if request.source_record_type:
        cmd.extend(["--source-record-type", request.source_record_type])
    if request.source_record_id:
        cmd.extend(["--source-record-id", request.source_record_id])

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
            "integration": _build_generate_fta_contract(
                artifacts,
                request.generate_fta_base_url,
                request.clear_graph_before_import,
                pdf_stem=pdf_stem,
                file_name=display_file_name,
                file_version_id=_resolve_sync_file_version_id(request, artifacts),
            ),
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
            # 若子脚本明确打印“错误: ...”，立刻把错误同步到 job，避免前端长期停留在 prepare/parse 阶段
            if text.startswith("错误:"):
                _update_stage("failed", text[:260])
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
        tail_lines = stdout_text.splitlines()[-60:]
        tail = "\n".join(tail_lines).strip()
        msg = f"流水线失败（return_code={return_code}）。请查看 stdout；末尾摘要：{tail[:2000]}"
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

    # 版本化产物：优先使用 run.py 输出的 VERSION_DIR，避免同步阶段找不到 chunks 文件
    version_dir = _extract_version_dir_from_stdout(stdout_text)
    if not version_dir:
        version_dir = _latest_version_dir(Path(request.output_dir).expanduser().resolve(), pdf_stem)
    if version_dir:
        artifacts = _build_artifacts_for_dir(pdf_stem, version_dir)
        try:
            _set_job(
                job_id,
                {
                    "artifacts": artifacts,
                    "integration": _build_generate_fta_contract(
                        artifacts,
                        request.generate_fta_base_url,
                        request.clear_graph_before_import,
                        pdf_stem=pdf_stem,
                        file_name=display_file_name,
                        file_version_id=_resolve_sync_file_version_id(request, artifacts),
                    ),
                },
            )
        except Exception:
            pass

    # 同步前做一次文件存在性校验，给出更明确的错误
    try:
        chunks_path = Path(artifacts["chunks_json"]).expanduser().resolve()
        if not chunks_path.exists():
            raise RuntimeError(f"chunks_json 不存在: {chunks_path}")
    except Exception as exc:
        msg = str(exc)
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
        sync_response = _post_generate_fta_import(job_id, request, artifacts, pdf_stem)
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
        # 将同步异常写入 job.message/error，便于前端“导入图谱”阶段直接显示原因
        sync_err = str(exc)
        try:
            logger.exception("sync to generate-fta failed job=%s err=%s", job_id, sync_err)
        except Exception:
            # ignore logging failures
            pass
        _set_job(
            job_id,
            {
                "status": "completed_with_sync_error",
                "stage": "completed_with_sync_error",
                "progress": 100,
                "sync_status": "failed",
                "sync_error": sync_err,
                "error": sync_err,
                "message": f"同步到故障树后端失败：{sync_err}",
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
            "import_maintenance_cases": "/api/knowledge/import-maintenance-cases",
            "get_job": "/api/kb/jobs/{job_id}",
            "import_relations_to_neo4j": "/api/kb/neo4j/import",
            "profile_work_orders": "/api/knowledge/profile",
            "import_work_orders": "/api/knowledge/import-work-orders",
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


@app.post("/api/knowledge/import-maintenance-cases")
def import_maintenance_cases_api(request: MaintenanceImportRequest):
    input_path = Path(request.input_path).expanduser().resolve()
    if not input_path.exists():
        raise HTTPException(status_code=400, detail=f"维修记录文件不存在: {input_path}")

    try:
        result = import_maintenance_cases(
            str(input_path),
            output_dir=request.output_dir,
            case_id_prefix=request.case_id_prefix,
            max_summary_chars=request.max_summary_chars,
            skip_entity=request.skip_entity,
            skip_relation=request.skip_relation,
            print_raw_text=request.print_raw_text,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("import maintenance cases failed path=%s err=%s", input_path, exc)
        raise HTTPException(status_code=500, detail=f"维修记录导入失败: {exc}")

    return {
        "status": "success",
        "source_type": "maintenance_record",
        "chunk_type": "case_summary",
        "skip_entity": request.skip_entity,
        "skip_relation": request.skip_relation,
        **result,
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
    display_file_name = _resolve_import_display_file_name(normalized_request, pdf_stem)
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
                pdf_stem=pdf_stem,
                file_name=display_file_name,
                file_version_id=_resolve_sync_file_version_id(normalized_request, artifacts),
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
    skip_clean: bool = Form(False),
    skip_entity: bool = Form(False),
    skip_relation: bool = Form(False),
    print_raw_text: bool = Form(False),
    sync_to_generate_fta: bool = Form(True),
    generate_fta_base_url: str = Form(DEFAULT_GENERATE_FTA_BASE_URL),
    # 默认不清空图谱：GNR 版本化知识库模式禁止 clear_graph=true
    clear_graph_before_import: bool = Form(False),
    source_type: str = Form("manual_document"),
    file_format: Optional[str] = Form(None),
    chunk_type: str = Form("document_section"),
    source_record_type: Optional[str] = Form(None),
    source_record_id: Optional[str] = Form(None),
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

    # 非 PDF 文件：跳过 MinerU，并在 output/{stem}/{stem}.md 写入可供后续分块的 Markdown
    ext = saved_path.suffix.lower()
    force_skip_mineru = ext in {".txt", ".csv", ".md"}
    if force_skip_mineru:
        try:
            result_dir = _next_version_dir(out_root, saved_path.stem)
            result_dir.mkdir(parents=True, exist_ok=True)
            md_path = result_dir / f"{saved_path.stem}.md"
            if ext == ".md":
                # 直接复用用户上传的 markdown
                md_path.write_bytes(content)
            else:
                # txt/csv：写入 markdown 代码块，便于 chunk_md.py 正常处理
                try:
                    text = (content or b"").decode("utf-8", errors="replace")
                except Exception:
                    text = str(content or b"")
                fence = "csv" if ext == ".csv" else "text"
                md_path.write_text(f"```{fence}\n{text}\n```\n", encoding="utf-8", errors="replace")
            logger.info("non-pdf upload prepared markdown ext=%s md=%s", ext, md_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"非 PDF 文件预处理失败（{ext}）: {exc}")

    req = PipelineJobRequest(
        pdf_path=str(saved_path),
        output_dir=str(out_root),
        chunk_size=int(chunk_size),
        skip_mineru=bool(skip_mineru) or force_skip_mineru,
        skip_clean=bool(skip_clean) or force_skip_mineru,
        skip_entity=bool(skip_entity),
        skip_relation=bool(skip_relation),
        print_raw_text=bool(print_raw_text),
        sync_to_generate_fta=bool(sync_to_generate_fta),
        generate_fta_base_url=str(generate_fta_base_url or DEFAULT_GENERATE_FTA_BASE_URL),
        clear_graph_before_import=bool(clear_graph_before_import),
        file_name=safe_name,
        source_type=str(source_type or "manual_document"),
        file_format=str(file_format).strip() if file_format is not None and str(file_format).strip() else None,
        chunk_type=str(chunk_type or "document_section"),
        source_record_type=str(source_record_type).strip() if source_record_type is not None and str(source_record_type).strip() else None,
        source_record_id=str(source_record_id).strip() if source_record_id is not None and str(source_record_id).strip() else None,
    )
    resp = run_pipeline_job(req)
    try:
        job_id = str(resp.get("job_id") or "")
        if job_id:
            _set_job(job_id, {"uploaded_file_path": str(saved_path), "uploaded_file_name": safe_name})
    except Exception:
        pass
    return resp


def _parse_field_mapping_json(field_mapping_json: Optional[str]) -> Dict[str, str]:
    raw = str(field_mapping_json or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"field_mapping_json 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="field_mapping_json 必须是 JSON 对象")
    normalized: Dict[str, str] = {}
    for key, value in data.items():
        k = str(key or "").strip()
        v = str(value or "").strip()
        if k and v:
            normalized[k] = v
    return normalized


@app.post("/api/knowledge/profile")
async def profile_work_orders(
    file: UploadFile = File(...),
    output_dir: str = Form("./output"),
):
    name = (file.filename or "upload.bin").strip()
    safe_name = "".join([c for c in name if c not in '\\/:*?"<>|']) or "upload.bin"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail="工单画像目前仅支持 csv / xlsx")

    out_root = Path(output_dir).expanduser().resolve()
    upload_dir = out_root / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{Path(safe_name).stem}_{uuid.uuid4().hex[:8]}{suffix}"

    try:
        content = await file.read()
        saved_path.write_bytes(content)
        profile = profile_work_order_file(saved_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"工单画像失败: {exc}") from exc

    return {
        "status": "success",
        "file_path": str(saved_path),
        **profile,
    }


@app.post("/api/knowledge/import-work-orders")
async def import_work_orders(
    file: UploadFile = File(...),
    output_dir: str = Form("./output"),
    file_id: Optional[str] = Form(None),
    field_mapping_json: Optional[str] = Form(None),
    sync_to_generate_fta: bool = Form(False),
    generate_fta_base_url: str = Form(DEFAULT_GENERATE_FTA_BASE_URL),
    clear_graph_before_import: bool = Form(False),
):
    name = (file.filename or "upload.bin").strip()
    safe_name = "".join([c for c in name if c not in '\\/:*?"<>|']) or "upload.bin"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail="工单导入目前仅支持 csv / xlsx")

    out_root = Path(output_dir).expanduser().resolve()
    upload_dir = out_root / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{Path(safe_name).stem}_{uuid.uuid4().hex[:8]}{suffix}"

    try:
        content = await file.read()
        saved_path.write_bytes(content)
        field_mapping = _parse_field_mapping_json(field_mapping_json)
        result = import_work_order_file(
            saved_path,
            output_dir=str(out_root),
            file_id=(str(file_id).strip() if file_id else None),
            field_mapping=field_mapping,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"工单导入失败: {exc}") from exc

    if sync_to_generate_fta:
        payload = {
            "chunks_file": result["artifacts"]["chunks_json"],
            "entities_file": result["artifacts"]["entities_merged_json"],
            "relations_file": result["artifacts"]["relations_jsonl"],
            "clear_graph": bool(clear_graph_before_import),
            "import_relations": True,
            "source": "knowledge_base_construction:work_order_import",
            "file_id": result["file"]["file_id"],
            "file_name": result["file"]["file_name"],
            "file_version_id": result["file"]["file_version_id"],
        }
        try:
            sync_response = _post_generate_fta_artifacts(
                base_url=generate_fta_base_url,
                payload=payload,
            )
            result["sync_response"] = sync_response
        except Exception as exc:
            result["sync_error"] = str(exc)
            result["status"] = "completed_with_sync_error"

    return result


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
