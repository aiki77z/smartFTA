from __future__ import annotations

import json
import csv
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET


SUPPORTED_FORMATS = {"md", "txt", "docx", "pdf", "csv"}

SOURCE_TYPE = "maintenance_record"
CHUNK_TYPE = "case_summary"
SOURCE_RECORD_TYPE = "maintenance_case"


def _run_command(cmd: List[str], description: str) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{description}失败: "
            + (result.stderr.strip() or result.stdout.strip() or f"returncode={result.returncode}")
        )


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    encodings = ("utf-8-sig", "utf-8", "gbk")
    last_error: Optional[Exception] = None
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                rows: List[Dict[str, str]] = []
                for row in reader:
                    cleaned: Dict[str, str] = {}
                    for key, value in (row or {}).items():
                        normalized_key = str(key or "").strip()
                        if not normalized_key:
                            continue
                        cleaned[normalized_key] = str(value or "").strip()
                    if any(cleaned.values()):
                        rows.append(cleaned)
                return rows
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"CSV 文件读取失败: {path} ({last_error})")


def _extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: List[str] = []
    for para in root.findall(".//w:p", ns):
        parts = []
        for node in para.findall(".//w:t", ns):
            if node.text:
                parts.append(node.text)
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _normalize_text(text: str) -> str:
    text = text.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _next_version_dir(output_root: Path, file_id: str) -> Path:
    base_dir = output_root / file_id
    base_dir.mkdir(parents=True, exist_ok=True)
    version_numbers: List[int] = []
    pattern = re.compile(rf"^{re.escape(file_id)}_v(\d+)$")
    for item in base_dir.iterdir():
        if not item.is_dir():
            continue
        match = pattern.match(item.name)
        if match:
            version_numbers.append(int(match.group(1)))
    next_version = (max(version_numbers) if version_numbers else 0) + 1
    version_dir = base_dir / f"{file_id}_v{next_version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    return version_dir


def _find_markdown_file(version_dir: Path, file_id: str) -> Path:
    candidates = [
        version_dir / f"{file_id}.md",
        version_dir / "ocr" / f"{file_id}.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    all_md = sorted(version_dir.rglob("*.md"))
    if all_md:
        return all_md[0]
    raise FileNotFoundError(f"未在 {version_dir} 中找到 Markdown 文件")


def _convert_pdf_to_markdown(
    input_path: Path,
    output_root: Path,
    *,
    file_id: Optional[str] = None,
    version_no: Optional[int] = None,
) -> tuple[Path, Path]:
    script_path = Path(__file__).with_name("trans_file_to_md.py")
    cmd = [
        sys.executable,
        str(script_path),
        "-i",
        str(input_path),
        "-o",
        str(output_root),
        "-b",
        "pipeline",
        "-m",
        "ocr",
    ]
    if file_id:
        cmd.extend(["--file-id", file_id])
    if version_no is not None:
        cmd.extend(["--version-no", str(version_no)])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "PDF 转 Markdown 失败: "
            + (result.stderr.strip() or result.stdout.strip() or f"returncode={result.returncode}")
        )

    version_dir: Optional[Path] = None
    for line in (result.stdout or "").splitlines():
        if line.startswith("VERSION_DIR="):
            version_dir = Path(line.split("=", 1)[1].strip())
            break
    if version_dir is None or not version_dir.exists():
        raise RuntimeError("未能从 PDF 转换结果中解析 VERSION_DIR")

    markdown_path = _find_markdown_file(version_dir, input_path.stem)
    return version_dir, markdown_path


def _detect_file_format(input_path: Path) -> str:
    suffix = input_path.suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(f"暂不支持的维修记录格式: .{suffix}")
    return suffix


def _split_markdown_sections(text: str) -> List[Dict[str, str]]:
    pattern = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
    matches = list(pattern.finditer(text))
    if not matches:
        return []

    sections: List[Dict[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        sections.append({"title": match.group(2).strip(), "content": body})
    return sections


def _split_by_case_markers(text: str) -> List[Dict[str, str]]:
    pattern = re.compile(
        r"(?m)^(?:案例(?:编号)?|维修(?:记录|案例|编号)|检修记录|故障案例)[^\n]{0,80}$"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return []

    cases: List[Dict[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if not content:
            continue
        title = match.group(0).strip()
        cases.append({"title": title, "content": content})
    return cases


def _split_cases(text: str, file_format: str) -> List[Dict[str, str]]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    if file_format == "md":
        markdown_sections = _split_markdown_sections(normalized)
        if markdown_sections:
            return markdown_sections

    marker_sections = _split_by_case_markers(normalized)
    if marker_sections:
        return marker_sections

    return [{"title": "维修记录", "content": normalized}]


def _pick_first_value(row: Dict[str, str], candidates: List[str]) -> str:
    for candidate in candidates:
        for key, value in row.items():
            if candidate in key and str(value or "").strip():
                return str(value).strip()
    return ""


def _build_case_from_row(row: Dict[str, str], row_index: int) -> Dict[str, Any]:
    record_id = _pick_first_value(row, ["维修记录编号", "维修编号", "案例编号", "记录编号", "ID"])
    subsystem = _pick_first_value(row, ["故障子系统", "子系统", "系统"])
    component = _pick_first_value(row, ["关联部件", "部件", "零件", "组件"])
    code = _pick_first_value(row, ["故障代码", "报警码", "代码"])
    phenomenon = _pick_first_value(row, ["现象描述", "故障现象", "异常现象", "现象"])
    threshold = _pick_first_value(row, ["监测参数与故障阈值", "阈值", "监测参数", "检测规则"])
    root_cause = _pick_first_value(row, ["潜在根本原因", "根本原因", "故障原因", "原因"])
    action = _pick_first_value(row, ["处理措施", "维修措施", "处置措施"])
    verification = _pick_first_value(row, ["验证结果", "处理结果", "结果"])
    investigation = _pick_first_value(row, ["排查过程", "检查过程", "诊断过程"])

    title = record_id or phenomenon or f"维修案例 {row_index}"
    lines = []
    if record_id:
        lines.append(f"维修记录编号：{record_id}")
    if subsystem:
        lines.append(f"故障子系统：{subsystem}")
    if component:
        lines.append(f"关联部件：{component}")
    if code:
        lines.append(f"故障代码：{code}")
    if phenomenon:
        lines.append(f"故障现象：{phenomenon}")
    if threshold:
        lines.append(f"监测参数与故障阈值：{threshold}")
    if investigation:
        lines.append(f"排查过程：{investigation}")
    if root_cause:
        lines.append(f"根因：{root_cause}")
    if action:
        lines.append(f"处理措施：{action}")
    if verification:
        lines.append(f"验证结果：{verification}")

    content = "\n".join(lines).strip()
    if not content:
        content = "\n".join(f"{k}：{v}" for k, v in row.items() if str(v or "").strip())

    return {
        "title": title,
        "content": content,
        "record_id": record_id,
    }


def _extract_labeled_value(text: str, labels: List[str]) -> str:
    for label in labels:
        pattern = re.compile(rf"(?mi)^\s*{label}\s*[：:]\s*(.+?)\s*$")
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return ""


def _extract_case_fields(text: str) -> Dict[str, str]:
    normalized = _normalize_text(text)
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    first_sentence = ""
    sentence_match = re.search(r"(.+?[。！？])", normalized)
    if sentence_match:
        first_sentence = sentence_match.group(1).strip()
    elif lines:
        first_sentence = lines[0][:120].strip()

    root_cause = _extract_labeled_value(normalized, ["根因", "故障原因", "原因"])
    action = _extract_labeled_value(normalized, ["处理措施", "维修措施", "处置措施", "处理结果"])
    verification = _extract_labeled_value(normalized, ["验证结果", "验证", "结果"])
    investigation = _extract_labeled_value(normalized, ["排查过程", "排查步骤", "诊断过程", "检查过程"])
    phenomenon = _extract_labeled_value(normalized, ["故障现象", "异常现象", "现象"])

    if not root_cause:
        match = re.search(r"(?:原因为|原因是|最终确认|判断为|发现)([^。；\n]{4,80})", normalized)
        if match:
            root_cause = match.group(1).strip("：: ，,。")
    if not action:
        match = re.search(r"(?:处理措施为|通过|采取|更换)([^。；\n]{4,120})", normalized)
        if match:
            action = match.group(0).strip("：: ，,。")
    if not verification:
        match = re.search(r"(恢复正常|测试[^。；\n]{0,40}正常|连续[^。；\n]{0,40}正常)", normalized)
        if match:
            verification = match.group(1).strip()
    if not phenomenon:
        phenomenon = first_sentence

    return {
        "phenomenon": phenomenon,
        "investigation_process": investigation,
        "root_cause": root_cause,
        "repair_action": action,
        "verification_result": verification,
        "summary_fallback": first_sentence or normalized[:120],
    }


def _build_case_summary(case_id: str, fields: Dict[str, str], raw_text: str, max_chars: int) -> str:
    parts = [f"维修案例 {case_id}："]
    if fields.get("phenomenon"):
        parts.append(fields["phenomenon"])
    if fields.get("investigation_process"):
        parts.append(f"排查过程：{fields['investigation_process']}。")
    if fields.get("root_cause"):
        parts.append(f"根因：{fields['root_cause']}。")
    if fields.get("repair_action"):
        parts.append(f"处理措施：{fields['repair_action']}。")
    if fields.get("verification_result"):
        parts.append(f"验证结果：{fields['verification_result']}。")

    summary = " ".join(part.strip() for part in parts if part and part.strip())
    if summary.endswith("："):
        summary += fields.get("summary_fallback") or raw_text[:80]
    summary = _normalize_text(summary)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary


def import_maintenance_cases(
    input_path: str,
    *,
    output_dir: str = "./output",
    case_id_prefix: str = "case",
    max_summary_chars: int = 800,
    skip_entity: bool = False,
    skip_relation: bool = False,
    print_raw_text: bool = False,
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    source_path = Path(input_path).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"文件不存在: {source_path}")

    file_format = _detect_file_format(source_path)
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    resolved_file_id = str(file_id or source_path.stem).strip()
    display_file_name = str(file_name or source_path.name).strip()
    version_no = None
    if file_version_id:
        match = re.fullmatch(rf"{re.escape(resolved_file_id)}_v(\d+)", file_version_id)
        if not match:
            raise ValueError("file_version_id does not belong to file_id")
        version_no = int(match.group(1))

    if file_format == "pdf":
        version_dir, text_source_path = _convert_pdf_to_markdown(
            source_path,
            output_root,
            file_id=resolved_file_id,
            version_no=version_no,
        )
        extracted_text = _read_text_file(text_source_path)
    else:
        version_dir = output_root / resolved_file_id / file_version_id if file_version_id else _next_version_dir(output_root, resolved_file_id)
        if file_version_id:
            version_dir.mkdir(parents=True, exist_ok=False)
        if file_format in {"md", "txt"}:
            extracted_text = _read_text_file(source_path)
        elif file_format == "docx":
            extracted_text = _extract_docx_text(source_path)
        elif file_format == "csv":
            extracted_text = ""
        else:
            raise ValueError(f"暂不支持的维修记录格式: {file_format}")
        text_source_path = version_dir / f"{resolved_file_id}_source.txt"
        if file_format != "csv":
            text_source_path.write_text(extracted_text, encoding="utf-8")

    file_id = resolved_file_id
    file_version_id = version_dir.name
    sections: List[Dict[str, Any]]
    if file_format == "csv":
        csv_rows = _read_csv_rows(source_path)
        if not csv_rows:
            raise ValueError("CSV 维修记录内容为空，无法导入")
        sections = []
        rendered_rows: List[str] = []
        for idx, row in enumerate(csv_rows, start=1):
            rendered = _build_case_from_row(row, idx)
            sections.append(rendered)
            rendered_rows.append(rendered["content"])
        text_source_path.write_text("\n\n".join(rendered_rows), encoding="utf-8")
    else:
        normalized_text = _normalize_text(extracted_text)
        if not normalized_text:
            raise ValueError("维修记录内容为空，无法导入")
        sections = _split_cases(normalized_text, file_format)
        if not sections:
            raise ValueError("未识别出任何维修案例")

    cases: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    for idx, section in enumerate(sections, start=1):
        row_record_id = str(section.get("record_id") or "").strip()
        case_id = row_record_id or f"{case_id_prefix}-{idx:03d}"
        raw_text = _normalize_text(section.get("content", ""))
        if not raw_text:
            continue

        fields = _extract_case_fields(raw_text)
        title = section.get("title") or f"维修案例 {case_id}"
        summary = _build_case_summary(case_id, fields, raw_text, max_summary_chars)

        case_record = {
            "case_id": case_id,
            "title": title,
            "content": raw_text,
            "phenomenon": fields.get("phenomenon") or "",
            "investigation_process": fields.get("investigation_process") or "",
            "root_cause": fields.get("root_cause") or "",
            "repair_action": fields.get("repair_action") or "",
            "verification_result": fields.get("verification_result") or "",
            "file": display_file_name,
            "file_id": file_id,
            "file_version_id": file_version_id,
            "is_active": True,
            "source_type": SOURCE_TYPE,
            "file_format": file_format,
            "source_record_type": SOURCE_RECORD_TYPE,
            "source_record_id": case_id,
        }
        cases.append(case_record)

        chunk = {
            "id": case_id,
            "chunk_id": case_id,
            "chunk_uid": f"{file_version_id}::{case_id}",
            "chunk_name": title,
            "content": summary,
            "chapter": "",
            "section": "",
            "subsection": "",
            "section_path": "",
            "source": idx,
            "file": display_file_name,
            "file_id": file_id,
            "file_version_id": file_version_id,
            "is_active": True,
            "source_type": SOURCE_TYPE,
            "file_format": file_format,
            "chunk_type": CHUNK_TYPE,
            "source_record_type": SOURCE_RECORD_TYPE,
            "source_record_id": case_id,
        }
        chunks.append(chunk)

    if not cases:
        raise ValueError("未生成有效维修案例")

    cases_path = version_dir / f"{file_id}_maintenance_cases.json"
    chunks_path = version_dir / f"{file_id}_chunks.json"
    _write_json(cases_path, cases)
    _write_json(chunks_path, chunks)

    artifacts: Dict[str, str] = {
        "version_dir": str(version_dir),
        "source_text": str(text_source_path),
        "maintenance_cases_json": str(cases_path),
        "chunks_json": str(chunks_path),
    }

    script_dir = Path(__file__).resolve().parent
    entities_jsonl = version_dir / f"{file_id}_entities.jsonl"
    entities_merged_json = version_dir / f"{file_id}_entities_merged.json"
    relations_jsonl = version_dir / f"{file_id}_relations.jsonl"
    relations_csv = version_dir / f"{file_id}_relations.csv"

    if not skip_entity:
        entity_script = script_dir / "extract_entities.py"
        cmd_entity = [
            sys.executable,
            str(entity_script),
            "--input",
            str(chunks_path),
            "--output-entities",
            str(entities_jsonl),
            "--output-merged",
            str(entities_merged_json),
        ]
        if print_raw_text:
            cmd_entity.append("--print-raw-text")
        _run_command(cmd_entity, "维修记录实体提取")
        artifacts["entities_jsonl"] = str(entities_jsonl)
        artifacts["entities_merged_json"] = str(entities_merged_json)

        if not skip_relation:
            relation_script = script_dir / "extract_relations.py"
            cmd_relation = [
                sys.executable,
                str(relation_script),
                "--input-chunks",
                str(chunks_path),
                "--input-entities",
                str(entities_merged_json),
                "--output-relations",
                str(relations_jsonl),
                "--output-csv",
                str(relations_csv),
            ]
            if print_raw_text:
                cmd_relation.append("--print-raw-text")
            _run_command(cmd_relation, "维修记录关系提取")
            artifacts["relations_jsonl"] = str(relations_jsonl)
            artifacts["relations_csv"] = str(relations_csv)

    return {
        "file": {
            "file_id": file_id,
            "file_name": display_file_name,
            "file_version_id": file_version_id,
            "file_format": file_format,
        },
        "counts": {
            "maintenance_cases": len(cases),
            "chunks": len(chunks),
        },
        "artifacts": artifacts,
    }
