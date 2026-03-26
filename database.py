from pymongo import MongoClient
from datetime import datetime
from config import MONGO_URI, MONGO_DB_NAME

client     = MongoClient(MONGO_URI)
db         = client[MONGO_DB_NAME]

trees_col    = db["fault_trees"]
versions_col = db["fault_tree_versions"]
chunks_col   = db["chunks"]


# ─────────────────────────────────────────────
# Chunks 相关
# ─────────────────────────────────────────────

def import_chunks(chunks: list):
    """把同学给的JSON文件整体导入MongoDB（会先清空旧数据）"""
    chunks_col.drop()
    chunks_col.insert_many(chunks)
    print(f"成功导入 {len(chunks)} 个chunks")


def search_chunks_by_keywords(keywords: list, limit: int = 15) -> list:
    """
    根据关键词列表检索相关chunks。
    只要chunk的key_word字段和keywords有任何交集就返回。
    limit限制最多返回多少条，避免喂给大模型的内容太长。
    """
    results = chunks_col.find(
        {"key_word": {"$in": keywords}},
        limit=limit
    )
    return list(results)


def get_chunk_by_id(chunk_id: int) -> dict:
    """根据chunk id查单条记录，前端溯源时用"""
    return chunks_col.find_one({"id": chunk_id})


# ─────────────────────────────────────────────
# 故障树主表
# ─────────────────────────────────────────────

def create_tree(tree_id: str, top_event: str):
    trees_col.insert_one({
        "_id": tree_id,
        "top_event": top_event,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
        "current_version": 0,
        "status": "generating"
    })


def get_tree_meta(tree_id: str) -> dict:
    return trees_col.find_one({"_id": tree_id})


# ─────────────────────────────────────────────
# 版本表
# ─────────────────────────────────────────────

def save_version(tree_id: str, tree_data: dict,
                 editor: str, description: str,
                 is_ai: bool) -> int:
    """
    保存一个新版本。
    版本号自动递增：在该tree已有版本基础上+1。
    同时更新主表的current_version。
    """
    latest = versions_col.find_one(
        {"tree_id": tree_id},
        sort=[("version", -1)]
    )
    new_version = (latest["version"] + 1) if latest else 1

    versions_col.insert_one({
        "tree_id":      tree_id,
        "version":      new_version,
        "is_ai_generated": is_ai,
        "created_at":   datetime.now(),
        "editor":       editor,
        "description":  description,
        "tree_data":    tree_data
    })

    trees_col.update_one(
        {"_id": tree_id},
        {"$set": {
            "current_version": new_version,
            "updated_at":      datetime.now(),
            "status": "ai_generated" if is_ai else "expert_modified"
        }}
    )
    return new_version


def get_version(tree_id: str, version: int = None) -> dict:
    """
    获取指定版本的完整数据。
    不传version则取current_version。
    """
    if version is None:
        meta = get_tree_meta(tree_id)
        if not meta:
            return None
        version = meta["current_version"]

    return versions_col.find_one(
        {"tree_id": tree_id, "version": version},
        {"_id": 0}   # 不返回MongoDB内部_id
    )


def rollback_version(tree_id: str, target_version: int):
    """撤回：把current_version改成target_version即可，历史数据不删除"""
    # 先确认target_version存在
    target = versions_col.find_one({"tree_id": tree_id, "version": target_version})
    if not target:
        raise ValueError(f"版本 {target_version} 不存在")

    trees_col.update_one(
        {"_id": tree_id},
        {"$set": {
            "current_version": target_version,
            "updated_at":      datetime.now(),
            "status":          "rolled_back"
        }}
    )


def get_version_list(tree_id: str) -> list:
    """
    获取该故障树的所有版本列表。
    不返回tree_data（太大），只返回元信息，用于前端展示版本历史。
    """
    versions = versions_col.find(
        {"tree_id": tree_id},
        {"tree_data": 0, "_id": 0}
    ).sort("version", 1)
    return list(versions)
