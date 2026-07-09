from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET


STANDARD_WORK_ORDER_FIELDS: List[Dict[str, Any]] = [
    {"key": "work_order_no", "label": "工单号", "required": True},
    {"key": "device_id", "label": "设备编号", "required": False},
    {"key": "device_name", "label": "设备名称", "required": False},
    {"key": "fault_time", "label": "故障时间", "required": False},
    {"key": "fault_phenomenon", "label": "故障现象", "required": True},
    {"key": "alarm_code", "label": "报警码", "required": False},
    {"key": "fault_cause", "label": "故障原因", "required": False},
    {"key": "handling_action", "label": "处理措施", "required": False},
    {"key": "replaced_parts", "label": "更换部件", "required": False},
    {"key": "handling_result", "label": "处理结果", "required": False},
    {"key": "downtime_duration", "label": "停机时长", "required": False},
    {"key": "remarks", "label": "备注", "required": False},
]

STANDARD_FIELD_KEYS = {item["key"] for item in STANDARD_WORK_ORDER_FIELDS}
REQUIRED_FIELD_KEYS = [item["key"] for item in STANDARD_WORK_ORDER_FIELDS if item["required"]]

FIELD_ALIASES: Dict[str, List[str]] = {
    "work_order_no": [
        "工单号", "工单编号", "工单id", "工单单号", "wo", "wono", "worderno", "workorderno", "orderid",
    ],
    "device_id": [
        "设备编号", "设备id", "设备编码", "资产编号", "assetid", "deviceid",
    ],
    "device_name": [
        "设备名称", "设备名", "device", "devicename", "equipmentname",
    ],
    "fault_time": [
        "故障时间", "异常时间", "报警时间", "发生时间", "时间", "faulttime",
    ],
    "fault_phenomenon": [
        "故障现象", "异常现象", "故障描述", "问题描述", "故障表现", "现象", "异常描述", "faultdescription",
    ],
    "alarm_code": [
        "报警码", "报警代码", "告警码", "告警代码", "alarmcode",
    ],
    "fault_cause": [
        "故障原因", "原因", "根因", "故障根因", "faultcause", "rootcause",
    ],
    "handling_action": [
        "处理措施", "维修措施", "处置措施", "处理方法", "处理", "解决措施", "actiontaken", "handlingaction",
    ],
    "replaced_parts": [
        "更换部件", "更换零件", "部件", "零件", "partsreplaced", "replacedparts",
    ],
    "handling_result": [
        "处理结果", "维修结果", "结果", "结论", "result", "handlingresult",
    ],
    "downtime_duration": [
        "停机时长", "停机时间", "停机分钟", "downtime", "downtimeduration",
    ],
    "remarks": [
        "备注", "说明", "补充说明", "comments", "remark", "remarks", "note",
    ],
}

WORK_ORDER_ENTITY_TYPE_MAP: Dict[str, str] = {
    "work_order_no": "技术系统与设备",
    "device_id": "技术系统与设备",
    "device_name": "技术系统与设备",
    "fault_phenomenon": "故障现象与报警",
    "alarm_code": "故障现象与报警",
    "fault_cause": "故障现象与报警",
    "handling_action": "技术概念与方法",
    "replaced_parts": "硬件组件与元器件",
    "handling_result": "技术概念与方法",
}


def save_json(data: Any, file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_header(text: Any) -> str:
    value = str(text or "").strip().lower()
    value = (
        value.replace("（", "(")
        .replace("）", ")")
        .replace("：", ":")
        .replace("，", ",")
        .replace("／", "/")
    )
    value = re.sub(r"[\s_\-:/\\()\[\]{}]+", "", value)
    return value


def normalize_cell_value(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text)


def build_alias_index() -> Dict[str, str]:
    alias_index: Dict[str, str] = {}
    for field_key, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            alias_index[normalize_header(alias)] = field_key
    return alias_index


ALIAS_INDEX = build_alias_index()


def infer_file_format(file_path: Path) -> str:
    ext = file_path.suffix.lower().lstrip(".")
    if ext == "markdown":
        return "md"
    return ext or "txt"


def read_csv_rows(file_path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with file_path.open("r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                headers = [str(h or "").strip() for h in (reader.fieldnames or [])]
                rows = []
                for row in reader:
                    rows.append({str(k or "").strip(): normalize_cell_value(v) for k, v in (row or {}).items()})
                return headers, rows
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"CSV 读取失败，无法识别编码: {file_path}") from last_error


def column_letters_to_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def read_xlsx_rows(file_path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }

    with zipfile.ZipFile(file_path, "r") as zf:
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("main:si", ns):
                texts = [node.text or "" for node in si.findall(".//main:t", ns)]
                shared_strings.append("".join(texts))

        workbook_root = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = workbook_root.findall("main:sheets/main:sheet", ns)
        if not sheets:
            return [], []
        first_sheet = sheets[0]
        rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if not rel_id:
            raise ValueError("XLSX 工作簿缺少首个工作表关系 ID")

        rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rel_root.findall("pkgrel:Relationship", ns):
            if rel.attrib.get("Id") == rel_id:
                target = rel.attrib.get("Target")
                break
        if not target:
            raise ValueError("XLSX 工作簿无法解析首个工作表路径")
        sheet_path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"

        sheet_root = ET.fromstring(zf.read(sheet_path))
        rows_raw: List[List[str]] = []
        for row in sheet_root.findall("main:sheetData/main:row", ns):
            row_cells: Dict[int, str] = {}
            for cell in row.findall("main:c", ns):
                ref = cell.attrib.get("r", "")
                letters = "".join(ch for ch in ref if ch.isalpha())
                if not letters:
                    continue
                col_idx = column_letters_to_index(letters)
                cell_type = cell.attrib.get("t")
                value = ""
                if cell_type == "inlineStr":
                    parts = [node.text or "" for node in cell.findall(".//main:t", ns)]
                    value = "".join(parts)
                else:
                    raw_value = cell.findtext("main:v", default="", namespaces=ns)
                    if cell_type == "s":
                        try:
                            value = shared_strings[int(raw_value)]
                        except Exception:
                            value = raw_value
                    else:
                        value = raw_value
                row_cells[col_idx] = normalize_cell_value(value)
            if not row_cells:
                continue
            max_col = max(row_cells)
            row_values = [row_cells.get(i, "") for i in range(max_col + 1)]
            rows_raw.append(row_values)

    if not rows_raw:
        return [], []

    headers = [normalize_cell_value(item) for item in rows_raw[0]]
    rows: List[Dict[str, str]] = []
    for row_values in rows_raw[1:]:
        row_dict = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            row_dict[header] = normalize_cell_value(row_values[idx] if idx < len(row_values) else "")
        rows.append(row_dict)
    return headers, rows


def load_tabular_rows(file_path: Path) -> Tuple[str, List[str], List[Dict[str, str]]]:
    file_format = infer_file_format(file_path)
    if file_format == "csv":
        headers, rows = read_csv_rows(file_path)
        return file_format, headers, rows
    if file_format == "xlsx":
        headers, rows = read_xlsx_rows(file_path)
        return file_format, headers, rows
    raise ValueError(f"暂不支持的工单文件格式: {file_format}，当前仅支持 csv / xlsx")


def recommend_field_mapping(headers: List[str]) -> Dict[str, Optional[str]]:
    normalized_headers = {header: normalize_header(header) for header in headers}
    mapping: Dict[str, Optional[str]] = {item["key"]: None for item in STANDARD_WORK_ORDER_FIELDS}

    for field_key in mapping.keys():
        best_header = None
        for header, normalized in normalized_headers.items():
            if normalized in ALIAS_INDEX and ALIAS_INDEX[normalized] == field_key:
                best_header = header
                break
        mapping[field_key] = best_header
    return mapping


def merge_field_mapping(headers: List[str], user_mapping: Optional[Dict[str, str]] = None) -> Dict[str, Optional[str]]:
    recommended = recommend_field_mapping(headers)
    if not user_mapping:
        return recommended

    header_set = {str(header or "").strip() for header in headers}
    merged = dict(recommended)
    for field_key, source_header in (user_mapping or {}).items():
        if field_key not in STANDARD_FIELD_KEYS:
            continue
        header = str(source_header or "").strip()
        if not header:
            merged[field_key] = None
            continue
        if header in header_set:
            merged[field_key] = header
    return merged


def summarize_quality_issues(headers: List[str], rows: List[Dict[str, str]], mapping: Dict[str, Optional[str]]) -> List[str]:
    issues: List[str] = []
    if not headers:
        issues.append("未识别到表头")
    if not rows:
        issues.append("未识别到有效数据行")

    missing_required = [item["label"] for item in STANDARD_WORK_ORDER_FIELDS if item["required"] and not mapping.get(item["key"])]
    if missing_required:
        issues.append(f"缺少关键字段映射: {', '.join(missing_required)}")

    empty_headers = [header for header in headers if not str(header or "").strip()]
    if empty_headers:
        issues.append("存在空表头列")

    return issues


def build_profile(file_path: Path, headers: List[str], rows: List[Dict[str, str]], mapping: Dict[str, Optional[str]], file_format: str) -> Dict[str, Any]:
    quality_issues = summarize_quality_issues(headers, rows, mapping)
    missing_required_field_keys = [key for key in REQUIRED_FIELD_KEYS if not mapping.get(key)]
    missing_required_labels = [
        item["label"] for item in STANDARD_WORK_ORDER_FIELDS if item["key"] in missing_required_field_keys
    ]
    sample_rows = rows[:3]
    return {
        "source_type": "work_order",
        "file_format": file_format,
        "file_name": file_path.name,
        "source_headers": headers,
        "row_count": len(rows),
        "recommended_field_mapping": mapping,
        "missing_required_fields": missing_required_labels,
        "quality_issues": quality_issues,
        "can_import": len(rows) > 0 and not missing_required_field_keys,
        "sample_rows": sample_rows,
    }


def build_standard_record(
    row: Dict[str, str],
    row_index: int,
    field_mapping: Dict[str, Optional[str]],
    *,
    file_id: str,
    file_version_id: str,
    file_name: str,
    file_format: str,
) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for field in STANDARD_WORK_ORDER_FIELDS:
        source_header = field_mapping.get(field["key"])
        value = normalize_cell_value(row.get(source_header, "")) if source_header else ""
        normalized[field["key"]] = value

    record_id = normalized.get("work_order_no") or f"WO_ROW_{row_index:06d}"
    normalized["record_id"] = record_id
    normalized["row_index"] = row_index
    normalized["source_type"] = "work_order"
    normalized["file_format"] = file_format
    normalized["source_record_type"] = "work_order"
    normalized["source_record_id"] = record_id
    normalized["file_id"] = file_id
    normalized["file_version_id"] = file_version_id
    normalized["file_name"] = file_name
    normalized["is_active"] = True
    normalized["raw_row"] = row
    normalized["field_mapping"] = field_mapping
    return normalized


def is_meaningful_work_order(record: Dict[str, Any]) -> bool:
    return bool(
        normalize_cell_value(record.get("work_order_no"))
        or normalize_cell_value(record.get("fault_phenomenon"))
        or normalize_cell_value(record.get("fault_cause"))
    )


def build_work_order_summary(record: Dict[str, Any]) -> str:
    parts = [f"工单 {record['source_record_id']} 记录"]

    if record.get("device_name"):
        parts.append(f"设备为{record['device_name']}")
    elif record.get("device_id"):
        parts.append(f"设备编号为{record['device_id']}")

    if record.get("fault_time"):
        parts.append(f"在{record['fault_time']}发生故障")
    if record.get("fault_phenomenon"):
        parts.append(f"故障现象为{record['fault_phenomenon']}")
    if record.get("alarm_code"):
        parts.append(f"报警码为{record['alarm_code']}")
    if record.get("fault_cause"):
        parts.append(f"原因为{record['fault_cause']}")
    if record.get("handling_action"):
        parts.append(f"处理措施为{record['handling_action']}")
    if record.get("replaced_parts"):
        parts.append(f"更换部件为{record['replaced_parts']}")
    if record.get("handling_result"):
        parts.append(f"处理结果为{record['handling_result']}")
    if record.get("downtime_duration"):
        parts.append(f"停机时长为{record['downtime_duration']}")
    if record.get("remarks"):
        parts.append(f"备注为{record['remarks']}")

    text = "，".join(parts)
    return text if text.endswith("。") else f"{text}。"


def build_work_order_chunk(record: Dict[str, Any], chunk_index: int) -> Dict[str, Any]:
    return {
        "id": str(chunk_index),
        "chunk_id": str(chunk_index),
        "chunk_uid": f"{record['file_version_id']}::{chunk_index}",
        "chunk_name": f"工单 {record['source_record_id']}",
        "content": build_work_order_summary(record),
        "source": record["row_index"],
        "file": record["file_name"],
        "file_id": record["file_id"],
        "file_version_id": record["file_version_id"],
        "is_active": True,
        "source_type": "work_order",
        "file_format": record["file_format"],
        "chunk_type": "table_row_summary",
        "source_record_type": "work_order",
        "source_record_id": record["source_record_id"],
    }


def summarize_entity_description(field_key: str, value: str, record: Dict[str, Any]) -> str:
    if field_key == "fault_phenomenon":
        return f"工单 {record['source_record_id']} 中记录的故障现象：{value}"
    if field_key == "fault_cause":
        return f"工单 {record['source_record_id']} 中记录的故障原因：{value}"
    if field_key == "alarm_code":
        return f"工单 {record['source_record_id']} 中记录的报警码：{value}"
    if field_key == "handling_action":
        return f"工单 {record['source_record_id']} 中记录的处理措施：{value}"
    if field_key == "replaced_parts":
        return f"工单 {record['source_record_id']} 中记录的更换部件：{value}"
    if field_key == "device_name":
        return f"工单 {record['source_record_id']} 对应设备：{value}"
    if field_key == "device_id":
        return f"工单 {record['source_record_id']} 对应设备编号：{value}"
    return value


def build_work_order_entities(records: List[Dict[str, Any]], chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chunk_id_by_record = {chunk["source_record_id"]: str(chunk["chunk_id"]) for chunk in chunks}
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}

    candidate_fields = [
        "device_id",
        "device_name",
        "fault_phenomenon",
        "alarm_code",
        "fault_cause",
        "handling_action",
        "replaced_parts",
        "handling_result",
    ]

    for record in records:
        chunk_id = chunk_id_by_record.get(record["source_record_id"], "")
        for field_key in candidate_fields:
            value = normalize_cell_value(record.get(field_key))
            if not value:
                continue
            entity_type = WORK_ORDER_ENTITY_TYPE_MAP.get(field_key, "故障现象与报警")
            entity_key = (value, entity_type)
            current = aggregated.setdefault(
                entity_key,
                {
                    "entity_name": value,
                    "name": value,
                    "entity_type": entity_type,
                    "description": summarize_entity_description(field_key, value, record),
                    "rule": "",
                    "investigateMethod": "",
                    "repairMethod": "",
                    "documents": [],
                    "chunk_ids": [],
                    "source_chunk_ids": [],
                    "file_id": record["file_id"],
                    "file_version_id": record["file_version_id"],
                    "is_active": True,
                },
            )
            if chunk_id and chunk_id not in current["chunk_ids"]:
                current["chunk_ids"].append(chunk_id)
            if chunk_id and chunk_id not in current["source_chunk_ids"]:
                current["source_chunk_ids"].append(chunk_id)
            doc = {"chunk_id": chunk_id} if chunk_id else None
            if doc and doc not in current["documents"]:
                current["documents"].append(doc)

    results: List[Dict[str, Any]] = []
    for item in aggregated.values():
        item["count"] = len(item["chunk_ids"])
        item["support_count"] = len(item["source_chunk_ids"])
        results.append(item)
    return results


def _entity_lookup(entities: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entity in entities:
        key = (str(entity.get("name") or entity.get("entity_name") or "").strip(), str(entity.get("entity_type") or "").strip())
        if key[0]:
            lookup[key] = entity
    return lookup


def _build_relation_entity_props(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": entity.get("name") or entity.get("entity_name", ""),
        "entity_type": entity.get("entity_type", ""),
        "description": entity.get("description", ""),
        "documents": entity.get("documents", []),
        "source_chunk_ids": entity.get("source_chunk_ids", []),
        "support_count": entity.get("support_count", entity.get("count", 1)),
        "file_id": entity.get("file_id", ""),
        "file_version_id": entity.get("file_version_id", ""),
        "is_active": bool(entity.get("is_active", True)),
        "source_type": "work_order",
        "file_format": entity.get("file_format", "csv"),
        "chunk_type": "table_row_summary",
        "source_record_type": "work_order",
        "source_record_id": entity.get("source_record_id", None),
    }


def build_work_order_relations(records: List[Dict[str, Any]], chunks: List[Dict[str, Any]], entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chunk_id_by_record = {chunk["source_record_id"]: str(chunk["chunk_id"]) for chunk in chunks}
    entity_map = _entity_lookup(entities)
    rows: List[Dict[str, Any]] = []

    def relation_for(
        chunk_id: str,
        file_id: str,
        file_version_id: str,
        file_name: str,
        relation_type: str,
        entity1_name: str,
        entity1_type: str,
        entity2_name: str,
        entity2_type: str,
    ) -> Optional[Dict[str, Any]]:
        e1 = entity_map.get((entity1_name, entity1_type))
        e2 = entity_map.get((entity2_name, entity2_type))
        if not e1 or not e2:
            return None
        return {
            "chunk_id": chunk_id,
            "entity1": entity1_name,
            "entity2": entity2_name,
            "relation_type": relation_type,
            "entity1_type": entity1_type,
            "entity2_type": entity2_type,
            "entity1_props": _build_relation_entity_props(e1),
            "entity2_props": _build_relation_entity_props(e2),
            "file_id": file_id,
            "file_version_id": file_version_id,
            "file_name": file_name,
            "is_active": True,
            "source_type": "work_order",
            "file_format": infer_file_format(Path(file_name)),
            "chunk_type": "table_row_summary",
            "source_record_type": "work_order",
            "source_record_id": None,
        }

    for record in records:
        chunk_id = chunk_id_by_record.get(record["source_record_id"], "")
        if not chunk_id:
            continue
        row_relations: List[Dict[str, Any]] = []
        file_id = record["file_id"]
        file_version_id = record["file_version_id"]
        file_name = record["file_name"]

        fault_phenomenon = normalize_cell_value(record.get("fault_phenomenon"))
        fault_cause = normalize_cell_value(record.get("fault_cause"))
        alarm_code = normalize_cell_value(record.get("alarm_code"))
        handling_action = normalize_cell_value(record.get("handling_action"))
        replaced_parts = normalize_cell_value(record.get("replaced_parts"))
        device_name = normalize_cell_value(record.get("device_name"))
        device_id = normalize_cell_value(record.get("device_id"))

        if fault_cause and fault_phenomenon:
            rel = relation_for(
                chunk_id,
                file_id,
                file_version_id,
                file_name,
                "触发",
                fault_cause,
                WORK_ORDER_ENTITY_TYPE_MAP["fault_cause"],
                fault_phenomenon,
                WORK_ORDER_ENTITY_TYPE_MAP["fault_phenomenon"],
            )
            if rel:
                row_relations.append(rel)

        if alarm_code and fault_phenomenon:
            rel = relation_for(
                chunk_id,
                file_id,
                file_version_id,
                file_name,
                "表征",
                alarm_code,
                WORK_ORDER_ENTITY_TYPE_MAP["alarm_code"],
                fault_phenomenon,
                WORK_ORDER_ENTITY_TYPE_MAP["fault_phenomenon"],
            )
            if rel:
                row_relations.append(rel)

        if handling_action and fault_cause:
            rel = relation_for(
                chunk_id,
                file_id,
                file_version_id,
                file_name,
                "处理",
                handling_action,
                WORK_ORDER_ENTITY_TYPE_MAP["handling_action"],
                fault_cause,
                WORK_ORDER_ENTITY_TYPE_MAP["fault_cause"],
            )
            if rel:
                row_relations.append(rel)

        if replaced_parts and (device_name or device_id):
            target_name = device_name or device_id
            target_type = WORK_ORDER_ENTITY_TYPE_MAP["device_name"] if device_name else WORK_ORDER_ENTITY_TYPE_MAP["device_id"]
            rel = relation_for(
                chunk_id,
                file_id,
                file_version_id,
                file_name,
                "关联",
                replaced_parts,
                WORK_ORDER_ENTITY_TYPE_MAP["replaced_parts"],
                target_name,
                target_type,
            )
            if rel:
                row_relations.append(rel)

        rows.append(
            {
                "chunk_id": chunk_id,
                "file_id": file_id,
                "file_version_id": file_version_id,
                "file_name": file_name,
                "is_active": True,
                "source_type": "work_order",
                "file_format": record["file_format"],
                "chunk_type": "table_row_summary",
                "source_record_type": "work_order",
                "source_record_id": record["source_record_id"],
                "relations": row_relations,
            }
        )

    return rows


def save_relations_csv(rows: List[Dict[str, Any]], csv_path: Path) -> None:
    fieldnames = [
        "chunk_id",
        "file_id",
        "file_version_id",
        "source_type",
        "file_format",
        "chunk_type",
        "source_record_type",
        "source_record_id",
        "entity1",
        "entity2",
        "relation_type",
        "entity1_type",
        "entity2_type",
    ]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            for rel in row.get("relations", []):
                writer.writerow(
                    {
                        "chunk_id": row.get("chunk_id", ""),
                        "file_id": row.get("file_id", ""),
                        "file_version_id": row.get("file_version_id", ""),
                        "source_type": row.get("source_type", ""),
                        "file_format": row.get("file_format", ""),
                        "chunk_type": row.get("chunk_type", ""),
                        "source_record_type": row.get("source_record_type", ""),
                        "source_record_id": row.get("source_record_id", ""),
                        "entity1": rel.get("entity1", ""),
                        "entity2": rel.get("entity2", ""),
                        "relation_type": rel.get("relation_type", ""),
                        "entity1_type": rel.get("entity1_type", ""),
                        "entity2_type": rel.get("entity2_type", ""),
                    }
                )


def next_version_dir(output_root: Path, file_id: str) -> Path:
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


def sanitize_file_id(value: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "work_orders"


def profile_work_order_file(file_path: Path) -> Dict[str, Any]:
    file_format, headers, rows = load_tabular_rows(file_path)
    mapping = recommend_field_mapping(headers)
    return build_profile(file_path, headers, rows, mapping, file_format)


def import_work_order_file(
    file_path: Path,
    *,
    output_dir: str = "./output",
    file_id: Optional[str] = None,
    file_version_id: Optional[str] = None,
    file_name: Optional[str] = None,
    field_mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    file_path = file_path.expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    file_format, headers, rows = load_tabular_rows(file_path)
    resolved_file_id = sanitize_file_id(file_id or file_path.stem)
    display_file_name = str(file_name or file_path.name).strip()
    if file_version_id:
        version_dir = output_root / resolved_file_id / file_version_id
        version_dir.mkdir(parents=True, exist_ok=False)
    else:
        version_dir = next_version_dir(output_root, resolved_file_id)
    resolved_file_version_id = version_dir.name

    merged_mapping = merge_field_mapping(headers, field_mapping)
    profile = build_profile(file_path, headers, rows, merged_mapping, file_format)

    work_orders: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    skipped_rows = 0
    for row_index, row in enumerate(rows, start=1):
        record = build_standard_record(
            row,
            row_index,
            merged_mapping,
            file_id=resolved_file_id,
            file_version_id=resolved_file_version_id,
            file_name=display_file_name,
            file_format=file_format,
        )
        if not is_meaningful_work_order(record):
            skipped_rows += 1
            continue
        work_orders.append(record)
        chunks.append(build_work_order_chunk(record, len(chunks)))

    entities = build_work_order_entities(work_orders, chunks)
    relations = build_work_order_relations(work_orders, chunks, entities)

    artifacts = {
        "result_dir": str(version_dir),
        "work_orders_json": str(version_dir / f"{resolved_file_id}_work_orders.json"),
        "chunks_json": str(version_dir / f"{resolved_file_id}_chunks.json"),
        "entities_merged_json": str(version_dir / f"{resolved_file_id}_entities_merged.json"),
        "relations_jsonl": str(version_dir / f"{resolved_file_id}_relations.jsonl"),
        "relations_csv": str(version_dir / f"{resolved_file_id}_relations.csv"),
        "data_profile_json": str(version_dir / f"{resolved_file_id}_data_profile.json"),
        "field_mapping_json": str(version_dir / f"{resolved_file_id}_field_mapping.json"),
    }

    save_json(work_orders, Path(artifacts["work_orders_json"]))
    save_json(chunks, Path(artifacts["chunks_json"]))
    save_json(entities, Path(artifacts["entities_merged_json"]))
    relations_jsonl_path = Path(artifacts["relations_jsonl"])
    relations_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with relations_jsonl_path.open("w", encoding="utf-8") as f:
        for row in relations:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    save_relations_csv(relations, Path(artifacts["relations_csv"]))
    save_json(profile, Path(artifacts["data_profile_json"]))
    save_json(merged_mapping, Path(artifacts["field_mapping_json"]))

    return {
        "status": "success",
        "source_type": "work_order",
        "file": {
            "file_id": resolved_file_id,
            "file_version_id": resolved_file_version_id,
            "file_name": display_file_name,
            "file_format": file_format,
        },
        "profiling": profile,
        "used_field_mapping": merged_mapping,
        "imported": {
            "work_orders": len(work_orders),
            "chunks": len(chunks),
            "entities": len(entities),
            "relations": sum(len(row.get("relations", [])) for row in relations),
            "skipped_rows": skipped_rows,
        },
        "artifacts": artifacts,
        "preview": {
            "first_work_order": work_orders[0] if work_orders else None,
            "first_chunk": chunks[0] if chunks else None,
            "first_entity": entities[0] if entities else None,
            "first_relation_row": relations[0] if relations else None,
        },
    }
