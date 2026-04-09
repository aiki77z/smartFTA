import os
import re
import json
import argparse
import bisect

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

def extract_image_path(text):
    """从给定文本中提取第一个 Markdown 图片路径，若无则返回空字符串"""
    match = re.search(r'!\[[^\]]*\]\(([^\s\)]+)(?:\s+["\'][^"\']*["\'])?\)', text)
    return match.group(1) if match else ""

def get_line_number(pos, line_starts):
    idx = bisect.bisect_right(line_starts, pos) - 1
    return idx + 1

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
        """判断是否为 Markdown 表格起始行（当前行包含 |，且下一行是分隔线）"""
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
                if '<table' in lines[end_idx].lower():
                    depth += 1
                if '</table>' in lines[end_idx].lower():
                    depth -= 1
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

def parse_markdown_hierarchy(content, chunk_size, doc_name, source_file):
    """
    分块策略：
    1. 按标题（#）划分文档，每个标题及其后续内容构成一个逻辑块。
    2. 每个逻辑块内，先通过 split_text_by_tables 分离表格和普通段落。
    3. 表格段整体保留为一个块（不切分），普通段若超长则按换行符二次分割。
    """
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
                "id": 0,
                "chunk_name": doc_name,
                "key_word": "",
                "content": block_content,
                "chapter": "",
                "section": "",
                "subsection": "",
                "section_path": "0.0.0",
                "source": line_no,
                "file": source_file
            }
            img = extract_image_path(block_content)
            if img:
                chunk_obj["image_path"] = img
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
                        "id": chunk_id,
                        "chunk_name": doc_name,
                        "key_word": "",
                        "content": chunk_text,
                        "chapter": "",
                        "section": "",
                        "subsection": "",
                        "section_path": "0.0.0",
                        "source": line_no,
                        "file": source_file
                    }
                    img = extract_image_path(chunk_text)
                    if img:
                        chunk_obj["image_path"] = img
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
                    "id": chunk_id,
                    "chunk_name": doc_name,
                    "key_word": "",
                    "content": chunk_text,
                    "chapter": "",
                    "section": "",
                    "subsection": "",
                    "section_path": "0.0.0",
                    "source": line_no,
                    "file": source_file
                }
                img = extract_image_path(chunk_text)
                if img:
                    chunk_obj["image_path"] = img
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
                chunk_obj = {
                    "id": chunk_id,
                    "chunk_name": doc_name,
                    "key_word": "",
                    "content": seg_text,
                    "chapter": cur_chapter_name,
                    "section": cur_section_name,
                    "subsection": cur_subsection_name,
                    "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                    "source": source_line,
                    "file": source_file,
                    "table": seg_text
                }
                img = extract_image_path(seg_text)
                if img:
                    chunk_obj["image_path"] = img
                all_chunks.append(chunk_obj)
                chunk_id += 1
            else:
                if len(seg_text) <= chunk_size:
                    chunk_obj = {
                        "id": chunk_id,
                        "chunk_name": doc_name,
                        "key_word": "",
                        "content": seg_text,
                        "chapter": cur_chapter_name,
                        "section": cur_section_name,
                        "subsection": cur_subsection_name,
                        "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                        "source": source_line,
                        "file": source_file
                    }
                    img = extract_image_path(seg_text)
                    if img:
                        chunk_obj["image_path"] = img
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
                                    "id": chunk_id,
                                    "chunk_name": doc_name,
                                    "key_word": "",
                                    "content": chunk_text,
                                    "chapter": cur_chapter_name,
                                    "section": cur_section_name,
                                    "subsection": cur_subsection_name,
                                    "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                                    "source": source_line,
                                    "file": source_file
                                }
                                img = extract_image_path(chunk_text)
                                if img:
                                    chunk_obj["image_path"] = img
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
                                "id": chunk_id,
                                "chunk_name": doc_name,
                                "key_word": "",
                                "content": chunk_text,
                                "chapter": cur_chapter_name,
                                "section": cur_section_name,
                                "subsection": cur_subsection_name,
                                "section_path": f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
                                "source": source_line,
                                "file": source_file
                            }
                            img = extract_image_path(chunk_text)
                            if img:
                                chunk_obj["image_path"] = img
                            all_chunks.append(chunk_obj)
                            chunk_id += 1

    return all_chunks

def process_single_document_flow(input_file_path, chunk_size):
    doc_name, content = load_single_file(input_file_path)
    if not content:
        print("内容为空，跳过处理。")
        return []
    all_chunks = parse_markdown_hierarchy(
        content, chunk_size, doc_name, os.path.basename(input_file_path)
    )
    return all_chunks

def main():
    parser = argparse.ArgumentParser(description='Markdown文档分块处理工具（支持表格保护）')
    parser.add_argument('--input', '-i', required=True, help='输入Markdown文件的完整路径 (.md)')
    parser.add_argument('--output', '-o', required=True, help='输出JSON文件路径')
    parser.add_argument('--chunk_size', '-s', type=int, default=800, help='分块大小（字符数）')
    args = parser.parse_args()

    if os.path.isdir(args.input):
        print(f"❌ 错误: 输入路径 '{args.input}' 是一个文件夹，请提供具体的文件路径。")
        return

    chunks = process_single_document_flow(args.input, args.chunk_size)
    if chunks:
        save_json(chunks, args.output)
        print(f"✅ 文档分块完成，结果已保存到 {args.output}")
        print(f"📊 共生成 {len(chunks)} 个文本块")
    else:
        print("⚠️ 未生成任何分块结果")

if __name__ == "__main__":
    main()