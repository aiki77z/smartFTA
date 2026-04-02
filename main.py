"""
main.py —— FastAPI 接口入口（v3）

接口列表：
  POST /api/tree/generate               根据用户prompt自动提取顶事件并生成故障树
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
import importlib.util
import sys
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from generator import generate_fault_tree, parse_user_prompt
from validator import validate_full, validate_semantics
from database import (
    create_tree,
    save_version,
    get_version,
    rollback_version,
    get_version_list,
    get_tree_meta,
    get_chunk_by_id,
    versions_col,
)
from diff_analyzer import analyze_and_store, corrections_col, generate_change_description

app = FastAPI(title="故障树智能生成系统", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def _load_py_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{module_name}（path={file_path}）")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ---- Optional: merge validator-service into this backend (same uvicorn port) ----
_VALIDATOR_DIR = Path(__file__).resolve().parent / "validator-service"
if _VALIDATOR_DIR.exists():
    try:
        # 动态加载 validator-service/main.py（规则校验、导出图片等）
        _validator_dir_str = str(_VALIDATOR_DIR)
        _had_validator_path = _validator_dir_str in sys.path
        if not _had_validator_path:
            sys.path.insert(0, _validator_dir_str)

        _validator_main = _load_py_module(
            "validator_service_main",
            _VALIDATOR_DIR / "main.py",
        )

        # Re-export legacy endpoints for existing frontend compatibility:
        # - POST /validate-fault-tree
        # - POST /export-fault-tree-image
        @app.post("/validate-fault-tree")
        def validate_fault_tree(payload: _validator_main.ValidateRequest):  # type: ignore[name-defined]
            return _validator_main.validate_fault_tree(payload)  # type: ignore[attr-defined]

        @app.post("/export-fault-tree-image")
        async def export_fault_tree_image(payload: _validator_main.ExportImageRequest):  # type: ignore[name-defined]
            try:
                return await _validator_main.export_fault_tree_image(payload)  # type: ignore[attr-defined]
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    except Exception as e:
        # Do not fail main backend if validator-service is present but broken.
        # The core /api/tree/* routes should still work.
        print(f"[warn] validator-service merge failed: {e}")
    finally:
        # Avoid permanently polluting sys.path in case other imports shadow.
        try:
            if "_validator_dir_str" in locals() and not locals().get("_had_validator_path", True):
                sys.path = [p for p in sys.path if p != locals()["_validator_dir_str"]]
        except Exception:
            pass


class GenerateRequest(BaseModel):
    prompt: str


class SaveRequest(BaseModel):
    tree_data: dict
    editor: str = "专家"
    description: str = "手动修改"


class ValidateRequest(BaseModel):
    tree_data: dict


class SemanticValidateRequest(BaseModel):
    tree_data: dict

@app.post("/api/tree/generate")
def api_generate(req: GenerateRequest):
    """
    生成故障树。

    当前输入模式：
    - 前端只传入一个 prompt
    - 后端先从 prompt 中提取顶事件与额外要求
    - 然后仅从数据库检索 chunks，不支持会话时直接上传 chunks

    示例输入：
      {"prompt": "我要生成一个顶事件为传感器故障的故障树"}
      {"prompt": "传感器故障"}
      {"prompt": "请分析驱动系统故障，生成3到5层故障树并保留溯源"}
    """
    try:
        parsed = parse_user_prompt(req.prompt)
        top_event = parsed["top_event"]
        requirements = parsed.get("requirements", "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"用户prompt解析失败：{str(e)}")

    tree_id = f"ft_{uuid.uuid4().hex[:8]}"
    create_tree(tree_id, top_event)

    try:
        tree_data = generate_fault_tree(top_event, requirements)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败：{str(e)}")

    version = save_version(
        tree_id=tree_id,
        tree_data=tree_data,
        editor="AI",
        description=f"AI初始生成（由prompt提取顶事件：{top_event}）",
        is_ai=True,
    )

    return {
        "tree_id": tree_id,
        "version": version,
        "parsed_prompt": {
            "top_event": top_event,
            "requirements": requirements,
        },
        "tree_data": tree_data,
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
    meta = get_tree_meta(tree_id)
    if not meta:
        raise HTTPException(status_code=404, detail="故障树不存在")

    validation = validate_full(req.tree_data, skip_semantic=True)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "故障树存在结构错误，无法保存，请修正后重试",
                "issues": [i for i in validation["issues"] if i["level"] == "ERROR"],
            },
        )

    prev_version_num = meta.get("current_version")
    prev_tree_data = None
    prev_ver = None
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
        is_ai=False,
    )

    learned_count = 0
    if prev_version_num and prev_ver and prev_ver.get("is_ai_generated"):
        learned_count = analyze_and_store(tree_id, prev_version_num, new_version)

    return {"success": True, "version": new_version, "learned_count": learned_count}


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


@app.post("/api/tree/validate/semantic")
def api_validate_semantic(req: SemanticValidateRequest):
    """
    仅做 AI 语义校验（调用 validator.py 的 validate_semantics）。
    前端用于“手动点击校验按钮后”刷新 AI 校验结果，不影响保存流程。
    """
    try:
        issues = validate_semantics(req.tree_data) or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI语义校验失败：{str(e)}")

    # validate_semantics 约束 level 主要为 WARNING/INFO；这里仍按通用格式统计
    error_count = sum(1 for i in issues if getattr(i, "level", "") == "ERROR")
    warning_count = sum(1 for i in issues if getattr(i, "level", "") == "WARNING")
    info_count = sum(1 for i in issues if getattr(i, "level", "") == "INFO")
    return {
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "issues": [i.to_dict() for i in issues],
    }


@app.get("/api/chunk/{chunk_id}")
def api_get_chunk(chunk_id: int):
    chunk = get_chunk_by_id(chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")
    chunk.pop("_id", None)
    return chunk


@app.get("/api/corrections/{tree_id}")
def api_get_corrections(tree_id: str):
    docs = list(corrections_col.find({"tree_id": tree_id}, {"_id": 0}).sort("created_at", -1))
    return docs


@app.get("/")
def root():
    return {
        "message": "故障树智能生成系统运行中",
        "docs": "/docs",
        "generate_input_example": {"prompt": "请生成一个顶事件为传感器故障的故障树，并保留溯源"},
    }
