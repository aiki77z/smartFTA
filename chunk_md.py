# chunk_md.py
import os
import re
import json
import argparse
import bisect
import time
import sys
from html import unescape
from html.parser import HTMLParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build_chunk_common_fields(
    *,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    return {
        "source_type": source_type,
        "file_format": file_format,
        "chunk_type": chunk_type,
        "source_record_type": source_record_type,
        "source_record_id": source_record_id,
    }


def save_json(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_single_file(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    if ext != '.md':
        raise ValueError(f"不支持的文件格式: {ext}，仅支持 .md 文件")

    print(f"正在读取文件: {file_path} ...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='gbk') as f:
            content = f.read()

    file_name = os.path.splitext(os.path.basename(file_path))[0]
    return file_name, content

def extract_image_paths(text):
    """
    提取文本中所有 Markdown 图片路径，返回列表。
    匹配格式: ![alt](path) 或 ![alt](path "title")
    """
    pattern = r'!\[[^\]]*\]\(([^\s\)]+)(?:\s+["\'][^"\']*["\'])?\)'
    matches = re.findall(pattern, text)
    return matches  # 返回列表，可能为空

def get_line_number(pos, line_starts):
    idx = bisect.bisect_right(line_starts, pos) - 1
    return idx + 1


class TableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.current_row = []
        elif tag in ("td", "th"):
            if self.current_row is None:
                self.current_row = []
            self.current_cell = []
            self.in_cell = True
        elif tag == "br" and self.in_cell and self.current_cell is not None:
            self.current_cell.append("\n")

    def handle_data(self, data):
        if self.in_cell and self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            text = normalize_cell_text("".join(self.current_cell or []))
            self.current_row.append(text)
            self.current_cell = None
            self.in_cell = False
        elif tag == "tr" and self.current_row is not None:
            if any(cell.strip() for cell in self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None

    def close(self):
        if self.in_cell and self.current_row is not None:
            text = normalize_cell_text("".join(self.current_cell or []))
            self.current_row.append(text)
        if self.current_row is not None and any(cell.strip() for cell in self.current_row):
            self.rows.append(self.current_row)
        super().close()


def normalize_cell_text(text):
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_html_table_rows(table_text):
    parser = TableHTMLParser()
    try:
        parser.feed(table_text)
        parser.close()
    except Exception:
        parser.rows = []

    if parser.rows:
        return parser.rows

    rows = []
    row_matches = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_text, flags=re.I | re.S)
    if not row_matches:
        row_matches = re.findall(r"<tr\b[^>]*>(.*?)(?=<tr\b|</table>|$)", table_text, flags=re.I | re.S)
    for row_html in row_matches:
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)(?:</t[dh]>|$)", row_html, flags=re.I | re.S)
        clean_cells = [normalize_cell_text(cell) for cell in cells]
        if any(clean_cells):
            rows.append(clean_cells)
    return rows


def parse_markdown_table_rows(table_text):
    rows = []
    for line in table_text.splitlines():
        stripped = line.strip()
        if not stripped or "|" not in stripped:
            continue
        cells = [normalize_cell_text(cell) for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
            continue
        if any(cells):
            rows.append(cells)
    return rows


def split_inline_attribute_value(text):
    text = normalize_cell_text(text)
    if not text:
        return "", ""
    match = re.match(
        r"^(.+?)\s*[:：]?\s*("
        r"[-+]?\d+(?:\.\d+)?(?:\s*[-~—至]\s*[-+]?\d+(?:\.\d+)?)?"
        r"\s*(?:rpm|RPM|hz|HZ|Hz|mpa|MPa|kPa|Pa|℃|°C|mm|cm|m|kg|MW|KW|kW|V|A|%|个|级|台|只|套|根|次)?"
        r"(?:（[^）]*）|\([^)]*\))?"
        r")$",
        text,
    )
    if match and len(match.group(1).strip()) >= 2:
        return match.group(1).strip(), match.group(2).strip()
    return text, ""


def looks_like_group_title(cells):
    non_empty = [cell for cell in cells if cell.strip()]
    if len(non_empty) != 1:
        return False
    text = non_empty[0]
    attr, value = split_inline_attribute_value(text)
    return not value and len(text) <= 30


def infer_unit(value):
    value = value or ""
    if "个" in value:
        return "个"
    match = re.search(r"(rpm|RPM|hz|HZ|Hz|mpa|MPa|kPa|Pa|℃|°C|mm|cm|m|kg|MW|KW|kW|V|A|%|个|级|台|只|套|根|次)", value)
    return match.group(1) if match else None


def looks_like_attribute_name(text):
    return bool(re.search(
        r"(级数|形式|结构|个数|数量|压比|转速|范围|临界值|阈值|参数|型号|类型|温度|压力|流量|电压|电流|功率|频率|尺寸|长度|高度|宽度|直径|材料|方式|名称)$",
        text or "",
    ))


def looks_like_position_value(text):
    return bool(re.search(r"(第\s*\d+\s*级|安装|布置|位于|位置)", text or ""))


def looks_like_header_row(cells):
    non_empty = [cell for cell in cells if cell.strip()]
    if len(non_empty) < 2:
        return False
    header_keywords = (
        "名称", "项目", "参数", "成分", "部件", "对象", "试验标准", "标准", "单位",
        "数值", "取值", "范围", "限值", "要求", "说明", "备注", "去矿物质水",
    )
    keyword_hits = sum(1 for cell in non_empty if any(keyword in cell for keyword in header_keywords))
    value_like_hits = sum(1 for cell in non_empty if split_inline_attribute_value(cell)[1])
    return keyword_hits >= 2 and value_like_hits == 0


def normalize_header_attribute(header):
    header = normalize_cell_text(header)
    if header in ("", "名称", "项目", "参数", "成分", "部件", "对象"):
        return ""
    return header


def table_rows_to_header_records(rows):
    if not rows:
        return []
    header = [normalize_cell_text(cell) for cell in rows[0]]
    records = []
    current_subject = ""

    for row_index, row in enumerate(rows[1:], start=2):
        cells = [normalize_cell_text(cell) for cell in row]
        if not any(cells):
            continue
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))

        first = cells[0] if cells else ""
        if first:
            current_subject = first
        subject = current_subject or first
        if not subject:
            continue

        for col_index in range(1, min(len(header), len(cells))):
            attribute = normalize_header_attribute(header[col_index])
            value = cells[col_index]
            if not attribute or not value:
                continue
            records.append({
                "subject": subject,
                "attribute": attribute,
                "value": value,
                "unit": infer_unit(value),
                "group": "",
                "raw_row": row,
                "row_index": row_index,
                "confidence": "high",
            })

    return records


def table_rows_to_records(rows):
    records = []
    current_group = ""
    inherited_subject = ""

    normalized_rows = [[normalize_cell_text(cell) for cell in row] for row in rows]
    if normalized_rows and looks_like_header_row(normalized_rows[0]):
        return table_rows_to_header_records(normalized_rows)

    for index, row in enumerate(normalized_rows, start=1):
        cells = list(row)
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue

        raw_row = row
        non_empty = [cell for cell in cells if cell]

        if looks_like_group_title(cells):
            current_group = non_empty[0]
            inherited_subject = current_group
            continue

        first = cells[0] if len(cells) > 0 else ""
        second = cells[1] if len(cells) > 1 else ""

        if first:
            inherited_subject = first

        subject = current_group or inherited_subject or first
        attribute = ""
        value = ""
        confidence = "medium"

        if len(cells) == 1:
            attribute, value = split_inline_attribute_value(cells[0])
            subject = current_group or ""
            if not value:
                value = cells[0]
                attribute = "说明"
                confidence = "low"
        elif first and second:
            if current_group:
                subject = current_group
                attribute = first
                value = second
            elif looks_like_position_value(second):
                subject = first
                attribute = "安装位置"
                value = second
            elif looks_like_attribute_name(first):
                subject = ""
                attribute = first
                value = second
            else:
                subject = first
                attribute = "取值"
                value = second
        elif first and not second:
            attribute, value = split_inline_attribute_value(first)
            subject = current_group or ""
            if not value:
                value = first
                attribute = "说明"
                confidence = "low"
        elif not first and second:
            subject = inherited_subject or current_group
            attribute = "补充说明"
            value = second
            confidence = "low" if not subject else "medium"

        if not value and len(cells) > 2:
            value = "；".join(cell for cell in cells[1:] if cell)

        if subject or attribute or value:
            records.append({
                "subject": subject,
                "attribute": attribute,
                "value": value,
                "unit": infer_unit(value),
                "group": current_group,
                "raw_row": raw_row,
                "row_index": index,
                "confidence": confidence,
            })

    return records


def build_table_summary(records, limit=80):
    lines = ["表格记录："]
    for idx, record in enumerate(records[:limit], start=1):
        subject = record.get("subject") or "未明确对象"
        attribute = record.get("attribute") or "说明"
        value = record.get("value") or ""
        if value:
            lines.append(f"{idx}. {subject}的{attribute}为{value}。")
        else:
            lines.append(f"{idx}. {subject}包含{attribute}。")
    if len(records) > limit:
        lines.append(f"... 另有 {len(records) - limit} 条表格记录未在摘要中展开。")
    return "\n".join(lines)


def parse_structured_table(table_text):
    result = {
        "status": "failed",
        "structured_table": None,
        "summary_text": "",
        "error": None,
    }
    try:
        lower_text = table_text.lower()
        if "<table" in lower_text or "<tr" in lower_text or "<td" in lower_text:
            table_type = "html"
            rows = parse_html_table_rows(table_text)
        else:
            table_type = "markdown"
            rows = parse_markdown_table_rows(table_text)

        if not rows:
            result["error"] = "no_table_rows_parsed"
            return result

        records = table_rows_to_records(rows)
        if not records:
            result["status"] = "partial"
            result["structured_table"] = {
                "table_type": table_type,
                "rows": rows,
                "records": [],
            }
            result["summary_text"] = "表格原始行：\n" + "\n".join(
                f"{idx}. {' | '.join(row)}" for idx, row in enumerate(rows, start=1)
            )
            return result

        result["status"] = "success"
        result["structured_table"] = {
            "table_type": table_type,
            "rows": rows,
            "records": records,
        }
        result["summary_text"] = build_table_summary(records)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def apply_structured_table_fields(chunk_obj, table_text):
    parsed = parse_structured_table(table_text)
    chunk_obj["raw_content"] = table_text
    chunk_obj["table"] = table_text
    chunk_obj["structured_parse_status"] = parsed.get("status", "failed")

    if parsed.get("structured_table") is not None:
        chunk_obj["structured_table"] = parsed["structured_table"]
    if parsed.get("error"):
        chunk_obj["structured_parse_error"] = parsed["error"]
    if parsed.get("summary_text"):
        chunk_obj["content"] = parsed["summary_text"]
    else:
        chunk_obj["content"] = table_text
    return chunk_obj


def split_text_by_tables(text):
    """
    将文本分割为普通段落和表格段落（包括 Markdown 表格和 HTML 表格）。
    返回列表，每个元素为 (segment_text, is_table)
    """
    lines = text.splitlines()
    segments = []
    i = 0
    n = len(lines)

    def is_md_table_start(idx):
        if idx >= n - 1:
            return False
        line = lines[idx].strip()
        next_line = lines[idx + 1].strip()
        if '|' not in line or re.fullmatch(r'[\s|:-]+', line):
            return False
        if re.fullmatch(r'[\s|:-]+', next_line) and '|' in next_line:
            cells = [c.strip() for c in next_line.strip('|').split('|')]
            if any(re.fullmatch(r':?-{3,}:?', c) for c in cells if c):
                return True
        return False

    def is_html_table_start(idx):
        return '<table' in lines[idx].lower()

    def collect_table(start_idx, table_type):
        end_idx = start_idx
        if table_type == 'md':
            while end_idx < n:
                line = lines[end_idx].strip()
                if not line:
                    break
                if '|' not in line and not re.fullmatch(r'[\s|:-]+', line):
                    break
                end_idx += 1
        else:  # html
            depth = 1
            end_idx = start_idx + 1
            while end_idx < n and depth > 0:
                lower_line = lines[end_idx].lower()
                if '<table' in lower_line:
                    depth += 1
                if '</table>' in lower_line:
                    depth -= 1
                if depth > 0 and lower_line.lstrip().startswith('#'):
                    break
                end_idx += 1
        table_text = '\n'.join(lines[start_idx:end_idx])
        return end_idx, table_text

    while i < n:
        line = lines[i].strip()
        if is_md_table_start(i):
            end, table_text = collect_table(i, 'md')
            segments.append((table_text, True))
            i = end
        elif is_html_table_start(i):
            end, table_text = collect_table(i, 'html')
            segments.append((table_text, True))
            i = end
        else:
            start = i
            while i < n and not is_md_table_start(i) and not is_html_table_start(i):
                i += 1
            para_text = '\n'.join(lines[start:i])
            if para_text.strip():
                segments.append((para_text, False))
    return segments

def parse_markdown_hierarchy(
    content,
    chunk_size,
    doc_name,
    source_file,
    file_id,
    file_version_id,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    lines_with_breaks = content.splitlines(keepends=True)
    line_starts = []
    pos = 0
    for line in lines_with_breaks:
        line_starts.append(pos)
        pos += len(line)

    titles = []
    in_code_block = False
    for idx, line in enumerate(lines_with_breaks):
        stripped = line.rstrip('\n').lstrip()
        if stripped.startswith('```'):
            in_code_block = not in_code_block
        if not in_code_block and stripped.startswith('#'):
            level = len(stripped) - len(stripped.lstrip('#'))
            if level >= 1:
                start_pos = line_starts[idx]
                end_pos = start_pos + len(line)
                title_text = stripped.strip()
                titles.append((start_pos, end_pos, level, title_text))

    if not titles:
        all_chunks = []
        block_content = content
        if len(block_content) <= chunk_size:
            line_no = get_line_number(0, line_starts)
            chunk_obj = {
                "id": "0",
                "chunk_name": doc_name,
                "content": block_content,
                "chapter": "",
                "section": "",
                "subsection": "",
                "section_path": "0.0.0",
                "source": line_no,
                "file": source_file,
                "chunk_id": "0",
                "file_id": file_id,
                "file_version_id": file_version_id,
                "is_active": True,
                "chunk_uid": f"{file_version_id}::0"
            }
            chunk_obj.update(
                build_chunk_common_fields(
                    source_type=source_type,
                    file_format=file_format,
                    chunk_type=chunk_type,
                    source_record_type=source_record_type,
                    source_record_id=source_record_id,
                )
            )
            img_paths = extract_image_paths(block_content)
            if img_paths:
                chunk_obj["image_paths"] = img_paths
            if block_content.strip():
                all_chunks.append(chunk_obj)
        else:
            lines = block_content.splitlines(keepends=True)
            current_lines = []
            current_len = 0
            chunk_id = 0
            for line in lines:
                line_len = len(line)
                if current_len + line_len > chunk_size and current_lines:
                    chunk_text = ''.join(current_lines)
                    line_no = get_line_number(line_starts[0], line_starts)
                    chunk_obj = {
                        "id": str(chunk_id),
                        "chunk_name": doc_name,
                        "content": chunk_text,
                        "chapter": "",
                        "section": "",
                        "subsection": "",
                        "section_path": "0.0.0",
                        "source": line_no,
                        "file": source_file,
                        "chunk_id": str(chunk_id),
                        "file_id": file_id,
                        "file_version_id": file_version_id,
                        "is_active": True,
                        "chunk_uid": f"{file_version_id}::{chunk_id}"
                    }
                    chunk_obj.update(
                        build_chunk_common_fields(
                            source_type=source_type,
                            file_format=file_format,
                            chunk_type=chunk_type,
                            source_record_type=source_record_type,
                            source_record_id=source_record_id,
                        )
                    )
                    img_paths = extract_image_paths(chunk_text)
                    if img_paths:
                        chunk_obj["image_paths"] = img_paths
                    if chunk_text.strip():
                        all_chunks.append(chunk_obj)
                        chunk_id += 1
                    current_lines = []
                    current_len = 0
                current_lines.append(line)
                current_len += line_len
            if current_lines:
                chunk_text = ''.join(current_lines)
                line_no = get_line_number(line_starts[0], line_starts)
                chunk_obj = {
                    "id": str(chunk_id),
                    "chunk_name": doc_name,
                    "content": chunk_text,
                    "chapter": "",
                    "section": "",
                    "subsection": "",
                    "section_path": "0.0.0",
                    "source": line_no,
                    "file": source_file,
                    "chunk_id": str(chunk_id),
                    "file_id": file_id,
                    "file_version_id": file_version_id,
                    "is_active": True,
                    "chunk_uid": f"{file_version_id}::{chunk_id}"
                }
                chunk_obj.update(
                    build_chunk_common_fields(
                        source_type=source_type,
                        file_format=file_format,
                        chunk_type=chunk_type,
                        source_record_type=source_record_type,
                        source_record_id=source_record_id,
                    )
                )
                img_paths = extract_image_paths(chunk_text)
                if img_paths:
                    chunk_obj["image_paths"] = img_paths
                if chunk_text.strip():
                    all_chunks.append(chunk_obj)
        return all_chunks

    titles.append((len(content), len(content), 0, ""))
    all_chunks = []
    chunk_id = 0

    heading_counters = [0, 0, 0, 0, 0, 0, 0]
    cur_chapter_name = ""
    cur_section_name = ""
    cur_subsection_name = ""
    cur_chapter_num = 0
    cur_section_num = 0
    cur_subsection_num = 0

    for i in range(len(titles) - 1):
        title_start, title_end, level, title_text = titles[i]
        next_title_start = titles[i+1][0]

        if 2 <= level <= 6:
            heading_counters[level] += 1
            for j in range(level + 1, 7):
                heading_counters[j] = 0

        if level == 1:
            cur_chapter_name = title_text.lstrip('#').strip()
        elif level == 2:
            cur_chapter_name = title_text.lstrip('#').strip()
            cur_chapter_num = heading_counters[2]
            cur_section_name = ""
            cur_section_num = 0
            cur_subsection_name = ""
            cur_subsection_num = 0
        elif level == 3:
            cur_section_name = title_text.lstrip('#').strip()
            cur_section_num = heading_counters[3]
            cur_subsection_name = ""
            cur_subsection_num = 0
        elif level == 4:
            cur_subsection_name = title_text.lstrip('#').strip()
            cur_subsection_num = heading_counters[4]
        elif level > 4:
            cur_subsection_name = title_text.lstrip('#').strip()

        content_start = title_end
        content_end = next_title_start
        full_block = content[content_start:content_end].lstrip('\n')
        if not full_block.strip():
            continue

        source_line = get_line_number(title_start, line_starts)
        segments = split_text_by_tables(full_block)

        for seg_text, is_table in segments:
            if not seg_text.strip():
                continue

            if is_table:
                # 表格段落：不进行长度切分，直接作为一个块
                chunk_obj = {
                    "id": str(chunk_id),
                    "chunk_name": doc_name,
                    "content": seg_text,
                    "chapter": cur_chapter_name,
                    "section": cur_section_name,
                    "subsection": cur_subsection_name,
                    "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                    "source": source_line,
                    "file": source_file,
                    "chunk_id": str(chunk_id),
                    "file_id": file_id,
                    "file_version_id": file_version_id,
                    "is_active": True,
                    "chunk_uid": f"{file_version_id}::{chunk_id}",
                    "table": seg_text
                }
                apply_structured_table_fields(chunk_obj, seg_text)
                chunk_obj.update(
                    build_chunk_common_fields(
                        source_type=source_type,
                        file_format=file_format,
                        chunk_type=chunk_type,
                        source_record_type=source_record_type,
                        source_record_id=source_record_id,
                    )
                )
                img_paths = extract_image_paths(seg_text)
                if img_paths:
                    chunk_obj["image_paths"] = img_paths
                all_chunks.append(chunk_obj)
                chunk_id += 1
            else:
                # 普通段落：可能超过 chunk_size 需要切分
                if len(seg_text) <= chunk_size:
                    chunk_obj = {
                        "id": str(chunk_id),
                        "chunk_name": doc_name,
                        "content": seg_text,
                        "chapter": cur_chapter_name,
                        "section": cur_section_name,
                        "subsection": cur_subsection_name,
                        "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                        "source": source_line,
                        "file": source_file,
                        "chunk_id": str(chunk_id),
                        "file_id": file_id,
                        "file_version_id": file_version_id,
                        "is_active": True,
                        "chunk_uid": f"{file_version_id}::{chunk_id}"
                    }
                    chunk_obj.update(
                        build_chunk_common_fields(
                            source_type=source_type,
                            file_format=file_format,
                            chunk_type=chunk_type,
                            source_record_type=source_record_type,
                            source_record_id=source_record_id,
                        )
                    )
                    img_paths = extract_image_paths(seg_text)
                    if img_paths:
                        chunk_obj["image_paths"] = img_paths
                    all_chunks.append(chunk_obj)
                    chunk_id += 1
                else:
                    lines = seg_text.splitlines(keepends=True)
                    current_lines = []
                    current_len = 0
                    for line in lines:
                        line_len = len(line)
                        if current_len + line_len > chunk_size and current_lines:
                            chunk_text = ''.join(current_lines)
                            if chunk_text.strip():
                                chunk_obj = {
                                    "id": str(chunk_id),
                                    "chunk_name": doc_name,
                                    "content": chunk_text,
                                    "chapter": cur_chapter_name,
                                    "section": cur_section_name,
                                    "subsection": cur_subsection_name,
                                    "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                                    "source": source_line,
                                    "file": source_file,
                                    "chunk_id": str(chunk_id),
                                    "file_id": file_id,
                                    "file_version_id": file_version_id,
                                    "is_active": True,
                                    "chunk_uid": f"{file_version_id}::{chunk_id}"
                                }
                                chunk_obj.update(
                                    build_chunk_common_fields(
                                        source_type=source_type,
                                        file_format=file_format,
                                        chunk_type=chunk_type,
                                        source_record_type=source_record_type,
                                        source_record_id=source_record_id,
                                    )
                                )
                                img_paths = extract_image_paths(chunk_text)
                                if img_paths:
                                    chunk_obj["image_paths"] = img_paths
                                all_chunks.append(chunk_obj)
                                chunk_id += 1
                            current_lines = []
                            current_len = 0
                        current_lines.append(line)
                        current_len += line_len
                    if current_lines:
                        chunk_text = ''.join(current_lines)
                        if chunk_text.strip():
                            chunk_obj = {
                                "id": str(chunk_id),
                                "chunk_name": doc_name,
                                "content": chunk_text,
                                "chapter": cur_chapter_name,
                                "section": cur_section_name,
                                "subsection": cur_subsection_name,
                                "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                                "source": source_line,
                                "file": source_file,
                                "chunk_id": str(chunk_id),
                                "file_id": file_id,
                                "file_version_id": file_version_id,
                                "is_active": True,
                                "chunk_uid": f"{file_version_id}::{chunk_id}"
                            }
                            chunk_obj.update(
                                build_chunk_common_fields(
                                    source_type=source_type,
                                    file_format=file_format,
                                    chunk_type=chunk_type,
                                    source_record_type=source_record_type,
                                    source_record_id=source_record_id,
                                )
                            )
                            img_paths = extract_image_paths(chunk_text)
                            if img_paths:
                                chunk_obj["image_paths"] = img_paths
                            all_chunks.append(chunk_obj)
                            chunk_id += 1

    return all_chunks

def process_single_document_flow(
    input_file_path,
    chunk_size,
    file_id,
    file_version_id,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    doc_name, content = load_single_file(input_file_path)
    if not content:
        print("内容为空，跳过处理。")
        return []
    all_chunks = parse_markdown_hierarchy(
        content,
        chunk_size,
        doc_name,
        os.path.basename(input_file_path),
        file_id,
        file_version_id,
        source_type,
        file_format,
        chunk_type,
        source_record_type,
        source_record_id,
    )
    return all_chunks

def main():
    parser = argparse.ArgumentParser(description='Markdown文档分块处理工具（支持表格保护）')
    parser.add_argument('--input', '-i', required=True, help='输入Markdown文件的完整路径 (.md)')
    parser.add_argument('--output', '-o', required=True, help='输出JSON文件路径')
    parser.add_argument('--chunk_size', '-s', type=int, default=800, help='分块大小（字符数）')
    parser.add_argument('--file_id', required=True, help='文件ID')
    parser.add_argument('--file_version_id', required=True, help='文件版本ID')
    parser.add_argument(
        '--source_type',
        default='manual_document',
        choices=['manual_document', 'standard_document', 'work_order', 'maintenance_record', 'time_series_event'],
        help='chunk 来源业务类型',
    )
    parser.add_argument(
        '--file_format',
        required=True,
        help='原始输入文件格式，如 pdf / md / xlsx / csv / docx / parquet',
    )
    parser.add_argument(
        '--chunk_type',
        default='document_section',
        choices=['document_section', 'table_row_summary', 'case_summary', 'sensor_event_summary'],
        help='chunk 内容形态',
    )
    parser.add_argument('--source_record_type', default=None, help='原始记录类型，文档类可为空')
    parser.add_argument('--source_record_id', default=None, help='原始记录ID，文档类可为空')
    args = parser.parse_args()

    start_time = time.time()

    if os.path.isdir(args.input):
        print(f"❌ 错误: 输入路径 '{args.input}' 是一个文件夹，请提供具体的文件路径。")
        return

    chunks = process_single_document_flow(
        args.input,
        args.chunk_size,
        args.file_id,
        args.file_version_id,
        args.source_type,
        args.file_format,
        args.chunk_type,
        args.source_record_type,
        args.source_record_id,
    )
    if chunks:
        save_json(chunks, args.output)
        print(f"✅ 文档分块完成，结果已保存到 {args.output}")
        print(f"📊 共生成 {len(chunks)} 个文本块")
    else:
        print("⚠️ 未生成任何分块结果")

    elapsed = time.time() - start_time
    print(f"\n=== 文档分块耗时: {elapsed:.2f} 秒 ===")

if __name__ == "__main__":
    main()
