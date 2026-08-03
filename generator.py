"""
Fault-tree generation pipeline.

New default flow:
1. Parse the prompt and extract `top_event` + optional `requirements`
2. Match the top event directly on the graph
3. Expand a bounded local subgraph around the matched graph node
4. Collect only chunks directly referenced by that subgraph
5. Ask the LLM to fill the final fault-tree JSON from the graph skeleton + evidence
6. Reuse corrections and validator on the generated draft
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from openai import OpenAI

from config import (
    ENABLE_GRAPH_RETRIEVAL,
    GRAPH_TREE_MAX_DEPTH,
    GRAPH_TREE_MAX_NODES,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_GENERATION_MAX_TOKENS,
    LLM_MODEL,
    LLM_PARSE_MAX_TOKENS,
    LLM_REPAIR_MAX_TOKENS,
)
from database import (
    collect_subgraph_chunks,
    expand_scoped_local_fault_subgraph,
    get_graph_node_by_id,
    get_chunks_by_ids,
    hydrate_documents_by_chunk_ids,
    list_graph_top_event_candidates,
    match_top_event_from_graph,
    resolve_selected_file_version_ids,
    resolve_top_event_catalog,
    search_chunks_by_entity_names,
    search_chunks_by_keywords,
)
from validator import validate_full

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

MAX_RETRY = 2
MAX_CHUNKS_FOR_PROMPT = 12
MAX_CHUNK_CHARS = 360
MAX_JSON_REPAIR_RETRY = 1
PROPERTY_FIELDS = (
    "description",
    "errorLevel",
    "priority",
    "probability",
    "showProbability",
    "rule",
    "rules",
    "investigateMethod",
)


def normalize_top_event_name(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = value.replace("（", "(").replace("）", ")")
    value = value.replace("：", ":")
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip("\"'[]{}<>")
    value = re.sub(r"^(请分析|分析|请生成|生成|请针对|针对|关于|处理|诊断|排查)", "", value)
    value = re.sub(r"(故障树|故障分析|故障诊断|排查建议)$", "", value)
    value = value.strip(",:;，。； ")
    return re.sub(r"\s+", " ", value).strip()


def build_top_event_normalized_candidates(name: str, aliases: Optional[List[str]] = None) -> List[str]:
    candidates: List[str] = []

    def add(value: str):
        value = normalize_top_event_name(value)
        if value and value not in candidates:
            candidates.append(value)
        compact = re.sub(r"\s+", "", value)
        if compact and compact not in candidates:
            candidates.append(compact)

    for item in [name] + list(aliases or []):
        add(item)
    return candidates


def parse_user_prompt(prompt: str) -> dict:
    raw = str(prompt or "").strip()
    if not raw:
        raise ValueError("prompt 不能为空")

    normalized = re.sub(r"\s+", " ", raw).strip()
    patterns = [
        r"顶事件(?:为|是)?[:：]?\s*(.+)$",
        r"分析(.+?)(?:故障树|故障|异常)?$",
        r"生成(.+?)(?:故障树)?$",
        r"针对(.+?)(?:进行|生成|分析)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            top_event = normalize_top_event_name(match.group(1))
            if top_event:
                return {"top_event": top_event, "requirements": ""}

    if len(normalized) <= 40 and "\n" not in normalized:
        return {"top_event": normalize_top_event_name(normalized), "requirements": ""}

    prompt_text = f"""
你是工业设备故障树系统的提示词解析器。
请从用户输入中提取：
1. top_event：用户要分析的顶事件
2. requirements：额外要求，没有就返回空字符串

只输出 JSON：
{{
  "top_event": "...",
  "requirements": "..."
}}

用户输入：
{normalized}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt_text}],
        temperature=0.1,
        max_tokens=LLM_PARSE_MAX_TOKENS,
    )
    parsed = _parse_json(response.choices[0].message.content)
    top_event = normalize_top_event_name(parsed.get("top_event"))
    if not top_event:
        raise ValueError("无法从 prompt 中提取顶事件")
    return {"top_event": top_event, "requirements": str(parsed.get("requirements") or "").strip()}


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _node_is_and(node: Dict[str, Any]) -> bool:
    node_type = str(node.get("node_type") or "").upper()
    if node_type == "AND":
        return True
    return "LogicGate" in (node.get("labels") or [])


def _subgraph_nodes_by_id(subgraph_bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {node["graph_node_id"]: node for node in (subgraph_bundle.get("nodes") or []) if node.get("graph_node_id")}


def _dedupe_values(values: List[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values or []:
        if value in (None, ""):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _parse_json_list_safe(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _edge_documents_seed(edge: Dict[str, Any]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for ref in edge.get("source_chunk_refs") or edge.get("source_chunk_ids") or []:
        if ref not in (None, ""):
            docs.append({"chunk_id": ref})
    for item in _parse_json_list_safe(edge.get("evidence_json")):
        if not isinstance(item, dict):
            continue
        chunk_id = item.get("chunk_id")
        if chunk_id not in (None, ""):
            docs.append({"chunk_id": chunk_id})
    return _dedupe_document_refs(docs)


def _edge_detail_payload(edge: Dict[str, Any]) -> Dict[str, Any]:
    evidence = [item for item in _parse_json_list_safe(edge.get("evidence_json")) if isinstance(item, dict)]
    evidence_texts = _dedupe_values([item.get("text") for item in evidence if item.get("text")])
    return {
        "relation_type": edge.get("relation_type") or "",
        "relation_type_code": edge.get("relation_type_code") or "",
        "relation_id": edge.get("relation_id") or "",
        "polarity": edge.get("polarity") or "",
        "certainty": edge.get("certainty") or "",
        "cross_chunk": edge.get("cross_chunk") or "",
        "source_relation_ids": edge.get("source_relation_ids") or [],
        "source_chunk_ids": edge.get("source_chunk_ids") or [],
        "source_chunk_refs": edge.get("source_chunk_refs") or [],
        "source_file_version_ids": edge.get("source_file_version_ids") or [],
        "evidence": evidence,
        "evidence_texts": evidence_texts,
        "documents": _edge_documents_seed(edge),
    }


def _compress_subgraph_to_tree_skeleton(subgraph_bundle: Dict[str, Any]) -> Dict[str, Any]:
    nodes_by_id = _subgraph_nodes_by_id(subgraph_bundle)
    root_id = subgraph_bundle["root"]
    root_node = nodes_by_id[root_id]

    incoming: Dict[str, List[Dict[str, Any]]] = {}
    outgoing: Dict[str, List[Dict[str, Any]]] = {}
    for edge in subgraph_bundle.get("edges") or []:
        incoming.setdefault(edge["target_graph_node_id"], []).append(edge)
        outgoing.setdefault(edge["source_graph_node_id"], []).append(edge)

    fault_nodes = {node_id: node for node_id, node in nodes_by_id.items() if not _node_is_and(node)}
    tree_nodes: List[Dict[str, Any]] = []
    tree_links: List[Dict[str, Any]] = []
    graph_to_tree: Dict[str, str] = {}
    documents_by_node_id: Dict[str, List[Dict[str, Any]]] = {}
    gate_by_tree_id: Dict[str, Optional[str]] = {}

    for graph_node_id, node in fault_nodes.items():
        tree_node_id = _stable_id("node", graph_node_id)
        graph_to_tree[graph_node_id] = tree_node_id
        direct_fault_children = [
            edge["source_graph_node_id"]
            for edge in incoming.get(graph_node_id, [])
            if edge["source_graph_node_id"] in fault_nodes
        ]
        and_gate_children = [
            edge["source_graph_node_id"]
            for edge in incoming.get(graph_node_id, [])
            if edge["source_graph_node_id"] not in fault_nodes and _node_is_and(nodes_by_id[edge["source_graph_node_id"]])
        ]
        has_children = bool(direct_fault_children or and_gate_children)
        if graph_node_id == root_id:
            node_type = "top_event"
        else:
            node_type = "intermediate_event" if has_children else "basic_event"

        gate: Optional[str] = None
        if and_gate_children and not direct_fault_children:
            gate = "AND"
        elif has_children:
            gate = "OR"

        tree_nodes.append(
            {
                "id": tree_node_id,
                "name": node.get("name"),
                "type": node_type,
                "gate": gate,
                "graphNodeId": graph_node_id,
                "kg_key": graph_node_id,
                "fileId": node.get("file_id"),
                "fileVersionId": node.get("file_version_id"),
                "source_chunk_ids": node.get("source_chunk_ids") or [],
                "source_chunk_refs": node.get("source_chunk_refs") or [],
                "graph_props": {field: node.get(field) for field in PROPERTY_FIELDS},
                "documents_seed": node.get("documents") or [],
            }
        )
        documents_by_node_id[tree_node_id] = []
        gate_by_tree_id[tree_node_id] = gate

    for parent_graph_id, parent_node in fault_nodes.items():
        parent_tree_id = graph_to_tree[parent_graph_id]
        for edge in incoming.get(parent_graph_id, []):
            child_graph_id = edge["source_graph_node_id"]
            if child_graph_id in fault_nodes:
                child_tree_id = graph_to_tree[child_graph_id]
                tree_links.append(
                    {
                        "type": "link",
                        "sourceId": child_tree_id,
                        "targetId": parent_tree_id,
                        "isCondition": False,
                        "relation": _edge_detail_payload(edge),
                    }
                )
                continue

            gate_node = nodes_by_id.get(child_graph_id)
            if not gate_node or not _node_is_and(gate_node):
                continue
            for gate_edge in incoming.get(child_graph_id, []):
                gate_child_graph_id = gate_edge["source_graph_node_id"]
                if gate_child_graph_id not in fault_nodes:
                    continue
                tree_links.append(
                    {
                        "type": "link",
                        "sourceId": graph_to_tree[gate_child_graph_id],
                        "targetId": parent_tree_id,
                        "isCondition": False,
                        "relation": {
                            "gate_type": "AND",
                            "member_relation": _edge_detail_payload(gate_edge),
                            "gate_relation": _edge_detail_payload(edge),
                        },
                    }
                )

    deduped_links = []
    seen = set()
    for link in tree_links:
        key = (link["sourceId"], link["targetId"])
        if key not in seen:
            seen.add(key)
            deduped_links.append(link)

    return {
        "root_tree_node_id": graph_to_tree[root_id],
        "nodes": tree_nodes,
        "links": deduped_links,
        "graph_to_tree": graph_to_tree,
        "gate_by_tree_id": gate_by_tree_id,
    }


def _format_evidence_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    evidence = []
    for chunk in chunks[:MAX_CHUNKS_FOR_PROMPT]:
        content = _chunk_content_excerpt(chunk, limit=MAX_CHUNK_CHARS)
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS] + "..."
        evidence.append(
            {
                "chunk_uid": chunk.get("chunk_uid"),
                "chunk_id": chunk.get("id", chunk.get("chunk_id")),
                "chunk_name": chunk.get("chunk_name") or chunk.get("title") or chunk.get("heading") or chunk.get("chapter") or "",
                "section_path": chunk.get("section_path") or chunk.get("section") or chunk.get("chapter") or "",
                "source_page": chunk.get("source_page") or chunk.get("source") or chunk.get("page") or "",
                "file_id": chunk.get("file_id", ""),
                "file_version_id": chunk.get("file_version_id", ""),
                "file": chunk.get("file", ""),
                "content": content,
            }
        )
    return evidence


def build_fault_tree_from_subgraph_and_chunks(
    top_event: str,
    subgraph_bundle: Dict[str, Any],
    evidence_chunks: List[Dict[str, Any]],
    requirements: str = "",
    part_details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    skeleton = _compress_subgraph_to_tree_skeleton(subgraph_bundle)
    evidence = _format_evidence_chunks(evidence_chunks)
    skeleton_for_prompt = {
        "root_tree_node_id": skeleton["root_tree_node_id"],
        "nodes": [
            {
                "id": node["id"],
                "name": node["name"],
                "type": node["type"],
                "gate": node["gate"],
                "graphNodeId": node["graphNodeId"],
                "kg_key": node["kg_key"],
                "fileId": node["fileId"],
                "fileVersionId": node["fileVersionId"],
                "graph_props": node["graph_props"],
                "source_chunk_ids": node["source_chunk_ids"],
                "source_chunk_refs": node["source_chunk_refs"],
                "documents_seed": node["documents_seed"],
            }
            for node in skeleton["nodes"]
        ],
        "links": skeleton["links"],
    }

    pd = part_details if isinstance(part_details, dict) and part_details else None
    part_details_text = json.dumps(pd, ensure_ascii=False, indent=2) if pd else ""
    physical_ref_block = ""
    if pd:
        physical_ref_block = f"""
11. 在构建故障树的每一个事件节点时，请在 description 字段的末尾，按照 [Ref: Object_X] 的格式标注其物理关联组件。
其中，Object_X 是该事件对应的物理组件 ID，如：Object_2、Object_3、Object_4 等。
这是为了模拟工业标准中「逻辑位点与物理备件」的对标。
以下是该设备的物理组件列表：
{part_details_text}
"""

    prompt = f"""
你是工业设备故障树生成助手。请根据“图谱局部子图骨架”和“精简证据 chunks”输出最终故障树 JSON。

要求：
1. 必须保留骨架中的节点和连线，不要凭空新增结构。
2. 图谱中没有单独的 OR 节点；如果某个父节点有多个子节点且未标明 AND，默认 gate=OR。
3. 如果骨架中父节点 gate=AND，则保留为 AND。
4. 顶事件节点 type=top_event，且要像其他故障事件节点一样保留完整 event 字段。
5. intermediate_event 和 basic_event 必须有完整 event 字段。
6. event 中优先使用 graph_props 里的字段，chunks 只用于补充和润色。
7. documents 只能输出轻量溯源引用字段，且每个节点最多 1-3 条：
   chunk_id、chunk_name、section_path、source_page、file_id、file_version_id。
   严禁在 documents 中输出 content、text、raw_text、page_content、file、chunk_uid 等正文或额外字段。
   chunk 正文由前端在节点详情展开时，按 file_version_id + chunk_id 从后端动态查询，不要写入故障树 JSON。
8. 每个节点都保留 graphNodeId 和 kg_key，值与输入骨架一致。
9. rules 使用数组；如果只有单条 rule 字符串，也请把它转成一条规则对象或留空数组。不要输出未定义字段。
10. 只输出 JSON，不要输出 markdown。{physical_ref_block}

输出格式：
{{
  "nodeList": [...],
  "linkList": [...]
}}

顶事件：{top_event}
额外要求：{requirements or "无"}

图谱骨架：
{json.dumps(skeleton_for_prompt, ensure_ascii=False, indent=2)}

证据 chunks：
{json.dumps(evidence, ensure_ascii=False, indent=2)}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=LLM_GENERATION_MAX_TOKENS,
    )
    tree = _parse_json(response.choices[0].message.content)
    return _post_process_generated_tree(tree, skeleton, evidence)


def _coerce_rules(rule_value: Any) -> List[Dict[str, Any]]:
    if isinstance(rule_value, list):
        rules = []
        for item in rule_value:
            if isinstance(item, dict):
                rules.append(
                    {
                        "deviceTypeId": str(item.get("deviceTypeId") or ""),
                        "measurePointName": str(item.get("measurePointName") or ""),
                        "symbol": str(item.get("symbol") or ""),
                        "thresholds": [str(x) for x in (item.get("thresholds") or [])],
                        "duration": str(item.get("duration") or ""),
                    }
                )
        return rules
    if isinstance(rule_value, str) and rule_value.strip():
        return [
            {
                "deviceTypeId": "",
                "measurePointName": rule_value.strip(),
                "symbol": "",
                "thresholds": [],
                "duration": "",
            }
        ]
    return []


_REF_SUFFIX_RE = re.compile(r"\s*\[Ref:\s*Object_\d+\s*\]\s*$", re.IGNORECASE)


def _sorted_object_keys(part_details: Dict[str, Any]) -> List[str]:
    keys = [str(k) for k in part_details.keys() if str(k).strip().startswith("Object_")]

    def _obj_num(k: str) -> int:
        m = re.search(r"Object_(\d+)", str(k))
        return int(m.group(1)) if m else 999

    keys.sort(key=lambda x: (_obj_num(x), x))
    return keys


def _fallback_object_key(part_details: Dict[str, Any]) -> str:
    keys = _sorted_object_keys(part_details)
    return keys[0] if keys else "Object_2"


def _best_object_key_for_event_label(label: str, part_details: Dict[str, Any]) -> Optional[str]:
    """Match event name / text to a mesh key (Object_N) using part name / id overlap."""
    raw = str(label or "").strip()
    if not raw:
        return None
    label_l = raw.lower()
    label_compact = re.sub(r"\s+", "", raw).lower()
    best_k: Optional[str] = None
    best_score = 0.0
    for obj_key, meta in part_details.items():
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name") or "").strip()
        pid = str(meta.get("id") or "").strip()
        score = 0.0
        if name:
            if name in raw:
                score = max(score, min(1.0, len(name) / max(len(raw), 1)))
            elif raw in name:
                score = max(score, 0.42)
            name_c = re.sub(r"\s+", "", name).lower()
            if name_c and name_c in label_compact:
                score = max(score, 0.48)
        if pid:
            pl = pid.lower()
            if pl and pl in label_l:
                score = max(score, 0.52)
        sk = str(obj_key)
        if sk and sk in raw:
            score = max(score, 0.35)
        if score > best_score:
            best_score = score
            best_k = str(obj_key)
    return best_k if best_score >= 0.12 else None


def apply_physical_refs_to_fault_tree_data(
    tree: Dict[str, Any],
    part_details: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Ensure non-top event nodes end with `` [Ref: Object_N]`` when part_details is provided.
    LLMs often skip the instruction; this is a deterministic post-pass.
    """
    pd = part_details if isinstance(part_details, dict) and part_details else None
    if not pd:
        return tree
    out = dict(tree)
    nodes_in = out.get("nodeList") or []
    fb = _fallback_object_key(pd)
    new_nodes: List[Dict[str, Any]] = []
    for n in nodes_in:
        if not isinstance(n, dict):
            new_nodes.append(n)
            continue
        nt = str(n.get("type") or "")
        if nt == "top_event":
            new_nodes.append(n)
            continue
        ev = n.get("event")
        if not isinstance(ev, dict):
            new_nodes.append(n)
            continue
        name = str(n.get("name") or ev.get("name") or "")
        desc = str(ev.get("description") or "")
        if _REF_SUFFIX_RE.search(desc):
            new_nodes.append(n)
            continue
        obj_key = _best_object_key_for_event_label(name, pd) or _best_object_key_for_event_label(desc, pd) or fb
        base = _REF_SUFFIX_RE.sub("", desc).rstrip()
        suffix = f" [Ref: {obj_key}]"
        new_desc = (base + suffix).strip() if base else f"[Ref: {obj_key}]"
        ev2 = dict(ev)
        ev2["description"] = new_desc
        nn = dict(n)
        nn["event"] = ev2
        new_nodes.append(nn)
    out["nodeList"] = new_nodes
    return out


_ALLOWED_DOCUMENT_FIELDS = (
    "chunk_id",
    "chunk_name",
    "section_path",
    "source_page",
    "file_id",
    "file_version_id",
)


def _document_ref_key(doc: Dict[str, Any]) -> str:
    if not isinstance(doc, dict):
        return ""
    chunk_id = doc.get("chunk_id")
    file_version_id = doc.get("file_version_id") or doc.get("fileVersionId") or ""
    if chunk_id in (None, ""):
        return ""
    return f"{file_version_id}::{chunk_id}" if file_version_id else str(chunk_id)


def _dedupe_document_refs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        key = _document_ref_key(doc)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(doc)
    return result


def _sanitize_fault_tree_documents(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Keep fault-tree evidence references lightweight; chunk content is loaded on demand."""
    if not isinstance(tree, dict):
        return tree
    out = dict(tree)
    sanitized_nodes: List[Dict[str, Any]] = []
    for node in out.get("nodeList") or []:
        if not isinstance(node, dict):
            sanitized_nodes.append(node)
            continue
        next_node = dict(node)
        event = next_node.get("event")
        if isinstance(event, dict):
            next_event = dict(event)
            docs = []
            for doc in next_event.get("documents") or []:
                if isinstance(doc, dict):
                    docs.append({field: doc.get(field, "") for field in _ALLOWED_DOCUMENT_FIELDS})
            next_event["documents"] = _dedupe_document_refs(docs)
            next_node["event"] = next_event
        sanitized_nodes.append(next_node)
    out["nodeList"] = sanitized_nodes
    sanitized_links: List[Dict[str, Any]] = []
    for link in out.get("linkList") or []:
        if not isinstance(link, dict):
            sanitized_links.append(link)
            continue
        next_link = dict(link)
        relation = next_link.get("relation")
        if isinstance(relation, dict):
            next_relation = dict(relation)
            docs = []
            for doc in next_relation.get("documents") or []:
                if isinstance(doc, dict):
                    docs.append({field: doc.get(field, "") for field in _ALLOWED_DOCUMENT_FIELDS})
            next_relation["documents"] = _dedupe_document_refs(docs)
            next_link["relation"] = next_relation
        sanitized_links.append(next_link)
    out["linkList"] = sanitized_links
    return out


def _chunk_content_excerpt(chunk: Dict[str, Any], limit: int = 220) -> str:
    for key in ("content", "text", "raw_text", "page_content", "body", "markdown"):
        value = chunk.get(key)
        if value not in (None, ""):
            text = re.sub(r"\s+", " ", str(value).strip())
            return text[:limit] + ("..." if len(text) > limit else "")
    return ""


def _post_process_generated_tree(
    tree: Dict[str, Any],
    skeleton: Dict[str, Any],
    evidence_chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    skeleton_nodes = {node["id"]: node for node in skeleton["nodes"]}
    graph_to_node = {node["graphNodeId"]: node for node in skeleton["nodes"]}
    evidence_doc_map: Dict[str, Dict[str, Any]] = {}
    for chunk in evidence_chunks:
        doc_ref = {
            "chunk_id": chunk.get("chunk_id"),
            "chunk_name": chunk.get("chunk_name") or chunk.get("title") or chunk.get("heading") or chunk.get("chapter") or "",
            "section_path": chunk.get("section_path") or chunk.get("section") or chunk.get("chapter") or "",
            "source_page": chunk.get("source_page") or chunk.get("source") or chunk.get("page") or "",
            "file_id": chunk.get("file_id", ""),
            "file_version_id": chunk.get("file_version_id", ""),
        }
        for key in (
            chunk.get("chunk_uid"),
            chunk.get("chunk_id"),
            f"{chunk.get('file_version_id')}::{chunk.get('chunk_id')}" if chunk.get("file_version_id") and chunk.get("chunk_id") not in (None, "") else None,
        ):
            if key not in (None, ""):
                evidence_doc_map[str(key)] = doc_ref

    def hydrate_relation_documents(relation: Dict[str, Any], fallback_file_version_id: str = "") -> Dict[str, Any]:
        if not isinstance(relation, dict):
            return {}
        next_relation = dict(relation)
        chunk_ids: List[Any] = []
        for doc in next_relation.get("documents") or []:
            if isinstance(doc, dict) and doc.get("chunk_id") not in (None, ""):
                chunk_ids.append(doc.get("chunk_id"))
        for item in next_relation.get("evidence") or []:
            if isinstance(item, dict) and item.get("chunk_id") not in (None, ""):
                chunk_ids.append(item.get("chunk_id"))
        chunk_ids.extend(next_relation.get("source_chunk_refs") or next_relation.get("source_chunk_ids") or [])
        docs = []
        for chunk_id in dict.fromkeys(chunk_ids):
            chunk_ref = str(chunk_id)
            if fallback_file_version_id and "::" not in chunk_ref:
                chunk_ref = f"{fallback_file_version_id}::{chunk_id}"
            if chunk_ref in evidence_doc_map:
                docs.append(evidence_doc_map[chunk_ref])
            elif str(chunk_id) in evidence_doc_map:
                docs.append(evidence_doc_map[str(chunk_id)])
            elif chunk_id not in (None, ""):
                docs.append({"chunk_id": chunk_id, "file_version_id": fallback_file_version_id})
        next_relation["documents"] = _dedupe_document_refs(docs)
        return next_relation

    node_list = tree.get("nodeList") or []
    if not node_list:
        node_list = []
        for node in skeleton["nodes"]:
            node_list.append(
                {
                    "type": node["type"],
                    "gate": node["gate"],
                    "name": node["name"],
                    "id": node["id"],
                    "transfer": "",
                    "event": None if node["type"] == "top_event" else {},
                    "graphNodeId": node["graphNodeId"],
                    "kg_key": node["kg_key"],
                }
            )

    normalized_nodes = []
    for index, node in enumerate(node_list, start=1):
        node_id = node.get("id") or _stable_id("node", f"generated-{index}")
        graph_node_id = node.get("graphNodeId") or node.get("kg_key")
        skeleton_node = None
        if graph_node_id and graph_node_id in graph_to_node:
            skeleton_node = graph_to_node[graph_node_id]
        elif node_id in skeleton_nodes:
            skeleton_node = skeleton_nodes[node_id]

        node_type = node.get("type") or (skeleton_node.get("type") if skeleton_node else "basic_event")
        gate = node.get("gate") if node.get("gate") is not None else (skeleton_node.get("gate") if skeleton_node else None)
        name = node.get("name") or (skeleton_node.get("name") if skeleton_node else "")
        file_id = node.get("fileId") or (skeleton_node.get("fileId") if skeleton_node else None)
        file_version_id = node.get("fileVersionId") or (skeleton_node.get("fileVersionId") if skeleton_node else None)
        if False and node_type == "top_event":
            event = None
        else:
            graph_props = dict((skeleton_node or {}).get("graph_props") or {})
            event = dict(node.get("event") or {})
            merged = {}
            merged["id"] = str(event.get("id") or f"E{index:03d}")
            merged["name"] = name
            merged["description"] = str(event.get("description") or graph_props.get("description") or f"{name}相关异常")
            merged["errorLevel"] = str(event.get("errorLevel") or graph_props.get("errorLevel") or "中")
            merged["priority"] = event.get("priority", graph_props.get("priority", 0))
            merged["probability"] = event.get("probability", graph_props.get("probability", 1e-8))
            merged["showProbability"] = event.get("showProbability", graph_props.get("showProbability", merged["probability"]))
            merged["rule"] = str(event.get("rule") or graph_props.get("rule") or "")
            merged["rules"] = _coerce_rules(event.get("rules") or graph_props.get("rules") or merged["rule"])
            merged["investigateMethod"] = str(
                event.get("investigateMethod") or graph_props.get("investigateMethod") or f"检查{name}相关状态与报警记录"
            )
            chunk_ids = []
            for chunk_id in (skeleton_node or {}).get("source_chunk_refs") or []:
                if chunk_id not in (None, ""):
                    chunk_ids.append(chunk_id)
            for chunk_id in (skeleton_node or {}).get("source_chunk_ids") or []:
                if chunk_id not in (None, ""):
                    chunk_ids.append(chunk_id)
            for chunk_id in node.get("source_chunk_refs") or []:
                if chunk_id not in (None, ""):
                    chunk_ids.append(chunk_id)
            for chunk_id in node.get("source_chunk_ids") or []:
                if chunk_id not in (None, ""):
                    chunk_ids.append(chunk_id)
            for doc in (skeleton_node or {}).get("documents_seed") or []:
                if doc.get("chunk_id") not in (None, ""):
                    chunk_ids.append(doc.get("chunk_id"))
            for doc in event.get("documents") or []:
                if isinstance(doc, dict) and doc.get("chunk_id") not in (None, ""):
                    chunk_ids.append(doc.get("chunk_id"))
            documents = []
            for chunk_id in dict.fromkeys(chunk_ids):
                chunk_ref = str(chunk_id)
                if file_version_id and "::" not in chunk_ref:
                    chunk_ref = f"{file_version_id}::{chunk_id}"
                if chunk_ref in evidence_doc_map:
                    documents.append(evidence_doc_map[chunk_ref])
                elif str(chunk_id) in evidence_doc_map:
                    documents.append(evidence_doc_map[str(chunk_id)])
            if not documents:
                documents = _match_documents_for_event(name, evidence_chunks, limit=2)
            merged["documents"] = _dedupe_document_refs(documents)
            event = merged

        normalized_nodes.append(
            {
                "type": node_type,
                "gate": gate,
                "name": name,
                "id": node_id,
                "transfer": node.get("transfer", ""),
                "event": event,
                "graphNodeId": graph_node_id or (skeleton_node.get("graphNodeId") if skeleton_node else None),
                "kg_key": graph_node_id or (skeleton_node.get("kg_key") if skeleton_node else None),
                "fileId": file_id,
                "fileVersionId": file_version_id,
            }
        )

    node_ids = {node["id"] for node in normalized_nodes}
    link_list = []
    seen = set()
    skeleton_link_map = {
        (link.get("sourceId"), link.get("targetId")): link
        for link in skeleton.get("links") or []
        if isinstance(link, dict)
    }
    for link in tree.get("linkList") or skeleton["links"]:
        source_id = link.get("sourceId")
        target_id = link.get("targetId")
        if source_id not in node_ids or target_id not in node_ids:
            continue
        key = (source_id, target_id)
        if key in seen:
            continue
        seen.add(key)
        skeleton_link = skeleton_link_map.get((source_id, target_id), {})
        relation = link.get("relation") if isinstance(link.get("relation"), dict) else None
        if not relation:
            relation = skeleton_link.get("relation") if isinstance(skeleton_link.get("relation"), dict) else {}
        relation = hydrate_relation_documents(relation)
        link_list.append(
            {
                "type": "link",
                "sourceId": source_id,
                "targetId": target_id,
                "isCondition": False,
                "relation": relation,
            }
        )

    return _sanitize_fault_tree_documents({"nodeList": normalized_nodes, "linkList": link_list})


def repair_fault_tree(
    draft_tree: dict,
    corrections_hint: str,
    chunks: list,
    *,
    include_meta: bool = False,
) -> dict:
    started = time.perf_counter()
    prompt = f"""
你会收到一棵草稿故障树和一组历史修正建议。
请只做最小必要修改，尽量保持节点 id 和主体结构不变。

草稿树：
{json.dumps(draft_tree, ensure_ascii=False, indent=2)}

可引用的 chunks：
{json.dumps(_format_evidence_chunks(chunks), ensure_ascii=False, indent=2)}

历史修正建议：
{corrections_hint}

只输出修复后的完整 JSON：
{{
  "nodeList": [...],
  "linkList": [...]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=LLM_REPAIR_MAX_TOKENS,
    )
    repaired = _sanitize_fault_tree_documents(_parse_json(response.choices[0].message.content))
    if include_meta:
        return {
            "tree": repaired,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "token_usage": _normalize_token_usage(getattr(response, "usage", None)),
        }
    return repaired


def generate_fault_tree(
    top_event: str,
    requirements: str = "",
    selected_file_version_ids: Optional[List[str]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    def emit(message: str):
        try:
            print(message)
        finally:
            if log_callback:
                try:
                    log_callback(message)
                except Exception:
                    pass

    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
    emit(f"[graph-match] matching top event '{top_event}'")
    matched = match_top_event_from_graph(
        top_event,
        normalized_candidates=normalized_candidates,
        selected_file_version_ids=scoped_file_version_ids,
    )
    matched_node = matched["matched_node"]
    emit(f"[graph-match] matched '{top_event}' -> '{matched['matched_name']}'")

    root_node_ids = [item.get("graph_node_id") for item in (matched.get("matched_nodes") or []) if item.get("graph_node_id")]
    subgraph_bundle = expand_scoped_local_fault_subgraph(
        root_node_ids or [matched["matched_node_id"]],
        max_depth=GRAPH_TREE_MAX_DEPTH,
        max_nodes=GRAPH_TREE_MAX_NODES,
        selected_file_version_ids=scoped_file_version_ids,
    )
    emit(
        f"[graph-subgraph] root={matched['matched_name']} roots={len(subgraph_bundle.get('roots') or [])} "
        f"nodes={len(subgraph_bundle.get('nodes') or [])} edges={len(subgraph_bundle.get('edges') or [])}"
    )
    if subgraph_bundle.get("disabled_cycle_edges"):
        emit(
            "[graph-subgraph] temporarily disabled "
            f"{len(subgraph_bundle.get('disabled_cycle_edges') or [])} causal cycle edges in selected file view"
        )
    if subgraph_bundle.get("pruned_transitive_edges"):
        emit(
            "[graph-subgraph] temporarily pruned "
            f"{len(subgraph_bundle.get('pruned_transitive_edges') or [])} transitive shortcut edges in selected file view"
        )

    chunk_ids = collect_subgraph_chunks(subgraph_bundle, chunk_limit=MAX_CHUNKS_FOR_PROMPT)
    evidence_chunks = hydrate_documents_by_chunk_ids(chunk_ids, selected_file_version_ids=scoped_file_version_ids)
    raw_chunk_docs = []
    chunk_doc_map = {
        str(item.get("chunk_uid") or item["chunk_id"]): item
        for item in evidence_chunks
    }
    for chunk_id in chunk_ids:
        doc = chunk_doc_map.get(str(chunk_id))
        if doc:
            raw_chunk_docs.append(doc)
    from database import get_chunks_by_ids  # local import to keep module surface small

    raw_chunks = get_chunks_by_ids(
        chunk_ids,
        limit=max(len(chunk_ids), 1),
        selected_file_version_ids=scoped_file_version_ids,
    )
    emit(f"[graph-chunks] collected {len(raw_chunks)} evidence chunks")

    draft_tree = None
    previous_issues = None
    for attempt in range(1, MAX_RETRY + 2):
        emit(f"[graph-llm] generating draft tree attempt={attempt}")
        try:
            draft_tree = build_fault_tree_from_subgraph_and_chunks(
                top_event=matched["matched_name"],
                subgraph_bundle=subgraph_bundle,
                evidence_chunks=raw_chunks,
                requirements=requirements,
            )
            emit(
                f"[graph-draft] generated draft attempt={attempt} "
                f"nodes={len(draft_tree.get('nodeList') or [])} links={len(draft_tree.get('linkList') or [])}"
            )
        except ValueError as exc:
            emit(f"[graph-draft] draft generation error attempt={attempt} error={exc}")
            if attempt > MAX_RETRY:
                raise
            previous_issues = [{"level": "ERROR", "message": str(exc)}]
            emit(f"[graph-regenerate] retrying draft generation next_attempt={attempt + 1}")
            continue

        emit(f"[graph-validate] validating draft attempt={attempt}")
        validation = validate_full(draft_tree, skip_semantic=True)
        draft_tree["validation"] = validation
        if validation["passed"]:
            emit(
                f"[graph-validate] validation passed attempt={attempt} "
                f"errors={validation['error_count']} warnings={validation['warning_count']}"
            )
            break
        previous_issues = validation["issues"]
        emit(
            f"[graph-validate] validation failed attempt={attempt} "
            f"errors={validation['error_count']} warnings={validation['warning_count']}"
        )
        for issue in (validation.get("issues") or [])[:6]:
            emit(
                "[graph-validate-issue] "
                f"level={issue.get('level')} code={issue.get('code')} "
                f"node={issue.get('node_id') or issue.get('node_name') or ''} "
                f"message={issue.get('message')}"
            )
        if attempt <= MAX_RETRY:
            emit(f"[graph-regenerate] retrying draft generation next_attempt={attempt + 1}")
        if attempt > MAX_RETRY:
            break

    if draft_tree is None:
        raise ValueError(f"Failed to build tree for '{top_event}'")

    final_tree = draft_tree
    try:
        from diff_analyzer import format_corrections_for_repair, get_relevant_corrections

        corrections = get_relevant_corrections(draft_tree)
        if corrections:
            emit(f"[history-repair] applying {len(corrections)} relevant corrections")
            repaired = repair_fault_tree(draft_tree, format_corrections_for_repair(corrections), raw_chunks)
            emit("[graph-validate] validating repaired draft")
            repair_validation = validate_full(repaired, skip_semantic=True)
            if repair_validation["passed"]:
                repaired["validation"] = repair_validation
                final_tree = repaired
                emit(
                    f"[history-repair] repaired draft accepted "
                    f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                )
            else:
                emit(
                    f"[history-repair] repaired draft rejected "
                    f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                )
        else:
            emit("[history-repair] no relevant corrections, skipped")
    except Exception as exc:
        emit(f"[history-repair] skipped due to error: {exc}")

    emit("[graph-validate] validating final tree")
    final_validation = validate_full(final_tree, skip_semantic=False)
    final_tree["validation"] = final_validation
    emit(
        f"[graph-validate] final validation "
        f"{'passed' if final_validation['passed'] else 'failed'} "
        f"errors={final_validation['error_count']} warnings={final_validation['warning_count']}"
    )
    evidence_chunk_ids = [chunk.get("chunk_uid") or chunk.get("chunk_id") for chunk in raw_chunks if chunk.get("chunk_uid") or chunk.get("chunk_id")]
    subgraph_node_ids = [node.get("graph_node_id") for node in (subgraph_bundle.get("nodes") or []) if node.get("graph_node_id")]
    final_tree["retrieval"] = {
        "source": "graph_local_subgraph",
        "matched_top_event": matched["matched_name"],
        "matched_node_id": matched["matched_node_id"],
        "matched_node_ids": root_node_ids or [matched["matched_node_id"]],
        "alternatives": matched.get("alternatives") or [],
        "source_file_version_ids": scoped_file_version_ids,
        "subgraph_node_count": len(subgraph_bundle.get("nodes") or []),
        "subgraph_edge_count": len(subgraph_bundle.get("edges") or []),
        "chunk_ids": chunk_ids,
        "evidence_chunk_ids": evidence_chunk_ids,
        "subgraph_node_ids": subgraph_node_ids,
    }
    final_tree["source_file_version_ids"] = scoped_file_version_ids
    return final_tree


def generate_fault_tree_with_progress(
    top_event: str,
    requirements: str = "",
    selected_file_version_ids: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[int, str, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    part_details: Optional[Dict[str, Any]] = None,
) -> dict:
    if progress_callback:
        progress_callback(10, "prepare", "Preparing generation request")
        if ENABLE_GRAPH_RETRIEVAL:
            progress_callback(25, "graph_match", "Matching top event from graph")
            progress_callback(40, "graph_subgraph", "Expanding local graph subgraph")
            progress_callback(55, "graph_chunks", "Collecting subgraph evidence chunks")
            progress_callback(70, "graph_llm", "Building fault tree from subgraph and chunks")
        else:
            progress_callback(25, "chunk_recall", "Resolving top event evidence chunks")
            progress_callback(45, "chunk_extract", "Extracting fault elements from chunks")
            progress_callback(70, "chunk_llm", "Building fault tree from recalled chunks")

    tree_data = generate_fault_tree(
        top_event,
        requirements,
        selected_file_version_ids=selected_file_version_ids,
        log_callback=log_callback,
        part_details=part_details,
    )
    if part_details:
        tree_data = apply_physical_refs_to_fault_tree_data(tree_data, part_details)

    if progress_callback:
        progress_callback(90, "persistence", "Generation completed, persisting result")
    return tree_data


def _chunk_reference(chunk: Dict[str, Any]) -> Any:
    return chunk.get("chunk_uid") or chunk.get("id", chunk.get("chunk_id"))


def _format_chunks_for_prompt(chunks: List[Dict[str, Any]]) -> str:
    lines = []
    for chunk in (chunks or [])[:MAX_CHUNKS_FOR_PROMPT]:
        content = _chunk_content_excerpt(chunk, limit=MAX_CHUNK_CHARS)
        lines.append(
            f"[chunk_id={_chunk_reference(chunk)} | {chunk.get('chunk_name', '')} | 章节:{chunk.get('section_path', '')} | 页码:{chunk.get('source', '')}]\n"
            f"{content}\n"
            f"{'─' * 50}"
        )
    return "\n".join(lines)


def _extract_keywords(text: str) -> List[str]:
    try:
        import jieba.analyse

        keywords = jieba.analyse.extract_tags(text, topK=8)
        return keywords if keywords else [text]
    except Exception:
        return [text]


def _append_unique_chunks(
    existing: List[Dict[str, Any]],
    docs: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    merged = list(existing or [])
    seen = {str(_chunk_reference(doc)) for doc in merged if _chunk_reference(doc) not in (None, "")}
    for doc in docs or []:
        key = _chunk_reference(doc)
        if key in (None, ""):
            continue
        key = str(key)
        if key in seen:
            continue
        seen.add(key)
        merged.append(doc)
        if len(merged) >= limit:
            break
    return merged[:limit]


def _collect_chunk_only_evidence(
    top_event: str,
    *,
    selected_file_version_ids: Optional[List[str]] = None,
    limit: int = MAX_CHUNKS_FOR_PROMPT,
) -> Dict[str, Any]:
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
    catalog_entry = resolve_top_event_catalog(
        normalized_candidates=normalized_candidates,
        selected_file_version_ids=scoped_file_version_ids,
    )

    raw_chunks: List[Dict[str, Any]] = []
    matched_top_event = top_event
    matched_node_id = None

    if catalog_entry:
        matched_top_event = catalog_entry.get("name") or top_event
        matched_node_id = catalog_entry.get("graph_node_id") or ((catalog_entry.get("graph_node_ids") or [None])[0])
        catalog_chunk_ids = list(catalog_entry.get("source_chunk_ids") or [])
        if catalog_chunk_ids:
            raw_chunks = _append_unique_chunks(
                raw_chunks,
                get_chunks_by_ids(
                    catalog_chunk_ids,
                    limit=max(len(catalog_chunk_ids), 1),
                    selected_file_version_ids=scoped_file_version_ids,
                ),
                limit=limit,
            )

    entity_names = _dedupe_keep_order(
        [
            matched_top_event,
            top_event,
            *((catalog_entry or {}).get("aliases") or []),
            *((catalog_entry or {}).get("normalized_aliases") or []),
        ]
    )
    if entity_names and len(raw_chunks) < limit:
        raw_chunks = _append_unique_chunks(
            raw_chunks,
            search_chunks_by_entity_names(
                entity_names,
                limit=limit,
                selected_file_version_ids=scoped_file_version_ids,
            ),
            limit=limit,
        )

    keyword_candidates = _dedupe_keep_order(entity_names + _extract_keywords(matched_top_event))
    if keyword_candidates and len(raw_chunks) < limit:
        raw_chunks = _append_unique_chunks(
            raw_chunks,
            search_chunks_by_keywords(
                keyword_candidates,
                limit=limit,
                selected_file_version_ids=scoped_file_version_ids,
            ),
            limit=limit,
        )

    chunk_ids = [_chunk_reference(chunk) for chunk in raw_chunks if _chunk_reference(chunk) not in (None, "")]
    return {
        "matched_top_event": matched_top_event,
        "matched_node_id": matched_node_id,
        "catalog_entry": catalog_entry,
        "chunk_ids": chunk_ids,
        "chunks": raw_chunks,
        "source_file_version_ids": scoped_file_version_ids,
    }


def extract_fault_elements_from_chunks(top_event: str, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    prompt = f"""你是工业设备故障分析专家，精通FTA故障树分析方法。

## 参考知识（来自设备手册）
{_format_chunks_for_prompt(chunks)}

## 任务
从上述知识中，提取与以下故障相关的所有故障事件、因果关系和触发条件：
顶事件：{top_event}

## 提取规则
1. 顶事件（top_event）只有一个，就是给定的顶事件
2. 中间事件（intermediate_event）表示还可以继续向下分解的原因
3. 底事件（basic_event）表示最根本、可检测、不可再分的原因
4. gate=OR 表示任一子事件发生即可导致父事件
5. gate=AND 表示所有子事件同时发生才导致父事件
6. rules 尽量提取量化触发条件；没有就输出空数组
7. investigateMethod 尽量给出具体排查方法

## 输出格式（严格JSON，无多余文字）
{{
  "events": [
    {{
      "name": "事件名称",
      "type": "top_event/intermediate_event/basic_event",
      "description": "详细描述",
      "errorLevel": "高/中/低",
      "investigateMethod": "排查方法",
      "rules": [
        {{
          "measurePointName": "监测点名称",
          "symbol": ">",
          "thresholds": ["阈值"],
          "duration": "持续时长"
        }}
      ]
    }}
  ],
  "relations": [
    {{"parent": "父事件名称", "child": "子事件名称", "gate": "OR"}}
  ]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=2200,
    )
    return _parse_json(response.choices[0].message.content)


def build_fault_tree_from_chunk_elements(
    top_event: str,
    elements: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    requirements: str = "",
    previous_issues: Optional[List[Dict[str, Any]]] = None,
    part_details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    chunks_ref = [
        {
            "chunk_id": _chunk_reference(chunk),
            "chunk_name": chunk.get("chunk_name") or chunk.get("title") or chunk.get("heading") or chunk.get("chapter") or "",
            "section_path": chunk.get("section_path") or chunk.get("section") or chunk.get("chapter") or "",
            "source_page": chunk.get("source_page") or chunk.get("source") or chunk.get("page") or "",
            "file_id": chunk.get("file_id", ""),
            "file_version_id": chunk.get("file_version_id", ""),
        }
        for chunk in chunks
    ]
    retry_hint = ""
    if previous_issues:
        error_msgs = [
            f"- [{item['level']}] {item['message']} (节点: {item.get('node_name', '')})"
            for item in previous_issues
            if item.get("level") in {"ERROR", "WARNING"}
        ]
        if error_msgs:
            retry_hint = "\n## 上次生成存在以下问题，请修正\n" + "\n".join(error_msgs) + "\n"

    pd = part_details if isinstance(part_details, dict) and part_details else None
    part_details_text = json.dumps(pd, ensure_ascii=False, indent=2) if pd else ""
    physical_ref_chunk = ""
    if pd:
        physical_ref_chunk = f"""
## 三维部件对标（与爆炸图网格名 Object_X 一致）
在构建故障树的每一个事件节点时，请在 description 字段的末尾，按照 [Ref: Object_X] 的格式标注其物理关联组件。
其中 Object_X 为该事件对应的物理组件 ID。以下是该设备的物理组件列表：
{part_details_text}
"""

    prompt = f"""你是工业设备故障树分析专家，精通FTA方法。

## 参考知识（来自设备手册）
{_format_chunks_for_prompt(chunks)}

## 可用的溯源chunk列表（用于填写documents字段）
{json.dumps(chunks_ref, ensure_ascii=False, indent=2)}

## 已提取的故障要素
{json.dumps(elements, ensure_ascii=False, indent=2)}
{retry_hint}
## 用户额外要求
{requirements or '无'}
{physical_ref_chunk}
## 任务
为顶事件“{top_event}”生成完整、规范的故障树。

## 生成规则
1. 每个节点id格式：node-{{8位十六进制}}，全部唯一
2. event.id格式：E001, E002...依次递增
3. 顶事件的event字段固定为null
4. 中间事件和底事件必须填完整event对象
5. errorLevel必须填写：高/中/低
6. rules尽量从手册提取触发条件；无量化数据则填[]
7. documents只能从给定chunk列表中选择，每个节点最多2条，且每条只允许包含：
   chunk_id、chunk_name、section_path、source_page、file_id、file_version_id。
   严禁输出 content、text、raw_text、page_content、file、chunk_uid 等正文或额外字段。
   chunk正文由前端点击节点详情时按 file_version_id + chunk_id 动态查询，不要写入故障树JSON。
8. investigateMethod尽量控制在40字以内
9. description尽量控制在60字以内
10. linkList的sourceId是子节点（原因方），targetId是父节点（结果方）
11. 故障树深度3-5层
12. 节点总数尽量控制在8~18个，避免无关扩展
13. 只输出完整JSON，不要输出markdown

## 输出格式
{{
  "nodeList": [...],
  "linkList": [...]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=LLM_GENERATION_MAX_TOKENS,
    )
    return _sanitize_fault_tree_documents(_parse_json(response.choices[0].message.content))


def _normalize_token_usage(usage: Any) -> Dict[str, Optional[int]]:
    if usage is None:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    return {
        "prompt_tokens": int(prompt_tokens) if prompt_tokens is not None else None,
        "completion_tokens": int(completion_tokens) if completion_tokens is not None else None,
        "total_tokens": int(total_tokens) if total_tokens is not None else None,
    }


def _add_token_usage(*parts: Optional[Dict[str, Optional[int]]]) -> Dict[str, Optional[int]]:
    merged = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    saw_value = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = part.get(key)
            if value is None:
                continue
            saw_value = True
            merged[key] += int(value)
    if saw_value:
        return merged
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }


def _make_stage_profile(
    duration_seconds: float,
    *,
    token_usage: Optional[Dict[str, Optional[int]]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "duration_seconds": round(float(duration_seconds or 0.0), 3),
        "token_usage": token_usage
        or {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        },
    }
    payload.update(extra)
    return payload


def _estimate_tree_depth(tree_data: Dict[str, Any]) -> int:
    node_list = tree_data.get("nodeList") or []
    link_list = tree_data.get("linkList") or []
    node_ids = {node.get("id") for node in node_list if node.get("id")}
    if not node_ids:
        return 0

    children_by_parent: Dict[str, List[str]] = {}
    incoming: Dict[str, int] = {node_id: 0 for node_id in node_ids}
    for link in link_list:
        source_id = link.get("sourceId")
        target_id = link.get("targetId")
        if source_id not in node_ids or target_id not in node_ids:
            continue
        children_by_parent.setdefault(target_id, []).append(source_id)
        incoming[source_id] = incoming.get(source_id, 0) + 1

    roots = [node_id for node_id in node_ids if incoming.get(node_id, 0) == 0]
    if not roots:
        roots = [node.get("id") for node in node_list if node.get("type") == "top_event" and node.get("id")]
    if not roots:
        return 1

    max_depth = 0
    stack: List[tuple[str, int]] = [(root, 1) for root in roots]
    seen_depths: Dict[str, int] = {}
    while stack:
        node_id, depth = stack.pop()
        if depth <= seen_depths.get(node_id, 0):
            continue
        seen_depths[node_id] = depth
        max_depth = max(max_depth, depth)
        for child_id in children_by_parent.get(node_id, []):
            stack.append((child_id, depth + 1))
    return max_depth


def _summarize_tree_structure(tree_data: Dict[str, Any]) -> Dict[str, Any]:
    node_list = tree_data.get("nodeList") or []
    link_list = tree_data.get("linkList") or []
    gate_counts = {"AND": 0, "OR": 0}
    for node in node_list:
        gate = str(node.get("gate") or "").strip().upper()
        if gate in gate_counts:
            gate_counts[gate] += 1
    return {
        "node_count": len(node_list),
        "link_count": len(link_list),
        "gate_count": gate_counts["AND"] + gate_counts["OR"],
        "and_gate_count": gate_counts["AND"],
        "or_gate_count": gate_counts["OR"],
        "max_depth": _estimate_tree_depth(tree_data),
    }


def parse_user_prompt(prompt: str, *, include_meta: bool = False) -> dict:
    raw = str(prompt or "").strip()
    if not raw:
        raise ValueError("prompt 不能为空")

    normalized = re.sub(r"\s+", " ", raw).strip()
    started = time.perf_counter()
    prompt_text = f"""
你是工业设备故障树系统的提示词解析器。请优先从用户输入中提取：
1. top_event：用户要分析或生成故障树的顶事件
2. requirements：额外要求，没有就返回空字符串
只输出 JSON：{{
  "top_event": "...",
  "requirements": "..."
}}

用户输入：{normalized}
"""
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt_text}],
            temperature=0.1,
            max_tokens=LLM_PARSE_MAX_TOKENS,
        )
        parsed = _parse_json(response.choices[0].message.content)
        top_event = normalize_top_event_name(parsed.get("top_event"))
        if top_event:
            result = {"top_event": top_event, "requirements": str(parsed.get("requirements") or "").strip()}
            if include_meta:
                result["_meta"] = {
                    "parse_method": "llm",
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    "token_usage": _normalize_token_usage(getattr(response, "usage", None)),
                }
            return result
    except Exception:
        pass

    patterns = [
        r"顶事件(?:为|是)?[:：]?\s*(.+)$",
        r"分析(.+?)(?:故障树|故障|异常)?$",
        r"生成(.+?)(?:故障树)?$",
        r"针对(.+?)(?:进行|生成|分析)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            top_event = normalize_top_event_name(match.group(1))
            if top_event:
                result = {"top_event": top_event, "requirements": ""}
                if include_meta:
                    result["_meta"] = {
                        "parse_method": "regex",
                        "duration_seconds": round(time.perf_counter() - started, 3),
                        "token_usage": _normalize_token_usage(None),
                    }
                return result

    if len(normalized) <= 40 and "\n" not in normalized:
        result = {"top_event": normalize_top_event_name(normalized), "requirements": ""}
        if include_meta:
            result["_meta"] = {
                "parse_method": "direct_text",
                "duration_seconds": round(time.perf_counter() - started, 3),
                "token_usage": _normalize_token_usage(None),
            }
        return result

    raise ValueError("无法从 prompt 中提取顶事件")


def generate_fault_tree(
    top_event: str,
    requirements: str = "",
    selected_file_version_ids: Optional[List[str]] = None,
    root_graph_node_id: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    def emit(message: str):
        try:
            print(message)
        finally:
            if log_callback:
                try:
                    log_callback(message)
                except Exception:
                    pass

    if not ENABLE_GRAPH_RETRIEVAL:
        retrieval = _collect_chunk_only_evidence(
            top_event,
            selected_file_version_ids=selected_file_version_ids,
            limit=MAX_CHUNKS_FOR_PROMPT,
        )
        raw_chunks = retrieval["chunks"] or []
        matched_top_event = retrieval.get("matched_top_event") or top_event
        matched_node_id = root_graph_node_id or retrieval.get("matched_node_id")
        scoped_file_version_ids = retrieval.get("source_file_version_ids") or resolve_selected_file_version_ids(
            selected_file_version_ids,
            fallback_to_active=True,
            require_active=False,
        )
        if not raw_chunks:
            raise ValueError(f"未找到与'{top_event}'相关的证据 chunks，请检查 top_event_catalog 或 chunks 数据")

        emit(f"[chunk-recall] matched '{top_event}' -> '{matched_top_event}'")
        emit(f"[chunk-recall] collected {len(raw_chunks)} evidence chunks")
        emit("[chunk-llm] extracting fault elements from chunks")
        elements = extract_fault_elements_from_chunks(matched_top_event, raw_chunks)
        emit(
            f"[chunk-llm] extracted elements "
            f"events={len(elements.get('events') or [])} relations={len(elements.get('relations') or [])}"
        )

        draft_tree = None
        previous_issues = None
        for attempt in range(1, MAX_RETRY + 2):
            emit(f"[chunk-llm] generating draft tree attempt={attempt}")
            try:
                draft_tree = build_fault_tree_from_chunk_elements(
                    top_event=matched_top_event,
                    elements=elements,
                    chunks=raw_chunks,
                    requirements=requirements,
                    previous_issues=previous_issues,
                )
                emit(
                    f"[chunk-draft] generated draft attempt={attempt} "
                    f"nodes={len(draft_tree.get('nodeList') or [])} links={len(draft_tree.get('linkList') or [])}"
                )
            except ValueError as exc:
                emit(f"[chunk-draft] draft generation error attempt={attempt} error={exc}")
                if attempt > MAX_RETRY:
                    raise
                previous_issues = [{"level": "ERROR", "message": str(exc)}]
                emit(f"[chunk-regenerate] retrying draft generation next_attempt={attempt + 1}")
                continue

            emit(f"[chunk-validate] validating draft attempt={attempt}")
            validation = validate_full(draft_tree, skip_semantic=True)
            draft_tree["validation"] = validation
            if validation["passed"]:
                emit(
                    f"[chunk-validate] validation passed attempt={attempt} "
                    f"errors={validation['error_count']} warnings={validation['warning_count']}"
                )
                break
            previous_issues = validation["issues"]
            emit(
                f"[chunk-validate] validation failed attempt={attempt} "
                f"errors={validation['error_count']} warnings={validation['warning_count']}"
            )
            if attempt <= MAX_RETRY:
                emit(f"[chunk-regenerate] retrying draft generation next_attempt={attempt + 1}")
            if attempt > MAX_RETRY:
                break

        if draft_tree is None:
            raise ValueError(f"Failed to build tree for '{top_event}'")

        final_tree = draft_tree
        try:
            from diff_analyzer import format_corrections_for_repair, get_relevant_corrections

            corrections = get_relevant_corrections(draft_tree)
            if corrections:
                emit(f"[history-repair] applying {len(corrections)} relevant corrections")
                repaired = repair_fault_tree(draft_tree, format_corrections_for_repair(corrections), raw_chunks)
                emit("[chunk-validate] validating repaired draft")
                repair_validation = validate_full(repaired, skip_semantic=True)
                if repair_validation["passed"]:
                    repaired["validation"] = repair_validation
                    final_tree = repaired
                    emit(
                        f"[history-repair] repaired draft accepted "
                        f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                    )
                else:
                    emit(
                        f"[history-repair] repaired draft rejected "
                        f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                    )
            else:
                emit("[history-repair] no relevant corrections, skipped")
        except Exception as exc:
            emit(f"[history-repair] skipped due to error: {exc}")

        emit("[chunk-validate] validating final tree")
        final_validation = validate_full(final_tree, skip_semantic=False)
        final_tree["validation"] = final_validation
        emit(
            f"[chunk-validate] final validation "
            f"{'passed' if final_validation['passed'] else 'failed'} "
            f"errors={final_validation['error_count']} warnings={final_validation['warning_count']}"
        )
        evidence_chunk_ids = [_chunk_reference(chunk) for chunk in raw_chunks if _chunk_reference(chunk) not in (None, "")]
        final_tree["retrieval"] = {
            "source": "top_event_catalog_chunks",
            "matched_top_event": matched_top_event,
            "matched_node_id": matched_node_id,
            "matched_node_ids": [matched_node_id] if matched_node_id else [],
            "alternatives": [],
            "source_file_version_ids": scoped_file_version_ids,
            "subgraph_node_count": 0,
            "subgraph_edge_count": 0,
            "chunk_ids": retrieval.get("chunk_ids") or evidence_chunk_ids,
            "evidence_chunk_ids": evidence_chunk_ids,
            "subgraph_node_ids": [],
        }
        final_tree["source_file_version_ids"] = scoped_file_version_ids
        return final_tree

    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    if root_graph_node_id:
        emit(f"[graph-match] using confirmed graph node '{root_graph_node_id}' for top event '{top_event}'")
        matched_node = get_graph_node_by_id(
            root_graph_node_id,
            selected_file_version_ids=scoped_file_version_ids,
        )
        if not matched_node:
            raise ValueError(f"Confirmed graph node not found in current scope: {root_graph_node_id}")
        matched = {
            "matched_node_id": matched_node.get("graph_node_id"),
            "matched_name": matched_node.get("name") or top_event,
            "matched_node": matched_node,
            "matched_nodes": [
                {
                    "graph_node_id": matched_node.get("graph_node_id"),
                    "name": matched_node.get("name"),
                    "normalized_name": matched_node.get("normalized_name"),
                    "file_id": matched_node.get("file_id"),
                    "file_version_id": matched_node.get("file_version_id"),
                }
            ],
            "alternatives": [],
        }
        root_node_ids = [matched_node.get("graph_node_id")]
        emit(f"[graph-match] confirmed '{top_event}' -> '{matched['matched_name']}'")
    else:
        normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
        emit(f"[graph-match] matching top event '{top_event}'")
        matched = match_top_event_from_graph(
            top_event,
            normalized_candidates=normalized_candidates,
            selected_file_version_ids=scoped_file_version_ids,
        )
        matched_node = matched["matched_node"]
        emit(f"[graph-match] matched '{top_event}' -> '{matched['matched_name']}'")
        root_node_ids = [item.get("graph_node_id") for item in (matched.get("matched_nodes") or []) if item.get("graph_node_id")]

    subgraph_bundle = expand_scoped_local_fault_subgraph(
        root_node_ids or [matched["matched_node_id"]],
        max_depth=GRAPH_TREE_MAX_DEPTH,
        max_nodes=GRAPH_TREE_MAX_NODES,
        selected_file_version_ids=scoped_file_version_ids,
    )
    emit(
        f"[graph-subgraph] root={matched['matched_name']} roots={len(subgraph_bundle.get('roots') or [])} "
        f"nodes={len(subgraph_bundle.get('nodes') or [])} edges={len(subgraph_bundle.get('edges') or [])}"
    )

    chunk_ids = collect_subgraph_chunks(subgraph_bundle, chunk_limit=MAX_CHUNKS_FOR_PROMPT)
    evidence_chunks = hydrate_documents_by_chunk_ids(chunk_ids, selected_file_version_ids=scoped_file_version_ids)
    raw_chunk_docs = []
    chunk_doc_map = {
        str(item.get("chunk_uid") or item["chunk_id"]): item
        for item in evidence_chunks
    }
    for chunk_id in chunk_ids:
        doc = chunk_doc_map.get(str(chunk_id))
        if doc:
            raw_chunk_docs.append(doc)
    from database import get_chunks_by_ids  # local import to keep module surface small

    raw_chunks = get_chunks_by_ids(
        chunk_ids,
        limit=max(len(chunk_ids), 1),
        selected_file_version_ids=scoped_file_version_ids,
    )
    emit(f"[graph-chunks] collected {len(raw_chunks)} evidence chunks")

    draft_tree = None
    previous_issues = None
    for attempt in range(1, MAX_RETRY + 2):
        emit(f"[graph-llm] generating draft tree attempt={attempt}")
        try:
            draft_tree = build_fault_tree_from_subgraph_and_chunks(
                top_event=matched["matched_name"],
                subgraph_bundle=subgraph_bundle,
                evidence_chunks=raw_chunks,
                requirements=requirements,
            )
            emit(
                f"[graph-draft] generated draft attempt={attempt} "
                f"nodes={len(draft_tree.get('nodeList') or [])} links={len(draft_tree.get('linkList') or [])}"
            )
        except ValueError as exc:
            emit(f"[graph-draft] draft generation error attempt={attempt} error={exc}")
            if attempt > MAX_RETRY:
                raise
            previous_issues = [{"level": "ERROR", "message": str(exc)}]
            emit(f"[graph-regenerate] retrying draft generation next_attempt={attempt + 1}")
            continue

        emit(f"[graph-validate] validating draft attempt={attempt}")
        validation = validate_full(draft_tree, skip_semantic=True)
        draft_tree["validation"] = validation
        if validation["passed"]:
            emit(
                f"[graph-validate] validation passed attempt={attempt} "
                f"errors={validation['error_count']} warnings={validation['warning_count']}"
            )
            break
        previous_issues = validation["issues"]
        emit(
            f"[graph-validate] validation failed attempt={attempt} "
            f"errors={validation['error_count']} warnings={validation['warning_count']}"
        )
        if attempt <= MAX_RETRY:
            emit(f"[graph-regenerate] retrying draft generation next_attempt={attempt + 1}")
        if attempt > MAX_RETRY:
            break

    if draft_tree is None:
        raise ValueError(f"Failed to build tree for '{top_event}'")

    final_tree = draft_tree
    try:
        from diff_analyzer import format_corrections_for_repair, get_relevant_corrections

        corrections = get_relevant_corrections(draft_tree)
        if corrections:
            emit(f"[history-repair] applying {len(corrections)} relevant corrections")
            repaired = repair_fault_tree(draft_tree, format_corrections_for_repair(corrections), raw_chunks)
            emit("[graph-validate] validating repaired draft")
            repair_validation = validate_full(repaired, skip_semantic=True)
            if repair_validation["passed"]:
                repaired["validation"] = repair_validation
                final_tree = repaired
                emit(
                    f"[history-repair] repaired draft accepted "
                    f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                )
            else:
                emit(
                    f"[history-repair] repaired draft rejected "
                    f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                )
        else:
            emit("[history-repair] no relevant corrections, skipped")
    except Exception as exc:
        emit(f"[history-repair] skipped due to error: {exc}")

    emit("[graph-validate] validating final tree")
    final_validation = validate_full(final_tree, skip_semantic=False)
    final_tree["validation"] = final_validation
    emit(
        f"[graph-validate] final validation "
        f"{'passed' if final_validation['passed'] else 'failed'} "
        f"errors={final_validation['error_count']} warnings={final_validation['warning_count']}"
    )
    evidence_chunk_ids = [chunk.get("chunk_uid") or chunk.get("chunk_id") for chunk in raw_chunks if chunk.get("chunk_uid") or chunk.get("chunk_id")]
    subgraph_node_ids = [node.get("graph_node_id") for node in (subgraph_bundle.get("nodes") or []) if node.get("graph_node_id")]
    final_tree["retrieval"] = {
        "source": "graph_local_subgraph",
        "matched_top_event": matched["matched_name"],
        "matched_node_id": matched["matched_node_id"],
        "matched_node_ids": root_node_ids or [matched["matched_node_id"]],
        "alternatives": matched.get("alternatives") or [],
        "source_file_version_ids": scoped_file_version_ids,
        "subgraph_node_count": len(subgraph_bundle.get("nodes") or []),
        "subgraph_edge_count": len(subgraph_bundle.get("edges") or []),
        "chunk_ids": chunk_ids,
        "evidence_chunk_ids": evidence_chunk_ids,
        "subgraph_node_ids": subgraph_node_ids,
    }
    final_tree["source_file_version_ids"] = scoped_file_version_ids
    return final_tree


def generate_fault_tree_with_progress(
    top_event: str,
    requirements: str = "",
    selected_file_version_ids: Optional[List[str]] = None,
    root_graph_node_id: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    part_details: Optional[Dict[str, Any]] = None,
) -> dict:
    if progress_callback:
        progress_callback(10, "prepare", "Preparing generation request")
        if ENABLE_GRAPH_RETRIEVAL:
            progress_callback(25, "graph_match", "Matching top event from graph")
            progress_callback(40, "graph_subgraph", "Expanding local graph subgraph")
            progress_callback(55, "graph_chunks", "Collecting subgraph evidence chunks")
            progress_callback(70, "graph_llm", "Building fault tree from subgraph and chunks")
        else:
            progress_callback(25, "chunk_recall", "Resolving top event evidence chunks")
            progress_callback(45, "chunk_extract", "Extracting fault elements from chunks")
            progress_callback(70, "chunk_llm", "Building fault tree from recalled chunks")

    tree_data = generate_fault_tree(
        top_event,
        requirements,
        selected_file_version_ids=selected_file_version_ids,
        root_graph_node_id=root_graph_node_id,
        log_callback=log_callback,
        part_details=part_details,
    )
    if part_details:
        tree_data = apply_physical_refs_to_fault_tree_data(tree_data, part_details)

    if progress_callback:
        progress_callback(90, "persistence", "Generation completed, persisting result")
    return tree_data


def generate_fault_tree(
    top_event: str,
    requirements: str = "",
    selected_file_version_ids: Optional[List[str]] = None,
    root_graph_node_id: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, str, str], None]] = None,
    part_details: Optional[Dict[str, Any]] = None,
) -> dict:
    # Keep this later definition as the runtime-active implementation.
    def emit(message: str):
        try:
            print(message)
        finally:
            if log_callback:
                try:
                    log_callback(message)
                except Exception:
                    pass

    overall_started = time.perf_counter()
    performance: Dict[str, Any] = {}
    llm_token_usage = _normalize_token_usage(None)
    repair_token_usage = _normalize_token_usage(None)
    llm_validation_token_usage = _normalize_token_usage(None)
    validation_duration_seconds = 0.0
    repair_duration_seconds = 0.0
    validation_runs = 0
    repair_attempted = False
    repair_accepted = False

    stage_started = time.perf_counter()
    scoped_file_version_ids = resolve_selected_file_version_ids(
        selected_file_version_ids,
        fallback_to_active=True,
        require_active=False,
    )
    if root_graph_node_id:
        emit(f"[graph-match] using confirmed graph node '{root_graph_node_id}' for top event '{top_event}'")
        matched_node = get_graph_node_by_id(
            root_graph_node_id,
            selected_file_version_ids=scoped_file_version_ids,
        )
        if not matched_node:
            raise ValueError(f"Confirmed graph node not found in current scope: {root_graph_node_id}")
        matched = {
            "matched_node_id": matched_node.get("graph_node_id"),
            "matched_name": matched_node.get("name") or top_event,
            "matched_node": matched_node,
            "matched_nodes": [
                {
                    "graph_node_id": matched_node.get("graph_node_id"),
                    "name": matched_node.get("name"),
                    "normalized_name": matched_node.get("normalized_name"),
                    "file_id": matched_node.get("file_id"),
                    "file_version_id": matched_node.get("file_version_id"),
                }
            ],
            "alternatives": [],
        }
        root_node_ids = [matched_node.get("graph_node_id")]
        emit(f"[graph-match] confirmed '{top_event}' -> '{matched['matched_name']}'")
    else:
        normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
        emit(f"[graph-match] matching top event '{top_event}'")
        matched = match_top_event_from_graph(
            top_event,
            normalized_candidates=normalized_candidates,
            selected_file_version_ids=scoped_file_version_ids,
        )
        emit(f"[graph-match] matched '{top_event}' -> '{matched['matched_name']}'")
        root_node_ids = [
            item.get("graph_node_id")
            for item in (matched.get("matched_nodes") or [])
            if item.get("graph_node_id")
        ]
    performance["graph_match"] = _make_stage_profile(
        time.perf_counter() - stage_started,
        matched_node_count=len(root_node_ids or ([matched.get("matched_node_id")] if matched.get("matched_node_id") else [])),
        matched_top_event=matched.get("matched_name"),
        used_confirmed_graph_node=bool(root_graph_node_id),
    )

    stage_started = time.perf_counter()
    subgraph_bundle = expand_scoped_local_fault_subgraph(
        root_node_ids or [matched["matched_node_id"]],
        max_depth=GRAPH_TREE_MAX_DEPTH,
        max_nodes=GRAPH_TREE_MAX_NODES,
        selected_file_version_ids=scoped_file_version_ids,
    )
    emit(
        f"[graph-subgraph] root={matched['matched_name']} roots={len(subgraph_bundle.get('roots') or [])} "
        f"nodes={len(subgraph_bundle.get('nodes') or [])} edges={len(subgraph_bundle.get('edges') or [])}"
    )
    if subgraph_bundle.get("disabled_cycle_edges"):
        emit(
            "[graph-subgraph] temporarily disabled "
            f"{len(subgraph_bundle.get('disabled_cycle_edges') or [])} causal cycle edges in selected file view"
        )
    if subgraph_bundle.get("pruned_transitive_edges"):
        emit(
            "[graph-subgraph] temporarily pruned "
            f"{len(subgraph_bundle.get('pruned_transitive_edges') or [])} transitive shortcut edges in selected file view"
        )
    performance["graph_subgraph"] = _make_stage_profile(
        time.perf_counter() - stage_started,
        root_count=len(subgraph_bundle.get("roots") or []),
        node_count=len(subgraph_bundle.get("nodes") or []),
        edge_count=len(subgraph_bundle.get("edges") or []),
        gate_group_count=len(subgraph_bundle.get("gate_groups") or []),
        pruned_transitive_edge_count=len(subgraph_bundle.get("pruned_transitive_edges") or []),
        multi_root_runtime_merge=bool(len(subgraph_bundle.get("roots") or []) > 1),
    )

    stage_started = time.perf_counter()
    initial_chunk_ids = collect_subgraph_chunks(subgraph_bundle, chunk_limit=MAX_CHUNKS_FOR_PROMPT)
    chunk_ids = list(initial_chunk_ids)
    raw_chunks: List[Dict[str, Any]] = []
    if chunk_ids:
        raw_chunks = get_chunks_by_ids(
            chunk_ids,
            limit=max(len(chunk_ids), 1),
            selected_file_version_ids=scoped_file_version_ids,
        )

    fallback_used = False
    if not raw_chunks:
        fallback_used = True
        emit("[graph-chunks] subgraph returned no direct chunks, falling back to top-event chunk recall")
        fallback_retrieval = _collect_chunk_only_evidence(
            matched["matched_name"],
            selected_file_version_ids=scoped_file_version_ids,
            limit=MAX_CHUNKS_FOR_PROMPT,
        )
        _append_unique_chunks(raw_chunks, fallback_retrieval.get("chunks") or [])
        for chunk_id in fallback_retrieval.get("chunk_ids") or []:
            if chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)

    if not raw_chunks:
        raise ValueError(
            f"No evidence chunks found for '{matched['matched_name']}'. Please check graph links, "
            "top_event_catalog, or chunks data."
        )

    emit(f"[graph-chunks] collected {len(raw_chunks)} evidence chunks")
    performance["chunk_recall"] = _make_stage_profile(
        time.perf_counter() - stage_started,
        recalled_chunk_count=len(chunk_ids),
        retained_chunk_count=len(raw_chunks),
        source_file_version_count=len({chunk.get("file_version_id") for chunk in raw_chunks if chunk.get("file_version_id")}),
        fallback_used=fallback_used,
    )

    llm_used_subgraph = ENABLE_GRAPH_RETRIEVAL
    elements: Dict[str, Any] = {}
    llm_stage_started = time.perf_counter()
    if not llm_used_subgraph:
        emit("[graph-llm] extracting fault elements from graph-recalled chunks without subgraph skeleton")
        extract_result = extract_fault_elements_from_chunks(
            matched["matched_name"],
            raw_chunks,
            include_meta=True,
        )
        elements = extract_result["elements"]
        llm_token_usage = _add_token_usage(llm_token_usage, extract_result.get("token_usage"))
        emit(
            f"[graph-llm] extracted elements "
            f"events={len(elements.get('events') or [])} relations={len(elements.get('relations') or [])}"
        )

    draft_tree = None
    previous_issues = None
    attempt = 0
    for attempt in range(1, MAX_RETRY + 2):
        emit(f"[graph-llm] generating draft tree attempt={attempt}")
        try:
            if llm_used_subgraph:
                draft_result = build_fault_tree_from_subgraph_and_chunks(
                    top_event=matched["matched_name"],
                    subgraph_bundle=subgraph_bundle,
                    evidence_chunks=raw_chunks,
                    requirements=requirements,
                    part_details=part_details,
                    include_meta=True,
                )
            else:
                draft_result = build_fault_tree_from_chunk_elements(
                    top_event=matched["matched_name"],
                    elements=elements,
                    chunks=raw_chunks,
                    requirements=requirements,
                    previous_issues=previous_issues,
                    part_details=part_details,
                    include_meta=True,
                )
            draft_tree = draft_result["tree"]
            llm_token_usage = _add_token_usage(llm_token_usage, draft_result.get("token_usage"))
            emit(
                f"[graph-draft] generated draft attempt={attempt} "
                f"nodes={len(draft_tree.get('nodeList') or [])} links={len(draft_tree.get('linkList') or [])}"
            )
        except ValueError as exc:
            emit(f"[graph-draft] draft generation error attempt={attempt} error={exc}")
            if attempt > MAX_RETRY:
                raise
            previous_issues = [{"level": "ERROR", "message": str(exc)}]
            emit(f"[graph-regenerate] retrying draft generation next_attempt={attempt + 1}")
            continue

        emit(f"[graph-validate] validating draft attempt={attempt}")
        if progress_callback:
            progress_callback(72, "graph_validate", f"Structural validation (draft attempt {attempt})…")
        validation_runs += 1
        validation_started = time.perf_counter()
        validation = validate_full(draft_tree, skip_semantic=True, include_meta=True)
        validation_duration_seconds += time.perf_counter() - validation_started
        draft_tree["validation"] = validation
        if validation["passed"]:
            emit(
                f"[graph-validate] validation passed attempt={attempt} "
                f"errors={validation['error_count']} warnings={validation['warning_count']}"
            )
            break
        previous_issues = validation["issues"]
        emit(
            f"[graph-validate] validation failed attempt={attempt} "
            f"errors={validation['error_count']} warnings={validation['warning_count']}"
        )
        if attempt <= MAX_RETRY:
            emit(f"[graph-regenerate] retrying draft generation next_attempt={attempt + 1}")
        if attempt > MAX_RETRY:
            break

    if draft_tree is None:
        raise ValueError(f"Failed to build tree for '{top_event}'")

    final_tree = draft_tree
    try:
        from diff_analyzer import format_corrections_for_repair, get_relevant_corrections

        corrections = get_relevant_corrections(draft_tree)
        if corrections:
            repair_attempted = True
            emit(f"[history-repair] applying {len(corrections)} relevant corrections")
            repaired_result = repair_fault_tree(
                draft_tree,
                format_corrections_for_repair(corrections),
                raw_chunks,
                include_meta=True,
            )
            repaired = repaired_result["tree"]
            repair_duration_seconds += float(repaired_result.get("duration_seconds") or 0.0)
            repair_token_usage = _add_token_usage(repair_token_usage, repaired_result.get("token_usage"))
            emit("[graph-validate] validating repaired draft")
            if progress_callback:
                progress_callback(78, "graph_validate", "Validating repaired draft…")
            validation_runs += 1
            validation_started = time.perf_counter()
            repair_validation = validate_full(repaired, skip_semantic=True, include_meta=True)
            validation_duration_seconds += time.perf_counter() - validation_started
            if repair_validation["passed"]:
                repaired["validation"] = repair_validation
                final_tree = repaired
                repair_accepted = True
                emit(
                    f"[history-repair] repaired draft accepted "
                    f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                )
            else:
                emit(
                    f"[history-repair] repaired draft rejected "
                    f"errors={repair_validation['error_count']} warnings={repair_validation['warning_count']}"
                )
        else:
            emit("[history-repair] no relevant corrections, skipped")
    except Exception as exc:
        emit(f"[history-repair] skipped due to error: {exc}")

    # LLM 常忽略「在 description 末尾追加 [Ref: Object_X]」；有部件列表时必须做确定性后处理
    if part_details:
        final_tree = apply_physical_refs_to_fault_tree_data(final_tree, part_details)

    emit("[graph-validate] validating final tree")
    if progress_callback:
        progress_callback(82, "graph_validate", "Running full validation (including semantics)…")
    validation_runs += 1
    validation_started = time.perf_counter()
    final_validation = validate_full(final_tree, skip_semantic=False, include_meta=True)
    validation_duration_seconds += time.perf_counter() - validation_started
    final_tree["validation"] = final_validation
    llm_validation_token_usage = _add_token_usage(
        llm_validation_token_usage,
        ((final_validation.get("meta") or {}).get("semantic_validation") or {}).get("token_usage"),
    )
    emit(
        f"[graph-validate] final validation "
        f"{'passed' if final_validation['passed'] else 'failed'} "
        f"errors={final_validation['error_count']} warnings={final_validation['warning_count']}"
    )

    evidence_chunk_ids = [
        chunk.get("chunk_uid") or chunk.get("chunk_id")
        for chunk in raw_chunks
        if chunk.get("chunk_uid") or chunk.get("chunk_id")
    ]
    subgraph_node_ids = [
        node.get("graph_node_id")
        for node in (subgraph_bundle.get("nodes") or [])
        if node.get("graph_node_id")
    ]
    final_tree["retrieval"] = {
        "source": "graph_local_subgraph",
        "matched_top_event": matched["matched_name"],
        "matched_node_id": matched["matched_node_id"],
        "matched_node_ids": root_node_ids or [matched["matched_node_id"]],
        "alternatives": matched.get("alternatives") or [],
        "source_file_version_ids": scoped_file_version_ids,
        "subgraph_node_count": len(subgraph_bundle.get("nodes") or []),
        "subgraph_edge_count": len(subgraph_bundle.get("edges") or []),
        "chunk_ids": chunk_ids or evidence_chunk_ids,
        "evidence_chunk_ids": evidence_chunk_ids,
        "subgraph_node_ids": subgraph_node_ids,
        "llm_used_subgraph": llm_used_subgraph,
    }
    final_tree["source_file_version_ids"] = scoped_file_version_ids

    tree_summary = _summarize_tree_structure(final_tree)
    performance["tree_generation"] = _make_stage_profile(
        max(0.0, time.perf_counter() - llm_stage_started - validation_duration_seconds - repair_duration_seconds),
        token_usage=llm_token_usage,
        attempt_count=attempt,
        llm_used_subgraph=llm_used_subgraph,
        extracted_event_count=len(elements.get("events") or []),
        extracted_relation_count=len(elements.get("relations") or []),
        node_count=tree_summary["node_count"],
        link_count=tree_summary["link_count"],
        gate_count=tree_summary["gate_count"],
        and_gate_count=tree_summary["and_gate_count"],
        or_gate_count=tree_summary["or_gate_count"],
        max_depth=tree_summary["max_depth"],
    )
    performance["tree_validation"] = _make_stage_profile(
        validation_duration_seconds + repair_duration_seconds,
        token_usage=_add_token_usage(llm_validation_token_usage, repair_token_usage),
        validation_run_count=validation_runs,
        repair_attempted=repair_attempted,
        repair_accepted=repair_accepted,
        repair_duration_seconds=round(repair_duration_seconds, 3),
        final_passed=bool(final_validation.get("passed")),
        final_error_count=int(final_validation.get("error_count") or 0),
        final_warning_count=int(final_validation.get("warning_count") or 0),
    )
    performance["overall"] = {
        "duration_seconds": round(time.perf_counter() - overall_started, 3),
    }
    final_tree["performance"] = performance
    return final_tree


def generate_fault_tree_with_progress(
    top_event: str,
    requirements: str = "",
    selected_file_version_ids: Optional[List[str]] = None,
    root_graph_node_id: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    part_details: Optional[Dict[str, Any]] = None,
) -> dict:
    if progress_callback:
        progress_callback(10, "prepare", "Preparing generation request")
        progress_callback(25, "graph_match", "Matching top event from graph")
        progress_callback(40, "graph_subgraph", "Expanding local graph subgraph")
        progress_callback(55, "graph_chunks", "Collecting subgraph evidence chunks")
        if ENABLE_GRAPH_RETRIEVAL:
            progress_callback(70, "graph_llm", "Building fault tree from subgraph and chunks")
        else:
            progress_callback(70, "graph_llm", "Building fault tree from graph-recalled chunks")

    tree_data = generate_fault_tree(
        top_event,
        requirements,
        selected_file_version_ids=selected_file_version_ids,
        root_graph_node_id=root_graph_node_id,
        log_callback=log_callback,
        progress_callback=progress_callback,
        part_details=part_details,
    )
    if part_details:
        tree_data = apply_physical_refs_to_fault_tree_data(tree_data, part_details)

    if progress_callback:
        progress_callback(90, "persistence", "Generation completed, persisting result")
    return tree_data


def _normalize_event_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _build_chunk_tree_fallback(top_event: str, elements: Dict[str, Any]) -> Dict[str, Any]:
    events = elements.get("events") or []
    relations = elements.get("relations") or []

    names: List[str] = []
    for item in events:
        name = _normalize_text(item.get("name"))
        if name and name not in names:
            names.append(name)
    if top_event not in names:
        names.insert(0, top_event)
    for rel in relations:
        for key in ("parent", "child"):
            name = _normalize_text(rel.get(key))
            if name and name not in names:
                names.append(name)

    node_ids = {name: _stable_id("node", name) for name in names}
    node_list = []
    for name in names:
        node_type = "top_event" if _normalize_event_key(name) == _normalize_event_key(top_event) else "basic_event"
        node_list.append(
            {
                "id": node_ids[name],
                "name": name,
                "type": node_type,
                "gate": None,
                "transfer": "",
                "event": None if node_type == "top_event" else {},
            }
        )

    link_list = []
    seen = set()
    for rel in relations:
        parent = _normalize_text(rel.get("parent"))
        child = _normalize_text(rel.get("child"))
        if not parent or not child or parent not in node_ids or child not in node_ids:
            continue
        key = (node_ids[child], node_ids[parent])
        if key in seen:
            continue
        seen.add(key)
        link_list.append(
            {
                "type": "link",
                "sourceId": node_ids[child],
                "targetId": node_ids[parent],
                "isCondition": False,
            }
        )

    return {"nodeList": node_list, "linkList": link_list}


def _match_documents_for_event(name: str, chunks: List[Dict[str, Any]], *, limit: int = 3) -> List[Dict[str, Any]]:
    normalized_name = _normalize_event_key(name)
    keywords = [
        token
        for token in re.split(r"[\s,，。；;、()（）:/]+", str(name or ""))
        if len(token.strip()) >= 2
    ]
    matched = []
    seen = set()

    def append_doc(chunk: Dict[str, Any]):
        chunk_key = str(_chunk_reference(chunk))
        if not chunk_key or chunk_key in seen:
            return False
        seen.add(chunk_key)
        matched.append(
            {
                "chunk_id": chunk.get("chunk_id", chunk.get("id")),
                "chunk_name": chunk.get("chunk_name") or chunk.get("title") or chunk.get("heading") or chunk.get("chapter") or "",
                "section_path": chunk.get("section_path") or chunk.get("section") or chunk.get("chapter") or "",
                "source_page": chunk.get("source_page") or chunk.get("source") or chunk.get("page") or "",
                "file_id": chunk.get("file_id", ""),
                "file_version_id": chunk.get("file_version_id", ""),
            }
        )
        return len(matched) >= limit

    for chunk in chunks or []:
        content = _chunk_content_excerpt(chunk, limit=2000)
        haystack = _normalize_event_key(content)
        if normalized_name and normalized_name in haystack:
            if append_doc(chunk):
                return matched
            continue
        if keywords and any(_normalize_event_key(token) in haystack for token in keywords):
            if append_doc(chunk):
                return matched

    for chunk in chunks or []:
        if append_doc(chunk):
            break
    return matched


def _post_process_chunk_generated_tree(
    tree: Dict[str, Any],
    *,
    top_event: str,
    elements: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fallback_tree = _build_chunk_tree_fallback(top_event, elements)
    raw_nodes = tree.get("nodeList") or fallback_tree["nodeList"]
    raw_links = tree.get("linkList") or fallback_tree["linkList"]
    extracted_events = elements.get("events") or []
    extracted_relations = elements.get("relations") or []

    event_index = {
        _normalize_event_key(item.get("name")): item
        for item in extracted_events
        if _normalize_text(item.get("name"))
    }
    nodes_by_name: Dict[str, Dict[str, Any]] = {}
    nodes_by_id: Dict[str, Dict[str, Any]] = {}

    def ensure_node(name: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_name = _normalize_text(name) or top_event
        key = _normalize_event_key(normalized_name)
        if key in nodes_by_name:
            existing = nodes_by_name[key]
            if payload:
                for field in ("type", "gate", "transfer", "event"):
                    if payload.get(field) not in (None, "") and existing.get(field) in (None, ""):
                        existing[field] = payload.get(field)
            return existing
        node = {
            "id": (payload or {}).get("id") or _stable_id("node", normalized_name),
            "name": normalized_name,
            "type": (payload or {}).get("type"),
            "gate": (payload or {}).get("gate"),
            "transfer": (payload or {}).get("transfer", ""),
            "event": (payload or {}).get("event"),
        }
        nodes_by_name[key] = node
        nodes_by_id[node["id"]] = node
        return node

    ensure_node(top_event, {"type": "top_event", "event": None})
    for item in extracted_events:
        ensure_node(item.get("name") or "", {"type": item.get("type"), "event": item})
    for node in raw_nodes:
        ensure_node(node.get("name") or "", node)
    for rel in extracted_relations:
        ensure_node(rel.get("parent") or "")
        ensure_node(rel.get("child") or "")

    link_pairs = []
    seen_pairs = set()

    def append_pair(source_id: str, target_id: str):
        if not source_id or not target_id or source_id == target_id:
            return
        pair = (source_id, target_id)
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        link_pairs.append(pair)

    for link in raw_links:
        source_id = link.get("sourceId")
        target_id = link.get("targetId")
        if source_id in nodes_by_id and target_id in nodes_by_id:
            append_pair(source_id, target_id)

    if not link_pairs:
        for rel in extracted_relations:
            parent_name = _normalize_text(rel.get("parent"))
            child_name = _normalize_text(rel.get("child"))
            if not parent_name or not child_name:
                continue
            parent = ensure_node(parent_name)
            child = ensure_node(child_name)
            append_pair(child["id"], parent["id"])

    children_map: Dict[str, List[str]] = {}
    gate_map: Dict[str, str] = {}
    for source_id, target_id in link_pairs:
        children_map.setdefault(target_id, []).append(source_id)
    for rel in extracted_relations:
        parent_name = _normalize_text(rel.get("parent"))
        child_name = _normalize_text(rel.get("child"))
        if not parent_name or not child_name:
            continue
        parent = ensure_node(parent_name)
        gate = str(rel.get("gate") or "").strip().upper()
        if gate in {"AND", "OR"}:
            gate_map[parent["id"]] = gate

    normalized_nodes = []
    ordered_nodes = sorted(
        nodes_by_name.values(),
        key=lambda item: (0 if _normalize_event_key(item["name"]) == _normalize_event_key(top_event) else 1, item["name"]),
    )
    for index, node in enumerate(ordered_nodes, start=1):
        node_id = node["id"]
        name = node["name"]
        explicit_type = str(node.get("type") or "").strip()
        if _normalize_event_key(name) == _normalize_event_key(top_event):
            node_type = "top_event"
        elif explicit_type in {"intermediate_event", "basic_event"}:
            node_type = explicit_type
        else:
            node_type = "intermediate_event" if children_map.get(node_id) else "basic_event"

        gate = node.get("gate")
        if node_type == "basic_event":
            gate = None
        elif not gate:
            gate = gate_map.get(node_id) or ("OR" if children_map.get(node_id) else None)

        if False and node_type == "top_event":
            event = None
        else:
            extracted = event_index.get(_normalize_event_key(name), {})
            raw_event = node.get("event") if isinstance(node.get("event"), dict) else {}
            probability = raw_event.get("probability", extracted.get("probability", 1e-8))
            event = {
                "id": str(raw_event.get("id") or extracted.get("id") or f"E{index:03d}"),
                "name": name,
                "description": str(
                    raw_event.get("description")
                    or extracted.get("description")
                    or f"{name} related abnormal condition"
                ),
                "errorLevel": str(raw_event.get("errorLevel") or extracted.get("errorLevel") or "中"),
                "priority": raw_event.get("priority", extracted.get("priority", 0)),
                "probability": probability,
                "showProbability": raw_event.get("showProbability", extracted.get("showProbability", probability)),
                "rule": str(raw_event.get("rule") or extracted.get("rule") or ""),
                "rules": _coerce_rules(
                    raw_event.get("rules")
                    or extracted.get("rules")
                    or raw_event.get("rule")
                    or extracted.get("rule")
                ),
                "investigateMethod": str(
                    raw_event.get("investigateMethod")
                    or extracted.get("investigateMethod")
                    or f"Inspect alarms, state changes, and evidence related to {name}"
                ),
                "documents": _match_documents_for_event(name, chunks, limit=3),
            }

        normalized_nodes.append(
            {
                "type": node_type,
                "gate": gate,
                "name": name,
                "id": node_id,
                "transfer": node.get("transfer", ""),
                "event": event,
            }
        )

    valid_ids = {node["id"] for node in normalized_nodes}
    normalized_links = [
        {
            "type": "link",
            "sourceId": source_id,
            "targetId": target_id,
            "isCondition": False,
        }
        for source_id, target_id in link_pairs
        if source_id in valid_ids and target_id in valid_ids
    ]

    return _sanitize_fault_tree_documents({"nodeList": normalized_nodes, "linkList": normalized_links})


def build_fault_tree_from_subgraph_and_chunks(
    top_event: str,
    subgraph_bundle: Dict[str, Any],
    evidence_chunks: List[Dict[str, Any]],
    requirements: str = "",
    part_details: Optional[Dict[str, Any]] = None,
    include_meta: bool = False,
) -> Dict[str, Any]:
    started = time.perf_counter()
    skeleton = _compress_subgraph_to_tree_skeleton(subgraph_bundle)
    evidence = _format_evidence_chunks(evidence_chunks)
    skeleton_for_prompt = {
        "root_tree_node_id": skeleton["root_tree_node_id"],
        "nodes": [
            {
                "id": node["id"],
                "name": node["name"],
                "type": node["type"],
                "gate": node["gate"],
                "graphNodeId": node["graphNodeId"],
                "kg_key": node["kg_key"],
                "fileId": node["fileId"],
                "fileVersionId": node["fileVersionId"],
                "graph_props": node["graph_props"],
                "source_chunk_ids": node["source_chunk_ids"],
                "source_chunk_refs": node["source_chunk_refs"],
                "documents_seed": node["documents_seed"],
            }
            for node in skeleton["nodes"]
        ],
        "links": skeleton["links"],
    }

    pd = part_details if isinstance(part_details, dict) and part_details else None
    part_details_text = json.dumps(pd, ensure_ascii=False, indent=2) if pd else ""
    physical_ref_block = ""
    if pd:
        physical_ref_block = f"""
Physical component mapping:
When appropriate, append `[Ref: Object_X]` to the end of `description` so the tree can be aligned with the physical component view.
Available components:
{part_details_text}
"""

    prompt = f"""You are an industrial fault-tree modeling expert.

This is the graph-retrieval path. A local knowledge-graph subgraph has already been converted into a trusted tree skeleton.
You must preserve that skeleton and only use evidence chunks to enrich node content, not to invent a different structure.

Top event: {top_event}
User requirements: {requirements or 'none'}

Skeleton constraints:
1. Preserve the existing node hierarchy and causal links from the skeleton.
2. Do not delete skeleton nodes and do not invent a new parallel structure.
3. If a parent node already has `gate=AND`, keep it as `AND`.
4. If a parent has multiple children and no explicit AND mark, keep or infer `gate=OR`.
5. The top event node must stay `type=top_event` and should keep a complete `event` object like other fault-event nodes.
6. Every event node must have a complete `event` object.
7. Keep `graphNodeId` and `kg_key` identical to the input skeleton.
8. `documents` must be filled from the recalled evidence chunks, but each document object may contain only:
   `chunk_id`, `chunk_name`, `section_path`, `source_page`, `file_id`, `file_version_id`.
   Never output chunk body fields such as `content`, `text`, `raw_text`, `page_content`, `file`, or `chunk_uid`.
   The frontend will load chunk content dynamically by `file_version_id + chunk_id` when a node detail panel is opened.
9. Return JSON only. 事件节点名称需要是中文的故障现象。

Graph skeleton:
{json.dumps(skeleton_for_prompt, ensure_ascii=False, indent=2)}

Evidence chunks:
{json.dumps(evidence, ensure_ascii=False, indent=2)}
{physical_ref_block}

Required output:
{{
  "nodeList": [...],
  "linkList": [...]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=LLM_GENERATION_MAX_TOKENS,
    )
    tree = _parse_json(response.choices[0].message.content)
    normalized_tree = _post_process_generated_tree(tree, skeleton, evidence)
    if include_meta:
        return {
            "tree": normalized_tree,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "token_usage": _normalize_token_usage(getattr(response, "usage", None)),
        }
    return normalized_tree


def extract_fault_elements_from_chunks(
    top_event: str,
    chunks: List[Dict[str, Any]],
    *,
    include_meta: bool = False,
) -> Dict[str, Any]:
    started = time.perf_counter()
    prompt = f"""You are an industrial fault-tree analysis expert.

Your task is to infer fault-tree semantics only from the recalled evidence chunks for top event "{top_event}".
Do not assume an existing graph skeleton. You must infer event hierarchy and logic gates from the text itself.

Requirements:
1. There must be exactly one `top_event`, and its name must be "{top_event}".
2. Extract `intermediate_event` and `basic_event` candidates with clear cause-effect relationships.
3. For every parent-child relation, decide a gate:
   - `AND`: children must occur together to cause the parent.
   - `OR`: any child can independently cause the parent.
4. If the evidence does not strongly support `AND`, use `OR`.
5. Keep the structure focused; avoid generic or redundant nodes.
6. Fill `description`, `errorLevel`, `investigateMethod`, and `rules` when evidence supports them.

Evidence chunks:
{_format_chunks_for_prompt(chunks)}

Return strict JSON only:
{{
  "events": [
    {{
      "name": "event name",
      "type": "top_event/intermediate_event/basic_event",
      "description": "event description",
      "errorLevel": "高/中/低",
      "investigateMethod": "how to investigate",
      "rules": [
        {{
          "measurePointName": "measure point",
          "symbol": ">",
          "thresholds": ["value"],
          "duration": "time window"
        }}
      ]
    }}
  ],
  "relations": [
    {{
      "parent": "parent event name",
      "child": "child event name",
      "gate": "AND/OR"
    }}
  ]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=2200,
    )
    parsed = _parse_json(response.choices[0].message.content)
    if include_meta:
        return {
            "elements": parsed,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "token_usage": _normalize_token_usage(getattr(response, "usage", None)),
        }
    return parsed


def build_fault_tree_from_chunk_elements(
    top_event: str,
    elements: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    requirements: str = "",
    previous_issues: Optional[List[Dict[str, Any]]] = None,
    part_details: Optional[Dict[str, Any]] = None,
    include_meta: bool = False,
) -> Dict[str, Any]:
    started = time.perf_counter()
    chunks_ref = [
        {
            "chunk_id": _chunk_reference(chunk),
            "chunk_name": chunk.get("chunk_name") or chunk.get("title") or chunk.get("heading") or chunk.get("chapter") or "",
            "section_path": chunk.get("section_path") or chunk.get("section") or chunk.get("chapter") or "",
            "source_page": chunk.get("source_page") or chunk.get("source") or chunk.get("page") or "",
            "file_id": chunk.get("file_id", ""),
            "file_version_id": chunk.get("file_version_id", ""),
        }
        for chunk in chunks
    ]

    retry_hint = ""
    if previous_issues:
        error_msgs = [
            f"- [{item['level']}] {item['message']} (node: {item.get('node_name', '')})"
            for item in previous_issues
            if item.get("level") in {"ERROR", "WARNING"}
        ]
        if error_msgs:
            retry_hint = "\nPrevious draft had these issues. Fix them in the new output:\n" + "\n".join(error_msgs) + "\n"

    pd = part_details if isinstance(part_details, dict) and part_details else None
    part_details_text = json.dumps(pd, ensure_ascii=False, indent=2) if pd else ""
    physical_ref_chunk = ""
    if pd:
        physical_ref_chunk = f"""
Physical component mapping:
Append a `[Ref: Object_X]` marker at the end of `description` when you can associate an event with a physical component.
Available components:
{part_details_text}
"""

    prompt = f"""You are an industrial fault-tree modeling expert.

This is the chunks-only generation path. There is no trusted graph skeleton available.
You must build a complete fault tree for top event "{top_event}" only from recalled chunks and extracted elements.

Evidence chunks:
{_format_chunks_for_prompt(chunks)}

Available documents for `event.documents`:
{json.dumps(chunks_ref, ensure_ascii=False, indent=2)}

Extracted fault elements:
{json.dumps(elements, ensure_ascii=False, indent=2)}
{retry_hint}
User requirements:
{requirements or 'none'}
{physical_ref_chunk}

Output rules:
1. Output complete `nodeList` and `linkList`.
2. There must be exactly one `top_event` node named "{top_event}", and it should keep a complete `event` object like other fault-event nodes.
3. Every other node must be `intermediate_event` or `basic_event`.
4. If a node still has children in `linkList`, it must be `intermediate_event`; if it has no children, it must be `basic_event`.
5. Every `top_event` and `intermediate_event` must explicitly carry `gate`. Use `OR` by default if evidence is insufficient.
6. Every non-top node must have a full `event` object including:
   `id`, `name`, `description`, `errorLevel`, `priority`, `probability`, `showProbability`, `rule`, `rules`, `investigateMethod`, `documents`
7. `documents` must be selected only from the provided chunk list. Each non-top node should reference 1-3 chunks.
   Each document object may contain only these six lightweight reference fields:
   `chunk_id`, `chunk_name`, `section_path`, `source_page`, `file_id`, `file_version_id`.
   Never output chunk body fields such as `content`, `text`, `raw_text`, `page_content`, `file`, or `chunk_uid`.
   The frontend will load chunk content dynamically by `file_version_id + chunk_id` when a node detail panel is opened.
8. `linkList.sourceId` is child and `linkList.targetId` is parent.
9. Do not output isolated nodes. Do not output an empty tree.
10. If evidence is limited, still produce the smallest coherent tree with meaningful hierarchy and logic gates.
11. Return JSON only.

Required JSON shape:
{{
  "nodeList": [
    {{
      "id": "node-xxxxxxxx",
      "name": "event name",
      "type": "top_event/intermediate_event/basic_event",
      "gate": "AND/OR/null",
      "transfer": "",
      "event": {{
        "id": "E001",
        "name": "event name",
        "description": "description",
        "errorLevel": "高/中/低",
        "priority": 0,
        "probability": 1e-8,
        "showProbability": 1e-8,
        "rule": "",
        "rules": [],
        "investigateMethod": "how to investigate",
        "documents": [
          {{
            "chunk_id": "...",
            "chunk_name": "...",
            "section_path": "...",
            "source_page": "...",
            "file_id": "...",
            "file_version_id": "..."
          }}
        ]
      }}
    }}
  ],
  "linkList": [
    {{
      "type": "link",
      "sourceId": "node-child",
      "targetId": "node-parent",
      "isCondition": false
    }}
  ]
}}
"""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=LLM_GENERATION_MAX_TOKENS,
    )
    tree = _parse_json(response.choices[0].message.content)
    normalized_tree = _post_process_chunk_generated_tree(
        tree,
        top_event=top_event,
        elements=elements,
        chunks=chunks,
    )
    if include_meta:
        return {
            "tree": normalized_tree,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "token_usage": _normalize_token_usage(getattr(response, "usage", None)),
        }
    return normalized_tree


def _extract_json_text(raw: str) -> str:
    clean = re.sub(r"```json|```", "", raw or "").strip()
    if not clean:
        return ""

    start_obj = clean.find("{")
    start_arr = clean.find("[")
    starts = [index for index in (start_obj, start_arr) if index != -1]
    if not starts:
        return clean
    start = min(starts)
    candidate = clean[start:]

    open_char = candidate[0]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(candidate):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return candidate[: index + 1]
    return candidate


def _try_repair_json_text(candidate: str):
    text = (candidate or "").strip()
    if not text:
        return None

    opens = {"{": "}", "[": "]"}
    stack = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in opens:
            stack.append(opens[char])
        elif char in ("]", "}") and stack and char == stack[-1]:
            stack.pop()

    repaired = text + "".join(reversed(stack))
    for _ in range(MAX_JSON_REPAIR_RETRY):
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            repaired = repaired.rstrip(",")
            continue
    return None


def _parse_json(raw: str) -> dict:
    candidate = _extract_json_text(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_error:
        repaired = _try_repair_json_text(candidate)
        if repaired is not None:
            return repaired
        raise ValueError(f"LLM JSON parse failed: {first_error}\nRaw output:\n{raw}")
