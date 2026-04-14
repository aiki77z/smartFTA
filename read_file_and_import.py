#!/usr/bin/env python3
"""
完整流水线：PDF → Markdown → 分块 → 实体提取 → 关系提取 → 导入 MongoDB + Neo4j
支持两种模式：
  1. 完整模式：从 PDF 开始生成所有数据并导入
  2. 导入模式：指定已有数据目录，直接导入 chunks/实体/关系文件到数据库

脚本位置：根目录（与 knowledge_base_construction/ 和 validator-service/ 同级）
"""

import argparse
import json
import sys
import subprocess
import time
import importlib
from pathlib import Path

# 获取根目录（脚本所在目录）
ROOT_DIR = Path(__file__).parent.absolute()
KB_SCRIPTS_DIR = ROOT_DIR / "knowledge_base_construction"
VALIDATOR_DIR = ROOT_DIR / "validator-service"

sys.path.insert(0, str(VALIDATOR_DIR))

import database
import config

try:
    import import_relations_to_neo4j as neo4j_importer
    NEO4J_AVAILABLE = True
except ImportError as e:
    print(f"警告: 无法导入 neo4j 导入模块，Neo4j 功能将被禁用。错误: {e}")
    NEO4J_AVAILABLE = False


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
    for _ in range(30):
        if candidate.exists() and candidate.stat().st_size > 100:
            return candidate
        time.sleep(1)
    raise RuntimeError(f"MD 文件生成失败或为空: {candidate}")


def import_chunks_from_file(file_path: Path):
    """从 JSON 文件导入 chunks 到 MongoDB"""
    print(f"\n=== 导入 chunks 到 MongoDB (来自 {file_path}) ===")
    with open(file_path, "r", encoding="utf-8") as f:
        chunks_data = json.load(f)
    if not isinstance(chunks_data, list):
        chunks_data = [chunks_data]
    database.import_chunks(chunks_data)
    print(f"已导入 {len(chunks_data)} 个 chunks")


def import_entities_from_file(file_path: Path):
    """从合并后的实体 JSON 文件导入实体反向索引到 MongoDB"""
    print(f"\n=== 导入实体反向索引到 MongoDB (来自 {file_path}) ===")
    with open(file_path, "r", encoding="utf-8") as f:
        entities_data = json.load(f)
    if not isinstance(entities_data, list):
        entities_data = [entities_data]
    for entry in entities_data:
        if "chunk_ids" in entry:
            entry["chunk_ids"] = [cid for cid in entry["chunk_ids"] if cid]
    database.import_entity_reverse_index(entities_data)
    print(f"已导入 {len(entities_data)} 个实体条目")


def import_relations_to_neo4j(relations_file: Path, args):
    """调用 Neo4j 导入模块导入关系数据"""
    print(f"\n=== 导入关系到 Neo4j (来自 {relations_file}) ===")
    if not NEO4J_AVAILABLE:
        print("跳过: Neo4j 模块不可用")
        return
    if not relations_file.exists():
        print(f"跳过: 关系文件不存在 {relations_file}")
        return
    rows = neo4j_importer.load_json(relations_file)
    if not rows:
        print("警告: 关系文件中未找到有效的关系数据，跳过 Neo4j 导入。")
        return
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        driver.verify_connectivity()
        neo4j_importer.ensure_constraints(driver, args.neo4j_database)
        if args.neo4j_clear:
            print("清空 Neo4j 图数据库...")
            neo4j_importer.clear_graph(driver, args.neo4j_database)
        relation_count = neo4j_importer.import_rows(driver, args.neo4j_database, rows, batch_size=200)
        print(f"成功导入 {len(rows)} 个 chunk 行和 {relation_count} 条 RELATION 关系到 Neo4j 数据库 '{args.neo4j_database}'。")
        neo4j_importer.print_summary(driver, args.neo4j_database)
    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(description="PDF 知识抽取 + MongoDB + Neo4j 导入流水线")
    parser.add_argument("--pdf", "-p", required=True, help="输入的 PDF 文件路径（或用于命名前缀）")
    parser.add_argument("--output-dir", "-o", default="./output", help="临时输出根目录（默认 ./output）")
    parser.add_argument("--chunk-size", "-s", type=int, default=800, help="分块大小（字符数），默认 800")
    parser.add_argument("--skip-mineru", action="store_true", help="跳过 MinerU 转换（假设已有 MD 文件）")
    parser.add_argument("--skip-entity", action="store_true", help="跳过实体提取（只导入 chunks）")
    parser.add_argument("--skip-relation", action="store_true", help="跳过关系统取（只到实体导入）")
    parser.add_argument("--print-raw-text", action="store_true", help="打印 LLM 原始返回文本（调试用）")

    # 导入模式：直接从已有数据目录导入，跳过所有生成步骤
    parser.add_argument("--import-only-dir", help="直接从此目录导入已有的 chunks/实体/关系文件（跳过生成步骤）")

    # MongoDB 连接参数
    parser.add_argument("--mongo-uri", help="MongoDB URI，如 mongodb://localhost:27017")
    parser.add_argument("--db-name", help="MongoDB 数据库名称")

    # Neo4j 连接参数
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7687", help="Neo4j URI")
    parser.add_argument("--neo4j-user", default="neo4j", help="Neo4j 用户名")
    parser.add_argument("--neo4j-password", help="Neo4j 密码（必填以启用 Neo4j 导入）")
    parser.add_argument("--neo4j-database", default="neo4j", help="Neo4j 数据库名")
    parser.add_argument("--neo4j-clear", action="store_true", help="导入前清空 Neo4j 图数据库")
    parser.add_argument("--skip-neo4j", action="store_true", help="跳过 Neo4j 导入")

    args = parser.parse_args()

    # 处理 MongoDB 配置覆盖
    if args.mongo_uri or args.db_name:
        try:
            if args.mongo_uri:
                config.MONGO_URI = args.mongo_uri
            if args.db_name:
                config.MONGO_DB_NAME = args.db_name
            importlib.reload(database)
            print(f"MongoDB 配置已更新: URI={config.MONGO_URI}, DB={config.MONGO_DB_NAME}")
        except Exception as e:
            print(f"警告：无法覆盖 MongoDB 配置，将使用原有配置: {e}")

    # 获取基础文件名 stem（从 --pdf 路径提取，即使文件不存在）
    pdf_path = Path(args.pdf)
    pdf_stem = pdf_path.stem

    # 判断是否为导入模式
    import_only_mode = args.import_only_dir is not None
    if import_only_mode:
        data_dir = Path(args.import_only_dir).resolve()
        if not data_dir.exists():
            print(f"错误: 导入目录不存在: {data_dir}")
            sys.exit(1)
        print(f"=== 导入模式：从目录 {data_dir} 读取已有数据文件 ===")
        # 构建文件路径
        chunks_file = data_dir / f"{pdf_stem}_chunks.json"
        entities_file = data_dir / f"{pdf_stem}_entities_merged.json"
        relations_file = data_dir / f"{pdf_stem}_relations.jsonl"

        # 检查必要文件
        if not chunks_file.exists():
            print(f"错误: chunks 文件不存在: {chunks_file}")
            sys.exit(1)
        # 导入 chunks
        import_chunks_from_file(chunks_file)

        # 实体导入
        if not args.skip_entity:
            if not entities_file.exists():
                print(f"警告: 实体文件不存在: {entities_file}，跳过实体导入")
            else:
                import_entities_from_file(entities_file)
        else:
            print("已跳过实体导入 (--skip-entity)")

        # 关系导入到 Neo4j
        neo4j_enabled = (args.neo4j_password is not None and not args.skip_neo4j and NEO4J_AVAILABLE)
        if not args.skip_relation and neo4j_enabled:
            if not relations_file.exists():
                print(f"警告: 关系文件不存在: {relations_file}，跳过 Neo4j 导入")
            else:
                import_relations_to_neo4j(relations_file, args)
        else:
            if args.skip_relation:
                print("已跳过关系统取 (--skip-relation)，不导入 Neo4j")
            elif not neo4j_enabled:
                print("跳过 Neo4j 导入: 未提供密码或模块不可用")
        print("\n=== 导入模式执行完成 ===")
        return

    # ---------- 以下为完整生成模式（原有流程） ----------
    # 确保输出目录存在
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    result_dir = output_root / pdf_stem
    result_dir.mkdir(parents=True, exist_ok=True)

    # 步骤1: PDF -> Markdown
    if not args.skip_mineru:
        print("\n=== 步骤1: PDF 转 Markdown (MinerU) ===")
        mineru_script = KB_SCRIPTS_DIR / "trans_file_to_md.py"
        if not mineru_script.exists():
            print(f"错误: 找不到 {mineru_script}")
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
            print(f"Markdown 文件: {md_file}")
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

    # 步骤2: 分块
    print("\n=== 步骤2: Markdown 分块 ===")
    chunk_script = KB_SCRIPTS_DIR / "chunk_md.py"
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
    import_chunks_from_file(chunks_json)

    if args.skip_entity:
        print("已跳过实体提取，流程结束。")
        return

    # 步骤3: 实体提取
    print("\n=== 步骤3: 实体提取 ===")
    entity_script = KB_SCRIPTS_DIR / "extract_entities.py"
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

    # 导入实体反向索引
    import_entities_from_file(merged_json)

    if args.skip_relation:
        print("已跳过关系统取，流程结束。")
        return

    # 步骤4: 关系提取
    print("\n=== 步骤4: 关系提取 ===")
    relation_script = KB_SCRIPTS_DIR / "extract_relations.py"
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

    # 步骤5: Neo4j 导入
    neo4j_enabled = (args.neo4j_password is not None and not args.skip_neo4j and NEO4J_AVAILABLE)
    if neo4j_enabled:
        import_relations_to_neo4j(relations_json, args)
    else:
        if not NEO4J_AVAILABLE:
            print("跳过 Neo4j 导入: 模块不可用")
        elif args.skip_neo4j:
            print("跳过 Neo4j 导入 (--skip-neo4j)")
        elif args.neo4j_password is None:
            print("跳过 Neo4j 导入: 未提供 --neo4j-password")

    print("\n=== 流水线执行完成 ===")
    print(f"结果目录: {result_dir}")
    print(f"已导入 MongoDB: chunks 和实体索引")
    if neo4j_enabled:
        print(f"已导入 Neo4j: {args.neo4j_uri} (数据库: {args.neo4j_database})")


if __name__ == "__main__":
    main()