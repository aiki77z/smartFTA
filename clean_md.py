# clean_md.py
import re
import sys
import time
import os
import argparse
from typing import List, Tuple

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


def call_llm(prompt: str, context: str = "", mode: str = "entity") -> str:
    """Use the KB v2 OpenAI-compatible LLM config for Markdown heading cleanup."""
    if not LLM_API_KEY:
        print("警告：未配置 LLM_API_KEY，跳过 Markdown 标题层级 LLM 清洗。")
        return ""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    messages = [
        {
            "role": "system",
            "content": "你是文档结构清洗助手，只输出用户要求的结果，不要解释。",
        },
        {"role": "user", "content": f"{context}\n\n{prompt}" if context else prompt},
    ]
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=float(os.getenv("CLEAN_MD_LLM_TEMPERATURE", "0.1")),
        max_tokens=int(os.getenv("CLEAN_MD_LLM_MAX_TOKENS", "4096")),
    )
    return response.choices[0].message.content or ""

def fix_heading_hierarchy(text: str) -> str:
    """提取文本中的 Markdown 标题，并调用 LLM 修正其层级结构。"""
    heading_pattern = re.compile(r'^(#{1,6}\s+.*)$', re.MULTILINE)
    headings = heading_pattern.findall(text)
    if not headings:
        return text
    if call_llm is None:
        print("警告：未找到旧版 llm_caller_relation，跳过 Markdown 标题层级 LLM 清洗。")
        return text

    prompt = f"""你是一个文档结构整理专家。请将下面给出的目录文本（heads.md）中的标题层级修正为规范的Markdown格式。

要求：
1. 识别标题层级规则：
   - 以“第X章”或“X章”开头的行 → 一级标题（#）
   - 以“X.X”开头（如1.1） → 二级标题（##）
   - 以“X.X.X”开头（如1.1.1） → 三级标题（###）
   - 以“X.”（如1.）且上一级是三级标题 → 四级标题（####）
   - 以“（1）”、“①”、“1）”等缩进列表形式且内容重要 → 四级或五级标题（#### 或 #####）
   - 无编号但明显属于子标题（如“维修体会与维修要点：”）→ 根据上下文设定标题层级
2. 输出时只输出修正后的Markdown文本，不要额外解释。
3. 保留原标题中的数字编号（如“1.1.1”）可选，但必须使用#表示层级。
4. 对于“例XX.”这类维修案例标题，统一处理为粗体（**例XX.**）并缩进，并作为五级标题（#####）。
5. 原文本有{len(headings)}行，应保持输出为相同行数，在输出结果前进行检查。

以下是需要处理的目录内容：
{chr(10).join(headings)}
"""
    fixed = call_llm(prompt, context="", mode="entity")
    if not fixed:
        return text

    fixed_lines = [line.rstrip() for line in fixed.splitlines() if line.strip()]
    if len(fixed_lines) != len(headings):
        print("警告：修正后的标题数量与原始不匹配，保持原文本不变。")
        return text

    fixed_iter = iter(fixed_lines)
    def replace_match(match: re.Match) -> str:
        return next(fixed_iter)
    return heading_pattern.sub(replace_match, text)

def fix_heading_hierarchy(text: str) -> str:
    """Fix Markdown heading levels using the KB v2 LLM client.

    Only heading lines are sent to the model. If the model output cannot be
    aligned line-by-line with the original headings, keep the original text.
    """
    heading_pattern = re.compile(r'^(#{1,6}\s+.*)$', re.MULTILINE)
    headings = heading_pattern.findall(text)
    if not headings:
        return text

    prompt = f"""你是工业设备手册的 Markdown 结构清洗助手。
请只处理下面这些 Markdown 标题行，把标题层级修正为更合理的 # / ## / ### / #### / #####。

要求：
1. 只输出修正后的标题行，不要解释，不要加代码块。
2. 输出行数必须与输入行数完全一致。
3. 每一行仍然必须是 Markdown 标题行，即以 1 到 6 个 # 开头。
4. 不要改写标题语义，不要删除编号、章节名、设备名。
5. 如果无法判断层级，优先保持原来的标题文本，只微调 # 数量。

输入标题行数：{len(headings)}
输入标题：
{chr(10).join(headings)}
"""
    try:
        fixed = call_llm(prompt, context="", mode="clean_md")
    except Exception as exc:
        print(f"警告：Markdown 标题层级 LLM 清洗失败，保留原文。错误: {exc}")
        return text
    if not fixed:
        return text

    fixed_lines = [line.rstrip() for line in fixed.splitlines() if line.strip()]
    if len(fixed_lines) != len(headings):
        print("警告：修正后的标题数量与原始标题数量不匹配，保留原 Markdown。")
        return text
    if any(not re.match(r'^#{1,6}\s+\S', line) for line in fixed_lines):
        print("警告：LLM 输出包含非 Markdown 标题行，保留原 Markdown。")
        return text

    fixed_iter = iter(fixed_lines)
    return heading_pattern.sub(lambda _: next(fixed_iter), text)


def process_md_file(input_file: str, output_file: str, headings_output: str = None, fixed_headings_output: str = None):
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    if headings_output:
        heading_pattern = re.compile(r'^(#{1,6}\s+.*)$', re.MULTILINE)
        headings = heading_pattern.findall(content)
        with open(headings_output, 'w', encoding='utf-8') as f:
            f.write('\n'.join(headings) + '\n')
        print(f"原始标题行已提取并保存至 {headings_output}")

    print(f"正在修正标题层级...")
    fixed_headings_content = fix_heading_hierarchy(content)

    if fixed_headings_output:
        heading_pattern = re.compile(r'^(#{1,6}\s+.*)$', re.MULTILINE)
        fixed_headings = heading_pattern.findall(fixed_headings_content)
        with open(fixed_headings_output, 'w', encoding='utf-8') as f:
            f.write('\n'.join(fixed_headings) + '\n')
        print(f"修正后标题行已提取并保存至 {fixed_headings_output}")

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(fixed_headings_content)
    print(f"标题层级已修正并保存至 {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean Markdown file by fixing heading hierarchy.")
    parser.add_argument('--input', '-i', required=True, help='Input Markdown file')
    parser.add_argument('--output', '-o', default=None, help='Output Markdown file')
    parser.add_argument('--headings-output', help='Output file for extracted original headings only')
    parser.add_argument('--fixed-headings-output', help='Output file for extracted fixed headings only')
    args = parser.parse_args()

    # === 新增：记录开始时间 ===
    start_time = time.time()

    input_file = args.input
    if args.output:
        output_file = args.output
    else:
        output_file = input_file[:-3] + "_cleaned.md"

    process_md_file(input_file, output_file, args.headings_output, args.fixed_headings_output)

    # === 新增：打印耗时 ===
    elapsed = time.time() - start_time
    print(f"\n=== 清理标题层级耗时: {elapsed:.2f} 秒 ===")
