import json
import os
import re
import argparse
from typing import List, Dict, Set
from generate_prompt_relation import generate_entity_prompt_and_context
from llm_caller_relation import call_llm
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 文件写入锁（确保多线程写入时不会冲突）
write_lock = threading.Lock()

def save_json(data, file_path):
    """保存数据到JSON文件"""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def parse_entities(text: str) -> List[Dict]:
    entities = []
    pattern = re.compile(r'实体名称[：:]\s*(.+?)\s*[，,]\s*实体类别[：:]\s*(.+)')
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        match = pattern.search(line)
        if match:
            entity_name = match.group(1).strip()
            entity_type = match.group(2).strip()
            entities.append({"entity_name": entity_name, "entity_type": entity_type})
    return entities

def is_entity_in_text(entity_name: str, text: str) -> bool:
    e = entity_name.strip().lower()
    t = text.lower()
    pattern = re.escape(e).replace(r'\ ', r'\s*')
    return re.search(pattern, t) is not None

def load_processed_chunk_ids(output_file: str) -> Set[str]:
    """加载已处理的chunk_id集合"""
    if not os.path.exists(output_file):
        return set()
    with open(output_file, "r", encoding="utf-8") as f:
        data = [json.loads(line.strip()) for line in f]
    return {item["chunk_id"] for item in data}

def process_single_chunk(chunk: Dict, print_raw_text: bool, write_lock, output_file: str, processed_chunk_ids: Set[str]):
    """
    处理单个chunk（供多线程调用）
    返回处理结果字典，如果处理失败或chunk已处理则返回None
    """
    chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or 0)
    if chunk_id in processed_chunk_ids:
        return None

    chunk_name = chunk.get("chunk_name") or chunk.get("chunk_name", "未知文档")
    content = chunk.get("content", "")

    # 构建增强上下文
    title_parts = []
    if chunk.get("section_path"):
        title_parts.append(f"章节路径: {chunk['section_path']}")
    if chunk.get("chapter"):
        title_parts.append(f"章: {chunk['chapter']}")
    if chunk.get("section"):
        title_parts.append(f"节: {chunk['section']}")
    if chunk.get("subsection"):
        title_parts.append(f"小节: {chunk['subsection']}")

    if title_parts:
        title_str = "\n".join(title_parts)
        enriched_content = f"{title_str}\n\n{content}"
    else:
        enriched_content = content

    print(f"处理文档：{chunk_name}，chunk_id: {chunk_id}")

    try:
        entity_prompt, entity_context = generate_entity_prompt_and_context(chunk_name, enriched_content)
        entity_text = call_llm(entity_context, entity_prompt)
        if print_raw_text:
            # 打印时带上chunk_id以区分线程输出
            print(f"LLM output for chunk {chunk_id}: {entity_text}")

        entities = parse_entities(entity_text)

        valid_entities = []
        seen_entities = set()
        for ent in entities:
            name = ent["entity_name"]
            if name not in seen_entities and is_entity_in_text(name, content) and len(name) <= 20:
                valid_entities.append(ent)
                seen_entities.add(name)

        result = {
            "chunk_id": chunk_id,
            "chunk_name": chunk_name,
            "content": content,
            "entities": valid_entities,
            "entity": valid_entities
        }

        # 使用锁安全写入文件
        with write_lock:
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
                f.flush()

        print(f"提取有效实体数：{len(valid_entities)} (chunk {chunk_id})")
        return result

    except Exception as e:
        print(f"处理chunk_id {chunk_id}时出错: {e}")
        return None

def extract_entities_incremental(chunks: List[Dict], output_file: str, print_raw_text: bool = False, max_workers: int = 5) -> List[Dict]:
    """
    增量提取实体（多线程并发版本），避免重复处理已处理的chunk
    """
    processed_chunk_ids = load_processed_chunk_ids(output_file)
    results = []

    # 加载已有结果（用于最终返回）
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            results = [json.loads(line.strip()) for line in f]

    # 过滤出需要处理的chunk
    chunks_to_process = [chunk for chunk in chunks
                         if str(chunk.get("chunk_id") or chunk.get("id") or 0) not in processed_chunk_ids]

    if not chunks_to_process:
        print("所有chunk均已处理，无需提取")
        return results

    print(f"待处理chunk数：{len(chunks_to_process)}，并发数：{max_workers}")

    # 使用线程池并发处理
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk = {
            executor.submit(process_single_chunk, chunk, print_raw_text, write_lock, output_file, processed_chunk_ids): chunk
            for chunk in chunks_to_process
        }

        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or 0)
                print(f"处理chunk {chunk_id}时发生未捕获异常: {e}")

    return results

def merge_entities(entities_results: List[Dict], output_file: str):
    """
    合并所有实体，记录每个实体出现的chunk_id
    """
    entity_map = {}

    for result in entities_results:
        chunk_id = result["chunk_id"]
        for entity in result["entities"]:
            entity_name = entity["entity_name"]
            entity_type = entity["entity_type"]

            if entity_name in entity_map:
                entity_map[entity_name]["chunk_ids"].add(chunk_id)
            else:
                entity_map[entity_name] = {
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "chunk_ids": {chunk_id}
                }

    merged_entities = [
        {
            "entity_name": entity["entity_name"],
            "entity_type": entity["entity_type"],
            "chunk_ids": list(entity["chunk_ids"]),
            "count": len(entity["chunk_ids"])
        }
        for entity in entity_map.values()
    ]

    save_json(merged_entities, output_file)

def main():
    parser = argparse.ArgumentParser(description='实体识别和合并工具（支持多线程并发）')
    parser.add_argument('--input', '-i', required=True, help='输入chunks JSON文件路径')
    parser.add_argument('--output-entities', '-oe', required=True, help='输出实体JSON文件路径')
    parser.add_argument('--output-merged', '-om', required=True, help='输出合并实体JSON文件路径')
    parser.add_argument('--skip-entity-extraction', action='store_true', help='跳过实体提取，只进行合并')
    parser.add_argument('--print-raw-text', '-p', action='store_true', help='打印LLM返回的原始实体文本（用于调试）')
    parser.add_argument('--max-workers', '-w', type=int, default=5, help='并发线程数（默认5）')

    args = parser.parse_args()

    print(f"正在加载chunks数据: {args.input}")
    with open(args.input, "r", encoding="utf-8") as f:
        chunks = json.load(f)
    print(f"加载完成，共 {len(chunks)} 个chunks")

    if not args.skip_entity_extraction:
        print(f"开始实体提取（多线程，并发数={args.max_workers}），结果将保存到: {args.output_entities}")
        entities_results = extract_entities_incremental(chunks, args.output_entities,
                                                        args.print_raw_text, args.max_workers)
        print(f"实体提取完成，共处理 {len(entities_results)} 个chunks")
    else:
        print("跳过实体提取，直接加载已有结果")
        if os.path.exists(args.output_entities):
            with open(args.output_entities, "r", encoding="utf-8") as f:
                entities_results = [json.loads(line.strip()) for line in f]
            print(f"加载已有实体结果: {len(entities_results)} 个chunks")
        else:
            print(f"错误: 实体结果文件不存在: {args.output_entities}")
            return

    print(f"开始实体合并，结果将保存到: {args.output_merged}")
    merge_entities(entities_results, args.output_merged)
    print("实体合并完成")

    with open(args.output_merged, "r", encoding="utf-8") as f:
        merged_entities = json.load(f)

    print(f"\n=== 统计信息 ===")
    print(f"总实体数量: {len(merged_entities)}")
    # 可选：按类型统计
    # type_counter = Counter(entity["entity_type"] for entity in merged_entities)
    # print("实体类型分布:")
    # for entity_type, count in type_counter.most_common():
    #     print(f"  {entity_type}: {count}")

if __name__ == "__main__":
    main()