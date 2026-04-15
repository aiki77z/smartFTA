import json
import os
import re
import argparse
import csv
import threading
import concurrent.futures
from typing import List, Dict, Set
from collections import Counter, defaultdict
from generate_prompt_relation import generate_relation_prompt_and_context_second
from llm_caller_relation import call_llm

def parse_relations_second(relation_text: str) -> List[Dict]:
    """
    解析 LLM 返回的关系文本，支持以下格式：
    1. JSON 数组 (带或不带 markdown 代码块)
    2. 尖括号四元组: <实体1, 关系类型, 具体动词, 实体2>
    3. 制表符/逗号分隔的三元组: 实体1\t实体2\t关系类型 或 实体1,实体2,关系类型
    4. 自然语言模式（正则后备）
    返回列表，每个元素为 {"entity1": str, "entity2": str, "relation_type": str}
    """
    if not relation_text or not relation_text.strip():
        return []

    try:
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', relation_text.strip(), flags=re.IGNORECASE)
        data = json.loads(cleaned)
        if isinstance(data, list):
            relations = []
            for item in data:
                if isinstance(item, dict) and 'entity1' in item and 'entity2' in item and 'relation_type' in item:
                    relations.append({
                        "entity1": item["entity1"],
                        "entity2": item["entity2"],
                        "relation_type": item["relation_type"]
                    })
            if relations:
                return relations
    except json.JSONDecodeError:
        pass

    relations = []
    lines = relation_text.strip().splitlines()

    angle_pattern = re.compile(r'<([^,>]+)[,，]\s*([^,>]+)[,，]\s*([^,>]+)[,，]\s*([^,>]+)>')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = angle_pattern.match(line)
        if match:
            entity1 = match.group(1).strip()
            relation_type = match.group(2).strip()
            verb = match.group(3).strip()
            entity2 = match.group(4).strip()
            relations.append({
                "entity1": entity1,
                "entity2": entity2,
                "relation_type": relation_type
            })
            continue

        if '\t' in line:
            parts = line.split('\t')
            if len(parts) == 3:
                relations.append({
                    "entity1": parts[0].strip(),
                    "entity2": parts[1].strip(),
                    "relation_type": parts[2].strip()
                })
                continue
        if ',' in line and line.count(',') == 2:
            parts = line.split(',')
            if len(parts) == 3:
                relations.append({
                    "entity1": parts[0].strip(),
                    "entity2": parts[1].strip(),
                    "relation_type": parts[2].strip()
                })
                continue

        pattern = r'([^->,\t]+?)\s*(?:[-–—>]+|\s+关系\s*[:：]?\s*)?\s*([^->,\t]+?)\s*[-–—>]+\s*([^->,\t]+)'
        match = re.search(pattern, line)
        if match:
            relations.append({
                "entity1": match.group(1).strip(),
                "entity2": match.group(3).strip(),
                "relation_type": match.group(2).strip()
            })

    return relations

def save_relations_to_csv_second(relations: List[Dict], output_csv_path: str) -> None:
    if not relations:
        print("警告：没有关系数据可保存，CSV文件将只包含表头")
    fieldnames = set()
    for rel in relations:
        fieldnames.update(rel.keys())
    preferred_order = ["chunk_id", "entity1", "entity2", "relation_type", "entity1_type", "entity2_type"]
    fieldnames = [f for f in preferred_order if f in fieldnames] + [f for f in fieldnames if f not in preferred_order]
    with open(output_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rel in relations:
            writer.writerow(rel)
    print(f"已保存 {len(relations)} 条关系到 {output_csv_path}")

def load_processed_chunk_ids(output_file: str) -> Set[str]:
    if not os.path.exists(output_file):
        return set()
    with open(output_file, "r", encoding="utf-8") as f:
        data = [json.loads(line.strip()) for line in f]
    return {item["chunk_id"] for item in data}

def extract_relations_incremental(chunks: List[Dict], entities_results: List[Dict], output_file: str, print_raw_text: bool = False) -> List[Dict]:
    """
    多线程并发调用 LLM 提取关系，结果增量写入 output_file（JSON Lines 格式）
    """
    processed_chunk_ids = load_processed_chunk_ids(output_file)
    results = []
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            results = [json.loads(line.strip()) for line in f]

    chunk_entities_map = {res["chunk_id"]: res["entities"] for res in entities_results}

    # 收集待处理的 chunk
    pending_chunks = []
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id") or chunk.get("id") or str(chunk.get("chunk_id", ""))
        if not chunk_id or chunk_id in processed_chunk_ids:
            continue
        pending_chunks.append((chunk_id, chunk))

    if not pending_chunks:
        print("没有需要处理的新 chunk")
        return results

    print(f"需要处理 {len(pending_chunks)} 个 chunk，使用多线程并发调用 LLM")

    # 单个 chunk 的处理函数（将在线程池中执行）
    def process_one(chunk_id: str, chunk: Dict):
        chunk_name = chunk.get("chunk_name") or chunk.get("chunk_name", "未知文档")
        content = chunk.get("content", "")
        entities = chunk_entities_map.get(chunk_id, [])
        print(f"处理文档关系：{chunk_name}，chunk_id: {chunk_id}，实体数: {len(entities)}")
        valid_relations = []
        try:
            if entities:
                relation_prompt, relation_context = generate_relation_prompt_and_context_second(
                    chunk_name, content, entities
                )
                relation_text = call_llm(relation_prompt, relation_context, mode="relation")

                if print_raw_text:
                    print(f"\n=== LLM 原始返回 (chunk_id: {chunk_id}) ===\n{relation_text}\n=================================\n")

                relations = parse_relations_second(relation_text)
                entity_names = {ent["entity_name"] for ent in entities}
                entity_type_map = {ent["entity_name"]: ent["entity_type"] for ent in entities}
                for rel in relations:
                    if rel["entity1"] in entity_names and rel["entity2"] in entity_names:
                        rel["entity1_type"] = entity_type_map.get(rel["entity1"], "")
                        rel["entity2_type"] = entity_type_map.get(rel["entity2"], "")
                        valid_relations.append(rel)

            result = {
                "chunk_id": chunk_id,
                "relations": valid_relations,
                "entity": entities,
                "relation": valid_relations
            }
            print(f"提取有效关系数：{len(valid_relations)}")
            return result
        except Exception as e:
            print(f"处理 chunk_id {chunk_id} 关系时出错: {e}")
            return None

    new_results = []
    # 使用线程池，最大并发数可根据需要调整
    max_workers = 5
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk = {executor.submit(process_one, cid, chunk): cid for cid, chunk in pending_chunks}
        # 主线程顺序处理已完成的任务，并追加写入文件（保证线程安全）
        with open(output_file, "a", encoding="utf-8") as f:
            for future in concurrent.futures.as_completed(future_to_chunk):
                cid = future_to_chunk[future]
                try:
                    result = future.result()
                    if result is not None:
                        f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        f.flush()
                        new_results.append(result)
                except Exception as e:
                    print(f"处理 chunk {cid} 时发生异常: {e}")

    results.extend(new_results)
    return results

def load_chunks(file_path: str) -> List[Dict]:
    """自动检测并加载 chunks 文件（支持 JSON 数组 或 JSON Lines）"""
    with open(file_path, "r", encoding="utf-8") as f:
        first_char = f.read(1)
        f.seek(0)
        if first_char == '[':
            return json.load(f)
        else:
            return [json.loads(line.strip()) for line in f if line.strip()]

def load_entities_from_chunks(chunks: List[Dict]) -> List[Dict]:
    """从 chunks 的 entities 字段构建 entities_results 列表"""
    results = []
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id") or chunk.get("id") or ""
        if chunk_id and "entities" in chunk:
            results.append({"chunk_id": chunk_id, "entities": chunk["entities"]})
    return results

def load_aggregated_entities(file_path: str) -> List[Dict]:
    """
    加载聚合格式的实体文件（每个实体包含 chunk_ids 列表），
    返回按 chunk_id 组织的列表 [{"chunk_id":..., "entities": [...]}]
    """
    with open(file_path, "r", encoding="utf-8") as f:
        first_char = f.read(1)
        f.seek(0)
        if first_char == '[':
            data = json.load(f)
        else:
            data = [json.loads(line.strip()) for line in f if line.strip()]
    chunk_to_entities = defaultdict(list)
    for ent in data:
        entity_name = ent.get("entity_name")
        entity_type = ent.get("entity_type")
        chunk_ids = ent.get("chunk_ids", [])
        if not entity_name or not entity_type:
            continue
        for cid in chunk_ids:
            chunk_to_entities[cid].append({
                "entity_name": entity_name,
                "entity_type": entity_type
            })
    return [{"chunk_id": cid, "entities": ents} for cid, ents in chunk_to_entities.items()]

def main():
    parser = argparse.ArgumentParser(description='实体关系提取工具')
    parser.add_argument('--input-chunks', '-ic', required=True, help='输入chunks JSON文件路径')
    parser.add_argument('--input-entities', '-ie', help='可选：输入实体JSON文件路径（若提供，则覆盖chunks内的实体）')
    parser.add_argument('--output-relations', '-or', required=True, help='输出关系JSON文件路径')
    parser.add_argument('--output-csv', '-oc', required=True, help='输出CSV文件路径')
    parser.add_argument('--skip-relation-extraction', action='store_true', help='跳过关系统取，只进行CSV转换')
    parser.add_argument('--print-raw-text', '-p', action='store_true', help='打印LLM返回的原始关系文本（用于调试）')
    args = parser.parse_args()

    print(f"正在加载chunks数据: {args.input_chunks}")
    chunks = load_chunks(args.input_chunks)
    print(f"加载完成，共 {len(chunks)} 个chunks")

    if args.input_entities and os.path.exists(args.input_entities):
        print(f"使用外部实体文件: {args.input_entities}")
        entities_results = load_aggregated_entities(args.input_entities)
        print(f"从聚合实体文件生成 {len(entities_results)} 个chunk的实体数据")
    else:
        print("未提供外部实体文件，将使用chunks中的entities字段")
        entities_results = load_entities_from_chunks(chunks)
        print(f"从chunks中提取实体结果，共 {len(entities_results)} 个chunk")

    if not args.skip_relation_extraction:
        print(f"开始关系提取，结果将保存到: {args.output_relations}")
        relations_results = extract_relations_incremental(chunks, entities_results, args.output_relations, print_raw_text=args.print_raw_text)
        print(f"关系提取完成，共处理 {len(relations_results)} 个chunks")
    else:
        print("跳过关系统取，直接加载已有结果")
        if os.path.exists(args.output_relations):
            with open(args.output_relations, "r", encoding="utf-8") as f:
                relations_results = [json.loads(line.strip()) for line in f]
            print(f"加载已有关系结果: {len(relations_results)} 个chunks")
        else:
            print(f"错误: 关系结果文件不存在: {args.output_relations}")
            return

    print(f"开始转换为CSV，结果将保存到: {args.output_csv}")
    all_relations = []
    for result in relations_results:
        for rel in result.get("relations", []):
            rel["chunk_id"] = result["chunk_id"]
            all_relations.append(rel)
    save_relations_to_csv_second(all_relations, args.output_csv)
    print(f"CSV转换完成，共 {len(all_relations)} 个关系")
    print(f"\n=== 统计信息 ===")
    print(f"总关系数量: {len(all_relations)}")

if __name__ == "__main__":
    main()