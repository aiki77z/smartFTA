"""
validator.py —— 故障树逻辑校验模块

适配新格式：{nodeList: [...], linkList: [...]}

校验分两层：
  第一层（结构校验）：纯代码规则，不调大模型
  第二层（语义校验）：调大模型判断逻辑合理性

问题级别：
  ERROR   → 必须修复
  WARNING → 建议修复
  INFO    → 提示信息
"""

from dataclasses import dataclass
from typing import Literal
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
import json
import re
import importlib.util
import sys
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
                # validator-service 的 event 完整性检查优先读 node.event，其次读 node.meta.event
                "event": n.get("event"),
                "meta": {"event": n.get("event")} if n.get("event") is not None else {},
            }
        )

    # 为“确实有子事件”的非 basic 事件合成 gate 节点，并把 child->parent 改写为 child->gate、gate->parent
    # validator-service 通过“是否存在 gate 子节点”判断逻辑门是否定义，因此必须合成。
    # 注意：如果一个 intermediate 节点没有任何子事件，它必须在规则校验中报错（叶子必须是 basic），
    # 因此这里不能给“无子事件”的 intermediate 强行合成 gate，否则会导致错误通过。
    for nid, n in nodes_by_id.items():
        t = str(n.get("type") or "")
        if t == "basic_event":
            continue

        # 没有任何子事件时，不应创建 gate（保持 validator-service 的 leaf intermediate 报错能力）
        if not (children_of.get(nid) or []):
            continue

        gate_value = n.get("gate")
        gate_str = None
        if isinstance(gate_value, str):
            g = gate_value.strip().upper()
            gate_str = g if g in ("AND", "OR") else "OR"
        else:
            # gate 可能是 None / 数字 / 其它，按 OR 兜底
            gate_str = "OR"

        gate_id = f"{nid}__gate"
        graph_nodes.append({"id": gate_id, "label": gate_str, "type": "gate"})

        # gate -> parent（在 validator-service 的 children_map 中 gate 作为 parent 的子节点）
        graph_edges.append({"id": f"{gate_id}->{nid}", "source": gate_id, "target": nid})

        # child -> gate
        for child_id in children_of.get(nid, []) or []:
            graph_edges.append(
                {
                    "id": f"{child_id}->{gate_id}",
                    "source": child_id,
                    "target": gate_id,
                }
            )

    return {"nodes": graph_nodes, "edges": graph_edges}


def validate_structure_via_validator_service(tree_data: dict) -> list:
    """
    使用 validator-service 的规则引擎进行结构/逻辑校验，返回 ValidationIssue 列表（source=logic）。
    若 validator-service 不存在/加载失败，则回退到本文件内置的 validate_structure 规则。
    """
    base_dir = Path(__file__).resolve().parent
    validator_dir = base_dir / "validator-service"
    if not validator_dir.exists():
        return validate_structure(tree_data)

    # 动态加载 validator-service/main.py，并保证其 `import ai_validate` 可以成功
    validator_dir_str = str(validator_dir)
    had_path = validator_dir_str in sys.path
    if not had_path:
        sys.path.insert(0, validator_dir_str)
    try:
        mod = _load_py_module("validator_service_main_for_backend", validator_dir / "main.py")

        graph = _tree_to_validator_service_graph(tree_data)
        payload = {"graph": graph}

        # 直接调用其校验函数（同步）
        out = mod.validate_fault_tree(mod.ValidateRequest(**payload))
        validation = out.get("validation") or {}
        raw_issues = validation.get("issues") or []

        issues = []
        for it in raw_issues:
            issues.append(
                ValidationIssue(
                    level=(it.get("level") or "WARNING"),
                    code=(it.get("code") or "STRUCT_ISSUE"),
                    message=(it.get("message") or ""),
                    node_id="",
                    node_name=",".join(it.get("node_ids") or []),
                    source="logic",
                )
            )
        return issues
    except Exception:
        # 出现任何问题都回退到内置规则，保证生成流程不断
        return validate_structure(tree_data)
    finally:
        if not had_path:
            try:
                sys.path = [p for p in sys.path if p != validator_dir_str]
            except Exception:
                pass

def validate_structure(tree_data: dict) -> list:
    issues = []
    node_list = tree_data.get("nodeList", [])
    link_list = tree_data.get("linkList", [])

    if not node_list:
        issues.append(ValidationIssue("ERROR", "EMPTY_TREE", "故障树为空，没有任何节点"))
        return issues

    # 建立id→node映射
    nodes = {n["id"]: n for n in node_list}

    # 检查1：顶事件数量
    top_events = [n for n in node_list if n.get("type") == "top_event"]
    if len(top_events) == 0:
        issues.append(ValidationIssue("ERROR", "NO_TOP_EVENT", "没有顶事件节点"))
    elif len(top_events) > 1:
        names = [n["name"] for n in top_events]
        issues.append(ValidationIssue("ERROR", "MULTIPLE_TOP_EVENTS", f"存在多个顶事件：{names}"))

    # 从linkList构建 父→子 映射 和 子→父 映射
    children_map = {n["id"]: [] for n in node_list}  # 父id → [子id]
    parent_map   = {n["id"]: [] for n in node_list}  # 子id → [父id]
    for link in link_list:
        src = link.get("sourceId")  # 子节点（原因）
        tgt = link.get("targetId")  # 父节点（结果）
        if src in nodes and tgt in nodes:
            children_map[tgt].append(src)
            parent_map[src].append(tgt)
        else:
            issues.append(ValidationIssue(
                "ERROR", "MISSING_NODE",
                f"linkList中引用了不存在的节点id: sourceId={src}, targetId={tgt}"
            ))

    # 检查2：底事件不能有子节点
    for node in node_list:
        nid = node["id"]
        if node.get("type") == "basic_event" and children_map.get(nid):
            issues.append(ValidationIssue(
                "ERROR", "BASIC_EVENT_HAS_CHILDREN",
                "底事件不能有子节点",
                node_id=nid, node_name=node.get("name", "")
            ))

    # 检查3：中间事件必须有子节点
    for node in node_list:
        nid = node["id"]
        if node.get("type") == "intermediate_event" and not children_map.get(nid):
            issues.append(ValidationIssue(
                "ERROR", "INTERMEDIATE_NO_CHILDREN",
                "中间事件必须有子节点",
                node_id=nid, node_name=node.get("name", "")
            ))

    # 检查4：逻辑门合法性
    for node in node_list:
        gate = node.get("gate")
        nid  = node["id"]
        if node.get("type") != "basic_event":
            if gate not in ("OR", "AND"):
                issues.append(ValidationIssue(
                    "ERROR", "INVALID_GATE",
                    f"逻辑门'{gate}'非法，非底事件必须是OR或AND",
                    node_id=nid, node_name=node.get("name", "")
                ))
        else:
            if gate is not None:
                issues.append(ValidationIssue(
                    "WARNING", "BASIC_EVENT_HAS_GATE",
                    "底事件的gate应为null",
                    node_id=nid, node_name=node.get("name", "")
                ))

    # 检查5：节点名称不能为空
    for node in node_list:
        if not node.get("name", "").strip():
            issues.append(ValidationIssue(
                "ERROR", "EMPTY_NAME", "节点名称为空",
                node_id=node["id"]
            ))

    # 检查6：循环引用
    cycle_nodes = _detect_cycles(nodes, children_map)
    for nid in cycle_nodes:
        issues.append(ValidationIssue(
            "ERROR", "CYCLE_DETECTED", "检测到循环引用",
            node_id=nid, node_name=nodes[nid].get("name", "")
        ))

    # 检查7：孤立节点（非顶事件但没有父节点）
    for node in node_list:
        nid = node["id"]
        if node.get("type") != "top_event" and not parent_map.get(nid):
            issues.append(ValidationIssue(
                "WARNING", "ORPHAN_NODE",
                "孤立节点：没有父节点也不是顶事件",
                node_id=nid, node_name=node.get("name", "")
            ))

    # 检查8：event字段完整性
    required_event_fields = ["id", "name", "description", "errorLevel",
                              "priority", "probability", "showProbability",
                              "rules", "investigateMethod", "documents"]
    for node in node_list:
        nid = node["id"]
        if node.get("type") == "top_event":
            # 顶事件event应为null
            if node.get("event") is not None:
                issues.append(ValidationIssue(
                    "WARNING", "TOP_EVENT_HAS_EVENT",
                    "顶事件的event字段应为null",
                    node_id=nid, node_name=node.get("name", "")
                ))
        else:
            event = node.get("event")
            if event is None:
                issues.append(ValidationIssue(
                    "ERROR", "MISSING_EVENT",
                    "中间事件/底事件必须有event对象",
                    node_id=nid, node_name=node.get("name", "")
                ))
            else:
                # 检查必填字段
                for field in required_event_fields:
                    if field not in event:
                        issues.append(ValidationIssue(
                            "ERROR", "MISSING_EVENT_FIELD",
                            f"event对象缺少字段：{field}",
                            node_id=nid, node_name=node.get("name", "")
                        ))
                # errorLevel必须填写
                if not event.get("errorLevel", "").strip():
                    issues.append(ValidationIssue(
                        "ERROR", "MISSING_ERROR_LEVEL",
                        "errorLevel（故障等级）不能为空",
                        node_id=nid, node_name=node.get("name", "")
                    ))
                # documents为空时给INFO提示
                if not event.get("documents"):
                    issues.append(ValidationIssue(
                        "INFO", "NO_DOCUMENTS",
                        "没有溯源文档（documents为空）",
                        node_id=nid, node_name=node.get("name", "")
                    ))

    return issues


def _detect_cycles(nodes: dict, children_map: dict) -> set:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    cycle_nodes = set()

    def dfs(nid):
        color[nid] = GRAY
        for child_id in children_map.get(nid, []):
            if child_id not in nodes:
                continue
            if color[child_id] == GRAY:
                cycle_nodes.add(nid)
                cycle_nodes.add(child_id)
            elif color[child_id] == WHITE:
                dfs(child_id)
        color[nid] = BLACK

    for nid in nodes:
        if color[nid] == WHITE:
            dfs(nid)
    return cycle_nodes


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
            temperature=0.2
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

    # 结构/逻辑校验：优先使用 validator-service 的规则引擎（合并后同仓库），失败则回退到内置规则
    struct_issues = validate_structure_via_validator_service(tree_data)
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