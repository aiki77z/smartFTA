"""
完整流水线：PDF -> Markdown -> 文档分块 -> 实体提取 -> 关系提取
依赖脚本（需与本文位于同一目录或可导入）：
    trans_file_to_md.py, chunk_md.py, extract_entities.py, extract_relations.py,
    generate_prompt_relation.py, llm_caller_relation.py
外部依赖：mineru 命令行工具，OpenAI 兼容 API（需配置环境变量）
"""

import os
import sys
import subprocess
import argparse
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.absolute()


def run_command(cmd, description):
    """
    执行 shell 命令，使用字节流捕获输出并手动 UTF-8 解码，
    彻底避免 Windows 下 GBK 编码导致的 UnicodeDecodeError
    """
    print(f"\n>>> {description}")
    print(f"命令: {' '.join(cmd)}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(cmd, capture_output=True, text=False, env=env)

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
    """在 mineru 输出目录中查找生成的 .md 文件，并等待其写入完成"""
    # mineru 默认生成: output_dir/pdf_stem/pdf_stem.md
    candidate = output_dir / pdf_stem / f"{pdf_stem}.md"
    if not candidate.exists():
        candidate = output_dir / pdf_stem / "ocr" / f"{pdf_stem}.md"
    if not candidate.exists():
        candidate = output_dir / f"{pdf_stem}.md"
    if not candidate.exists():
        # 递归搜索所有 .md 文件（取第一个）
        md_files = list(output_dir.rglob("*.md"))
        if md_files:
            candidate = md_files[0]
        else:
            raise FileNotFoundError(f"未在 {output_dir} 中找到任何 .md 文件")

    # 等待文件内容非空（MinerU 可能异步写入）
    wait_seconds = 30
    for _ in range(wait_seconds):
        if candidate.exists() and candidate.stat().st_size > 100:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"MD 文件生成失败或为空: {candidate}")

    return candidate


def main():
    parser = argparse.ArgumentParser(
        description="PDF 知识抽取完整流水线：PDF → MD → 分块 → 实体 → 关系"
    )
    parser.add_argument("--pdf", "-p", required=True, help="输入的 PDF 文件路径")
    parser.add_argument("--output-dir", "-o", default="./output",
                        help="输出根目录（默认 ./output），所有结果将保存在 output_dir/pdf_stem/ 下")
    parser.add_argument("--chunk-size", "-s", type=int, default=800,
                        help="分块大小（字符数），默认 800")
    parser.add_argument("--skip-mineru", action="store_true",
                        help="跳过 MinerU 转换步骤（假设已有 MD 文件）")
    parser.add_argument("--skip-entity", action="store_true",
                        help="跳过实体提取（只执行到分块）")
    parser.add_argument("--skip-relation", action="store_true",
                        help="跳过关系统取（只执行到实体合并）")
    parser.add_argument("--print-raw-text", action="store_true",
                        help="打印 LLM 返回的原始文本（用于调试）")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"错误: PDF 文件不存在 {pdf_path}")
        sys.exit(1)

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    pdf_stem = pdf_path.stem  # 不含扩展名的文件名

    # 定义结果存放目录：output_root/pdf_stem/
    result_dir = output_root / pdf_stem
    result_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 步骤1: PDF -> Markdown (调用 trans_file_to_md.py) ----------
    md_file = None
    if not args.skip_mineru:
        print("\n=== 步骤1: PDF 转 Markdown (MinerU) ===")
        mineru_script = SCRIPT_DIR / "trans_file_to_md.py"
        if not mineru_script.exists():
            print(f"错误: 找不到 {mineru_script}，请确保脚本位于同一目录")
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
            print(f"找到 Markdown 文件: {md_file} (大小: {md_file.stat().st_size} 字节)")
        except (FileNotFoundError, RuntimeError) as e:
            print(f"错误: {e}")
            sys.exit(1)
    else:
        try:
            md_file = find_md_file(output_root, pdf_stem)
            print(f"使用现有 Markdown 文件: {md_file}")
        except (FileNotFoundError, RuntimeError) as e:
            print(f"错误: 找不到有效的 MD 文件 - {e}")
            sys.exit(1)

    # ---------- 步骤2: Markdown 分块 (调用 chunk_md.py) ----------
    print("\n=== 步骤2: Markdown 分块 ===")
    chunk_script = SCRIPT_DIR / "chunk_md.py"
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

    if args.skip_entity:
        print("已跳过实体提取，流程结束。")
        return

    # ---------- 步骤3: 实体提取 (调用 extract_entities.py) ----------
    print("\n=== 步骤3: 实体提取 ===")
    entity_script = SCRIPT_DIR / "extract_entities.py"
    if not entity_script.exists():
        print(f"错误: 找不到 {entity_script}")
        sys.exit(1)

    entities_json = result_dir / f"{pdf_stem}_entities.jsonl"
    merged_json = result_dir / f"{pdf_stem}_entities_merged.json"

    cmd_entity = [
        sys.executable, str(entity_script),
        "--input", str(chunks_json),
        "--output-entities", str(entities_json),
        "--output-merged", str(merged_json)
    ]
    if args.print_raw_text:
        cmd_entity.append("--print-raw-text")
    run_command(cmd_entity, "实体提取")
    print(f"实体结果: {entities_json}")
    print(f"合并实体: {merged_json}")

    if args.skip_relation:
        print("已跳过关系统取，流程结束。")
        return

    # ---------- 步骤4: 关系提取 (调用 extract_relations.py) ----------
    print("\n=== 步骤4: 关系提取 ===")
    relation_script = SCRIPT_DIR / "extract_relations.py"
    if not relation_script.exists():
        print(f"错误: 找不到 {relation_script}")
        sys.exit(1)

    relations_json = result_dir / f"{pdf_stem}_relations.jsonl"
    relations_csv = result_dir / f"{pdf_stem}_relations.csv"

    cmd_relation = [
        sys.executable, str(relation_script),
        "--input-chunks", str(entities_json),
        "--input-entities", str(merged_json),
        "--output-relations", str(relations_json),
        "--output-csv", str(relations_csv)
    ]
    if args.print_raw_text:
        cmd_relation.append("--print-raw-text")
    run_command(cmd_relation, "关系提取")
    print(f"关系 JSON: {relations_json}")
    print(f"关系 CSV:  {relations_csv}")

    # 输出最终统计
    print("\n=== 流水线执行完成 ===")
    print(f"结果存放目录: {result_dir}")
    print(f"生成文件:")
    print(f"  - 分块: {chunks_json}")
    print(f"  - 实体 (逐块): {entities_json}")
    print(f"  - 实体 (合并): {merged_json}")
    print(f"  - 关系 (逐块): {relations_json}")
    print(f"  - 关系 (CSV):  {relations_csv}")


if __name__ == "__main__":
    main()