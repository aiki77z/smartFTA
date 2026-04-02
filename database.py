from datetime import datetime
from pymongo import MongoClient
from config import MONGO_URI, MONGO_DB_NAME

client = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

trees_col = db["fault_trees"]
versions_col = db["fault_tree_versions"]
chunks_col = db["chunks"]


def import_chunks(chunks: list):
    """把JSON文件整体导入MongoDB（会先清空旧数据）"""
    chunks_col.drop()
    if chunks:
        chunks_col.insert_many(chunks)
    print(f"成功导入 {len(chunks)} 个chunks")


def search_chunks_by_keywords(keywords: list, limit: int = 8) -> list:
    """
    根据关键词列表检索相关chunks。

    检索策略：
    1. 先按 key_word 精确交集召回
    2. 若召回不足，再按 chunk_name / content / chapter / section 做模糊补充
    3. 去重后返回前 limit 条
    """
    cleaned_keywords = []
    for kw in keywords or []:
        text = str(kw).strip()
        if text and text not in cleaned_keywords:
            cleaned_keywords.append(text)

    if not cleaned_keywords:
        return []

    results = []
    seen_ids = set()

    exact_hits = chunks_col.find({"key_word": {"$in": cleaned_keywords}}, limit=limit)
    for doc in exact_hits:
        doc_id = doc.get("id")
        if doc_id not in seen_ids:
            results.append(doc)
            seen_ids.add(doc_id)

    if len(results) >= limit:
        return results[:limit]

    regex_clauses = []
    for kw in cleaned_keywords:
        regex = {"$regex": kw, "$options": "i"}
        regex_clauses.extend(
            [
                {"chunk_name": regex},
                {"content": regex},
                {"chapter": regex},
                {"section": regex},
                {"subsection": regex},
            ]
        )

    if regex_clauses:
        fuzzy_hits = chunks_col.find({"$or": regex_clauses}, limit=limit * 3)
        for doc in fuzzy_hits:
            doc_id = doc.get("id")
            if doc_id not in seen_ids:
                results.append(doc)
                seen_ids.add(doc_id)
            if len(results) >= limit:
                break

    return results[:limit]


def get_chunk_by_id(chunk_id: int) -> dict:
    return chunks_col.find_one({"id": chunk_id})


def create_tree(tree_id: str, top_event: str):
    trees_col.insert_one(
        {
            "_id": tree_id,
            "top_event": top_event,
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "current_version": 0,
            "status": "generating",
        }
    )


def get_tree_meta(tree_id: str) -> dict:
    return trees_col.find_one({"_id": tree_id})


def save_version(tree_id: str, tree_data: dict, editor: str, description: str, is_ai: bool) -> int:
    latest = versions_col.find_one({"tree_id": tree_id}, sort=[("version", -1)])
    new_version = (latest["version"] + 1) if latest else 1

    versions_col.insert_one(
        {
            "tree_id": tree_id,
            "version": new_version,
            "is_ai_generated": is_ai,
            "created_at": datetime.now(),
            "editor": editor,
            "description": description,
            "tree_data": tree_data,
        }
    )

    trees_col.update_one(
        {"_id": tree_id},
        {
            "$set": {
                "current_version": new_version,
                "updated_at": datetime.now(),
                "status": "ai_generated" if is_ai else "expert_modified",
            }
        },
    )
    return new_version


def get_version(tree_id: str, version: int = None) -> dict:
    if version is None:
        meta = get_tree_meta(tree_id)
        if not meta:
            return None
        version = meta["current_version"]

    return versions_col.find_one({"tree_id": tree_id, "version": version}, {"_id": 0})


def rollback_version(tree_id: str, target_version: int):
    target = versions_col.find_one({"tree_id": tree_id, "version": target_version})
    if not target:
        raise ValueError(f"版本 {target_version} 不存在")

    trees_col.update_one(
        {"_id": tree_id},
        {"$set": {"current_version": target_version, "updated_at": datetime.now(), "status": "rolled_back"}},
    )


def get_version_list(tree_id: str) -> list:
    versions = versions_col.find({"tree_id": tree_id}, {"tree_data": 0, "_id": 0}).sort("version", 1)
    return list(versions)
