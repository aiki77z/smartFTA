from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"
DEFAULT_KB_DIR = BASE_DIR.parent / "FTA-KB"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_ascii_id(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "_", value.strip())
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text or "pr_docx"


def extract_doc_no(stem: str) -> str:
    match = re.search(r"([A-Za-z0-9.]+-\d+-PR-\d+_\d+)", stem)
    return match.group(1) if match else stem


def derive_ids(index_no: int, docx_path: Path) -> tuple[str, str]:
    doc_no = extract_doc_no(docx_path.stem)
    file_id = f"pr_{index_no:03d}_{safe_ascii_id(doc_no)}"
    return file_id, f"{file_id}_v1"


def find_pr_docx_files(root: Path, start: int, end: int) -> List[tuple[int, Path]]:
    if not root.exists():
        raise FileNotFoundError(f"PR_DOCX_ROOT does not exist: {root}")
    selected: List[tuple[int, Path]] = []
    for item in sorted(root.iterdir(), key=lambda path: path.name):
        if not item.is_dir():
            continue
        match = re.match(r"^(\d{3})\.", item.name)
        if not match:
            continue
        index_no = int(match.group(1))
        if index_no < start or index_no > end:
            continue
        docx_files = sorted(path for path in item.glob("*.docx") if not path.name.startswith("~$"))
        if not docx_files:
            print(f"[WARN] No docx found under {item}")
            continue
        selected.append((index_no, docx_files[0]))
    return selected


def import_kb_modules(kb_dir: Path) -> Dict[str, Any]:
    kb_dir = kb_dir.expanduser().resolve()
    if not kb_dir.exists():
        raise FileNotFoundError(f"KB directory does not exist: {kb_dir}")
    sys.path.insert(0, str(kb_dir))

    from env_loader import load_local_env

    load_local_env(kb_dir / ".env", override=True)

    import pr_docx_import as pr
    from knowledge_store import import_chunks

    return {"pr": pr, "import_chunks": import_chunks}


def build_pr_chunks(pr: Any, docx_path: Path, file_id: str, file_version_id: str) -> Dict[str, Any]:
    tables = pr.extract_docx_tables(docx_path)
    paragraphs = pr.extract_docx_paragraphs(docx_path)
    fields, episodes = pr._extract_fields_and_episodes(tables, paragraphs)
    record = pr._build_record(docx_path, fields, file_id, file_version_id, docx_path.name, 1)
    chunks = pr._build_chunks(record, episodes)
    if not chunks:
        raise RuntimeError(f"No chunks generated for {docx_path}")
    return {
        "record": record,
        "chunks": chunks,
        "parse_report": {
            "source_path": str(docx_path),
            "tables": len(tables),
            "paragraphs": len(paragraphs),
            "episodes": len(episodes),
            "chunks": len(chunks),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def process_one(
    *,
    pr: Any,
    import_chunks_func: Any,
    docx_path: Path,
    index_no: int,
    output_dir: Path,
    mode: str,
) -> Dict[str, Any]:
    file_id, file_version_id = derive_ids(index_no, docx_path)
    built = build_pr_chunks(pr, docx_path, file_id, file_version_id)
    version_dir = output_dir / file_id / file_version_id
    version_dir.mkdir(parents=True, exist_ok=True)

    chunks_path = version_dir / f"{file_id}_chunks.json"
    work_order_path = version_dir / f"{file_id}_work_orders.json"
    report_path = version_dir / f"{file_id}_parse_report.json"
    write_json(chunks_path, built["chunks"])
    write_json(work_order_path, [built["record"]])
    write_json(report_path, built["parse_report"])

    import_result = import_chunks_func(
        built["chunks"],
        mode=mode,
        file_id=file_id,
        file_version_id=file_version_id,
        is_active=True,
    )
    return {
        "index_no": index_no,
        "file_id": file_id,
        "file_version_id": file_version_id,
        "docx_path": str(docx_path),
        "chunks_path": str(chunks_path),
        "chunks": len(built["chunks"]),
        "import_result": import_result,
    }


def run_annotation(version_ids: Iterable[str], output_csv: Path, raw_jsonl: Path) -> None:
    version_id_arg = ";".join(version_ids)
    cmd = [
        sys.executable,
        str(BASE_DIR / "generate_ai_annotation_dataset.py"),
        "--file-version-ids",
        version_id_arg,
        "--output-csv",
        str(output_csv),
        "--raw-jsonl",
        str(raw_jsonl),
        "--debug-context",
    ]
    subprocess.run(cmd, cwd=str(BASE_DIR), check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process numbered PR docx work orders into MongoDB chunks, optionally run AI annotation.")
    parser.add_argument("--start", type=int, required=True, help="起始工单编号，例如 1 或 301。")
    parser.add_argument("--end", type=int, required=True, help="结束工单编号，例如 100 或 302。")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="默认读取 annotation-data-pipeline/.env。")
    parser.add_argument("--input-root", default=None, help="PR_docx 根目录；不传则读取 .env 的 PR_DOCX_ROOT。")
    parser.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR), help="FTA-KB 目录。")
    parser.add_argument("--output-dir", default=str(BASE_DIR / "work_order_outputs"), help="工单 chunks 产物输出目录。")
    parser.add_argument("--mode", choices=["replace", "append"], default="replace", help="导入 MongoDB chunks 的模式。replace 是按 file_version_id 局部替换。")
    parser.add_argument("--annotate", action="store_true", help="导入 chunks 后继续调用大模型，合并生成一个待审核 CSV。")
    parser.add_argument("--output-csv", default=None, help="--annotate 时的输出 CSV。")
    parser.add_argument("--raw-jsonl", default=None, help="--annotate 时的 raw JSONL。")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    input_root = Path(args.input_root or os.getenv("PR_DOCX_ROOT", "")).expanduser()
    if not str(input_root):
        raise RuntimeError("缺少 PR_DOCX_ROOT。请在 .env 中配置，或传入 --input-root。")

    modules = import_kb_modules(Path(args.kb_dir))
    pr = modules["pr"]
    import_chunks_func = modules["import_chunks"]

    pairs = find_pr_docx_files(input_root.resolve(), args.start, args.end)
    if not pairs:
        raise RuntimeError(f"没有找到编号 {args.start}-{args.end} 范围内的 docx 文件。")

    output_dir = Path(args.output_dir).expanduser().resolve()
    results = []
    for index_no, docx_path in pairs:
        result = process_one(
            pr=pr,
            import_chunks_func=import_chunks_func,
            docx_path=docx_path.resolve(),
            index_no=index_no,
            output_dir=output_dir,
            mode=args.mode,
        )
        results.append(result)
        print(
            f"[{index_no:03d}] file_version_id={result['file_version_id']} "
            f"chunks={result['chunks']} imported={result['import_result'].get('inserted')}"
        )

    manifest_path = output_dir / f"pr_{args.start:03d}_{args.end:03d}_manifest.json"
    write_json(manifest_path, results)
    print(f"Manifest saved: {manifest_path}")

    if args.annotate:
        output_csv = Path(args.output_csv or BASE_DIR / f"ai_annotation_draft_pr_{args.start:03d}_{args.end:03d}.csv")
        raw_jsonl = Path(args.raw_jsonl or BASE_DIR / f"raw_ai_annotations_pr_{args.start:03d}_{args.end:03d}.jsonl")
        run_annotation([item["file_version_id"] for item in results], output_csv, raw_jsonl)
        print(f"Annotation CSV saved: {output_csv}")
        print(f"Raw JSONL saved: {raw_jsonl}")


if __name__ == "__main__":
    main()
