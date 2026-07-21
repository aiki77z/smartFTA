from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from work_order_import import sanitize_file_id


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
SOURCE_TYPE = "work_order"
FILE_FORMAT = "docx"
CHUNK_TYPE = "table_row_summary"
SOURCE_RECORD_TYPE = "pr_work_order"

FIELD_LABELS = {
    "work_order_no": ("编号", "资料编号", "document no", "doc. no", "document number"),
    "title": ("主题", "标题", "title"),
    "maintenance_type": ("检修类型", "intervention"),
    "problem_type": ("问题类型", "problem"),
    "component_code": ("部套图号", "assembly"),
    "component_name": ("部套名称", "component"),
    "fault_phenomenon": ("问题描述", "主要发现描述", "preliminary inspection", "the preliminary inspection show"),
    "handling_action": ("答复", "处理建议", "处理措施", "reply"),
    "feedback": ("实施反馈", "反馈"),
    "attachments": ("附件",),
}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_command(cmd: List[str], description: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(Path(__file__).resolve().parent))
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"returncode={result.returncode}"
        raise RuntimeError(f"{description}失败: {message}")


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _load_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _norm_key(text: Any) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"\s+", "", value)
    value = value.replace("：", ":")
    return re.sub(r"[()（）/\\_\-\.]+", "", value)


def _clean_text(text: Any) -> str:
    text = str(text or "").replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _append_field(fields: Dict[str, str], key: str, value: str) -> None:
    value = _clean_text(value)
    if not value:
        return
    if fields.get(key):
        if value not in fields[key]:
            fields[key] = f"{fields[key]}\n{value}"
    else:
        fields[key] = value


def _cell_text(cell: ET.Element) -> str:
    paragraphs: List[str] = []
    for para in cell.findall(".//w:p", NS):
        parts: List[str] = []
        for node in para.iter():
            if node.tag == f"{{{NS['w']}}}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{{{NS['w']}}}tab":
                parts.append("\t")
            elif node.tag == f"{{{NS['w']}}}br":
                parts.append("\n")
        text = _clean_text("".join(parts))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def extract_docx_tables(path: Path) -> List[List[List[str]]]:
    with zipfile.ZipFile(path, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))

    tables: List[List[List[str]]] = []
    for table in root.findall(".//w:tbl", NS):
        rows: List[List[str]] = []
        for row in table.findall("./w:tr", NS):
            cells = [_cell_text(cell) for cell in row.findall("./w:tc", NS)]
            if any(cell.strip() for cell in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def extract_docx_paragraphs(path: Path) -> List[str]:
    with zipfile.ZipFile(path, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))

    paragraphs: List[str] = []
    for para in root.findall(".//w:p", NS):
        parts = [node.text or "" for node in para.findall(".//w:t", NS)]
        text = _clean_text("".join(parts))
        if text:
            paragraphs.append(text)
    return paragraphs


def _field_for_label(label: str) -> Optional[str]:
    normalized = _norm_key(label)
    for field, aliases in FIELD_LABELS.items():
        for alias in aliases:
            if _norm_key(alias) and _norm_key(alias) in normalized:
                return field
    return None


def _split_inline_kv(text: str) -> Optional[Tuple[str, str]]:
    text = _clean_text(text)
    if not text:
        return None
    match = re.match(r"^(.{1,40}?)[：:]\s*(.+)$", text, flags=re.S)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    for label in ("问题描述", "答复", "实施反馈", "附件（如有）", "附件"):
        if text.startswith(label):
            value = re.sub(rf"^{re.escape(label)}[：:]?", "", text, count=1).strip()
            return label, value
    return None


def _selected_checkbox_value(text: str) -> str:
    hits = re.findall(r"☒\s*([^☐☒\s]+)", text or "")
    return "、".join(hit.strip() for hit in hits if hit.strip())


def _parse_name_metadata(path: Path) -> Dict[str, str]:
    blob = " ".join(part for part in [path.parent.name, path.stem] if part)
    pr_match = re.search(r"(SGC(?:[.\-][A-Z0-9]+)*[.\-]PR[.\-]?\d+(?:_\w+)?)", blob, flags=re.I)
    simple_pr = re.search(r"\b(PR\d{3,})\b", blob, flags=re.I)
    work_order_no = (pr_match.group(1) if pr_match else simple_pr.group(1) if simple_pr else "").strip()

    phase = project_unit = event = title = ""
    name = path.parent.name
    if "_0-" in name:
        tail = name.split("_0-", 1)[1]
        parts = [p.strip() for p in tail.split(".") if p.strip()]
        if parts:
            phase = parts[0]
        if len(parts) >= 2:
            project_unit = parts[1]
        if len(parts) >= 3:
            event = parts[2]
        if len(parts) >= 4:
            title = parts[-1]
        elif len(parts) >= 2:
            title = parts[-1]

    return {
        "work_order_no": work_order_no,
        "maintenance_type": phase,
        "device_name": project_unit,
        "event": event,
        "title": title,
    }


def _extract_specific_patterns(fields: Dict[str, str], text: str) -> None:
    patterns = [
        ("work_order_no", r"(?:编号|资料编号|document\s*no\.?)\s*[：:]?\s*([A-Z0-9.\-_]{3,80})"),
        ("project", r"(?:job\s*no\.?|项目)\s*[：:]\s*([^|。\n]{2,80})"),
        ("device_name", r"(?:unit|机组)\s*[：:]\s*([^|。\n]{2,80})"),
        ("component_code", r"(?:assembly|部套图号)\s*[：:]\s*([^|。\n]{1,80})"),
        ("component_name", r"(?:component|部套名称)\s*[：:]\s*([^|。\n]{1,120})"),
        ("fault_time", r"(?:date|日期)\s*[：:]\s*([0-9]{2,4}[.\-/年][0-9]{1,2}[.\-/月][0-9]{1,2}日?)"),
    ]
    for key, pattern in patterns:
        if fields.get(key):
            continue
        match = re.search(pattern, text, flags=re.I)
        if match:
            fields[key] = _clean_text(match.group(1))


def _extract_fields_and_episodes(tables: List[List[List[str]]], paragraphs: List[str]) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    fields: Dict[str, str] = {}
    episodes: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    previous_header: Optional[List[str]] = None

    def ensure_episode() -> Dict[str, str]:
        nonlocal current
        if current:
            episodes.append(current)
        current = {}
        return current

    for table in tables:
        for row in table:
            clean_row = [_clean_text(cell) for cell in row]
            if not any(clean_row):
                continue

            handled_inline_cells = False
            for cell in clean_row:
                inline_cell = _split_inline_kv(cell)
                if not inline_cell:
                    continue
                key = _field_for_label(inline_cell[0])
                if not key:
                    continue
                value = _selected_checkbox_value(inline_cell[1]) if key in {"maintenance_type", "problem_type"} else inline_cell[1]
                _append_field(fields, key, value)
                if key == "fault_phenomenon":
                    ensure_episode()["fault_phenomenon"] = value
                elif key in {"handling_action", "feedback"}:
                    current[key] = _first_non_empty(current.get(key), value)
                handled_inline_cells = True
            if handled_inline_cells:
                previous_header = None
                continue

            if len(clean_row) == 2:
                key = _field_for_label(clean_row[0])
                if key:
                    value = _selected_checkbox_value(clean_row[1]) if key in {"maintenance_type", "problem_type"} else clean_row[1]
                    _append_field(fields, key, value)
                    if key == "fault_phenomenon":
                        ensure_episode()["fault_phenomenon"] = value
                    elif key in {"handling_action", "feedback"}:
                        current[key] = _first_non_empty(current.get(key), value)
                    previous_header = None
                    continue

            row_text = "\n".join(cell for cell in clean_row if cell)
            _extract_specific_patterns(fields, row_text)

            inline = _split_inline_kv(row_text) if len(clean_row) == 1 else None
            if inline:
                key = _field_for_label(inline[0])
                if key:
                    value = _selected_checkbox_value(inline[1]) if key in {"maintenance_type", "problem_type"} else inline[1]
                    _append_field(fields, key, value)
                    if key == "fault_phenomenon":
                        ensure_episode()["fault_phenomenon"] = value
                    elif key in {"handling_action", "feedback"}:
                        current[key] = _first_non_empty(current.get(key), value)
                    previous_header = None
                    continue

            if previous_header and len(previous_header) == len(clean_row):
                for header, value in zip(previous_header, clean_row):
                    key = _field_for_label(header)
                    if key and value:
                        _append_field(fields, key, value)
                previous_header = None
                continue

            if any(_field_for_label(cell) for cell in clean_row) and len(clean_row) > 2:
                previous_header = clean_row
            else:
                previous_header = None

    if current:
        episodes.append(current)

    if not fields.get("fault_phenomenon"):
        text = "\n".join(paragraphs)
        match = re.search(r"(?:问题描述|主要发现描述)[：:]?\s*(.{20,1200})", text, flags=re.S)
        if match:
            fields["fault_phenomenon"] = _clean_text(match.group(1))

    return fields, episodes


def _detect_template(tables: List[List[List[str]]], paragraphs: List[str]) -> str:
    text = "\n".join(paragraphs[:80]).lower()
    if "gas turbine" in text and "problem report" in text:
        return "bilingual_problem_report"
    if any(any("问题描述" in cell or "答复" in cell for row in table for cell in row) for table in tables):
        return "simple_cn_pr"
    return "fallback"


def _pick_docx_files(input_path: Path) -> List[Path]:
    if input_path.is_file():
        return [input_path]
    all_files = sorted(input_path.rglob("*.docx"))
    by_record_dir: Dict[Path, List[Path]] = {}
    for file in all_files:
        try:
            rel = file.relative_to(input_path)
            group = input_path / rel.parts[0]
        except Exception:
            group = file.parent
        by_record_dir.setdefault(group, []).append(file)

    picked: List[Path] = []
    for files in by_record_dir.values():
        roots = [
            f for f in files
            if "office" not in {part.lower() for part in f.parts} and "_origin" not in f.stem.lower()
        ]
        candidates = roots or [f for f in files if "_origin" not in f.stem.lower()] or files
        picked.append(sorted(candidates, key=lambda p: (-p.stat().st_size, len(str(p))))[0])
    return sorted(picked)


def _build_record(path: Path, fields: Dict[str, str], file_id: str, file_version_id: str, file_name: str, row_index: int) -> Dict[str, Any]:
    meta = _parse_name_metadata(path)
    record_id = _first_non_empty(fields.get("work_order_no"), meta.get("work_order_no"), f"PR_DOCX_{row_index:06d}")
    title = _first_non_empty(fields.get("title"), meta.get("title"), path.stem)
    device_name = _first_non_empty(fields.get("device_name"), meta.get("device_name"), fields.get("project"))
    phenomenon = _first_non_empty(fields.get("fault_phenomenon"), title)
    action = _first_non_empty(fields.get("handling_action"), fields.get("feedback"))
    result = fields.get("handling_result") or ("闭环" if re.search(r"闭环|状态良好|恢复正常", action) else "")

    return {
        "record_id": record_id,
        "work_order_no": record_id,
        "title": title,
        "project": fields.get("project", ""),
        "device_id": fields.get("device_id", ""),
        "device_name": device_name,
        "fault_time": fields.get("fault_time", ""),
        "maintenance_type": _first_non_empty(fields.get("maintenance_type"), meta.get("maintenance_type")),
        "problem_type": fields.get("problem_type", ""),
        "component_code": fields.get("component_code", ""),
        "component_name": fields.get("component_name", ""),
        "fault_phenomenon": phenomenon,
        "alarm_code": fields.get("alarm_code", ""),
        "fault_cause": fields.get("fault_cause", ""),
        "handling_action": action,
        "replaced_parts": fields.get("component_name", ""),
        "handling_result": result,
        "downtime_duration": fields.get("downtime_duration", ""),
        "remarks": fields.get("attachments", ""),
        "feedback": fields.get("feedback", ""),
        "source_path": str(path),
        "row_index": row_index,
        "source_type": SOURCE_TYPE,
        "file_format": FILE_FORMAT,
        "source_record_type": SOURCE_RECORD_TYPE,
        "source_record_id": record_id,
        "file_id": file_id,
        "file_version_id": file_version_id,
        "file_name": file_name,
        "is_active": True,
        "raw_fields": fields,
    }


def _summary(record: Dict[str, Any], episode: Optional[Dict[str, str]] = None) -> str:
    phenomenon = _first_non_empty((episode or {}).get("fault_phenomenon"), record.get("fault_phenomenon"))
    action = _first_non_empty((episode or {}).get("handling_action"), record.get("handling_action"))
    feedback = _first_non_empty((episode or {}).get("feedback"), record.get("feedback"))
    parts = [f"工单 {record['work_order_no']} 记录"]
    for label, key in [
        ("标题", "title"),
        ("检修类型", "maintenance_type"),
        ("问题类型", "problem_type"),
        ("设备", "device_name"),
        ("部套", "component_name"),
        ("部套图号", "component_code"),
        ("故障时间", "fault_time"),
    ]:
        if record.get(key):
            parts.append(f"{label}为{record[key]}")
    if phenomenon:
        parts.append(f"故障现象为{phenomenon}")
    if record.get("fault_cause"):
        parts.append(f"原因为{record['fault_cause']}")
    if action:
        parts.append(f"处理建议或答复为{action}")
    if feedback:
        parts.append(f"实施反馈为{feedback}")
    if record.get("handling_result"):
        parts.append(f"处理结果为{record['handling_result']}")
    return "；".join(parts) + "。"


def _safe_chunk_id(record_id: str, index: int) -> str:
    base = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", record_id).strip("_")
    return f"{base}_{index}"


def _build_chunks(record: Dict[str, Any], episodes: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    usable_episodes = episodes or [{"fault_phenomenon": record.get("fault_phenomenon", "")}]
    chunks = []
    for index, episode in enumerate(usable_episodes, start=1):
        chunk_id = _safe_chunk_id(record["source_record_id"], index)
        chunks.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "chunk_uid": f"{record['file_version_id']}::{chunk_id}",
                "chunk_name": f"PR {record['work_order_no']} - {record.get('title') or '工单'}",
                "content": _summary(record, episode),
                "source": record["row_index"],
                "file": record["file_name"],
                "file_id": record["file_id"],
                "file_version_id": record["file_version_id"],
                "is_active": True,
                "source_type": SOURCE_TYPE,
                "file_format": FILE_FORMAT,
                "chunk_type": CHUNK_TYPE,
                "source_record_type": SOURCE_RECORD_TYPE,
                "source_record_id": record["source_record_id"],
                "turn_index": index,
            }
        )
    return chunks


def _next_version_dir(output_root: Path, file_id: str) -> Path:
    base_dir = output_root / file_id
    max_version = 0
    if base_dir.exists():
        prefix = f"{file_id}_v"
        for item in base_dir.iterdir():
            if item.is_dir() and item.name.startswith(prefix) and item.name[len(prefix):].isdigit():
                max_version = max(max_version, int(item.name[len(prefix):]))
    return base_dir / f"{file_id}_v{max_version + 1}"


def profile_pr_docx_file(input_path: Path) -> Dict[str, Any]:
    paths = _pick_docx_files(input_path.expanduser().resolve())
    samples = []
    template_stats: Dict[str, int] = {}
    missing = []
    for index, path in enumerate(paths[:20], start=1):
        tables = extract_docx_tables(path)
        paragraphs = extract_docx_paragraphs(path)
        fields, _episodes = _extract_fields_and_episodes(tables, paragraphs)
        meta = _parse_name_metadata(path)
        template = _detect_template(tables, paragraphs)
        template_stats[template] = template_stats.get(template, 0) + 1
        record_id = _first_non_empty(fields.get("work_order_no"), meta.get("work_order_no"), f"PR_DOCX_{index:06d}")
        if not _first_non_empty(fields.get("fault_phenomenon"), meta.get("title")):
            missing.append({"record_id": record_id, "missing": ["fault_phenomenon"]})
        samples.append({"record_id": record_id, "file_name": path.name, "template": template, "fields": {**meta, **fields}})
    return {
        "source_type": SOURCE_TYPE,
        "file_format": FILE_FORMAT,
        "file_name": input_path.name,
        "source_headers": list(FIELD_LABELS.keys()),
        "row_count": len(paths),
        "recommended_field_mapping": {},
        "missing_required_fields": [],
        "quality_issues": [f"{len(missing)} 个样本缺少故障现象"] if missing else [],
        "can_import": bool(paths),
        "sample_rows": samples[:3],
        "template_stats": template_stats,
    }


def import_pr_docx(
    input_path: Path,
    *,
    output_dir: str = "./output",
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    input_path = input_path.expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    paths = _pick_docx_files(input_path)
    if not paths:
        raise ValueError(f"未找到可导入的 PR docx 文件: {input_path}")

    resolved_file_id = sanitize_file_id(file_id or input_path.stem)
    version_dir = output_root / resolved_file_id / file_version_id if file_version_id else _next_version_dir(output_root, resolved_file_id)
    version_dir.mkdir(parents=True, exist_ok=False)
    resolved_file_version_id = version_dir.name
    display_file_name = str(file_name or input_path.name).strip()

    records: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    parse_report = {"total_docx": len(paths), "template_stats": {}, "missing_key_fields": [], "files": []}

    for index, path in enumerate(paths, start=1):
        tables = extract_docx_tables(path)
        paragraphs = extract_docx_paragraphs(path)
        fields, episodes = _extract_fields_and_episodes(tables, paragraphs)
        template = _detect_template(tables, paragraphs)
        parse_report["template_stats"][template] = parse_report["template_stats"].get(template, 0) + 1
        record = _build_record(path, fields, resolved_file_id, resolved_file_version_id, display_file_name, index)
        if not record.get("fault_phenomenon"):
            parse_report["missing_key_fields"].append({"record_id": record["source_record_id"], "missing": ["fault_phenomenon"]})
        records.append(record)
        record_chunks = _build_chunks(record, episodes)
        chunks.extend(record_chunks)
        parse_report["files"].append(
            {
                "source_path": str(path),
                "record_id": record["source_record_id"],
                "template": template,
                "tables": len(tables),
                "chunks": len(record_chunks),
            }
        )

    if not chunks:
        raise ValueError(f"PR DOCX 未生成有效分块: {input_path}")

    artifacts = {
        "result_dir": str(version_dir),
        "work_orders_json": str(version_dir / f"{resolved_file_id}_work_orders.json"),
        "chunks_json": str(version_dir / f"{resolved_file_id}_chunks.json"),
        "entities_jsonl": str(version_dir / f"{resolved_file_id}_entities.jsonl"),
        "entities_merged_json": str(version_dir / f"{resolved_file_id}_entities_merged.json"),
        "relations_jsonl": str(version_dir / f"{resolved_file_id}_relations.jsonl"),
        "relations_csv": str(version_dir / f"{resolved_file_id}_relations.csv"),
        "data_profile_json": str(version_dir / f"{resolved_file_id}_data_profile.json"),
        "parse_report_json": str(version_dir / f"{resolved_file_id}_parse_report.json"),
    }

    profile = profile_pr_docx_file(input_path)
    _write_json(Path(artifacts["work_orders_json"]), records)
    _write_json(Path(artifacts["chunks_json"]), chunks)
    _write_json(Path(artifacts["data_profile_json"]), profile)
    _write_json(Path(artifacts["parse_report_json"]), parse_report)

    script_dir = Path(__file__).resolve().parent
    _run_command(
        [
            sys.executable,
            str(script_dir / "extract_entities.py"),
            "--input",
            artifacts["chunks_json"],
            "--output-entities",
            artifacts["entities_jsonl"],
            "--output-merged",
            artifacts["entities_merged_json"],
        ],
        "PR DOCX 实体抽取",
    )
    _run_command(
        [
            sys.executable,
            str(script_dir / "extract_relations.py"),
            "--input-chunks",
            artifacts["chunks_json"],
            "--input-entities",
            artifacts["entities_merged_json"],
            "--output-relations",
            artifacts["relations_jsonl"],
            "--output-csv",
            artifacts["relations_csv"],
        ],
        "PR DOCX 关系抽取",
    )

    entities = _load_json_list(Path(artifacts["entities_merged_json"]))
    relations = _load_jsonl_rows(Path(artifacts["relations_jsonl"]))
    relation_count = sum(len(row.get("relations") or []) for row in relations)

    return {
        "status": "success",
        "source_type": SOURCE_TYPE,
        "file": {
            "file_id": resolved_file_id,
            "file_version_id": resolved_file_version_id,
            "file_name": display_file_name,
            "file_format": FILE_FORMAT,
        },
        "profiling": profile,
        "imported": {
            "work_orders": len(records),
            "chunks": len(chunks),
            "entities": len(entities),
            "relations": relation_count,
            "skipped_rows": 0,
        },
        "artifacts": artifacts,
        "parse_report": parse_report,
        "preview": {
            "first_work_order": records[0] if records else None,
            "first_chunk": chunks[0] if chunks else None,
            "first_entity": entities[0] if entities else None,
            "first_relation_row": relations[0] if relations else None,
        },
    }
