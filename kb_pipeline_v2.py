from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from env_loader import load_local_env
from import_chunks import _load_chunks
from knowledge_store import activate_file_version, create_file_version_record, import_chunks as import_chunks_to_db
from llm_annotation_extractor import generate_annotation_dataset, load_chunks_from_json


ROOT_DIR = Path(__file__).resolve().parent


def run_command(cmd: list[str], description: str) -> subprocess.CompletedProcess[str]:
    print(f"\n>>> {description}", flush=True)
    print("COMMAND:", " ".join(f'"{item}"' if " " in str(item) else str(item) for item in cmd), flush=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    start = time.time()
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
    out_lines: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            text = (line or "").rstrip("\n")
            out_lines.append(text)
            print(text, flush=True)
    return_code = proc.wait()
    elapsed = time.time() - start
    print(f"ELAPSED: {elapsed:.2f}s", flush=True)
    if return_code != 0:
        raise RuntimeError(f"{description} failed with code {return_code}")
    return subprocess.CompletedProcess(cmd, return_code, "\n".join(out_lines), "")


def print_stage(stage: str, message: str) -> None:
    print(f"KB_STAGE={stage}|{message}", flush=True)

def infer_file_id(input_path: Path, explicit: str = "") -> str:
    text = (explicit or "").strip()
    if text:
        return text
    stem = input_path.stem.strip()
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem, flags=re.UNICODE).strip("_")
    return safe or f"file_{int(time.time())}"


def infer_file_format(input_path: Path, explicit: str = "") -> str:
    text = (explicit or "").strip().lower()
    if text:
        return text
    suffix = input_path.suffix.lower().lstrip(".")
    if suffix == "markdown":
        return "md"
    return suffix or "txt"


def infer_version_no(file_version_id: str, file_id: str) -> int:
    match = re.fullmatch(re.escape(file_id) + r"_v(\d+)", file_version_id)
    if not match:
        raise ValueError(f"file_version_id must look like <file_id>_vN, got: {file_version_id}")
    return int(match.group(1))


def ensure_text_markdown_input(input_path: Path, output_root: Path, file_id: str, file_version_id: str) -> Path:
    version_dir = output_root / file_id / file_version_id
    version_dir.mkdir(parents=True, exist_ok=True)
    md_path = version_dir / f"{file_id}.md"
    suffix = input_path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        shutil.copyfile(input_path, md_path)
    elif suffix in {".txt", ".csv"}:
        text = input_path.read_text(encoding="utf-8", errors="replace")
        md_path.write_text(text, encoding="utf-8")
    else:
        raise ValueError(f"Only pdf/md/txt/csv are supported by kb_pipeline_v2 for now: {input_path}")
    return md_path


def run_document_to_chunks(
    *,
    input_path: Path,
    output_root: Path,
    file_id: str,
    file_version_id: str,
    chunk_size: int,
    source_type: str,
    file_format: str,
    chunk_type: str,
    skip_clean: bool,
    skip_mineru: bool,
) -> dict[str, Any]:
    suffix = input_path.suffix.lower()
    effective_skip_mineru = skip_mineru or suffix in {".md", ".markdown", ".txt", ".csv"}
    if effective_skip_mineru:
        ensure_text_markdown_input(input_path, output_root, file_id, file_version_id)
    cmd = [
        sys.executable,
        str(ROOT_DIR / "run.py"),
        "--pdf",
        str(input_path),
        "--pdf-stem",
        file_id,
        "--file-version-id",
        file_version_id,
        "--output-dir",
        str(output_root),
        "--chunk-size",
        str(chunk_size),
        "--source-type",
        source_type,
        "--file-format",
        file_format,
        "--chunk-type",
        chunk_type,
        "--skip-entity",
    ]
    if effective_skip_mineru:
        cmd.append("--skip-mineru")
    if skip_clean:
        cmd.append("--skip-clean")
    else:
        # Clean only works on markdown text and is safe for all supported inputs.
        pass
    run_command(cmd, "文档 OCR/清洗/chunk")
    result_dir = output_root / file_id / file_version_id
    chunks_json = result_dir / f"{file_id}_chunks.json"
    if not chunks_json.exists():
        raise FileNotFoundError(f"chunks artifact not found: {chunks_json}")
    return {"result_dir": result_dir, "chunks_json": chunks_json}


def import_chunks_to_mongodb(
    *,
    chunks_json: Path,
    file_id: str,
    file_version_id: str,
    file_name: str,
    source: str,
) -> dict[str, Any]:
    chunks = _load_chunks(str(chunks_json))
    file_version = create_file_version_record(
        file_id=file_id,
        file_name=file_name,
        file_version_id=file_version_id,
        source=source,
        metadata={"artifacts": {"chunks_file": str(chunks_json)}},
    )
    result = import_chunks_to_db(
        chunks,
        mode="replace",
        file_id=file_version["file_id"],
        file_version_id=file_version["file_version_id"],
        is_active=True,
    )
    return {"file_version": file_version, "chunks": result}


def run_file_internal_clustering(
    *,
    annotations_csv: Path,
    file_id: str,
    output_dir: Path,
    env_file: Path,
    embedding_backend: str,
    reuse_embeddings_jsonl: str,
    refinement_merge_mode: str,
) -> Path:
    cmd = [
        sys.executable,
        str(ROOT_DIR / "entity_clustering" / "cluster_entities.py"),
        "--env-file",
        str(env_file),
        "--input-csv",
        str(annotations_csv),
        "--file-id",
        file_id,
        "--output-dir",
        str(output_dir),
        "--embedding-backend",
        embedding_backend,
        "--refinement-merge-mode",
        refinement_merge_mode,
        "--candidate-top-k",
        "20",
        "--candidate-min-name",
        "0.45",
        "--candidate-min-embedding",
        "0.88",
        "--candidate-min-neighbor",
        "0.35",
    ]
    if reuse_embeddings_jsonl:
        cmd.extend(["--reuse-embeddings-jsonl", reuse_embeddings_jsonl])
    run_command(cmd, "文件内实体消歧聚类")
    intermediate = output_dir / "cluster_intermediate.json"
    if not intermediate.exists():
        raise FileNotFoundError(f"cluster intermediate not found: {intermediate}")
    return intermediate


def run_cross_file_import(
    *,
    intermediate_json: Path,
    file_id: str,
    file_version_id: str,
    file_name: str,
    env_file: Path,
    diagnostics_jsonl: Path,
    dry_run: bool,
) -> None:
    cmd = [
        sys.executable,
        str(ROOT_DIR / "cross_file_entity_clustering.py"),
        "--env-file",
        str(env_file),
        "--intermediate-json",
        str(intermediate_json),
        "--file-id",
        file_id,
        "--file-version-id",
        file_version_id,
        "--file-name",
        file_name,
        "--diagnostics-jsonl",
        str(diagnostics_jsonl),
    ]
    if dry_run:
        cmd.append("--dry-run")
    run_command(cmd, "跨文件实体聚类并导入 Neo4j/MongoDB")


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartFTA KB v2 full pipeline: document -> chunks -> LLM extraction -> clustering -> graph.")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--output-dir", default=str(ROOT_DIR / "output"))
    parser.add_argument("--file-id", default="")
    parser.add_argument("--file-version-id", default="")
    parser.add_argument("--file-name", default="")
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--source-type", default="manual_document")
    parser.add_argument("--file-format", default="")
    parser.add_argument("--chunk-type", default="document_section")
    parser.add_argument("--skip-mineru", action="store_true")
    parser.add_argument("--skip-clean", action="store_true")
    parser.add_argument("--skip-llm", action="store_true", help="Only create empty annotation artifacts for smoke test.")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--llm-base-url", default="")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=2200)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--embedding-backend", choices=["none", "hash", "sentence-transformers", "openai-compatible"], default="none")
    parser.add_argument("--reuse-embeddings-jsonl", default="")
    parser.add_argument("--refinement-merge-mode", choices=["single", "batch", "strong-batch"], default="single")
    parser.add_argument("--skip-cross-file-import", action="store_true", help="Stop after file-internal clustering.")
    parser.add_argument("--cross-file-dry-run", action="store_true")
    args = parser.parse_args()

    env_file = Path(args.env_file).resolve()
    load_local_env(env_file, override=True)
    input_path = Path(args.input_file).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"input file not found: {input_path}")
    file_id = infer_file_id(input_path, args.file_id)
    file_version_id = clean_version_id = args.file_version_id.strip() if args.file_version_id else f"{file_id}_v1"
    infer_version_no(file_version_id, file_id)
    file_name = args.file_name.strip() or input_path.name
    file_format = infer_file_format(input_path, args.file_format)
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    print_stage("parse", "Parsing document and creating chunks")
    artifacts = run_document_to_chunks(
        input_path=input_path,
        output_root=output_root,
        file_id=file_id,
        file_version_id=clean_version_id,
        chunk_size=args.chunk_size,
        source_type=args.source_type,
        file_format=file_format,
        chunk_type=args.chunk_type,
        skip_clean=args.skip_clean,
        skip_mineru=args.skip_mineru,
    )
    print_stage("chunk", "Importing chunks to MongoDB")
    mongo_result = import_chunks_to_mongodb(
        chunks_json=artifacts["chunks_json"],
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        source="kb_pipeline_v2",
    )
    print(json.dumps({"mongodb_import": mongo_result}, ensure_ascii=False, indent=2, default=str))

    result_dir: Path = artifacts["result_dir"]
    annotations_csv = result_dir / f"{file_id}.csv"
    raw_jsonl = result_dir / f"{file_id}_raw_llm.jsonl"
    chunks = load_chunks_from_json(artifacts["chunks_json"])
    print_stage("entity", "Calling LLM to extract entities and relations")
    llm_result = generate_annotation_dataset(
        chunks=chunks,
        output_csv=annotations_csv,
        raw_jsonl=raw_jsonl,
        model=args.llm_model or os.getenv("LLM_MODEL", "deepseek-chat"),
        api_key=args.llm_api_key or os.getenv("LLM_API_KEY", ""),
        base_url=args.llm_base_url or os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        workers=args.workers,
        dry_run=args.skip_llm,
        debug_context=True,
        sleep_seconds=args.sleep,
        continue_on_error=True,
    )
    print(json.dumps({"llm_extraction": llm_result}, ensure_ascii=False, indent=2))

    print_stage("relation", "Clustering entities and aggregating relations")
    cluster_output_dir = result_dir / "entity_clustering"
    intermediate_json = run_file_internal_clustering(
        annotations_csv=annotations_csv,
        file_id=file_id,
        output_dir=cluster_output_dir,
        env_file=env_file,
        embedding_backend=args.embedding_backend,
        reuse_embeddings_jsonl=args.reuse_embeddings_jsonl,
        refinement_merge_mode=args.refinement_merge_mode,
    )

    diagnostics_jsonl = result_dir / "cross_file_diagnostics.jsonl"
    if not args.skip_cross_file_import:
        print_stage("syncing", "Cross-file clustering and importing graph")
        run_cross_file_import(
            intermediate_json=intermediate_json,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            env_file=env_file,
            diagnostics_jsonl=diagnostics_jsonl,
            dry_run=args.cross_file_dry_run,
        )
    if not args.cross_file_dry_run and not args.skip_cross_file_import:
        try:
            activate_file_version(file_id, file_version_id)
        except Exception as exc:
            print(f"WARNING: activate_file_version failed: {exc}")

    print_stage("success", "KB V2 pipeline finished")
    summary = {
        "status": "success",
        "file_id": file_id,
        "file_version_id": file_version_id,
        "file_name": file_name,
        "artifacts": {
            "result_dir": str(result_dir),
            "chunks_json": str(artifacts["chunks_json"]),
            "annotations_csv": str(annotations_csv),
            "raw_llm_jsonl": str(raw_jsonl),
            "cluster_intermediate_json": str(intermediate_json),
            "cross_file_diagnostics_jsonl": str(diagnostics_jsonl) if not args.skip_cross_file_import else "",
        },
    }
    (result_dir / f"{file_id}_kb_pipeline_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
