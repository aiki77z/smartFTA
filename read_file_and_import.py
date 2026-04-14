#!/usr/bin/env python3
"""
完整流水线：PDF → Markdown → 分块 → 实体提取 → 关系提取 → 导入 MongoDB（仅 chunks 和实体索引）

脚本位置：根目录（与 knowledge_base_construction/ 和 validator-service/ 同级）
依赖：
    - knowledge_base_construction/output/ 下的脚本：
        trans_file_to_md.py, chunk_md.py, extract_entities.py, extract_relations.py
    - validator-service/database.py 及 config.py（需配置 MongoDB）
    - 外部命令：mineru（MinerU CLI）
    - Python 包：pymongo

使用方法：
    python pipeline_full_with_import.py --pdf document.pdf --mongo-uri mongodb://localhost:27017 --db-name mydb
"""

import argparse
import json
import sys
import subprocess
import time
from pathlib import Path

# 获取根目录（脚本所在目录）
ROOT_DIR = Path(__file__).parent.absolute()
KB_OUTPUT_DIR = ROOT_DIR / "knowledge_base_construction" / "output"
VALIDATOR_DIR = ROOT_DIR / "validator-service"

# 将 validator-service 添加到 Python 路径，以便导入 database 模块
sys.path.insert(0, str(VALIDATOR_DIR))

# 导入 database 模块（必须已配置 config.py）
try:
    from database import import_chunks, import_entity_reverse_index
except ImportError as e:
    print(f"无法导入 database 模块，请检查 validator-service 路径和 config.py: {e}")
    sys.exit(1)


def run_command(cmd, description, cwd=None):
    """执行命令，安全处理 UTF-8 输出"""
    print(f"\n>>> {description}")
    print(f"命令: {' '.join(cmd)}")
    env = dict(subprocess.os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, capture_output=True, text=False, env=env, cwd=cwd)
    stdout = result.stdout.decode('utf-8', errors='replace')
    stderr = result.stderr.decode('utf-8', errors='replace')
    if result.returncode != 0:
        print(f"错误: {description} 失败 (返回码 {result.returncode})")
        if stderr:
            print("stderr:", stderr)
        if stdout:
            print("stdout:", stdout)
        sys.exit(1)
    if stdout:
        print(stdout)
    if stderr:
        print(stderr)
    return result


def find_md_file(output_dir, pdf_stem):
    """在 MinerU 输出目录中查找生成的 .md 文件"""
    candidate = output_dir / pdf_stem / f"{pdf_stem}.md"
    if not candidate.exists():
        candidate = output_dir / pdf_stem / "ocr" / f"{pdf_stem}.md"
    if not candidate.exists():
        candidate = output_dir / f"{pdf_stem}.md"
    if not candidate.exists():
        md_files = list(output_dir.rglob("*.md"))
        if md_files:
            candidate = md_files[0]
        else:
            raise FileNotFoundError(f"未在 {output_dir} 中找到任何 .md 文件")
    # 等待文件写入完成
    for _ in range(30):
        if candidate.exists() and candidate.stat().st_size > 100:
            return candidate
        time.sleep(1)
    raise RuntimeError(f"MD 文件生成失败或为空: {candidate}")


def main():
    parser = argparse.ArgumentParser(description="PDF 知识抽取 + MongoDB 导入流水线（含关系提取，但不导入关系）")
    parser.add_argument("--pdf", "-p", required=True, help="输入的 PDF 文件路径")
    parser.add_argument("--output-dir", "-o", default="./output", help="临时输出根目录（默认 ./output）")
    parser.add_argument("--chunk-size", "-s", type=int, default=800, help="分块大小（字符数），默认 800")
    parser.add_argument("--skip-mineru", action="store_true", help="跳过 MinerU 转换（假设已有 MD 文件）")
    parser.add_argument("--skip-entity", action="store_true", help="跳过实体提取（只导入 chunks）")
    parser.add_argument("--skip-relation", action="store_true", help="跳过关系统取（只到实体导入）")
    parser.add_argument("--print-raw-text", action="store_true", help="打印 LLM 原始返回文本（调试用）")
    # MongoDB 连接参数（会覆盖 config.py 中的配置）
    parser.add_argument("--mongo-uri", help="MongoDB URI，如 mongodb://localhost:27017")
    parser.add_argument("--db-name", help="MongoDB 数据库名称")
    args = parser.parse_args()

    # 覆盖 database 模块中的全局配置（如果提供了参数）
    if args.mongo_uri or args.db_name:
        try:
            import config
            if args.mongo_uri:
                config.MONGO_URI = args.mongo_uri
            if args.db_name:
                config.MONGO_DB_NAME = args.db_name
            # 重新加载 database 模块使配置生效
            import importlib
            importlib.reload(config)
            importlib.reload(database)
            from database import import_chunks, import_entity_reverse_index
        except Exception as e:
            print(f"警告：无法覆盖 MongoDB 配置，将使用 config.py 中的默认值: {e}")

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"错误: PDF 文件不存在 {pdf_path}")
        sys.exit(1)

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    pdf_stem = pdf_path.stem
    result_dir = output_root / pdf_stem
    result_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 步骤1: PDF -> Markdown ----------
    if not args.skip_mineru:
        print("\n=== 步骤1: PDF 转 Markdown (MinerU) ===")
        mineru_script = KB_OUTPUT_DIR / "trans_file_to_md.py"
        if not mineru_script.exists():
            print(f"错误: 找不到 {mineru_script}，请确认 knowledge_base_construction/output/ 目录存在")
            sys.exit(1)
        cmd = [
            sys.executable, str(mineru_script),
            "-i", str(pdf_path),
            "-o", str(output_root),
            "-b", "pipeline",
            "-m", "ocr"
        ]
        run_command(cmd, "MinerU 转换")
        try:
            md_file = find_md_file(output_root, pdf_stem)
            print(f"Markdown 文件: {md_file} (大小: {md_file.stat().st_size} 字节)")
        except (FileNotFoundError, RuntimeError) as e:
            print(f"错误: {e}")
            sys.exit(1)
    else:
        try:
            md_file = find_md_file(output_root, pdf_stem)
            print(f"使用现有 Markdown 文件: {md_file}")
        except (FileNotFoundError, RuntimeError) as e:
            print(f"错误: {e}")
            sys.exit(1)

    # ---------- 步骤2: Markdown 分块 ----------
    print("\n=== 步骤2: Markdown 分块 ===")
    chunk_script = KB_OUTPUT_DIR / "chunk_md.py"
    if not chunk_script.exists():
        print(f"错误: 找不到 {chunk_script}")
        sys.exit(1)
    chunks_json = result_dir / f"{pdf_stem}_chunks.json"
    cmd_chunk = [
        sys.executable, str(chunk_script),
        "--input", str(md_file),
        "--output", str(chunks_json),
        "--chunk_size", str(args.chunk_size)
    ]
    run_command(cmd_chunk, "文档分块")
    print(f"分块结果保存至: {chunks_json}")

    # 导入 chunks 到 MongoDB
    print("\n=== 导入 chunks 到 MongoDB ===")
    with open(chunks_json, "r", encoding="utf-8") as f:
        chunks_data = json.load(f)
    if not isinstance(chunks_data, list):
        chunks_data = [chunks_data]
    import_chunks(chunks_data)
    print(f"已导入 {len(chunks_data)} 个 chunks")

    if args.skip_entity:
        print("已跳过实体提取，流程结束。")
        return

    # ---------- 步骤3: 实体提取 ----------
    print("\n=== 步骤3: 实体提取 ===")
    entity_script = KB_OUTPUT_DIR / "extract_entities.py"
    if not entity_script.exists():
        print(f"错误: 找不到 {entity_script}")
        sys.exit(1)
    entities_jsonl = result_dir / f"{pdf_stem}_entities.jsonl"
    merged_json = result_dir / f"{pdf_stem}_entities_merged.json"
    cmd_entity = [
        sys.executable, str(entity_script),
        "--input", str(chunks_json),
        "--output-entities", str(entities_jsonl),
        "--output-merged", str(merged_json)
    ]
    if args.print_raw_text:
        cmd_entity.append("--print-raw-text")
    run_command(cmd_entity, "实体提取")
    print(f"逐块实体: {entities_jsonl}")
    print(f"合并实体: {merged_json}")

    # 导入实体反向索引到 MongoDB
    print("\n=== 导入实体反向索引到 MongoDB ===")
    with open(merged_json, "r", encoding="utf-8") as f:
        entities_data = json.load(f)
    if not isinstance(entities_data, list):
        entities_data = [entities_data]
    # 清理空 chunk_ids（可选）
    for entry in entities_data:
        if "chunk_ids" in entry:
            entry["chunk_ids"] = [cid for cid in entry["chunk_ids"] if cid]
    import_entity_reverse_index(entities_data)
    print(f"已导入 {len(entities_data)} 个实体条目")

    if args.skip_relation:
        print("已跳过关系统取，流程结束。")
        return

    # ---------- 步骤4: 关系提取（仅生成文件，不导入数据库） ----------
    print("\n=== 步骤4: 关系提取（仅生成文件，不导入） ===")
    relation_script = KB_OUTPUT_DIR / "extract_relations.py"
    if not relation_script.exists():
        print(f"错误: 找不到 {relation_script}")
        sys.exit(1)

    relations_json = result_dir / f"{pdf_stem}_relations.jsonl"
    relations_csv = result_dir / f"{pdf_stem}_relations.csv"

    cmd_relation = [
        sys.executable, str(relation_script),
        "--input-chunks", str(entities_jsonl),
        "--input-entities", str(merged_json),
        "--output-relations", str(relations_json),
        "--output-csv", str(relations_csv)
    ]
    if args.print_raw_text:
        cmd_relation.append("--print-raw-text")
    run_command(cmd_relation, "关系提取")
    print(f"关系 JSON: {relations_json}")
    print(f"关系 CSV:  {relations_csv}")

    print("\n=== 流水线执行完成 ===")
    print(f"结果目录: {result_dir}")
    print(f"已导入数据库：")
    print(f"  - Chunks 集合: 'chunks'")
    print(f"  - 实体索引集合: 'entity_reverse_index'")
    print(f"关系数据已生成文件，未导入数据库：")


if __name__ == "__main__":
    main()