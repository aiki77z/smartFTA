"""
专家修正差异分析与修正经验检索模块。

当前启用版本：
- 使用节点名称的精确匹配，从历史 corrections 中检索相关修正记录
- 支持对 AI 版本和专家版本做差异分析并写入 MongoDB
- 支持基于版本差异自动生成简短修改说明

说明：
- `reason` 字段不是模型生成的，而是在差异分析时由程序按修正类型写入
- 语义匹配（embedding / Elasticsearch）方案暂未启用，相关扩展代码已按注释形式保留
"""

from datetime import datetime

from database import db, get_version

corrections_col = db["corrections"]


def analyze_and_store(tree_id: str, ai_version: int, expert_version: int) -> int:
    """
    对比 AI 版本和专家版本，提取差异，并将每条修正独立存入 corrections 集合。
    返回写入条数。
    """
    ai_ver = get_version(tree_id, ai_version)
    expert_ver = get_version(tree_id, expert_version)

    if not ai_ver or not expert_ver:
        return 0

    ai_data = ai_ver.get("tree_data", {})
    expert_data = expert_ver.get("tree_data", {})

    corrections = _diff_trees(ai_data, expert_data)
    if not corrections:
        return 0

    top_event = _get_top_event_name(expert_data)
    now = datetime.now()

    docs = []
    for correction in corrections:
        docs.append({
            "tree_id": tree_id,
            "ai_version": ai_version,
            "expert_version": expert_version,
            "top_event": top_event,
            "node_name": correction.get("node_name") or correction.get("from_name", ""),
            "node_type": correction.get("node_type", ""),
            "correction_type": correction["type"],
            "detail": correction,
            "created_at": now,
        })

    corrections_col.insert_many(docs)
    print(f"[学习] 已记录 {len(docs)} 条修正，顶事件：{top_event}")
    return len(docs)


def generate_change_description(old_tree_data: dict, new_tree_data: dict) -> str:
    """
    基于前后两个版本的故障树，自动生成简短版本说明。
    """
    if not old_tree_data or not new_tree_data:
        return "手动修改"

    diffs = _diff_trees(old_tree_data, new_tree_data)
    if not diffs:
        return "未检测到结构性修改"

    descriptions = []

    old_top = _get_top_event_name(old_tree_data)
    new_top = _get_top_event_name(new_tree_data)
    if old_top and new_top and old_top != new_top:
        descriptions.append(f"将顶事件名称从{old_top}修改为{new_top}")

    for diff in diffs:
        ctype = diff.get("type", "")
        if ctype == "name_edited":
            from_name = diff.get("from_name", "")
            to_name = diff.get("to_name", "")
            if from_name and to_name:
                text = f"将节点名称从{from_name}修改为{to_name}"
                if text not in descriptions:
                    descriptions.append(text)
        elif ctype == "gate_changed":
            node_name = diff.get("node_name", "未命名节点")
            descriptions.append(
                f"将{node_name}的逻辑门从{diff.get('from_gate', '')}调整为{diff.get('to_gate', '')}"
            )
        elif ctype == "node_added":
            node_name = diff.get("node_name", "未命名节点")
            parent_name = diff.get("parent_name", "")
            if parent_name:
                descriptions.append(f"在{parent_name}下新增节点{node_name}")
            else:
                descriptions.append(f"新增节点{node_name}")
        elif ctype == "node_deleted":
            descriptions.append(f"删除节点{diff.get('node_name', '未命名节点')}")
        elif ctype == "link_added":
            if diff.get("from_node") and diff.get("to_node"):
                descriptions.append(f"新增连接{diff['from_node']}->{diff['to_node']}")
        elif ctype == "link_deleted":
            if diff.get("from_node") and diff.get("to_node"):
                descriptions.append(f"删除连接{diff['from_node']}->{diff['to_node']}")

    deduped = []
    for item in descriptions:
        if item and item not in deduped:
            deduped.append(item)

    if not deduped:
        return "手动修改"
    if len(deduped) == 1:
        return deduped[0]
    if len(deduped) == 2:
        return "；".join(deduped)
    return "；".join(deduped[:3]) + "等修改"


def _diff_trees(ai_data: dict, expert_data: dict) -> list:
    """
    对比两棵树的 nodeList 和 linkList，返回结构化差异列表。

    reason 字段来源：
    - 完全由程序按差异类型写入
    - 不是模型推理出来的
    """
    corrections = []

    ai_nodes = {n["id"]: n for n in ai_data.get("nodeList", [])}
    expert_nodes = {n["id"]: n for n in expert_data.get("nodeList", [])}

    ai_ids = set(ai_nodes.keys())
    expert_ids = set(expert_nodes.keys())

    for nid in expert_ids - ai_ids:
        node = expert_nodes[nid]
        parent_name = _find_parent_name(nid, expert_data, expert_nodes)
        corrections.append({
            "type": "node_added",
            "node_type": node.get("type"),
            "node_name": node.get("name", ""),
            "gate": node.get("gate"),
            "error_level": node.get("event", {}).get("errorLevel", "") if node.get("event") else "",
            "description": node.get("event", {}).get("description", "") if node.get("event") else "",
            "parent_name": parent_name,
            "reason": "AI遗漏了该故障节点",
        })

    for nid in ai_ids - expert_ids:
        node = ai_nodes[nid]
        corrections.append({
            "type": "node_deleted",
            "node_type": node.get("type"),
            "node_name": node.get("name", ""),
            "reason": "AI生成了不存在或不合理的故障节点",
        })

    for nid in ai_ids & expert_ids:
        ai_node = ai_nodes[nid]
        expert_node = expert_nodes[nid]

        ai_gate = ai_node.get("gate")
        expert_gate = expert_node.get("gate")
        if ai_gate != expert_gate and expert_gate is not None:
            corrections.append({
                "type": "gate_changed",
                "node_name": expert_node.get("name", ""),
                "node_type": expert_node.get("type", ""),
                "from_gate": ai_gate,
                "to_gate": expert_gate,
                "reason": f"AI将逻辑门设为{ai_gate}，专家修正为{expert_gate}",
            })

        ai_name = ai_node.get("name", "").strip()
        expert_name = expert_node.get("name", "").strip()
        if ai_name and expert_name and ai_name != expert_name:
            corrections.append({
                "type": "name_edited",
                "node_name": ai_name,
                "node_type": expert_node.get("type", ""),
                "from_name": ai_name,
                "to_name": expert_name,
                "reason": "AI使用了不准确的术语或描述",
            })

    ai_links = {(l["sourceId"], l["targetId"]) for l in ai_data.get("linkList", [])}
    expert_links = {(l["sourceId"], l["targetId"]) for l in expert_data.get("linkList", [])}

    for src, tgt in expert_links - ai_links:
        src_name = expert_nodes.get(src, {}).get("name", src)
        tgt_name = expert_nodes.get(tgt, {}).get("name", tgt)
        corrections.append({
            "type": "link_added",
            "node_name": src_name,
            "node_type": expert_nodes.get(src, {}).get("type", ""),
            "from_node": src_name,
            "to_node": tgt_name,
            "reason": "AI遗漏了该因果连接",
        })

    for src, tgt in ai_links - expert_links:
        src_name = ai_nodes.get(src, {}).get("name", src)
        tgt_name = ai_nodes.get(tgt, {}).get("name", tgt)
        corrections.append({
            "type": "link_deleted",
            "node_name": src_name,
            "node_type": ai_nodes.get(src, {}).get("type", ""),
            "from_node": src_name,
            "to_node": tgt_name,
            "reason": "AI生成了错误的因果连接",
        })

    return corrections


def _find_parent_name(node_id: str, tree_data: dict, nodes_by_id: dict) -> str:
    for link in tree_data.get("linkList", []):
        if link.get("sourceId") == node_id:
            parent_id = link.get("targetId")
            return nodes_by_id.get(parent_id, {}).get("name", "")
    return ""


def _get_top_event_name(tree_data: dict) -> str:
    for node in tree_data.get("nodeList", []):
        if node.get("type") == "top_event":
            return node.get("name", "")
    return ""


def get_relevant_corrections(draft_tree: dict, max_per_node: int = 3) -> list:
    """
    当前启用的是“完全匹配版本”：
    - 匹配范围：草稿树中的所有节点，不只匹配顶事件
    - 匹配方式：按 node_name 精确匹配，再按类型 / 修正价值 / 时间做排序
    - max_per_node：每个节点最多匹配多少条修正记录

    语义匹配：（注释）
    1. 为每个草稿节点构造 semantic_text
    2. 为历史 correction 记录构造 semantic_text
    3. 生成 embedding
    4. 用向量相似度或 Elasticsearch kNN 做召回
    5. 按阈值过滤，再结合类型和时间重排
    """
    draft_nodes = draft_tree.get("nodeList", []) if draft_tree else []
    if not draft_nodes:
        return []

    node_name_to_type = {
        node.get("name", "").strip(): node.get("type", "")
        for node in draft_nodes
        if node.get("name", "").strip()
    }
    node_names = list(node_name_to_type.keys())
    if not node_names:
        return []

    raw_hits = list(
        corrections_col.find(
            {"node_name": {"$in": node_names}},
            {"_id": 0}
        ).sort("created_at", -1).limit(200)
    )
    if not raw_hits:
        return []

    newest_ts = raw_hits[0]["created_at"].timestamp()
    oldest_ts = raw_hits[-1]["created_at"].timestamp()
    ts_range = max(newest_ts - oldest_ts, 1)

    """高价值修正类型：
    - gate_changed
    - node_deleted
    """
    high_value = {"gate_changed", "node_deleted"}
    

    scored = []
    for hit in raw_hits:
        score = 0
        hit_name = hit.get("node_name", "")
        hit_type = hit.get("node_type", "")
        correction_type = hit.get("correction_type", "")

        if hit_name in node_name_to_type:
            score += 3
            if node_name_to_type[hit_name] == hit_type:
                score += 2

        if correction_type in high_value:
            score += 1

        """时间衰减：
        - 越新的修正，权重越高
        - 越旧的修正，权重越低
        """
        ts = hit["created_at"].timestamp() if hit.get("created_at") else oldest_ts
        score += (ts - oldest_ts) / ts_range

        enriched = dict(hit)
        enriched["matched_node_name"] = hit_name
        scored.append((score, enriched))

    scored.sort(key=lambda x: x[0], reverse=True)

    seen_counts = {}
    results = []
    for _, hit in scored:
        name = hit.get("node_name", "")
        if seen_counts.get(name, 0) < max_per_node:
            results.append(hit)
            seen_counts[name] = seen_counts.get(name, 0) + 1

    return results


def format_corrections_for_repair(corrections: list) -> str:
    """
    将检索到的修正记录格式化为 repair prompt。
    """
    if not corrections:
        return ""

    grouped = {}
    for correction in corrections:
        group_name = correction.get("matched_node_name") or correction.get("node_name") or "（未识别节点）"
        grouped.setdefault(group_name, []).append(correction)

    lines = [
        "## 定向修正建议（基于历史专家修正经验）",
        "以下建议来自历史中名称完全匹配的修正记录。",
        "请仅针对提及节点做最小化修改，不要重构整棵故障树。",
        "",
    ]

    for group_name, hits in grouped.items():
        lines.append(f"### 当前草稿节点【{group_name}】")
        for hit in hits:
            detail = hit.get("detail", hit)
            ctype = hit.get("correction_type") or detail.get("type", "")
            source = hit.get("top_event", "未知场景")

            if ctype == "gate_changed":
                lines.append(
                    f"- 【逻辑门】历史中该节点的逻辑门曾由 {detail.get('from_gate')} 调整为 "
                    f"{detail.get('to_gate')}（来自【{source}】）。请检查当前逻辑门是否合理。"
                )
            elif ctype == "node_deleted":
                lines.append(
                    f"- 【疑似冗余】历史中该节点曾被专家删除（来自【{source}】）。"
                    f"请确认当前节点是否真实存在。"
                )
            elif ctype == "node_added":
                parent = detail.get("parent_name", "")
                parent_hint = f"，常挂载于【{parent}】下" if parent else ""
                lines.append(
                    f"- 【易遗漏】历史中专家补充了节点【{detail.get('node_name', '')}】"
                    f"{parent_hint}（来自【{source}】）。请检查当前树是否也需要补充。"
                )
            elif ctype == "name_edited":
                lines.append(
                    f"- 【术语修正】历史中名称曾从【{detail.get('from_name')}】修改为"
                    f"【{detail.get('to_name')}】（来自【{source}】）。"
                )
            elif ctype == "link_added":
                lines.append(
                    f"- 【补充连接】历史中补充了【{detail.get('from_node')}】→【{detail.get('to_node')}】"
                    f"的因果连接（来自【{source}】）。"
                )
            elif ctype == "link_deleted":
                lines.append(
                    f"- 【删除连接】历史中删除了【{detail.get('from_node')}】→【{detail.get('to_node')}】"
                    f"的因果连接（来自【{source}】）。"
                )
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# 下面保留“语义匹配版本”的实现草稿，当前全部注释停用。
# 当前运行中的版本仍然是上面的“node_name 精确匹配”逻辑。
#
# 恢复语义匹配时，建议步骤：
# 1. 在 config.py 中恢复 EMBEDDING / 阈值相关配置
# 2. 取消下面工具函数和语义检索函数的注释
# 3. 将当前 get_relevant_corrections() 切换为语义版实现
# 4. 在 analyze_and_store() 中恢复 semantic_text / embedding 字段写入
# ============================================================================

# from difflib import SequenceMatcher
# import math
# from openai import OpenAI
# from config import (
#     EMBEDDING_API_KEY,
#     EMBEDDING_BASE_URL,
#     EMBEDDING_MODEL,
#     SEMANTIC_SIMILARITY_THRESHOLD,
# )
#
# embedding_client = (
#     OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)
#     if EMBEDDING_API_KEY else None
# )
# _embedding_cache = {}
#
# def _build_relation_maps(tree_data: dict) -> tuple:
#     parent_map = {}
#     child_map = {}
#     for link in tree_data.get("linkList", []):
#         src = link.get("sourceId")
#         tgt = link.get("targetId")
#         if src and tgt:
#             parent_map.setdefault(src, []).append(tgt)
#             child_map.setdefault(tgt, []).append(src)
#     return parent_map, child_map
#
# def _build_node_semantic_text(top_event: str, node: dict,
#                               parent_names: list, child_names: list) -> str:
#     event = node.get("event") or {}
#     rules = event.get("rules", [])
#     rule_texts = []
#     for rule in rules:
#         measure = rule.get("measurePointName", "")
#         symbol = rule.get("symbol", "")
#         thresholds = ",".join(rule.get("thresholds", []))
#         duration = rule.get("duration", "")
#         rule_texts.append(f"{measure}{symbol}{thresholds} 持续:{duration}")
#
#     parts = [
#         f"顶事件:{top_event}",
#         f"节点类型:{node.get('type', '')}",
#         f"节点名称:{node.get('name', '')}",
#         f"父节点:{'、'.join(parent_names)}" if parent_names else "",
#         f"子节点:{'、'.join(child_names)}" if child_names else "",
#         f"描述:{event.get('description', '')}" if event.get('description') else "",
#         f"排查方法:{event.get('investigateMethod', '')}" if event.get('investigateMethod') else "",
#         f"规则:{'；'.join(rule_texts)}" if rule_texts else "",
#     ]
#     return "\n".join(part for part in parts if part)
#
# def _build_correction_semantic_text(top_event: str, detail: dict) -> str:
#     parts = [
#         f"场景顶事件:{top_event}",
#         f"修正类型:{detail.get('type', '')}",
#         f"节点名称:{detail.get('node_name', '')}",
#         f"节点类型:{detail.get('node_type', '')}",
#         f"原名称:{detail.get('from_name', '')}" if detail.get("from_name") else "",
#         f"新名称:{detail.get('to_name', '')}" if detail.get("to_name") else "",
#         f"原逻辑门:{detail.get('from_gate', '')}" if detail.get("from_gate") else "",
#         f"新逻辑门:{detail.get('to_gate', '')}" if detail.get("to_gate") else "",
#         f"父节点:{detail.get('parent_name', '')}" if detail.get("parent_name") else "",
#         f"起点:{detail.get('from_node', '')}" if detail.get("from_node") else "",
#         f"终点:{detail.get('to_node', '')}" if detail.get("to_node") else "",
#         f"描述:{detail.get('description', '')}" if detail.get("description") else "",
#         f"原因:{detail.get('reason', '')}" if detail.get("reason") else "",
#     ]
#     return "\n".join(part for part in parts if part)
#
# def _get_text_embedding(text: str):
#     if not text:
#         return None
#     if text in _embedding_cache:
#         return _embedding_cache[text]
#     if not EMBEDDING_MODEL or not embedding_client:
#         return None
#
#     try:
#         response = embedding_client.embeddings.create(
#             model=EMBEDDING_MODEL,
#             input=text
#         )
#         vector = response.data[0].embedding
#         _embedding_cache[text] = vector
#         return vector
#     except Exception:
#         return None
#
# def _cosine_similarity(vec_a: list, vec_b: list) -> float:
#     if not vec_a or not vec_b or len(vec_a) != len(vec_b):
#         return 0.0
#     dot = sum(a * b for a, b in zip(vec_a, vec_b))
#     norm_a = math.sqrt(sum(a * a for a in vec_a))
#     norm_b = math.sqrt(sum(b * b for b in vec_b))
#     if norm_a == 0 or norm_b == 0:
#         return 0.0
#     return dot / (norm_a * norm_b)
#
# def _tokenize_for_similarity(text: str) -> set:
#     try:
#         import jieba
#         tokens = [tok.strip() for tok in jieba.lcut(text) if tok.strip()]
#     except ImportError:
#         tokens = [char for char in text if char.strip()]
#     return set(tokens)
#
# def _char_ngrams(text: str, n: int = 2) -> set:
#     clean = text.replace("\n", "").strip()
#     if not clean:
#         return set()
#     if len(clean) < n:
#         return {clean}
#     return {clean[i:i+n] for i in range(len(clean) - n + 1)}
#
# def _lexical_similarity(text_a: str, text_b: str) -> float:
#     ratio = SequenceMatcher(None, text_a, text_b).ratio()
#     tokens_a = _tokenize_for_similarity(text_a)
#     tokens_b = _tokenize_for_similarity(text_b)
#     token_jaccard = len(tokens_a & tokens_b) / (len(tokens_a | tokens_b) or 1) if (tokens_a or tokens_b) else 0.0
#     chargrams_a = _char_ngrams(text_a)
#     chargrams_b = _char_ngrams(text_b)
#     char_jaccard = len(chargrams_a & chargrams_b) / (len(chargrams_a | chargrams_b) or 1) if (chargrams_a or chargrams_b) else 0.0
#     return ratio * 0.45 + token_jaccard * 0.35 + char_jaccard * 0.20
#
# def _compute_similarity(text_a: str, text_b: str, emb_a=None, emb_b=None) -> float:
#     if emb_a and emb_b:
#         return _cosine_similarity(emb_a, emb_b)
#     return _lexical_similarity(text_a, text_b)
#
# def get_relevant_corrections_semantic(draft_tree: dict, max_per_node: int = 3) -> list:
#     """
#     语义匹配版本：
#     - 对整棵草稿树中的所有节点构造语义文本
#     - 优先使用 embedding 相似度
#     - 若未配置 embedding，则退化为词法相似度
#     - 达到阈值的历史修正记录会被召回
#     """
#     draft_nodes = draft_tree.get("nodeList", []) if draft_tree else []
#     if not draft_nodes:
#         return []
#
#     nodes_by_id = {n.get("id"): n for n in draft_nodes if n.get("id")}
#     if not nodes_by_id:
#         return []
#
#     parent_map, child_map = _build_relation_maps(draft_tree)
#     top_event = _get_top_event_name(draft_tree)
#     raw_hits = list(corrections_col.find({}).sort("created_at", -1).limit(500))
#     if not raw_hits:
#         return []
#
#     newest_ts = raw_hits[0]["created_at"].timestamp()
#     oldest_ts = raw_hits[-1]["created_at"].timestamp()
#     ts_range = max(newest_ts - oldest_ts, 1)
#     high_value = {"gate_changed", "node_deleted"}
#     all_matches = []
#
#     for node in draft_nodes:
#         draft_name = node.get("name", "").strip()
#         if not draft_name:
#             continue
#
#         node_id = node.get("id")
#         draft_type = node.get("type", "")
#         parent_names = [nodes_by_id[pid].get("name", "") for pid in parent_map.get(node_id, []) if pid in nodes_by_id]
#         child_names = [nodes_by_id[cid].get("name", "") for cid in child_map.get(node_id, []) if cid in nodes_by_id]
#
#         draft_text = _build_node_semantic_text(top_event, node, parent_names, child_names)
#         draft_embedding = _get_text_embedding(draft_text)
#         node_matches = []
#
#         for hit in raw_hits:
#             detail = hit.get("detail", {})
#             corr_text = hit.get("semantic_text") or _build_correction_semantic_text(hit.get("top_event", ""), detail)
#             corr_embedding = hit.get("embedding")
#             similarity = _compute_similarity(draft_text, corr_text, draft_embedding, corr_embedding)
#
#             exact_name_match = (draft_name == hit.get("node_name", ""))
#             type_match = (draft_type == hit.get("node_type", ""))
#             if similarity < SEMANTIC_SIMILARITY_THRESHOLD and not exact_name_match:
#                 continue
#
#             score = similarity * 10
#             if exact_name_match:
#                 score += 3
#             if type_match:
#                 score += 2
#             if hit.get("correction_type") in high_value:
#                 score += 1
#
#             ts = hit["created_at"].timestamp() if hit.get("created_at") else oldest_ts
#             score += (ts - oldest_ts) / ts_range
#
#             enriched = dict(hit)
#             enriched["matched_node_name"] = draft_name
#             enriched["matched_node_type"] = draft_type
#             enriched["similarity"] = round(similarity, 4)
#             node_matches.append((score, enriched))
#
#         node_matches.sort(key=lambda x: x[0], reverse=True)
#         all_matches.extend(hit for _, hit in node_matches[:max_per_node])
#
#     all_matches.sort(
#         key=lambda x: (x.get("similarity", 0), x.get("created_at", datetime.min)),
#         reverse=True,
#     )
#
#     deduped = []
#     seen = set()
#     for hit in all_matches:
#         key = (
#             hit.get("matched_node_name", ""),
#             hit.get("correction_type", ""),
#             hit.get("node_name", ""),
#             str(hit.get("detail", {})),
#         )
#         if key not in seen:
#             deduped.append(hit)
#             seen.add(key)
#     return deduped
