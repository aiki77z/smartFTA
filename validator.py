from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Issue:
    level: str  # "ERROR" | "WARNING" | "INFO"
    code: str
    message: str
    node_id: Optional[str] = None
    node_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"level": self.level, "code": self.code, "message": self.message}
        if self.node_id:
            out["node_id"] = self.node_id
        if self.node_name:
            out["node_name"] = self.node_name
        return out


def _str(v: Any) -> str:
    return str(v) if v is not None else ""


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def validate_full(tree_data: Dict[str, Any], *, skip_semantic: bool = False) -> Dict[str, Any]:
    """
    Structural validator used by:
    - POST /api/tree/validate
    - POST /api/tree/{tree_id}/save (blocks save when ERROR exists)
    - generation pipeline post-checks

    IMPORTANT policy:
    - Missing "soft" event fields should NOT block submission.
      They are reported as INFO (or specialized INFO codes).
    """
    issues: List[Issue] = []

    if not isinstance(tree_data, dict):
        issues.append(Issue("ERROR", "INVALID_PAYLOAD", "tree_data 必须是对象（dict）"))
        return {"passed": False, "error_count": 1, "warning_count": 0, "info_count": 0, "issues": [i.to_dict() for i in issues]}

    node_list = tree_data.get("nodeList")
    link_list = tree_data.get("linkList")
    if not isinstance(node_list, list) or not isinstance(link_list, list):
        issues.append(Issue("ERROR", "INVALID_TREE_SHAPE", "缺少 nodeList 或 linkList（必须为数组）"))
        return {"passed": False, "error_count": 1, "warning_count": 0, "info_count": 0, "issues": [i.to_dict() for i in issues]}

    # Basic node-level checks.
    id_set = set()
    for n in node_list:
        if not isinstance(n, dict):
            issues.append(Issue("ERROR", "INVALID_NODE", "nodeList 中存在非对象节点"))
            continue
        node_id = _str(n.get("id") or "").strip()
        node_name = _str(n.get("name") or "").strip()
        node_type = _str(n.get("type") or "").strip()
        if not node_id:
            issues.append(Issue("ERROR", "MISSING_NODE_ID", "节点缺少 id"))
        else:
            if node_id in id_set:
                issues.append(Issue("ERROR", "DUPLICATE_NODE_ID", f"节点 id 重复：{node_id}", node_id=node_id, node_name=node_name))
            id_set.add(node_id)

        if not node_type:
            issues.append(Issue("ERROR", "MISSING_NODE_TYPE", "节点缺少 type", node_id=node_id or None, node_name=node_name or None))
        if not node_name:
            issues.append(Issue("ERROR", "MISSING_NODE_NAME", "节点缺少 name", node_id=node_id or None, node_name=node_name or None))

        # Event checks
        ev = n.get("event")
        if node_type == "top_event":
            continue

        if not isinstance(ev, dict):
            issues.append(Issue("ERROR", "MISSING_EVENT", "非顶事件节点必须包含 event 对象", node_id=node_id or None, node_name=node_name or None))
            continue

        # Hard required fields (block submission)
        hard_fields = ("id", "name")
        for f in hard_fields:
            if _is_missing(ev.get(f)):
                issues.append(Issue("ERROR", "MISSING_EVENT_FIELD", f"event对象缺少字段：{f}", node_id=node_id or None, node_name=node_name or None))

        # Soft fields (do NOT block submission) -> INFO
        # Keep existing INFO codes used by UI.
        if _is_missing(ev.get("errorLevel")):
            issues.append(Issue("INFO", "NO_ERROR_LEVEL", "没有故障等级（errorLevel为空）", node_id=node_id or None, node_name=node_name or None))
        if not isinstance(ev.get("documents"), list) or len(ev.get("documents") or []) == 0:
            issues.append(Issue("INFO", "NO_DOCUMENTS", "没有溯源文档（documents为空）", node_id=node_id or None, node_name=node_name or None))

        soft_fields = ("description", "priority", "probability", "showProbability", "investigateMethod", "rules")
        for f in soft_fields:
            if f in ("errorLevel", "documents"):
                continue
            if ev.get(f) is None:
                issues.append(Issue("INFO", "MISSING_EVENT_FIELD", f"event对象缺少字段：{f}", node_id=node_id or None, node_name=node_name or None))
            elif isinstance(ev.get(f), str) and not str(ev.get(f)).strip():
                issues.append(Issue("INFO", "MISSING_EVENT_FIELD", f"event对象缺少字段：{f}", node_id=node_id or None, node_name=node_name or None))

    # Link checks (basic)
    for e in link_list:
        if not isinstance(e, dict):
            issues.append(Issue("ERROR", "INVALID_LINK", "linkList 中存在非对象边"))
            continue
        s = _str(e.get("sourceId") or "").strip()
        t = _str(e.get("targetId") or "").strip()
        if not s or not t:
            issues.append(Issue("ERROR", "MISSING_LINK_ENDPOINT", "连线缺少 sourceId 或 targetId"))
            continue
        if s not in id_set or t not in id_set:
            issues.append(Issue("ERROR", "BROKEN_LINK", "连线指向不存在的节点", node_id=None, node_name=None))

    error_count = sum(1 for i in issues if i.level == "ERROR")
    warning_count = sum(1 for i in issues if i.level == "WARNING")
    info_count = sum(1 for i in issues if i.level == "INFO")
    return {
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "issues": [i.to_dict() for i in issues],
    }

"""
validator.py —— 故障树逻辑校验模块

适配新格式：{nodeList: [...], linkList: [...]}

校验分两层：
  第一层（结构校验）：调用同仓库 validator-service/main.py 中的规则引擎（代码规则）
  第二层（语义校验）：调大模型判断逻辑合理性

问题级别：
  ERROR   → 必须修复
  WARNING → 建议修复
  INFO    → 提示信息
"""

from dataclasses import dataclass
from typing import Literal
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_VALIDATION_MAX_TOKENS
import json
import re
import importlib.util
import sys
import time
from pathlib import Path

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


@dataclass
class ValidationIssue:
    level:     Literal["ERROR", "WARNING", "INFO"]
    code:      str
    message:   str
    node_id:   str = ""
    node_name: str = ""
    # 来源标记：logic=代码规则校验，ai=大模型语义校验
    source:    Literal["logic", "ai"] = "logic"

    def to_dict(self):
        return {
            "level":     self.level,
            "code":      self.code,
            "message":   self.message,
            "node_id":   self.node_id,
            "node_name": self.node_name,
            "source":    self.source,
        }


def _load_py_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{module_name}（path={file_path}）")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree_to_validator_service_graph(tree_data: dict) -> dict:
    """
    将 {nodeList, linkList} 转成 validator-service `/validate-fault-tree` 的 graph 格式：
      - 节点类型：top/intermediate/basic/gate
      - 通过“合成 gate 节点”的方式表达 AND/OR（validator-service 规则依赖 gate 子节点）
      - 边方向：child -> parent
    """
    node_list = tree_data.get("nodeList", []) or []
    link_list = tree_data.get("linkList", []) or []

    nodes_by_id = {str(n.get("id")): n for n in node_list if n.get("id") is not None}

    def map_type(t: str) -> str:
        if t == "top_event":
            return "top"
        if t == "intermediate_event":
            return "intermediate"
        if t == "basic_event":
            return "basic"
        if t == "gate":
            return "gate"
        # fallback：未知当中间事件处理
        return "intermediate"

    # 原始 child->parent 关系（事件节点之间）
    parents_of = {}
    children_of = {}
    for l in link_list:
        child = str(l.get("sourceId", ""))
        parent = str(l.get("targetId", ""))
        if not child or not parent:
            continue
        if child not in nodes_by_id or parent not in nodes_by_id:
            continue
        parents_of.setdefault(child, []).append(parent)
        children_of.setdefault(parent, []).append(child)

    graph_nodes = []
    graph_edges = []

    # 先放入事件节点（不含 gate）
    for nid, n in nodes_by_id.items():
        t = str(n.get("type") or "")
        graph_nodes.append(
            {
                "id": nid,
                "label": n.get("name") or nid,
                "type": map_type(t),
                "event": n.get("event"),
                "meta": {"event": n.get("event")} if n.get("event") is not None else {},
            }
        )

    # 为“确实有子事件”的非 basic 事件合成 gate 节点，并把 child->parent 改写为 child->gate、gate->parent
    explicit_gate_ids = {
        nid
        for nid, n in nodes_by_id.items()
        if str(n.get("type") or "") == "gate"
    }
    for l in link_list:
        child = str(l.get("sourceId", ""))
        parent = str(l.get("targetId", ""))
        if child not in nodes_by_id or parent not in nodes_by_id:
            continue
        if child in explicit_gate_ids or parent in explicit_gate_ids:
            graph_edges.append({"id": f"{child}->{parent}", "source": child, "target": parent})

    for nid, n in nodes_by_id.items():
        t = str(n.get("type") or "")
        if t in {"basic_event", "gate"}:
            continue

        if not (children_of.get(nid) or []):
            continue

        has_explicit_gate = any(
            str(nodes_by_id.get(child_id, {}).get("type") or "") == "gate"
            for child_id in children_of.get(nid, []) or []
        )
        if has_explicit_gate:
            continue

        gate_value = n.get("gate")
        gate_str = None
        if isinstance(gate_value, str):
            g = gate_value.strip().upper()
            gate_str = g if g in ("AND", "OR") else "OR"
        else:
            gate_str = "OR"

        gate_id = f"{nid}__gate"
        graph_nodes.append({"id": gate_id, "label": gate_str, "type": "gate"})

        graph_edges.append({"id": f"{gate_id}->{nid}", "source": gate_id, "target": nid})

        for child_id in children_of.get(nid, []) or []:
            graph_edges.append(
                {
                    "id": f"{child_id}->{gate_id}",
                    "source": child_id,
                    "target": gate_id,
                }
            )

    return {"nodes": graph_nodes, "edges": graph_edges}


def _logic_issue_from_service_failure(code: str, message: str) -> list:
    return [
        ValidationIssue(
            level="ERROR",
            code=code,
            message=message,
            source="logic",
        )
    ]


def validate_structure(tree_data: dict) -> list:
    """
    使用 validator-service/main.py 中的规则引擎进行结构/逻辑校验，
    返回 ValidationIssue 列表（source=logic）。
    """
    base_dir = Path(__file__).resolve().parent
    validator_dir = base_dir / "validator-service"
    if not validator_dir.exists():
        return _logic_issue_from_service_failure(
            "VALIDATOR_SERVICE_MISSING",
            "未找到 validator-service 目录，无法进行规则校验。",
        )

    validator_dir_str = str(validator_dir)
    had_path = validator_dir_str in sys.path
    if not had_path:
        sys.path.insert(0, validator_dir_str)
    try:
        mod = _load_py_module("validator_service_main_for_backend", validator_dir / "main.py")

        graph = _tree_to_validator_service_graph(tree_data)
        payload = {"graph": graph}

        out = mod.validate_fault_tree(mod.ValidateRequest(**payload))
        validation = out.get("validation") or {}
        raw_issues = validation.get("issues") or []

        def _downgrade_level(it: dict) -> str:
            """
            validator-service 的部分规则会把“信息不完整”当成 ERROR。
            但在本系统里这些字段允许为空（不阻断提交），只做 INFO 提示。
            """
            level = str(it.get("level") or "WARNING").upper()
            code = str(it.get("code") or "")
            msg = str(it.get("message") or "")
            if code == "MISSING_EVENT_FIELD":
                # 这些字段缺失不应阻断提交（允许后续人工补齐）
                soft = ("description", "priority", "probability", "showProbability", "investigateMethod", "rule", "rules")
                if any(f in msg for f in soft):
                    return "INFO"
            if code in ("NO_ERROR_LEVEL", "NO_DOCUMENTS"):
                return "INFO"
            return level

        issues = []
        for it in raw_issues:
            issues.append(
                ValidationIssue(
                    level=_downgrade_level(it),
                    code=(it.get("code") or "STRUCT_ISSUE"),
                    message=(it.get("message") or ""),
                    node_id="",
                    node_name=",".join(it.get("node_ids") or []),
                    source="logic",
                )
            )
        return issues
    except Exception as e:
        return _logic_issue_from_service_failure(
            "VALIDATOR_SERVICE_FAILED",
            f"规则校验服务调用失败：{e}",
        )
    finally:
        if not had_path:
            try:
                sys.path = [p for p in sys.path if p != validator_dir_str]
            except Exception:
                pass


def validate_semantics(tree_data: dict) -> list:
    node_list = tree_data.get("nodeList", [])
    link_list = tree_data.get("linkList", [])

    children_map = {}
    for link in link_list:
        tgt = link.get("targetId")
        src = link.get("sourceId")
        children_map.setdefault(tgt, []).append(src)

    nodes_by_id = {n["id"]: n for n in node_list}

    lines = []
    for node in node_list:
        nid  = node["id"]
        children_ids = children_map.get(nid, [])
        child_names  = [nodes_by_id[c]["name"] for c in children_ids if c in nodes_by_id]
        if child_names:
            lines.append(f"[{node['type']}] {node['name']} --({node.get('gate')}门)--> {child_names}")
        else:
            lines.append(f"[{node['type']}] {node['name']} （底事件）")

    tree_summary = "\n".join(lines)

    prompt = f"""你是故障树分析（FTA）专家。请审查以下故障树的逻辑合理性。

## 故障树结构
{tree_summary}

## 审查要点
1. 逻辑门是否用对（OR/AND）
2. 因果关系是否合理
3. 底事件是否足够具体可检测
4. 是否存在明显遗漏的重要故障路径

## 输出格式
严格输出JSON数组，没有问题输出[]：
[
  {{
    "level": "WARNING",
    "code": "WRONG_GATE",
    "message": "具体描述问题",
    "node_name": "有问题的节点名称"
  }}
]
level只能是 WARNING 或 INFO。
"""
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=LLM_VALIDATION_MAX_TOKENS,
        )
        raw   = response.choices[0].message.content
        clean = re.sub(r"```json|```", "", raw).strip()
        items = json.loads(clean)
        return [
            ValidationIssue(
                level=i.get("level", "WARNING"),
                code=i.get("code", "SEMANTIC_ISSUE"),
                message=i.get("message", ""),
                node_name=i.get("node_name", ""),
                source="ai",
            )
            for i in items
        ]
    except Exception as e:
        return [
            ValidationIssue(
                "INFO",
                "SEMANTIC_CHECK_FAILED",
                f"语义校验未能完成（{str(e)}），建议人工复查",
                source="ai",
            )
        ]


def validate_full(tree_data: dict, skip_semantic: bool = False) -> dict:
    all_issues = []

    struct_issues = validate_structure(tree_data)
    all_issues.extend(struct_issues)

    has_error = any(i.level == "ERROR" for i in struct_issues)
    if not has_error and not skip_semantic:
        semantic_issues = validate_semantics(tree_data)
        all_issues.extend(semantic_issues)

    error_count   = sum(1 for i in all_issues if i.level == "ERROR")
    warning_count = sum(1 for i in all_issues if i.level == "WARNING")
    info_count    = sum(1 for i in all_issues if i.level == "INFO")

    return {
        "passed":        error_count == 0,
        "error_count":   error_count,
        "warning_count": warning_count,
        "info_count":    info_count,
        "issues":        [i.to_dict() for i in all_issues]
    }


def _normalize_token_usage(usage) -> dict:
    if usage is None:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def validate_semantics(tree_data: dict, *, include_meta: bool = False):
    node_list = tree_data.get("nodeList", [])
    link_list = tree_data.get("linkList", [])
    started = time.perf_counter()

    children_map = {}
    for link in link_list:
        tgt = link.get("targetId")
        src = link.get("sourceId")
        children_map.setdefault(tgt, []).append(src)

    nodes_by_id = {n["id"]: n for n in node_list}

    lines = []
    for node in node_list:
        nid = node["id"]
        children_ids = children_map.get(nid, [])
        child_names = [nodes_by_id[c]["name"] for c in children_ids if c in nodes_by_id]
        if child_names:
            lines.append(f"[{node['type']}] {node['name']} --({node.get('gate')}闂?--> {child_names}")
        else:
            lines.append(f"[{node['type']}] {node['name']} 锛堝簳浜嬩欢锛?")

    tree_summary = "\n".join(lines)

    prompt = f"""浣犳槸鏁呴殰鏍戝垎鏋愶紙FTA锛変笓瀹躲€傝瀹℃煡浠ヤ笅鏁呴殰鏍戠殑閫昏緫鍚堢悊鎬с€?

## 鏁呴殰鏍戠粨鏋?
{tree_summary}

## 瀹℃煡瑕佺偣
1. 閫昏緫闂ㄦ槸鍚︾敤瀵癸紙OR/AND锛?
2. 鍥犳灉鍏崇郴鏄惁鍚堢悊
3. 搴曚簨浠舵槸鍚﹁冻澶熷叿浣撳彲妫€娴?
4. 鏄惁瀛樺湪鏄庢樉閬楁紡鐨勯噸瑕佹晠闅滆矾寰?

## 杈撳嚭鏍煎紡
涓ユ牸杈撳嚭JSON鏁扮粍锛屾病鏈夐棶棰樿緭鍑篬]锛?
[
  {{
    "level": "WARNING",
    "code": "WRONG_GATE",
    "message": "鍏蜂綋鎻忚堪闂",
    "node_name": "鏈夐棶棰樼殑鑺傜偣鍚嶇О"
  }}
]
level鍙兘鏄?WARNING 鎴?INFO銆?
"""
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=LLM_VALIDATION_MAX_TOKENS,
        )
        raw = response.choices[0].message.content
        clean = re.sub(r"```json|```", "", raw).strip()
        items = json.loads(clean)
        issues = [
            ValidationIssue(
                level=i.get("level", "WARNING"),
                code=i.get("code", "SEMANTIC_ISSUE"),
                message=i.get("message", ""),
                node_name=i.get("node_name", ""),
                source="ai",
            )
            for i in items
        ]
        if include_meta:
            return {
                "issues": issues,
                "duration_seconds": round(time.perf_counter() - started, 3),
                "token_usage": _normalize_token_usage(getattr(response, "usage", None)),
                "ran": True,
            }
        return issues
    except Exception as e:
        issues = [
            ValidationIssue(
                "INFO",
                "SEMANTIC_CHECK_FAILED",
                f"语义校验未能完成（{str(e)}），建议人工复查",
                source="ai",
            )
        ]
        if include_meta:
            return {
                "issues": issues,
                "duration_seconds": round(time.perf_counter() - started, 3),
                "token_usage": _normalize_token_usage(None),
                "ran": True,
            }
        return issues


def validate_full(tree_data: dict, skip_semantic: bool = False, *, include_meta: bool = False) -> dict:
    all_issues = []

    structure_started = time.perf_counter()
    struct_issues = validate_structure(tree_data)
    structure_duration = round(time.perf_counter() - structure_started, 3)
    all_issues.extend(struct_issues)

    has_error = any(i.level == "ERROR" for i in struct_issues)
    semantic_meta = {
        "ran": False,
        "duration_seconds": 0.0,
        "token_usage": _normalize_token_usage(None),
    }
    if not has_error and not skip_semantic:
        semantic_result = validate_semantics(tree_data, include_meta=include_meta)
        if include_meta:
            semantic_issues = semantic_result.get("issues") or []
            semantic_meta = {
                "ran": bool(semantic_result.get("ran")),
                "duration_seconds": float(semantic_result.get("duration_seconds") or 0.0),
                "token_usage": semantic_result.get("token_usage") or _normalize_token_usage(None),
            }
        else:
            semantic_issues = semantic_result
        all_issues.extend(semantic_issues)

    error_count = sum(1 for i in all_issues if i.level == "ERROR")
    warning_count = sum(1 for i in all_issues if i.level == "WARNING")
    info_count = sum(1 for i in all_issues if i.level == "INFO")

    result = {
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "issues": [i.to_dict() for i in all_issues],
    }
    if include_meta:
        result["meta"] = {
            "structure_validation": {
                "duration_seconds": structure_duration,
                "error_count": sum(1 for i in struct_issues if i.level == "ERROR"),
                "warning_count": sum(1 for i in struct_issues if i.level == "WARNING"),
                "info_count": sum(1 for i in struct_issues if i.level == "INFO"),
            },
            "semantic_validation": semantic_meta,
        }
    return result
