"""
main.py —— FastAPI 接口入口（v2）

接口列表：
  POST /api/tree/generate               生成故障树（含定向修复步骤）
  GET  /api/tree/{tree_id}              获取当前版本故障树
  GET  /api/tree/{tree_id}/version/{v}  获取指定版本故障树
  POST /api/tree/{tree_id}/save         专家保存修改（自动触发差异学习）
  POST /api/tree/{tree_id}/rollback/{v} 撤回到指定版本
  GET  /api/tree/{tree_id}/history      获取版本历史列表
  POST /api/tree/validate               单独校验一棵树（调试用）
  GET  /api/chunk/{chunk_id}            根据id查chunk（前端溯源用）
  GET  /api/corrections/{tree_id}       查看某棵树的修正记录（调试用）
"""

import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from generator import generate_fault_tree
from validator import validate_full
from database import (
    create_tree, save_version, get_version,
    rollback_version, get_version_list, get_tree_meta, get_chunk_by_id,
    versions_col
)
from diff_analyzer import analyze_and_store, corrections_col, generate_change_description

app = FastAPI(title="故障树智能生成系统", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# 请求体定义
# ─────────────────────────────────────────────

class GenerateRequest(BaseModel):
    top_event:    str
    requirements: str = ""


class SaveRequest(BaseModel):
    tree_data:   dict
    editor:      str = "专家"
    description: str = "手动修改"


class ValidateRequest(BaseModel):
    tree_data: dict


# ─────────────────────────────────────────────
# 接口实现
# ─────────────────────────────────────────────

@app.post("/api/tree/generate")
def api_generate(req: GenerateRequest):
    """
    生成故障树。
    流程：检索chunks → LLM#1提取要素 → LLM#2生成草稿
          → 节点级修正检索 → LLM#3定向修复（有记录时）→ 存版本1 → 返回
    """
    tree_id = f"ft_{uuid.uuid4().hex[:8]}"
    create_tree(tree_id, req.top_event)

    try:
        tree_data = generate_fault_tree(req.top_event, req.requirements)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败：{str(e)}")

    version = save_version(
        tree_id=tree_id,
        tree_data=tree_data,
        editor="AI",
        description="AI初始生成",
        is_ai=True
    )

    return {
        "tree_id":   tree_id,
        "version":   version,
        "tree_data": tree_data
    }


@app.get("/api/tree/{tree_id}")
def api_get_tree(tree_id: str):
    ver = get_version(tree_id)
    if not ver:
        raise HTTPException(status_code=404, detail="故障树不存在")
    return ver


@app.get("/api/tree/{tree_id}/version/{version}")
def api_get_version(tree_id: str, version: int):
    ver = get_version(tree_id, version)
    if not ver:
        raise HTTPException(status_code=404, detail=f"版本 {version} 不存在")
    return ver


@app.post("/api/tree/{tree_id}/save")
def api_save(tree_id: str, req: SaveRequest):
    """
    专家保存修改。
    若上一版本是AI生成，则触发差异分析，将修正模式持久化到 corrections 集合。
    """
    meta = get_tree_meta(tree_id)
    if not meta:
        raise HTTPException(status_code=404, detail="故障树不存在")

    # 结构校验，有ERROR不允许保存
    validation = validate_full(req.tree_data, skip_semantic=True)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "故障树存在结构错误，无法保存，请修正后重试",
                "issues": [i for i in validation["issues"] if i["level"] == "ERROR"]
            }
        )

    prev_version_num = meta.get("current_version")
    prev_tree_data = None
    if prev_version_num:
        prev_ver = versions_col.find_one({"tree_id": tree_id, "version": prev_version_num})
        if prev_ver:
            prev_tree_data = prev_ver.get("tree_data")

    description = (req.description or "").strip()
    if description in ("", "手动修改") and prev_tree_data:
        description = generate_change_description(prev_tree_data, req.tree_data)
    elif not description:
        description = "手动修改"

    new_version = save_version(
        tree_id=tree_id,
        tree_data=req.tree_data,
        editor=req.editor,
        description=description,
        is_ai=False
    )

    # 修正学习：仅在上一版本是AI生成时触发
    learned_count = 0
    if prev_version_num:
        if prev_ver and prev_ver.get("is_ai_generated"):
            # analyze_and_store 现在直接返回写入条数（int）
            learned_count = analyze_and_store(tree_id, prev_version_num, new_version)

    return {
        "success":       True,
        "version":       new_version,
        "learned_count": learned_count
    }


@app.post("/api/tree/{tree_id}/rollback/{target_version}")
def api_rollback(tree_id: str, target_version: int):
    try:
        rollback_version(tree_id, target_version)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ver = get_version(tree_id, target_version)
    return {"success": True, "version": target_version, "tree_data": ver["tree_data"]}


@app.get("/api/tree/{tree_id}/history")
def api_history(tree_id: str):
    return get_version_list(tree_id)


@app.post("/api/tree/validate")
def api_validate(req: ValidateRequest):
    return validate_full(req.tree_data)


@app.get("/api/chunk/{chunk_id}")
def api_get_chunk(chunk_id: int):
    chunk = get_chunk_by_id(chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")
    chunk.pop("_id", None)
    return chunk


@app.get("/api/corrections/{tree_id}")
def api_get_corrections(tree_id: str):
    """查看某棵树积累的修正记录，便于调试和展示学习效果。"""
    docs = list(corrections_col.find(
        {"tree_id": tree_id},
        {"_id": 0}
    ).sort("created_at", -1))
    return docs


@app.get("/")
def root():
    return {"message": "故障树智能生成系统运行中", "docs": "/docs"}
