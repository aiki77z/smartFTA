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

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


@dataclass
class ValidationIssue:
    level:     Literal["ERROR", "WARNING", "INFO"]
    code:      str
    message:   str
    node_id:   str = ""
    node_name: str = ""

    def to_dict(self):
        return {
            "level":     self.level,
            "code":      self.code,
            "message":   self.message,
            "node_id":   self.node_id,
            "node_name": self.node_name
        }


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
                node_name=i.get("node_name", "")
            )
            for i in items
        ]
    except Exception as e:
        return [ValidationIssue("INFO", "SEMANTIC_CHECK_FAILED",
                                f"语义校验未能完成（{str(e)}），建议人工复查")]


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