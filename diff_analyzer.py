"""
专家修正差异分析与修正经验检索模块。

修正记录检索：
- 若配置了 EMBEDDING_MODEL：启动修复时一次性加载 corrections（可 exclude_tree_id），
  草稿节点与修正记录构造 semantic_text，批量 embedding 后在内存用 numpy 算余弦相似度，
  按 SEMANTIC_SIMILARITY_THRESHOLD 过滤，再结合类型权重与时间重排。
- 未配置 embedding 或草稿侧无法得到向量时：回退为 node_name 精确匹配。

说明：`reason` 字段由程序按差异类型写入，不是模型生成。
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from openai import OpenAI

from config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    SEMANTIC_SIMILARITY_THRESHOLD,
)
from database import db, get_version

corrections_col = db["corrections"]

_embedding_client: Optional[OpenAI] = None
_embedding_cache: Dict[str, List[float]] = {}


def _embedding_openai() -> Optional[OpenAI]:
    global _embedding_client
    if not EMBEDDING_API_KEY:
        return None
    if _embedding_client is None:
        _embedding_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)
    return _embedding_client


def _build_relation_maps(tree_data: dict) -> Tuple[dict, dict]:
    parent_map: dict = {}
    child_map: dict = {}
    for link in tree_data.get("linkList", []):
        src = link.get("sourceId")
        tgt = link.get("targetId")
        if src and tgt:
            parent_map.setdefault(src, []).append(tgt)
            child_map.setdefault(tgt, []).append(src)
    return parent_map, child_map


def _build_node_semantic_text(
    top_event: str, node: dict, parent_names: List[str], child_names: List[str]
) -> str:
    event = node.get("event") or {}
    rules = event.get("rules", [])
    rule_texts = []
    for rule in rules:
        measure = rule.get("measurePointName", "")
        symbol = rule.get("symbol", "")
        thresholds = ",".join(rule.get("thresholds", []))
        duration = rule.get("duration", "")
        rule_texts.append(f"{measure}{symbol}{thresholds} 持续:{duration}")

    parts = [
        f"顶事件:{top_event}",
        f"节点类型:{node.get('type', '')}",
        f"节点名称:{node.get('name', '')}",
        f"父节点:{'、'.join(parent_names)}" if parent_names else "",
        f"子节点:{'、'.join(child_names)}" if child_names else "",
        f"描述:{event.get('description', '')}" if event.get("description") else "",
        f"排查方法:{event.get('investigateMethod', '')}" if event.get("investigateMethod") else "",
        f"规则:{'；'.join(rule_texts)}" if rule_texts else "",
    ]
    return "\n".join(part for part in parts if part)


def _build_correction_semantic_text(top_event: str, detail: dict) -> str:
    parts = [
        f"场景顶事件:{top_event}",
        f"修正类型:{detail.get('type', '')}",
        f"节点名称:{detail.get('node_name', '')}",
        f"节点类型:{detail.get('node_type', '')}",
        f"原名称:{detail.get('from_name', '')}" if detail.get("from_name") else "",
        f"新名称:{detail.get('to_name', '')}" if detail.get("to_name") else "",
        f"原逻辑门:{detail.get('from_gate', '')}" if detail.get("from_gate") else "",
        f"新逻辑门:{detail.get('to_gate', '')}" if detail.get("to_gate") else "",
        f"父节点:{detail.get('parent_name', '')}" if detail.get("parent_name") else "",
        f"起点:{detail.get('from_node', '')}" if detail.get("from_node") else "",
        f"终点:{detail.get('to_node', '')}" if detail.get("to_node") else "",
        f"描述:{detail.get('description', '')}" if detail.get("description") else "",
        f"原因:{detail.get('reason', '')}" if detail.get("reason") else "",
    ]
    return "\n".join(part for part in parts if part)


def _embed_strings_ordered(strings: List[str]) -> List[Optional[List[float]]]:
    """对一批文本按出现顺序解析为向量；相同字符串只请求一次 API。"""
    if not strings or not EMBEDDING_MODEL:
        return [None] * len(strings)
    client = _embedding_openai()
    if not client:
        return [None] * len(strings)

    unique: List[str] = []
    pos: Dict[str, int] = {}
    for s in strings:
        key = s or ""
        if key not in pos:
            pos[key] = len(unique)
            unique.append(key)

    vecs_for_unique: List[Optional[List[float]]] = [None] * len(unique)
    to_request: List[str] = []
    request_slot: List[int] = []
    for i, text in enumerate(unique):
        if text in _embedding_cache:
            vecs_for_unique[i] = _embedding_cache[text]
        else:
            to_request.append(text)
            request_slot.append(i)

    batch_size = 64
    for start in range(0, len(to_request), batch_size):
        chunk = to_request[start : start + batch_size]
        try:
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=chunk)
            for j, item in enumerate(response.data):
                vec = item.embedding
                txt = chunk[j]
                _embedding_cache[txt] = vec
                slot = request_slot[start + j]
                vecs_for_unique[slot] = vec
        except Exception:
            for j, txt in enumerate(chunk):
                slot = request_slot[start + j]
                vecs_for_unique[slot] = _embedding_cache.get(txt)

    text_to_vec = {u: vecs_for_unique[i] for i, u in enumerate(unique)}
    return [text_to_vec.get(s or "", None) for s in strings]


def _cosine_similarity_matrix(
    draft_vecs: List[Optional[List[float]]], corr_vecs: List[Optional[List[float]]]
) -> np.ndarray:
    """shape (n_draft, n_corr)，无向量或维度不一致处为 -1.0（低于阈值）。"""
    n_d = len(draft_vecs)
    n_c = len(corr_vecs)
    out = np.full((n_d, n_c), -1.0, dtype=np.float64)
    dim_ref = None
    for v in draft_vecs:
        if v and len(v) > 0:
            dim_ref = len(v)
            break
    if dim_ref is None:
        return out

    d_rows = []
    d_idx = []
    for i, v in enumerate(draft_vecs):
        if v and len(v) == dim_ref:
            d_rows.append(np.asarray(v, dtype=np.float64))
            d_idx.append(i)
    c_cols = []
    c_idx = []
    for j, v in enumerate(corr_vecs):
        if v and len(v) == dim_ref:
            c_cols.append(np.asarray(v, dtype=np.float64))
            c_idx.append(j)
    if not d_rows or not c_cols:
        return out
    d_mat = np.stack(d_rows, axis=0)
    c_mat = np.stack(c_cols, axis=0)
    d_norm = np.linalg.norm(d_mat, axis=1, keepdims=True)
    c_norm = np.linalg.norm(c_mat, axis=1, keepdims=True)
    d_norm = np.maximum(d_norm, 1e-12)
    c_norm = np.maximum(c_norm, 1e-12)
    sim_block = (d_mat @ c_mat.T) / (d_norm * c_norm.T)
    for bi, i in enumerate(d_idx):
        for bj, j in enumerate(c_idx):
            out[i, j] = float(sim_block[bi, bj])
    return out


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

    semantic_texts = [_build_correction_semantic_text(top_event, c) for c in corrections]
    embeddings = _embed_strings_ordered(semantic_texts)

    docs = []
    for correction, sem_txt, emb in zip(corrections, semantic_texts, embeddings):
        doc = {
            "tree_id": tree_id,
            "ai_version": ai_version,
            "expert_version": expert_version,
            "top_event": top_event,
            "node_name": correction.get("node_name") or correction.get("from_name", ""),
            "node_type": correction.get("node_type", ""),
            "correction_type": correction["type"],
            "detail": correction,
            "created_at": now,
        }
        if sem_txt:
            doc["semantic_text"] = sem_txt
        if emb:
            doc["embedding"] = emb
        docs.append(doc)

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


def _correction_dedupe_key(hit: dict) -> str:
    """同一条 Mongo corrections 文档在语义检索中只应保留一条（匹配分最高的草稿节点）。"""
    oid = hit.get("_id")
    if oid is not None:
        return str(oid)
    return "|".join(
        [
            str(hit.get("tree_id", "")),
            str(hit.get("created_at", "")),
            str(hit.get("correction_type", "")),
            str(hit.get("node_name", "")),
            str(hit.get("detail", {})),
        ]
    )


def _dedupe_corrections_keep_best_score(matches: List[dict]) -> List[dict]:
    best: Dict[str, dict] = {}
    for hit in matches:
        key = _correction_dedupe_key(hit)
        sc = float(hit.get("_match_score", hit.get("similarity", 0.0)))
        prev = best.get(key)
        if prev is None or sc > float(prev.get("_match_score", prev.get("similarity", -1.0))):
            best[key] = hit
    return list(best.values())


def _safe_created_timestamp(hit: dict, fallback: float = 0.0) -> float:
    ca = hit.get("created_at")
    if ca is None:
        return fallback
    if hasattr(ca, "timestamp"):
        try:
            return float(ca.timestamp())
        except Exception:
            return fallback
    return fallback


def _get_relevant_corrections_exact(
    draft_tree: dict, max_per_node: int, exclude_tree_id: Optional[str]
) -> list:
    """按 node_name 精确匹配（embedding 未配置时的回退方案）。"""
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

    mongo_q: dict = {"node_name": {"$in": node_names}}
    if exclude_tree_id:
        mongo_q["tree_id"] = {"$ne": exclude_tree_id}

    raw_hits = list(corrections_col.find(mongo_q).sort("created_at", -1).limit(200))
    if not raw_hits:
        return []

    newest_ts = _safe_created_timestamp(raw_hits[0])
    oldest_ts = _safe_created_timestamp(raw_hits[-1], newest_ts)
    ts_range = max(newest_ts - oldest_ts, 1.0)

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

        ts = _safe_created_timestamp(hit, oldest_ts)
        score += (ts - oldest_ts) / ts_range

        enriched = dict(hit)
        enriched["matched_node_name"] = hit_name
        scored.append((score, enriched))

    scored.sort(key=lambda x: x[0], reverse=True)

    seen_counts: dict = {}
    results = []
    for _, hit in scored:
        name = hit.get("node_name", "")
        if seen_counts.get(name, 0) < max_per_node:
            results.append(hit)
            seen_counts[name] = seen_counts.get(name, 0) + 1

    return results


def _get_relevant_corrections_semantic(
    draft_tree: dict, max_per_node: int, exclude_tree_id: Optional[str]
) -> list:
    """
    内存向量召回：草稿节点与历史修正均构造 semantic_text，余弦相似度矩阵 (numpy)，
    阈值过滤后按类型权重与时间重排；每条修正无 embedding 时在内存中现算。
    """
    draft_nodes = draft_tree.get("nodeList", []) if draft_tree else []
    if not draft_nodes:
        return []

    nodes_by_id = {n.get("id"): n for n in draft_nodes if n.get("id")}
    if not nodes_by_id:
        return []

    parent_map, child_map = _build_relation_maps(draft_tree)
    top_event = _get_top_event_name(draft_tree)

    mongo_q: dict = {}
    if exclude_tree_id:
        mongo_q["tree_id"] = {"$ne": exclude_tree_id}

    raw_hits = list(corrections_col.find(mongo_q).sort("created_at", -1).limit(5000))
    if not raw_hits:
        return []

    newest_ts = _safe_created_timestamp(raw_hits[0])
    oldest_ts = _safe_created_timestamp(raw_hits[-1], newest_ts)
    ts_range = max(newest_ts - oldest_ts, 1.0)
    high_value = {"gate_changed", "node_deleted"}

    draft_rows: List[Tuple[dict, str, str, str]] = []
    for node in draft_nodes:
        draft_name = (node.get("name") or "").strip()
        if not draft_name:
            continue
        nid = node.get("id")
        parent_names = [
            nodes_by_id[pid].get("name", "")
            for pid in parent_map.get(nid, [])
            if pid in nodes_by_id
        ]
        child_names = [
            nodes_by_id[cid].get("name", "")
            for cid in child_map.get(nid, [])
            if cid in nodes_by_id
        ]
        draft_text = _build_node_semantic_text(top_event, node, parent_names, child_names)
        draft_type = node.get("type", "") or ""
        draft_rows.append((node, draft_name, draft_type, draft_text))

    if not draft_rows:
        return []

    draft_texts = [row[3] for row in draft_rows]
    draft_vecs = _embed_strings_ordered(draft_texts)
    if not any(draft_vecs):
        return _get_relevant_corrections_exact(draft_tree, max_per_node, exclude_tree_id)

    corr_texts: List[str] = []
    corr_vecs: List[Optional[List[float]]] = []
    draft_dim = next((len(v) for v in draft_vecs if v and len(v) > 0), None)

    for hit in raw_hits:
        detail = hit.get("detail", {})
        if not isinstance(detail, dict):
            detail = {}
        t = hit.get("semantic_text") or _build_correction_semantic_text(
            hit.get("top_event", ""), detail
        )
        corr_texts.append(t)
        ev = hit.get("embedding")
        use_ev = ev if isinstance(ev, list) and len(ev) > 0 else None
        if use_ev and draft_dim is not None and len(use_ev) != draft_dim:
            use_ev = None
        corr_vecs.append(use_ev)

    missing_idx = [i for i, v in enumerate(corr_vecs) if v is None and corr_texts[i]]
    if missing_idx:
        to_embed = [corr_texts[i] for i in missing_idx]
        new_vecs = _embed_strings_ordered(to_embed)
        for k, idx in enumerate(missing_idx):
            corr_vecs[idx] = new_vecs[k]

    sim = _cosine_similarity_matrix(draft_vecs, corr_vecs)
    n_d, n_c = sim.shape

    all_matches: List[dict] = []
    for i in range(n_d):
        _, draft_name, draft_type, _ = draft_rows[i]
        row_scores: List[Tuple[float, dict]] = []
        for j in range(n_c):
            s = float(sim[i, j])
            hit = raw_hits[j]
            hit_node = (hit.get("node_name") or "").strip()
            exact = draft_name == hit_node
            if s < SEMANTIC_SIMILARITY_THRESHOLD and not exact:
                continue
            eff_sim = s if s >= 0.0 else (1.0 if exact else 0.0)
            score = eff_sim * 10.0
            if exact:
                score += 3.0
            if draft_type and draft_type == hit.get("node_type", ""):
                score += 2.0
            if hit.get("correction_type") in high_value:
                score += 1.0
            ts = _safe_created_timestamp(hit, oldest_ts)
            score += (ts - oldest_ts) / ts_range

            enriched = dict(hit)
            enriched["matched_node_name"] = draft_name
            enriched["matched_node_type"] = draft_type
            enriched["similarity"] = round(max(s, 0.0), 4)
            enriched["_match_score"] = score
            row_scores.append((score, enriched))

        row_scores.sort(key=lambda x: x[0], reverse=True)
        for _, enriched in row_scores[:max_per_node]:
            all_matches.append(enriched)

    # 同一 Mongo 文档常被多个草稿节点同时命中，此前按 (matched_node_name,…) 去重会重复计数。
    deduped = _dedupe_corrections_keep_best_score(all_matches)
    deduped.sort(
        key=lambda h: (
            float(h.get("_match_score", h.get("similarity", 0.0))),
            _safe_created_timestamp(h),
        ),
        reverse=True,
    )
    for h in deduped:
        h.pop("_match_score", None)
    return deduped


def get_relevant_corrections(
    draft_tree: dict,
    max_per_node: int = 3,
    exclude_tree_id: Optional[str] = None,
    max_distinct: int = 50,
) -> list:
    """
    检索与当前草稿相关的历史修正。

    - 若配置 EMBEDDING_MODEL 且可用 API：语义召回（内存 numpy 余弦相似度 + 阈值 + 类型/时间重排）。
    - 否则：node_name 精确匹配。

    max_per_node：每个草稿节点在合并前最多保留几条候选（与草稿节点数相乘曾为「条数膨胀」来源）；
    合并后按 Mongo 文档 _id 去重，每条修正记录至多出现一次（保留匹配分最高的草稿节点）。
    max_distinct：去重后最多返回多少条（防止提示过长）。

    exclude_tree_id：排除指定 tree（例如当前正在编辑的树），避免自引用；
    未传时尝试使用 draft_tree[\"tree_id\"] / draft_tree[\"_tree_id\"]。
    """
    excl = exclude_tree_id
    if not excl and isinstance(draft_tree, dict):
        excl = draft_tree.get("tree_id") or draft_tree.get("_tree_id")

    if EMBEDDING_MODEL and _embedding_openai():
        out = _get_relevant_corrections_semantic(draft_tree, max_per_node, excl)
    else:
        out = _get_relevant_corrections_exact(draft_tree, max_per_node, excl)
    if max_distinct > 0 and len(out) > max_distinct:
        return out[:max_distinct]
    return out


def _sanitize_correction_for_debug(hit: Dict[str, Any]) -> Dict[str, Any]:
    """避免日志刷屏：省略 embedding 向量正文，保留维度说明。"""
    out = {}
    for k, v in hit.items():
        if k == "embedding" and isinstance(v, list):
            out[k] = f"<已省略 embedding 向量，维度={len(v)}>"
        else:
            out[k] = v
    return out


def log_corrections_debug(corrections: list) -> None:
    """
    将本次参与定向修复的 corrections 以 JSON 形式打印到后端日志（控制台）。
    用于核对检索条件、相似度与 detail 是否与预期一致。
    """
    if not corrections:
        return
    n = len(corrections)
    print(f"[修复][调试] ========== 检索到的 corrections 全文（共 {n} 条，已去重）==========")
    for idx, hit in enumerate(corrections, 1):
        safe = _sanitize_correction_for_debug(hit)
        try:
            body = json.dumps(safe, ensure_ascii=False, indent=2, default=str)
        except TypeError:
            body = repr(safe)
        print(f"[修复][调试] ----- 第 {idx}/{n} 条 -----")
        print(body)
    print("[修复][调试] ========== corrections 调试输出结束 ==========")


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
        "以下建议来自历史修正记录（含语义相似召回或名称匹配）。",
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
