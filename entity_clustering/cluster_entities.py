from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore


CURRENT_DIR = Path(__file__).resolve().parent


ENTITY_TYPE_CODE_MAP = {
    "故障事件": "FaultEvent",
    "故障类别": "FaultCategory",
    "报警码": "AlarmCode",
    "维修方法": "MaintenanceAction",
    "触发规则": "TriggerRule",
}


def entity_type_code(entity_type: str) -> str:
    return ENTITY_TYPE_CODE_MAP.get(clean_scalar(entity_type), clean_scalar(entity_type))


WEAK_SUFFIXES = [
    "故障",
    "异常",
    "告警",
    "报警",
    "问题",
    "状态",
    "现象",
    "风险",
    "危险",
]

STOP_WORDS = [
    "可能",
    "实际",
    "当前",
    "本地",
    "对应",
    "严重",
    "轻微",
    "中度",
    "发生",
    "出现",
    "导致",
    "引起",
]

SYNONYM_REPLACEMENTS = [
    ("报警", "告警"),
    ("故障码", "告警码"),
    ("错误码", "告警码"),
    ("通讯", "通信"),
    ("联接", "连接"),
    ("连线", "线缆"),
    ("电缆", "线缆"),
    ("线缆", "线缆"),
    ("高于", "超过"),
    ("大于", "超过"),
    ("低于", "小于"),
    ("摄氏度", "℃"),
    ("度", "℃"),
    ("伏特", "v"),
]

DIRECTION_CONFLICT_GROUPS = [
    (
        {"过高", "偏高", "高于", "大于", "超过", "升高", "上升", "过压", "过频", "超频"},
        {"过低", "偏低", "低于", "小于", "不足", "降低", "下降", "欠压", "欠频"},
    ),
    (
        {"闭合", "接通", "导通", "开启", "打开", "启动", "运行", "投入"},
        {"断开", "关断", "关闭", "停止", "停机", "退出"},
    ),
    (
        {"正常", "合格", "有效", "成功"},
        {"异常", "不合格", "无效", "失败"},
    ),
    (
        {"交流", "ac"},
        {"直流", "dc"},
    ),
    (
        {"输入", "进线", "输入侧"},
        {"输出", "出线", "输出侧"},
    ),
    (
        {"充电", "充电侧"},
        {"放电", "放电侧"},
    ),
    (
        {"正极", "正端", "正母线", "正母排"},
        {"负极", "负端", "负母线", "负母排"},
    ),
    (
        {"电池侧"},
        {"母线侧"},
    ),
]

STAGE_CONFLICT_GROUPS = [
    (
        "future_expiry",
        ["即将过期", "将过期", "快过期", "即将到期", "将到期", "快到期", "临近过期", "临近到期", "接近过期", "接近到期"],
        "expired",
        ["已过期", "已经过期", "已到期", "已经到期", "已失效", "已经失效", "过期", "到期", "失效"],
    ),
]

TYPE_SEMANTIC_BOOST_THRESHOLDS = {
    "维修方法": {"min_name": 0.75, "min_embedding": 0.96},
}

REFINE_WEAK_EVENT_TERMS = [
    "危险",
    "风险",
    "的可能",
    "可能",
    "产生",
    "发生",
    "出现",
    "致人",
    "导致",
    "致",
    "加载的",
]

REFINE_REPAIR_ACTION_SYNONYMS = [
    ("拆卸", "取下", "拔下", "移除"),
    ("联系", "通知"),
    ("置于", "设置为", "调整为"),
    ("检查", "确认", "排查"),
]

REFINE_FAULT_STATE_SYNONYMS = [
    ("不匹配", "不正确", "不一致", "不兼容"),
    ("过大", "急剧增大", "增大", "过高"),
]

OBJECT_SENSITIVE_REPAIR_ACTIONS = {
    "更换",
    "替换",
    "检查",
    "排查",
    "确认",
    "拆卸",
    "取下",
    "拔下",
    "移除",
    "清洗",
    "清理",
    "紧固",
    "安装",
    "测量",
    "测试",
    "维护",
    "维修",
}

QUALIFIER_CANONICAL = {
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
    "a": "A",
    "b": "B",
    "c": "C",
}

QUALIFIER_GROUPS = [
    ("side", (("left", ["左侧", "左边", "左"]), ("right", ["右侧", "右边", "右"]))),
    ("position", (("front", ["前侧", "前端", "前"]), ("rear", ["后侧", "后端", "后"]))),
    ("upper_lower", (("upper", ["上侧", "上部", "上"]), ("lower", ["下侧", "下部", "下"]))),
    ("north_south", (("north", ["北向", "北侧", "北面"]), ("south", ["南向", "南侧", "南面"]))),
    ("east_west", (("east", ["东向", "东侧", "东面"]), ("west", ["西向", "西侧", "西面"]))),
    ("uplink_downlink", (("uplink", ["上行"]), ("downlink", ["下行"]))),
    ("inbound_outbound", (("inbound", ["入站", "入方向"]), ("outbound", ["出站", "出方向"]))),
    ("internal_external", (("internal", ["内部", "内侧", "内置"]), ("external", ["外部", "外侧", "外置"]))),
    ("phase", (("A", ["A相", "a相"]), ("B", ["B相", "b相"]), ("C", ["C相", "c相"]))),
    ("current_type", (("AC", ["交流", "AC", "ac"]), ("DC", ["直流", "DC", "dc"]))),
    ("io", (("input", ["输入侧", "输入", "进线"]), ("output", ["输出侧", "输出", "出线"]))),
    ("polarity", (("positive", ["正极", "正端", "正母线", "正母排"]), ("negative", ["负极", "负端", "负母线", "负母排"]))),
]


@dataclass
class Mention:
    mention_id: str
    local_entity_id: str
    file_id: str
    sample_id: str
    chapter_id: str
    source_type: str
    entity_type: str
    mention: str
    normalized_name: str
    evidence: list[dict[str, Any]]
    chunk_ids: list[str]
    neighbor_tokens: set[str] = field(default_factory=set)
    embedding: list[float] = field(default_factory=list)


@dataclass
class RawRelation:
    relation_id: str
    file_id: str
    sample_id: str
    source_mention_id: str
    target_mention_id: str
    source_text: str
    target_text: str
    relation_type: str
    cross_chunk: str
    involved_chunk_ids: str
    evidence: list[dict[str, Any]]
    polarity: str
    certainty: str


@dataclass
class Cluster:
    cluster_id: str
    file_id: str
    entity_type: str
    canonical_name: str
    mention_ids: list[str] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    neighbor_tokens: set[str] = field(default_factory=set)
    merge_reasons: set[str] = field(default_factory=set)
    embedding: list[float] = field(default_factory=list)


def parse_json_cell(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", clean_scalar(value)).lower()
    text = text.replace(" ", "")
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:°c|℃|摄氏度|度)", r"\1℃", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:伏特|volt|volts)", r"\1v", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:安培|amp|amps)", r"\1a", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:赫兹)", r"\1hz", text, flags=re.IGNORECASE)
    for old, new in SYNONYM_REPLACEMENTS:
        text = text.replace(old, new)
    for word in STOP_WORDS:
        if len(text) > len(word) + 2:
            text = text.replace(word, "")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。；：、,.。;:()（）【】\\[\\]<>《》\"'“”‘’`~!！?？]", "", text)
    return text


def weak_normalize_name(value: str) -> str:
    text = normalize_name(value)
    changed = True
    while changed:
        changed = False
        for suffix in WEAK_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)]
                changed = True
    return text


def extract_alarm_code(value: str) -> str:
    text = unicodedata.normalize("NFKC", clean_scalar(value)).upper()
    text = re.sub(r"\s+", "", text).replace("_", "-")
    return text


def extract_rule_signature(value: str) -> str:
    text = normalize_name(value)
    identifiers = re.findall(r"[a-z]+(?:\.[a-z]+)+\.?\d+(?:-\d+)?", text, flags=re.IGNORECASE)
    protected_text = text
    for identifier in identifiers:
        protected_text = protected_text.replace(identifier.lower(), " ")
    numbers = re.findall(r"\d+(?:\.\d+)?", protected_text)
    units = re.findall(r"(?:℃|°c|v|a|hz|%|秒|分钟|min|ms|kwh|kw)", text, flags=re.IGNORECASE)
    comparators = []
    for token in ["超过", "大于", "高于", "低于", "小于", "不少于", "不高于", "持续"]:
        if token in value:
            comparators.append(token)
    return "|".join(comparators + [item.lower() for item in identifiers] + numbers + [unit.lower() for unit in units])


RULE_COMPARATOR_SYNONYMS = [
    ("低于", "小于", "不足", "不满足"),
    ("高于", "大于", "超过", "超出"),
    ("不低于", "不少于", "至少"),
    ("不高于", "不超过", "至多"),
    ("达到", "满足"),
    ("持续", "保持"),
]


def extract_rule_constraint_tokens(value: str) -> set[str]:
    raw = unicodedata.normalize("NFKC", clean_scalar(value))
    normalized = normalize_name(raw)
    tokens: set[str] = set()
    for canonical, *variants in RULE_COMPARATOR_SYNONYMS:
        if any(term in raw or term in normalized for term in (canonical, *variants)):
            tokens.add(f"cmp:{canonical}")

    for identifier in re.findall(r"[A-Za-z]+(?:\.[A-Za-z]+)+\.?\d+(?:-\d+)?", raw):
        tokens.add(f"id:{identifier.upper()}")
    for identifier in re.findall(r"[a-z]+(?:\.[a-z]+)+\.?\d+(?:-\d+)?", normalized, flags=re.IGNORECASE):
        tokens.add(f"id:{identifier.upper()}")

    measurement_pattern = re.compile(
        r"\d+(?:\.\d+)?\s*(?:bar|mpa|pa|kpa|℃|°c|v|kv|a|ma|hz|%|秒|s|分钟|min|ms|kwh|kw|rpm)",
        flags=re.IGNORECASE,
    )
    for measurement in measurement_pattern.findall(raw):
        tokens.add("value:" + re.sub(r"\s+", "", measurement).upper())
    return tokens


def tokenized_rule_length(value: str) -> int:
    text = normalize_name(value)
    protected = text
    constraint_tokens = extract_rule_constraint_tokens(value)
    for token in sorted(constraint_tokens, key=len, reverse=True):
        _, _, body = token.partition(":")
        if body:
            protected = protected.replace(body.lower(), " X ")
    protected = re.sub(r"\s+", "", protected)
    return len(protected)


def remove_rule_constraints(value: str) -> str:
    text = normalize_name(value)
    for token in sorted(extract_rule_constraint_tokens(value), key=len, reverse=True):
        _, _, body = token.partition(":")
        if body:
            text = text.replace(body.lower(), "")
    for canonical, *variants in RULE_COMPARATOR_SYNONYMS:
        for term in (canonical, *variants):
            text = text.replace(normalize_name(term), "")
    return text


def rule_information_gain_score(name: str, all_names: set[str]) -> float:
    name_tokens = extract_rule_constraint_tokens(name)
    if not name_tokens:
        return 0.0
    name_extra = remove_rule_constraints(name)
    best_gain = 0.0
    for other in all_names:
        other = clean_scalar(other)
        if not other or other == name:
            continue
        other_tokens = extract_rule_constraint_tokens(other)
        if not other_tokens or not other_tokens <= name_tokens:
            continue
        other_extra = remove_rule_constraints(other)
        if normalize_name(other) in normalize_name(name) and len(normalize_name(name)) > len(normalize_name(other)):
            gain_length = len(name_extra)
        else:
            gain_length = max(0, len(name_extra) - len(other_extra))
        best_gain = max(best_gain, min(1.0, gain_length / 8))
    return best_gain


def trigger_rule_canonical_quality(name: str, all_names: set[str]) -> float:
    normalized = normalize_name(name)
    if not normalized:
        return 0.0
    tokens = extract_rule_constraint_tokens(name)
    comparator_score = 1.0 if any(token.startswith("cmp:") for token in tokens) else 0.0
    threshold_score = 1.0 if any(token.startswith(("id:", "value:")) for token in tokens) else 0.0
    extra_text = remove_rule_constraints(name)
    information_gain = rule_information_gain_score(name, all_names)
    object_like_score = min(1.0, len(extra_text) / 8) if extra_text else 0.0
    effective_length = tokenized_rule_length(name)
    length_score = max(0.0, 1.0 - abs(effective_length - 14) / 14)
    peers = [other for other in all_names if other and other != name]
    centrality = (
        sum(normalized_text_similarity(name, other) for other in peers) / len(peers)
        if peers
        else 1.0
    )
    return (
        0.30 * information_gain
        + 0.25 * object_like_score
        + 0.20 * threshold_score
        + 0.15 * comparator_score
        + 0.05 * length_score
        + 0.05 * centrality
    )


def repair_action_signature(value: str) -> set[str]:
    normalized = normalize_name(value)
    signature: set[str] = set()
    for group in REFINE_REPAIR_ACTION_SYNONYMS:
        canonical = group[0]
        if any(action in normalized for action in group):
            signature.add(canonical)
    for action in OBJECT_SENSITIVE_REPAIR_ACTIONS:
        if action in normalized:
            signature.add(action)
    return signature


def repair_object_text(value: str) -> str:
    text = apply_synonym_groups(normalize_name(value), REFINE_REPAIR_ACTION_SYNONYMS)
    weak_terms = ["进行", "执行", "处理", "状态", "参数", "的", "对", "将", "把", "位置"]
    for action in sorted(OBJECT_SENSITIVE_REPAIR_ACTIONS, key=len, reverse=True):
        text = text.replace(action, "")
    for weak in weak_terms:
        text = text.replace(weak, "")
    return text


def repair_information_gain_score(name: str, all_names: set[str]) -> float:
    name_actions = repair_action_signature(name)
    name_object = repair_object_text(name)
    if not name_object:
        return 0.0
    best_gain = 0.0
    for other in all_names:
        other = clean_scalar(other)
        if not other or other == name:
            continue
        other_actions = repair_action_signature(other)
        if name_actions and other_actions and not (name_actions & other_actions):
            continue
        other_object = repair_object_text(other)
        if not other_object:
            continue
        name_norm = normalize_name(name)
        other_norm = normalize_name(other)
        if other_object in name_object and len(name_object) >= len(other_object) + 2:
            gain_length = len(name_object) - len(other_object)
        elif other_norm in name_norm and len(name_norm) > len(other_norm):
            gain_length = len(name_object)
        else:
            gain_length = 0
        best_gain = max(best_gain, min(1.0, gain_length / 8))
    return best_gain


def repair_method_canonical_quality(name: str, all_names: set[str]) -> float:
    normalized = normalize_name(name)
    if not normalized:
        return 0.0
    actions = repair_action_signature(name)
    action_score = min(1.0, 0.45 + 0.30 * len(actions) + (0.15 if ("或" in name or "/" in name) else 0.0))
    object_text = repair_object_text(name)
    object_score = min(1.0, len(object_text) / 10) if object_text else 0.0
    information_gain = repair_information_gain_score(name, all_names)
    length = len(normalized)
    length_score = max(0.0, 1.0 - abs(length - 18) / 18)
    peers = [other for other in all_names if other and other != name]
    centrality = (
        sum(normalized_text_similarity(name, other) for other in peers) / len(peers)
        if peers
        else 1.0
    )
    return (
        0.30 * information_gain
        + 0.30 * object_score
        + 0.20 * action_score
        + 0.10 * centrality
        + 0.10 * length_score
    )


def direction_signature(value: str) -> set[str]:
    raw = unicodedata.normalize("NFKC", clean_scalar(value)).lower()
    normalized = normalize_name(value)
    haystack = raw + "|" + normalized
    signatures: set[str] = set()
    for index, (positive_terms, negative_terms) in enumerate(DIRECTION_CONFLICT_GROUPS):
        if any(term in haystack for term in positive_terms):
            signatures.add(f"{index}:positive")
        if any(term in haystack for term in negative_terms):
            signatures.add(f"{index}:negative")
    return signatures


def object_qualifier_signature(value: str) -> dict[str, set[str]]:
    raw = unicodedata.normalize("NFKC", clean_scalar(value))
    normalized = normalize_name(value)
    haystack = raw + "|" + normalized
    signature: dict[str, set[str]] = defaultdict(set)

    for match in re.finditer(r"(?<![A-Za-z0-9])(?:#|no\.?|编号|序号)?\s*([0-9]{1,3})\s*(?:号|#)?", raw, flags=re.IGNORECASE):
        signature["index"].add(match.group(1))
    for match in re.finditer(r"(?:-|_|#|no\.?|编号|序号)([0-9]{1,3})(?![0-9])", raw, flags=re.IGNORECASE):
        signature["index"].add(match.group(1))
    for match in re.finditer(r"第\s*([0-9一二三四五六七八九十]{1,3})\s*(?:个|路|组|台|级|号)", raw, flags=re.IGNORECASE):
        signature["index"].add(QUALIFIER_CANONICAL.get(match.group(1).lower(), match.group(1)))

    for group_name, variants in QUALIFIER_GROUPS:
        for canonical, terms in variants:
            if any(term in haystack for term in terms):
                signature[group_name].add(canonical)
    return signature


def has_object_qualifier_conflict(left: str, right: str) -> bool:
    left_signature = object_qualifier_signature(left)
    right_signature = object_qualifier_signature(right)
    for group_name in set(left_signature) | set(right_signature):
        left_values = left_signature.get(group_name, set())
        right_values = right_signature.get(group_name, set())
        if len(left_values) > 1 or len(right_values) > 1:
            continue
        if left_values and right_values and left_values != right_values:
            return True
        if group_name == "index" and bool(left_values) != bool(right_values):
            return True
    return False


def has_direction_conflict(left: str, right: str) -> bool:
    if has_stage_conflict(left, right):
        return True
    left_signature = direction_signature(left)
    right_signature = direction_signature(right)
    for index in range(len(DIRECTION_CONFLICT_GROUPS)):
        left_positive = f"{index}:positive" in left_signature
        left_negative = f"{index}:negative" in left_signature
        right_positive = f"{index}:positive" in right_signature
        right_negative = f"{index}:negative" in right_signature
        left_ambiguous = left_positive and left_negative
        right_ambiguous = right_positive and right_negative
        if left_ambiguous or right_ambiguous:
            continue
        if left_positive and right_negative:
            return True
        if left_negative and right_positive:
            return True
    return False


def has_qualifier_marker(value: str) -> bool:
    return bool(object_qualifier_signature(value) or direction_signature(value) or stage_signature(value))


def minimal_difference_parts(left: str, right: str) -> tuple[str, str, int]:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    left_parts: list[str] = []
    right_parts: list[str] = []
    common_length = 0
    matcher = SequenceMatcher(None, left_norm, right_norm)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag == "equal":
            common_length += left_end - left_start
            continue
        left_parts.append(left_norm[left_start:left_end])
        right_parts.append(right_norm[right_start:right_end])
    return "".join(left_parts), "".join(right_parts), common_length


def diff_parts_are_repair_action_synonyms(left_diff: str, right_diff: str) -> bool:
    if not left_diff or not right_diff:
        return False
    for group in REFINE_REPAIR_ACTION_SYNONYMS:
        left_hit = any(action in left_diff for action in group)
        right_hit = any(action in right_diff for action in group)
        if left_hit and right_hit:
            return True
    return False


def important_name_difference_conflict(left: str, right: str, entity_type: str) -> bool:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm or left_norm == right_norm:
        return False
    if normalized_text_similarity(left_norm, right_norm) < 0.70:
        return False
    left_diff, right_diff, common_length = minimal_difference_parts(left_norm, right_norm)
    if common_length < 4:
        return False
    if not left_diff or not right_diff:
        return False
    if left_diff == right_diff:
        return False
    if entity_type == "维修方法" and diff_parts_are_repair_action_synonyms(left_diff, right_diff):
        return False
    if left_diff in LOW_INFORMATION_COMMON_TERMS or right_diff in LOW_INFORMATION_COMMON_TERMS:
        return False
    return True


def stage_signature(value: str) -> set[str]:
    raw = unicodedata.normalize("NFKC", clean_scalar(value))
    normalized = normalize_name(value)
    haystack = raw + "|" + normalized
    signatures: set[str] = set()
    for group_index, (left_label, left_terms, right_label, right_terms) in enumerate(STAGE_CONFLICT_GROUPS):
        if any(term in haystack for term in left_terms):
            signatures.add(f"{group_index}:{left_label}")
        elif any(term in haystack for term in right_terms):
            signatures.add(f"{group_index}:{right_label}")
    return signatures


def has_stage_conflict(left: str, right: str) -> bool:
    left_signature = stage_signature(left)
    right_signature = stage_signature(right)
    for group_index, (_left_label, _left_terms, _right_label, _right_terms) in enumerate(STAGE_CONFLICT_GROUPS):
        left_values = {item.split(":", 1)[1] for item in left_signature if item.startswith(f"{group_index}:")}
        right_values = {item.split(":", 1)[1] for item in right_signature if item.startswith(f"{group_index}:")}
        if len(left_values) == 1 and len(right_values) == 1 and left_values != right_values:
            return True
    return False


def dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            clean_scalar(item.get("chunk_id")),
            clean_scalar(item.get("text_field")),
            clean_scalar(item.get("start")),
            clean_scalar(item.get("end")),
            clean_scalar(item.get("text")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def evidence_chunk_ids(evidence: list[dict[str, Any]]) -> list[str]:
    ids = []
    for item in evidence:
        chunk_id = clean_scalar(item.get("chunk_id"))
        if chunk_id and chunk_id not in ids:
            ids.append(chunk_id)
    return ids


def join_list(values: list[str] | set[str]) -> str:
    return ";".join(sorted(clean_scalar(value) for value in values if clean_scalar(value)))


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def split_involved(value: Any) -> str:
    if isinstance(value, list):
        return join_list([clean_scalar(item) for item in value])
    return clean_scalar(value)


def evidence_text(evidence: list[dict[str, Any]], max_items: int = 3) -> str:
    texts = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        text = clean_scalar(item.get("text"))
        if text:
            texts.append(text)
        if len(texts) >= max_items:
            break
    return " ".join(texts)


def mention_embedding_text(mention: Mention) -> str:
    return " ".join(
        part
        for part in [
            mention.entity_type,
            mention.normalized_name,
            mention.mention,
            evidence_text(mention.evidence),
        ]
        if clean_scalar(part)
    )


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def hash_embedding(text: str, dim: int = 256) -> list[float]:
    normalized = normalize_name(text)
    vector = [0.0] * dim
    if not normalized:
        return vector
    tokens = set()
    for size in (2, 3, 4):
        if len(normalized) < size:
            continue
        for index in range(len(normalized) - size + 1):
            tokens.add(normalized[index : index + size])
    if not tokens:
        tokens.add(normalized)
    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    return l2_normalize(vector)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    score = sum(a * b for a, b in zip(left, right))
    return max(0.0, min(1.0, score))


def build_openai_compatible_embeddings(
    texts: list[str],
    *,
    api_key: str,
    base_url: str,
    model_name: str,
    batch_size: int = 10,
) -> list[list[float]]:
    if OpenAI is None:
        raise RuntimeError("openai is not installed. Install openai or use --embedding-backend hash.")
    if not api_key:
        raise RuntimeError("--embedding-api-key or EMBEDDING_API_KEY is required for openai-compatible embedding.")
    if not model_name:
        raise RuntimeError("--embedding-model or EMBEDDING_MODEL is required for openai-compatible embedding.")
    client = OpenAI(api_key=api_key, base_url=base_url)
    vectors: list[list[float]] = [[] for _ in texts]
    unique_texts: list[str] = []
    text_slots: dict[str, list[int]] = defaultdict(list)
    for index, text in enumerate(texts):
        key = text or ""
        if key not in text_slots:
            unique_texts.append(key)
        text_slots[key].append(index)
    safe_batch_size = max(1, int(batch_size or 10))
    for start in range(0, len(unique_texts), safe_batch_size):
        batch = unique_texts[start : start + safe_batch_size]
        response = client.embeddings.create(model=model_name, input=batch)
        for text, item in zip(batch, response.data):
            vector = [float(value) for value in item.embedding]
            for slot in text_slots.get(text, []):
                vectors[slot] = vector
    return vectors


def build_embeddings(
    mentions: list[Mention],
    backend: str,
    model_name: str = "",
    dim: int = 256,
    api_key: str = "",
    base_url: str = "",
    batch_size: int = 10,
) -> None:
    if backend == "none":
        return
    texts = [mention_embedding_text(mention) for mention in mentions]
    if backend == "hash":
        for mention, text in zip(mentions, texts):
            mention.embedding = hash_embedding(text, dim)
        return
    if backend == "sentence-transformers":
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise RuntimeError("sentence-transformers is not installed. Use --embedding-backend hash or install sentence-transformers.") from exc
        if not model_name:
            raise RuntimeError("--embedding-model is required when --embedding-backend sentence-transformers.")
        model = SentenceTransformer(model_name)
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
        for mention, vector in zip(mentions, vectors):
            mention.embedding = [float(value) for value in vector]
        return
    if backend == "openai-compatible":
        vectors = build_openai_compatible_embeddings(
            texts,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            batch_size=batch_size,
        )
        for mention, vector in zip(mentions, vectors):
            mention.embedding = vector
        return
    raise ValueError(f"Unsupported embedding backend: {backend}")


def load_existing_embeddings(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    embeddings: dict[str, list[float]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        mention_id = clean_scalar(item.get("mention_id"))
        vector = item.get("embedding")
        if mention_id and isinstance(vector, list):
            embeddings[mention_id] = [float(value) for value in vector]
    return embeddings


def apply_existing_embeddings(mentions: list[Mention], path: Path) -> int:
    embeddings = load_existing_embeddings(path)
    applied = 0
    for mention in mentions:
        vector = embeddings.get(mention.mention_id)
        if vector:
            mention.embedding = vector
            applied += 1
    return applied


def load_mentions_and_relations(input_csv: Path, file_id_filter: str) -> tuple[list[Mention], list[RawRelation]]:
    mentions: list[Mention] = []
    relations: list[RawRelation] = []
    entity_lookup: dict[tuple[str, str], str] = {}
    name_lookup: dict[tuple[str, str, str], list[str]] = defaultdict(list)

    with input_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            file_id = clean_scalar(row.get("file_id"))
            if file_id_filter and file_id != file_id_filter:
                continue
            sample_id = clean_scalar(row.get("sample_id"))
            entities = parse_json_cell(row.get("entities_json"))
            if not entities:
                entities = parse_json_cell(row.get("context_entities_json")) + parse_json_cell(row.get("target_entities_json"))

            for entity in entities:
                local_id = clean_scalar(entity.get("id")) or f"E{len(mentions) + 1}"
                evidence = dedupe_evidence(parse_json_cell(json_dumps(entity.get("evidence", []))))
                mention_text = clean_scalar(entity.get("mention"))
                normalized_name = clean_scalar(entity.get("normalized_name")) or mention_text
                entity_type = clean_scalar(entity.get("type"))
                mention_id = f"{file_id}#{sample_id}#{local_id}"
                mention = Mention(
                    mention_id=mention_id,
                    local_entity_id=local_id,
                    file_id=file_id,
                    sample_id=sample_id,
                    chapter_id=clean_scalar(row.get("chapter_id")),
                    source_type=clean_scalar(row.get("source_type")),
                    entity_type=entity_type,
                    mention=mention_text,
                    normalized_name=normalized_name,
                    evidence=evidence,
                    chunk_ids=evidence_chunk_ids(evidence),
                )
                mentions.append(mention)
                entity_lookup[(sample_id, local_id)] = mention_id
                for key_name in {mention_text, normalized_name}:
                    key = (sample_id, entity_type, normalize_name(key_name))
                    name_lookup[key].append(mention_id)

            raw_relations = parse_json_cell(row.get("relations_json"))
            for relation in raw_relations:
                source_raw = clean_scalar(relation.get("source"))
                target_raw = clean_scalar(relation.get("target"))
                relation_type = clean_scalar(relation.get("relation_type"))
                source_mid = resolve_relation_endpoint(sample_id, source_raw, entity_lookup, name_lookup, mentions)
                target_mid = resolve_relation_endpoint(sample_id, target_raw, entity_lookup, name_lookup, mentions)
                if not source_mid or not target_mid:
                    continue
                rel = RawRelation(
                    relation_id=f"{file_id}#{sample_id}#R{len(relations) + 1}",
                    file_id=file_id,
                    sample_id=sample_id,
                    source_mention_id=source_mid,
                    target_mention_id=target_mid,
                    source_text=source_raw,
                    target_text=target_raw,
                    relation_type=relation_type,
                    cross_chunk=str(relation.get("cross_chunk", False)).lower(),
                    involved_chunk_ids=split_involved(relation.get("involved_chunk_ids")),
                    evidence=dedupe_evidence(parse_json_cell(json_dumps(relation.get("evidence", [])))),
                    polarity=clean_scalar(relation.get("polarity")) or "positive",
                    certainty=clean_scalar(relation.get("certainty")) or "certain",
                )
                relations.append(rel)

    mention_by_id = {mention.mention_id: mention for mention in mentions}
    for rel in relations:
        source = mention_by_id.get(rel.source_mention_id)
        target = mention_by_id.get(rel.target_mention_id)
        if not source or not target:
            continue
        source.neighbor_tokens.add(f"out:{rel.relation_type}:{target.entity_type}:{normalize_name(target.normalized_name)}")
        target.neighbor_tokens.add(f"in:{rel.relation_type}:{source.entity_type}:{normalize_name(source.normalized_name)}")

    add_context_neighbor_tokens(mentions)

    return mentions, relations


def resolve_relation_endpoint(
    sample_id: str,
    raw: str,
    entity_lookup: dict[tuple[str, str], str],
    name_lookup: dict[tuple[str, str, str], list[str]],
    mentions: list[Mention],
) -> str:
    if (sample_id, raw) in entity_lookup:
        return entity_lookup[(sample_id, raw)]
    normalized = normalize_name(raw)
    candidates = []
    for mention in mentions:
        if mention.sample_id != sample_id:
            continue
        if normalize_name(mention.normalized_name) == normalized or normalize_name(mention.mention) == normalized:
            candidates.append(mention.mention_id)
    return candidates[0] if len(candidates) == 1 else ""


def hard_signature(mention: Mention) -> str:
    if mention.entity_type == "报警码":
        return extract_alarm_code(mention.normalized_name or mention.mention)
    if mention.entity_type == "触发规则":
        return extract_rule_signature(mention.normalized_name or mention.mention)
    return ""


def hard_conflict(left: Mention, cluster: Cluster) -> bool:
    if left.entity_type != cluster.entity_type:
        return True
    if has_direction_conflict(left.normalized_name or left.mention, cluster.canonical_name):
        return True
    if has_object_qualifier_conflict(left.normalized_name or left.mention, cluster.canonical_name):
        return True
    if left.entity_type == "报警码":
        left_code = extract_alarm_code(left.normalized_name or left.mention)
        cluster_code = extract_alarm_code(cluster.canonical_name)
        return bool(left_code and cluster_code and left_code != cluster_code)
    if left.entity_type == "触发规则":
        left_sig = extract_rule_signature(left.normalized_name or left.mention)
        cluster_sig = extract_rule_signature(cluster.canonical_name)
        return bool(left_sig and cluster_sig and left_sig != cluster_sig)
    return False


def mention_conflict_texts(mention: Mention) -> list[str]:
    texts = [mention.normalized_name, mention.mention]
    for item in mention.evidence:
        if isinstance(item, dict):
            texts.append(clean_scalar(item.get("text")))
    return [text for text in dict.fromkeys(clean_scalar(item) for item in texts) if text]


def mention_name_conflict_texts(mention: Mention) -> list[str]:
    return [text for text in dict.fromkeys(clean_scalar(item) for item in [mention.normalized_name, mention.mention]) if text]


def cluster_conflict_texts(cluster: Cluster) -> list[str]:
    texts = [cluster.canonical_name, *sorted(cluster.aliases)]
    for item in cluster.evidence:
        if isinstance(item, dict):
            texts.append(clean_scalar(item.get("text")))
    return [text for text in dict.fromkeys(clean_scalar(item) for item in texts) if text]


def cluster_name_conflict_texts(cluster: Cluster) -> list[str]:
    texts = [cluster.canonical_name]
    return [text for text in dict.fromkeys(clean_scalar(item) for item in texts) if text]


def evidence_qualifier_conflict(mention: Mention, cluster: Cluster) -> bool:
    mention_names = mention_name_conflict_texts(mention)
    cluster_names = cluster_name_conflict_texts(cluster)
    has_conflict = False
    for left in mention_names:
        for right in cluster_names:
            pair_conflict = (
                has_direction_conflict(left, right)
                or has_object_qualifier_conflict(left, right)
                or important_name_difference_conflict(left, right, mention.entity_type)
                or object_slot_conflict(left, right, mention.entity_type)
            )
            if pair_conflict:
                has_conflict = True
                continue
            if normalized_text_similarity(left, right) >= 0.75:
                return False
    return has_conflict


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def token_weight(token: str) -> float:
    if token.startswith(("out:", "in:")):
        return 1.0
    if token.startswith(("co:", "adj:")):
        return 0.25
    return 0.5


def split_neighbor_token(token: str) -> dict[str, str]:
    text = clean_scalar(token)
    if text.startswith(("out:", "in:")):
        parts = text.split(":", 3)
        if len(parts) == 4:
            return {
                "kind": parts[0],
                "relation_type": parts[1],
                "entity_type": parts[2],
                "name": parts[3],
            }
    if text.startswith(("co:", "adj:")):
        kind, rest = text.split(":", 1)
        parts = rest.split(":", 1)
        if len(parts) == 2:
            return {
                "kind": kind,
                "relation_type": "",
                "entity_type": parts[0],
                "name": parts[1],
            }
    return {"kind": "", "relation_type": "", "entity_type": "", "name": text}


@lru_cache(maxsize=200000)
def normalized_text_similarity(left: str, right: str) -> float:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_weak = weak_normalize_name(left_norm)
    right_weak = weak_normalize_name(right_norm)
    if left_weak and left_weak == right_weak:
        return 0.92
    if len(left_norm) >= 3 and len(right_norm) >= 3 and (left_norm in right_norm or right_norm in left_norm):
        return 0.82
    return SequenceMatcher(None, left_norm, right_norm).ratio()


@lru_cache(maxsize=500000)
def neighbor_token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    left_data = split_neighbor_token(left)
    right_data = split_neighbor_token(right)
    left_relation_like = left_data["kind"] in {"out", "in"}
    right_relation_like = right_data["kind"] in {"out", "in"}
    if left_relation_like != right_relation_like:
        return 0.0
    if left_relation_like:
        if left_data["kind"] != right_data["kind"]:
            return 0.0
        relation_score = 1.0 if left_data["relation_type"] == right_data["relation_type"] else 0.0
        type_score = 1.0 if left_data["entity_type"] == right_data["entity_type"] else 0.0
        if relation_score <= 0.0 or type_score <= 0.0:
            return 0.0
        name_similarity = normalized_text_similarity(left_data["name"], right_data["name"])
        return 0.45 * relation_score + 0.20 * type_score + 0.35 * name_similarity

    left_context_like = left_data["kind"] in {"co", "adj"}
    right_context_like = right_data["kind"] in {"co", "adj"}
    if left_context_like and right_context_like:
        if left_data["entity_type"] != right_data["entity_type"]:
            return 0.0
        name_similarity = normalized_text_similarity(left_data["name"], right_data["name"])
        kind_score = 1.0 if left_data["kind"] == right_data["kind"] else 0.7
        return 0.25 * kind_score + 0.25 + 0.50 * name_similarity

    return normalized_text_similarity(left_data["name"], right_data["name"])


def weighted_jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    intersection = left & right
    union_weight = sum(token_weight(token) for token in union)
    intersection_weight = sum(token_weight(token) for token in intersection)
    return intersection_weight / union_weight if union_weight else 0.0


def soft_neighbor_similarity(left: set[str], right: set[str], min_pair_similarity: float = 0.72) -> float:
    if not left and not right:
        return 0.0
    if not left or not right:
        return 0.0
    exact_score = weighted_jaccard(left, right)
    matched_right: set[str] = set()
    soft_intersection = 0.0
    for left_token in sorted(left, key=token_weight, reverse=True):
        best_token = ""
        best_score = 0.0
        for right_token in right:
            if right_token in matched_right:
                continue
            similarity = neighbor_token_similarity(left_token, right_token)
            if similarity > best_score:
                best_score = similarity
                best_token = right_token
        if best_token and best_score >= min_pair_similarity:
            matched_right.add(best_token)
            soft_intersection += min(token_weight(left_token), token_weight(best_token)) * best_score
    union_weight = sum(token_weight(token) for token in left) + sum(token_weight(token) for token in right) - soft_intersection
    soft_score = soft_intersection / union_weight if union_weight > 0 else 0.0
    return max(exact_score, soft_score)


def rank_candidate_clusters(
    mention: Mention,
    clusters: Iterable[Cluster],
    *,
    exclude_cluster_id: str = "",
    top_k: int = 20,
    min_name: float = 0.45,
    min_embedding: float = 0.88,
    min_neighbor: float = 0.35,
) -> list[tuple[Cluster, dict[str, float]]]:
    ranked: list[tuple[float, Cluster, dict[str, float]]] = []
    for cluster in clusters:
        if cluster.cluster_id == exclude_cluster_id:
            continue
        if not cluster.mention_ids:
            continue
        if cluster.file_id != mention.file_id or cluster.entity_type != mention.entity_type:
            continue
        if hard_conflict(mention, cluster):
            continue
        if evidence_qualifier_conflict(mention, cluster):
            continue

        name_score = normalized_text_similarity(mention.normalized_name or mention.mention, cluster.canonical_name)
        embedding_score = cosine_similarity(mention.embedding, cluster.embedding)
        neighbor_score = soft_neighbor_similarity(mention.neighbor_tokens, cluster.neighbor_tokens)
        if name_score < min_name and embedding_score < min_embedding and neighbor_score < min_neighbor:
            continue

        coarse_score = max(name_score, embedding_score, neighbor_score)
        coarse_score += 0.10 * neighbor_score + 0.05 * name_score
        ranked.append(
            (
                coarse_score,
                cluster,
                {
                    "name_score": name_score,
                    "embedding_score": embedding_score,
                    "neighbor_score": neighbor_score,
                },
            )
        )

    ranked.sort(key=lambda item: item[0], reverse=True)
    if top_k > 0:
        ranked = ranked[:top_k]
    return [(cluster, scores) for _score, cluster, scores in ranked]


def chunk_number(chunk_id: str) -> int | None:
    text = clean_scalar(chunk_id)
    match = re.search(r"(\d+)$", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def mention_context_token(mention: Mention) -> str:
    return f"{mention.entity_type}:{normalize_name(mention.normalized_name or mention.mention)}"


def add_context_neighbor_tokens(mentions: list[Mention]) -> None:
    by_chunk: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        for chunk_id in mention.chunk_ids:
            by_chunk[chunk_id].append(mention)

    chunk_numbers = {chunk_id: chunk_number(chunk_id) for chunk_id in by_chunk}
    for chunk_id, chunk_mentions in by_chunk.items():
        for mention in chunk_mentions:
            for other in chunk_mentions:
                if other.mention_id == mention.mention_id:
                    continue
                mention.neighbor_tokens.add(f"co:{mention_context_token(other)}")

            current_no = chunk_numbers.get(chunk_id)
            if current_no is None:
                continue
            for other_chunk_id, other_no in chunk_numbers.items():
                if other_no is None or abs(other_no - current_no) != 1:
                    continue
                for other in by_chunk[other_chunk_id]:
                    if other.mention_id == mention.mention_id:
                        continue
                    mention.neighbor_tokens.add(f"adj:{mention_context_token(other)}")


def name_score(mention: Mention, cluster: Cluster) -> float:
    left = normalize_name(mention.normalized_name or mention.mention)
    right = normalize_name(cluster.canonical_name)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_weak = weak_normalize_name(left)
    right_weak = weak_normalize_name(right)
    if left_weak and left_weak == right_weak:
        return 0.92
    if len(left) >= 3 and len(right) >= 3 and (left in right or right in left):
        return 0.82
    return SequenceMatcher(None, left, right).ratio()


def semantic_boost_thresholds(entity_type: str, min_name: float, min_embedding: float) -> tuple[float, float]:
    thresholds = TYPE_SEMANTIC_BOOST_THRESHOLDS.get(clean_scalar(entity_type))
    if not thresholds:
        return min_name, min_embedding
    return float(thresholds["min_name"]), float(thresholds["min_embedding"])


def apply_synonym_groups(text: str, groups: list[tuple[str, ...]]) -> str:
    result = text
    for group in groups:
        canonical = group[0]
        for item in group[1:]:
            result = result.replace(item, canonical)
    return result


def candidate_terms(value: str) -> set[str]:
    text = normalize_name(value)
    if not text:
        return set()
    terms = set(re.findall(r"[a-z0-9]+", text, flags=re.IGNORECASE))
    cjk_parts = re.findall(r"[\u4e00-\u9fff]+", text)
    for part in cjk_parts:
        for size in range(2, min(5, len(part) + 1)):
            for index in range(0, len(part) - size + 1):
                terms.add(part[index : index + size])
    return {term for term in terms if len(term) >= 2}


def common_terms_for_names(names: list[str], min_ratio: float = 0.5) -> set[str]:
    clean_names = list(dict.fromkeys(name for name in names if clean_scalar(name)))
    if len(clean_names) < 2:
        return set()
    counts: Counter[str] = Counter()
    for name in clean_names:
        counts.update(candidate_terms(name))
    threshold = max(2, math.ceil(len(clean_names) * min_ratio))
    common = {term for term, count in counts.items() if count >= threshold}
    return {term for term in common if not any(term != other and term in other for other in common)}


def remove_common_terms(text: str, common_terms: set[str]) -> str:
    result = text
    for term in sorted(common_terms, key=len, reverse=True):
        if term and len(result) >= len(term):
            result = result.replace(term, "")
    return result


def comparison_common_terms(left: str, cluster: Cluster) -> set[str]:
    names = [left, cluster.canonical_name, *cluster.aliases]
    return common_terms_for_names(names, min_ratio=0.5)


LOW_INFORMATION_COMMON_TERMS = {
    "故障",
    "异常",
    "问题",
    "状态",
    "现象",
    "风险",
    "危险",
    "可能",
    "发生",
    "出现",
    "产生",
    "导致",
    "处理",
    "进行",
    "执行",
    "相关",
    "某些",
    "部分",
    "部件",
}

RESIDUAL_STRUCTURAL_TERMS = [
    "之间",
    "与",
    "和",
    "及",
    "到",
    "至",
    "之",
    "的",
    "连接",
    "通信",
    "通讯",
    "线缆",
    "线",
]


def informative_common_terms(common_terms: set[str]) -> set[str]:
    return {
        term
        for term in common_terms
        if term not in LOW_INFORMATION_COMMON_TERMS and not any(term != weak and term in weak for weak in LOW_INFORMATION_COMMON_TERMS)
    }


def informative_common_score(common_terms: set[str]) -> float:
    informative = informative_common_terms(common_terms)
    if not informative:
        return 0.0
    longest = max(len(term) for term in informative)
    return min(1.0, longest / 6)


def shared_object_sensitive_repair_action(left: str, right: str) -> bool:
    left_text = normalize_name(left)
    right_text = normalize_name(right)
    return any(action in left_text and action in right_text for action in OBJECT_SENSITIVE_REPAIR_ACTIONS)


def residual_object_text(value: str, entity_type: str, common_terms: set[str] | None = None) -> str:
    text = specificity_residue(value, entity_type, common_terms)
    for term in RESIDUAL_STRUCTURAL_TERMS:
        text = text.replace(term, "")
    return text


def residual_object_conflict(left: str, right: str, entity_type: str, common_terms: set[str] | None = None) -> bool:
    if entity_type not in {"故障事件", "维修方法"}:
        return False
    if informative_common_score(common_terms or set()) < 0.75:
        return False
    left_object = residual_object_text(left, entity_type, common_terms)
    right_object = residual_object_text(right, entity_type, common_terms)
    if not left_object or not right_object:
        return False
    if left_object in right_object or right_object in left_object:
        return False
    return SequenceMatcher(None, left_object, right_object).ratio() < 0.50


def object_slot_conflict(left: str, right: str, entity_type: str, common_terms: set[str] | None = None) -> bool:
    if entity_type != "维修方法" or not shared_object_sensitive_repair_action(left, right):
        return False
    left_residue = specificity_residue(left, entity_type, common_terms)
    right_residue = specificity_residue(right, entity_type, common_terms)
    if not left_residue or not right_residue:
        return False
    if left_residue in right_residue or right_residue in left_residue:
        return False
    return SequenceMatcher(None, left_residue, right_residue).ratio() < 0.72


def refine_core_name(value: str, entity_type: str, common_terms: set[str] | None = None) -> str:
    text = normalize_name(value)
    if entity_type == "维修方法":
        text = apply_synonym_groups(text, REFINE_REPAIR_ACTION_SYNONYMS)
        for weak in ["处理", "进行", "执行", "将", "把", "状态", "参数", "的"]:
            text = text.replace(weak, "")
    else:
        text = apply_synonym_groups(text, REFINE_FAULT_STATE_SYNONYMS)
        for weak in REFINE_WEAK_EVENT_TERMS:
            if len(text) > len(weak) + 1:
                text = text.replace(weak, "")
    if common_terms:
        text = remove_common_terms(text, common_terms)
    return text


def canonical_name_quality(name: str, entity_type: str, all_names: set[str]) -> float:
    normalized = normalize_name(name)
    if not normalized:
        return 0.0
    if entity_type == "触发规则":
        return trigger_rule_canonical_quality(name, all_names)
    if entity_type == "维修方法":
        return repair_method_canonical_quality(name, all_names)
    length = len(normalized)
    ideal = 14 if entity_type == "维修方法" else 12
    length_score = max(0.0, 1.0 - abs(length - ideal) / max(ideal, 1))

    core = refine_core_name(name, entity_type)
    object_score = min(1.0, len(core) / 8) if core else 0.0

    action_score = 0.5
    if entity_type == "维修方法":
        action_hits = 0
        for group in REFINE_REPAIR_ACTION_SYNONYMS:
            if any(action in normalized for action in group):
                action_hits += 1
        action_score = min(1.0, 0.35 + 0.35 * action_hits + (0.20 if ("或" in name or "/" in name) else 0.0))
    elif entity_type == "故障事件":
        event_terms = ["故障", "异常", "失效", "停机", "过高", "过低", "过大", "降低", "升高", "危险", "风险"]
        action_score = 1.0 if any(term in name for term in event_terms) else 0.55

    peers = [other for other in all_names if other and other != name]
    specificity_score = 0.5
    if peers:
        core_norm = refine_core_name(name, entity_type)
        specificity_core = core_norm
        if entity_type == "维修方法":
            for action in OBJECT_SENSITIVE_REPAIR_ACTIONS:
                specificity_core = specificity_core.replace(action, "")
        specificity_values = []
        for other in peers:
            other_core = refine_core_name(other, entity_type)
            other_specificity_core = other_core
            if entity_type == "维修方法":
                for action in OBJECT_SENSITIVE_REPAIR_ACTIONS:
                    other_specificity_core = other_specificity_core.replace(action, "")
            if not specificity_core or not other_specificity_core:
                specificity_values.append(0.5)
            elif other_specificity_core in specificity_core and len(specificity_core) >= len(other_specificity_core) + 2:
                specificity_values.append(1.0)
            elif specificity_core in other_specificity_core and len(other_specificity_core) >= len(specificity_core) + 2:
                specificity_values.append(0.0)
            else:
                specificity_values.append(0.5)
        specificity_score = sum(specificity_values) / len(specificity_values)
        centrality = sum(
            max(
                SequenceMatcher(None, normalize_name(name), normalize_name(other)).ratio(),
                high_core_similarity(name, other, entity_type),
            )
            for other in peers
        ) / len(peers)
    else:
        centrality = 1.0

    if entity_type == "维修方法":
        return 0.20 * length_score + 0.25 * action_score + 0.25 * object_score + 0.15 * centrality + 0.15 * specificity_score
    return 0.35 * length_score + 0.30 * action_score + 0.20 * object_score + 0.15 * centrality


def choose_canonical_name(entity_type: str, names: set[str], current: str) -> str:
    candidates = {clean_scalar(name) for name in names if clean_scalar(name)}
    if current:
        candidates.add(current)
    if not candidates:
        return current
    return max(
        sorted(candidates),
        key=lambda item: (
            canonical_name_quality(item, entity_type, candidates),
            len(refine_core_name(item, entity_type)),
            len(normalize_name(item)),
        ),
    )


def specificity_residue(value: str, entity_type: str, common_terms: set[str] | None = None) -> str:
    text = normalize_name(value)
    if entity_type == "维修方法":
        text = apply_synonym_groups(text, REFINE_REPAIR_ACTION_SYNONYMS)
        weak_terms = ["进行", "执行", "处理", "状态", "参数", "的", "对", "将", "把"]
    else:
        text = apply_synonym_groups(text, REFINE_FAULT_STATE_SYNONYMS)
        weak_terms = [
            *REFINE_WEAK_EVENT_TERMS,
            "故障",
            "异常",
            "问题",
            "状态",
            "现象",
            "某些",
            "部分",
            "部件",
            "相关",
            "发生",
            "出现",
            "产生",
            "的",
        ]
    changed = True
    while changed:
        changed = False
        for weak in weak_terms:
            if weak and weak in text and len(text) >= len(weak):
                text = text.replace(weak, "")
                changed = True
        for suffix in WEAK_SUFFIXES:
            if text.endswith(suffix) and len(text) >= len(suffix):
                text = text[: -len(suffix)]
                changed = True
    if common_terms:
        text = remove_common_terms(text, common_terms)
    return text


def has_unexplained_specificity_gap(
    left: str,
    right: str,
    entity_type: str,
    common_terms: set[str] | None = None,
) -> bool:
    if entity_type not in {"故障事件", "维修方法"}:
        return False
    left_residue = specificity_residue(left, entity_type, common_terms)
    right_residue = specificity_residue(right, entity_type, common_terms)
    if not left_residue or not right_residue:
        return abs(len(left_residue) - len(right_residue)) >= 4
    if left_residue in right_residue or right_residue in left_residue:
        return False
    ratio = SequenceMatcher(None, left_residue, right_residue).ratio()
    length_gap = abs(len(left_residue) - len(right_residue))
    if object_slot_conflict(left, right, entity_type, common_terms):
        return True
    if informative_common_score(common_terms or set()) >= 0.50 and min(len(left_residue), len(right_residue)) >= 2:
        return ratio < 0.45
    return length_gap >= 4 and ratio < 0.72


def high_core_similarity(left: str, right: str, entity_type: str, common_terms: set[str] | None = None) -> float:
    left_core = refine_core_name(left, entity_type, common_terms)
    right_core = refine_core_name(right, entity_type, common_terms)
    if not left_core or not right_core:
        return 0.0
    if left_core == right_core:
        return 1.0
    if len(left_core) >= 2 and len(right_core) >= 2 and (left_core in right_core or right_core in left_core):
        return 0.92
    return SequenceMatcher(None, left_core, right_core).ratio()


def refine_candidate_merge_reason(mention: Mention, cluster: Cluster, score: float, reasons: list[str]) -> str:
    if hard_conflict(mention, cluster):
        return ""
    if score < 0.5:
        return ""
    entity_type = mention.entity_type
    left = mention.normalized_name or mention.mention
    right = cluster.canonical_name
    common_terms = comparison_common_terms(left, cluster)
    common_score = informative_common_score(common_terms)
    core_score = high_core_similarity(left, right, entity_type)
    e_score = cosine_similarity(mention.embedding, cluster.embedding)
    neighbor_score = soft_neighbor_similarity(mention.neighbor_tokens, cluster.neighbor_tokens)
    if has_unexplained_specificity_gap(left, right, entity_type, common_terms):
        if not (e_score >= 0.96 and neighbor_score >= 0.90 and core_score >= 0.80):
            return ""
    same_or_near_chunk = bool(set(mention.chunk_ids) & {clean_scalar(item.get("chunk_id")) for item in cluster.evidence if isinstance(item, dict)})
    if not same_or_near_chunk:
        mention_nums = [chunk_number(chunk_id) for chunk_id in mention.chunk_ids]
        cluster_nums = [
            chunk_number(clean_scalar(item.get("chunk_id")))
            for item in cluster.evidence
            if isinstance(item, dict)
        ]
        same_or_near_chunk = any(
            left_no is not None and right_no is not None and abs(left_no - right_no) <= 1
            for left_no in mention_nums
            for right_no in cluster_nums
        )

    if core_score >= 0.92 and e_score >= 0.85:
        return f"refine=weak_core_match;core={core_score:.2f};embedding={e_score:.2f}"

    if entity_type == "维修方法":
        if core_score >= 0.75 and e_score >= 0.86:
            return f"refine=repair_action_synonym;core={core_score:.2f};embedding={e_score:.2f}"
        if common_score >= 0.50 and e_score >= 0.88 and (core_score >= 0.55 or neighbor_score >= 0.45):
            return (
                f"refine=repair_common_context;core={core_score:.2f};"
                f"embedding={e_score:.2f};neighbor={neighbor_score:.2f};common={common_score:.2f}"
            )

    if entity_type == "故障事件":
        if core_score >= 0.65 and e_score >= 0.90 and neighbor_score >= 0.75 and same_or_near_chunk:
            return f"refine=generic_fault_compatible;core={core_score:.2f};embedding={e_score:.2f};neighbor={neighbor_score:.2f}"
        if common_score >= 0.50 and e_score >= 0.88 and neighbor_score >= 0.65 and (core_score >= 0.50 or same_or_near_chunk):
            return (
                f"refine=fault_common_context;core={core_score:.2f};"
                f"embedding={e_score:.2f};neighbor={neighbor_score:.2f};common={common_score:.2f}"
            )

    return ""


def cluster_score(
    mention: Mention,
    cluster: Cluster,
    name_weight: float = 0.55,
    embedding_weight: float = 0.20,
    neighbor_weight: float = 0.20,
    chunk_weight: float = 0.05,
    graph_boost_threshold: float = 0.75,
    graph_boost_min_name: float = 0.45,
    graph_boost_score: float = 0.90,
    semantic_boost_min_name: float = 0.90,
    semantic_boost_min_embedding: float = 0.95,
    semantic_boost_score: float = 0.90,
) -> tuple[float, list[str]]:
    if hard_conflict(mention, cluster):
        return 0.0, ["hard_conflict"]
    if evidence_qualifier_conflict(mention, cluster):
        return 0.0, ["evidence_qualifier_conflict"]
    left_name = mention.normalized_name or mention.mention
    common_terms = comparison_common_terms(left_name, cluster)
    if residual_object_conflict(left_name, cluster.canonical_name, mention.entity_type, common_terms):
        return 0.0, ["residual_object_conflict"]
    if object_slot_conflict(left_name, cluster.canonical_name, mention.entity_type, common_terms):
        return 0.0, ["object_slot_conflict"]
    n_score = name_score(mention, cluster)
    e_score = cosine_similarity(mention.embedding, cluster.embedding)
    rel_neighbor_score = soft_neighbor_similarity(mention.neighbor_tokens, cluster.neighbor_tokens)
    chunk_score = 0.0
    cluster_chunks = {clean_scalar(item.get("chunk_id")) for item in cluster.evidence if isinstance(item, dict)}
    mention_chunks = set(mention.chunk_ids)
    same_chunk = bool(cluster_chunks and mention_chunks and cluster_chunks & mention_chunks)
    if same_chunk:
        chunk_score = 1.0
    if not mention.embedding or not cluster.embedding:
        total_weight = name_weight + neighbor_weight + chunk_weight
        score = (name_weight * n_score + neighbor_weight * rel_neighbor_score + chunk_weight * chunk_score) / total_weight
    else:
        total_weight = name_weight + embedding_weight + neighbor_weight + chunk_weight
        score = (
            name_weight * n_score
            + embedding_weight * e_score
            + neighbor_weight * rel_neighbor_score
            + chunk_weight * chunk_score
        ) / total_weight
    graph_boosted = False
    if not same_chunk and rel_neighbor_score >= graph_boost_threshold and n_score >= graph_boost_min_name:
        score = max(score, graph_boost_score)
        graph_boosted = True
    semantic_min_name, semantic_min_embedding = semantic_boost_thresholds(
        mention.entity_type,
        semantic_boost_min_name,
        semantic_boost_min_embedding,
    )
    semantic_boosted = False
    if e_score >= semantic_min_embedding and n_score >= semantic_min_name:
        score = max(score, semantic_boost_score)
        semantic_boosted = True
    reasons = [
        f"name={n_score:.2f}",
        f"embedding={e_score:.2f}",
        f"neighbor={rel_neighbor_score:.2f}",
        f"chunk={chunk_score:.2f}",
        f"graph_boost={str(graph_boosted).lower()}",
        f"semantic_boost={str(semantic_boosted).lower()}",
        f"semantic_min_name={semantic_min_name:.2f}",
        f"semantic_min_embedding={semantic_min_embedding:.2f}",
    ]
    return score, reasons


def add_to_cluster(cluster: Cluster, mention: Mention, reason: str) -> None:
    previous_count = len(cluster.mention_ids)
    cluster.mention_ids.append(mention.mention_id)
    cluster.aliases.update([mention.mention, mention.normalized_name])
    cluster.canonical_name = choose_canonical_name(cluster.entity_type, cluster.aliases, cluster.canonical_name)
    cluster.evidence.extend(mention.evidence)
    cluster.evidence = dedupe_evidence(cluster.evidence)
    cluster.neighbor_tokens.update(mention.neighbor_tokens)
    if mention.embedding:
        if cluster.embedding and len(cluster.embedding) == len(mention.embedding):
            cluster.embedding = l2_normalize(
                [
                    (cluster.embedding[index] * previous_count + mention.embedding[index]) / (previous_count + 1)
                    for index in range(len(mention.embedding))
                ]
            )
        else:
            cluster.embedding = list(mention.embedding)
    cluster.merge_reasons.add(reason)


def merge_cluster_into(
    target: Cluster,
    source: Cluster,
    *,
    mentions_by_id: dict[str, Mention],
    mention_to_cluster: dict[str, str],
    reason: str,
) -> None:
    for mention_id in list(source.mention_ids):
        mention = mentions_by_id.get(mention_id)
        if not mention:
            continue
        add_to_cluster(target, mention, reason)
        mention_to_cluster[mention_id] = target.cluster_id
    source.mention_ids.clear()


def refine_clusters(
    clusters: list[Cluster],
    mentions: list[Mention],
    mention_to_cluster: dict[str, str],
    *,
    name_weight: float,
    embedding_weight: float,
    neighbor_weight: float,
    chunk_weight: float,
    graph_boost_threshold: float,
    graph_boost_min_name: float,
    graph_boost_score: float,
    semantic_boost_min_name: float,
    semantic_boost_min_embedding: float,
    semantic_boost_score: float,
    max_rounds: int = 50,
    merge_mode: str = "single",
    candidate_top_k: int = 20,
    candidate_min_name: float = 0.45,
    candidate_min_embedding: float = 0.88,
    candidate_min_neighbor: float = 0.35,
) -> int:
    mentions_by_id = {mention.mention_id: mention for mention in mentions}
    refined_merges = 0
    for _round in range(max(1, max_rounds)):
        changed = False
        active_clusters = [cluster for cluster in clusters if cluster.mention_ids]
        strong_batch_candidates: list[tuple[float, str, list[str], Cluster, Cluster]] = []
        for mention in mentions:
            source_cluster_id = mention_to_cluster.get(mention.mention_id)
            if not source_cluster_id:
                continue
            source_cluster = next((cluster for cluster in active_clusters if cluster.cluster_id == source_cluster_id), None)
            if not source_cluster or not source_cluster.mention_ids:
                continue
            best_cluster = None
            best_reason = ""
            best_score = 0.0
            best_score_reasons: list[str] = []
            candidate_clusters = rank_candidate_clusters(
                mention,
                active_clusters,
                exclude_cluster_id=source_cluster_id,
                top_k=candidate_top_k,
                min_name=candidate_min_name,
                min_embedding=candidate_min_embedding,
                min_neighbor=candidate_min_neighbor,
            )
            for cluster, _candidate_scores in candidate_clusters:
                score, score_reasons = cluster_score(
                    mention,
                    cluster,
                    name_weight=name_weight,
                    embedding_weight=embedding_weight,
                    neighbor_weight=neighbor_weight,
                    chunk_weight=chunk_weight,
                    graph_boost_threshold=graph_boost_threshold,
                    graph_boost_min_name=graph_boost_min_name,
                    graph_boost_score=graph_boost_score,
                    semantic_boost_min_name=semantic_boost_min_name,
                    semantic_boost_min_embedding=semantic_boost_min_embedding,
                    semantic_boost_score=semantic_boost_score,
                )
                reason = refine_candidate_merge_reason(mention, cluster, score, score_reasons)
                if reason and score > best_score:
                    best_cluster = cluster
                    best_reason = reason
                    best_score = score
                    best_score_reasons = score_reasons
            if best_cluster:
                if merge_mode == "strong-batch":
                    if best_score >= semantic_boost_score:
                        strong_batch_candidates.append(
                            (best_score, best_reason, best_score_reasons, best_cluster, source_cluster)
                        )
                    continue
                merge_cluster_into(
                    best_cluster,
                    source_cluster,
                    mentions_by_id=mentions_by_id,
                    mention_to_cluster=mention_to_cluster,
                    reason=f"{best_reason};score={best_score:.3f};" + ",".join(best_score_reasons),
                )
                refined_merges += 1
                changed = True
                if merge_mode != "batch":
                    break
        if merge_mode == "strong-batch" and strong_batch_candidates:
            touched_cluster_ids: set[str] = set()
            for best_score, best_reason, best_score_reasons, best_cluster, source_cluster in sorted(
                strong_batch_candidates,
                key=lambda item: item[0],
                reverse=True,
            ):
                if not best_cluster.mention_ids or not source_cluster.mention_ids:
                    continue
                if best_cluster.cluster_id in touched_cluster_ids or source_cluster.cluster_id in touched_cluster_ids:
                    continue
                merge_cluster_into(
                    best_cluster,
                    source_cluster,
                    mentions_by_id=mentions_by_id,
                    mention_to_cluster=mention_to_cluster,
                    reason=f"{best_reason};score={best_score:.3f};" + ",".join(best_score_reasons),
                )
                touched_cluster_ids.add(best_cluster.cluster_id)
                touched_cluster_ids.add(source_cluster.cluster_id)
                refined_merges += 1
                changed = True
        clusters[:] = [cluster for cluster in clusters if cluster.mention_ids]
        if not changed:
            break
    return refined_merges


def cluster_mentions(
    mentions: list[Mention],
    auto_threshold: float,
    review_threshold: float,
    name_weight: float = 0.55,
    embedding_weight: float = 0.20,
    neighbor_weight: float = 0.20,
    chunk_weight: float = 0.05,
    graph_boost_threshold: float = 0.75,
    graph_boost_min_name: float = 0.45,
    graph_boost_score: float = 0.90,
    semantic_boost_min_name: float = 0.90,
    semantic_boost_min_embedding: float = 0.95,
    semantic_boost_score: float = 0.90,
    enable_refinement: bool = True,
    refinement_max_rounds: int = 50,
    refinement_merge_mode: str = "single",
    candidate_top_k: int = 20,
    candidate_min_name: float = 0.45,
    candidate_min_embedding: float = 0.88,
    candidate_min_neighbor: float = 0.35,
) -> tuple[list[Cluster], dict[str, str], list[dict[str, Any]]]:
    clusters: list[Cluster] = []
    mention_to_cluster: dict[str, str] = {}
    review_candidates: list[dict[str, Any]] = []
    buckets: dict[tuple[str, str], list[Mention]] = defaultdict(list)
    for mention in mentions:
        buckets[(mention.file_id, mention.entity_type)].append(mention)

    cluster_index = 0
    for (_file_id, _entity_type), bucket_mentions in sorted(buckets.items()):
        for mention in bucket_mentions:
            exact_match = None
            exact_key = normalize_name(mention.normalized_name or mention.mention)
            hard_sig = hard_signature(mention)
            for cluster in clusters:
                if cluster.file_id != mention.file_id or cluster.entity_type != mention.entity_type:
                    continue
                cluster_keys = {normalize_name(cluster.canonical_name)}
                cluster_keys.update(normalize_name(alias) for alias in cluster.aliases if clean_scalar(alias))
                if exact_key and exact_key in cluster_keys:
                    if evidence_qualifier_conflict(mention, cluster):
                        continue
                    exact_match = cluster
                    break
                if hard_sig and hard_sig == hard_signature(
                    Mention("", "", cluster.file_id, "", "", "", cluster.entity_type, cluster.canonical_name, cluster.canonical_name, [], [])
                ):
                    if evidence_qualifier_conflict(mention, cluster):
                        continue
                    exact_match = cluster
                    break
            if exact_match:
                add_to_cluster(exact_match, mention, "exact_or_hard_signature")
                mention_to_cluster[mention.mention_id] = exact_match.cluster_id
                continue

            best_cluster = None
            best_score = 0.0
            best_reasons: list[str] = []
            candidate_clusters = rank_candidate_clusters(
                mention,
                clusters,
                top_k=candidate_top_k,
                min_name=candidate_min_name,
                min_embedding=candidate_min_embedding,
                min_neighbor=candidate_min_neighbor,
            )
            for cluster, _candidate_scores in candidate_clusters:
                score, reasons = cluster_score(
                    mention,
                    cluster,
                    name_weight=name_weight,
                    embedding_weight=embedding_weight,
                    neighbor_weight=neighbor_weight,
                    chunk_weight=chunk_weight,
                    graph_boost_threshold=graph_boost_threshold,
                    graph_boost_min_name=graph_boost_min_name,
                    graph_boost_score=graph_boost_score,
                    semantic_boost_min_name=semantic_boost_min_name,
                    semantic_boost_min_embedding=semantic_boost_min_embedding,
                    semantic_boost_score=semantic_boost_score,
                )
                if score > best_score:
                    best_score = score
                    best_cluster = cluster
                    best_reasons = reasons

            if best_cluster and best_score >= auto_threshold:
                add_to_cluster(best_cluster, mention, f"score={best_score:.3f};" + ",".join(best_reasons))
                mention_to_cluster[mention.mention_id] = best_cluster.cluster_id
            else:
                if best_cluster and best_score >= review_threshold:
                    review_candidates.append(
                        {
                            "mention_id": mention.mention_id,
                            "mention": mention.mention,
                            "normalized_name": mention.normalized_name,
                            "entity_type": mention.entity_type,
                            "candidate_cluster_id": best_cluster.cluster_id,
                            "candidate_canonical_name": best_cluster.canonical_name,
                            "score": round(best_score, 4),
                            "reasons": ";".join(best_reasons),
                            "mention_evidence": json_dumps(mention.evidence),
                            "cluster_aliases": join_list(best_cluster.aliases),
                        }
                    )
                cluster_index += 1
                cluster = Cluster(
                    cluster_id=f"{mention.file_id}#C{cluster_index:05d}",
                    file_id=mention.file_id,
                    entity_type=mention.entity_type,
                    canonical_name=mention.normalized_name or mention.mention,
                )
                add_to_cluster(cluster, mention, "new_cluster")
                clusters.append(cluster)
                mention_to_cluster[mention.mention_id] = cluster.cluster_id

    if enable_refinement:
        refine_clusters(
            clusters,
            mentions,
            mention_to_cluster,
            name_weight=name_weight,
            embedding_weight=embedding_weight,
            neighbor_weight=neighbor_weight,
            chunk_weight=chunk_weight,
            graph_boost_threshold=graph_boost_threshold,
            graph_boost_min_name=graph_boost_min_name,
            graph_boost_score=graph_boost_score,
            semantic_boost_min_name=semantic_boost_min_name,
            semantic_boost_min_embedding=semantic_boost_min_embedding,
            semantic_boost_score=semantic_boost_score,
            max_rounds=refinement_max_rounds,
            merge_mode=refinement_merge_mode,
            candidate_top_k=candidate_top_k,
            candidate_min_name=candidate_min_name,
            candidate_min_embedding=candidate_min_embedding,
            candidate_min_neighbor=candidate_min_neighbor,
        )

    return clusters, mention_to_cluster, review_candidates


def build_clustered_relations(relations: list[RawRelation], mention_to_cluster: dict[str, str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for rel in relations:
        source_cluster = mention_to_cluster.get(rel.source_mention_id)
        target_cluster = mention_to_cluster.get(rel.target_mention_id)
        if not source_cluster or not target_cluster:
            continue
        key = (source_cluster, rel.relation_type, target_cluster, rel.polarity, rel.certainty)
        if key not in grouped:
            grouped[key] = {
                "source_cluster_id": source_cluster,
                "relation_type": rel.relation_type,
                "target_cluster_id": target_cluster,
                "polarity": rel.polarity,
                "certainty": rel.certainty,
                "cross_chunk": rel.cross_chunk,
                "involved_chunk_ids": rel.involved_chunk_ids,
                "evidence": [],
                "source_relation_ids": [],
            }
        grouped[key]["evidence"].extend(rel.evidence)
        grouped[key]["evidence"] = dedupe_evidence(grouped[key]["evidence"])
        grouped[key]["source_relation_ids"].append(rel.relation_id)
    return list(grouped.values())


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def mention_to_dict(mention: Mention, mention_to_cluster: dict[str, str]) -> dict[str, Any]:
    return {
        "mention_id": mention.mention_id,
        "local_entity_id": mention.local_entity_id,
        "file_id": mention.file_id,
        "sample_id": mention.sample_id,
        "chapter_id": mention.chapter_id,
        "source_type": mention.source_type,
        "entity_type": entity_type_code(mention.entity_type),
        "entity_type_code": entity_type_code(mention.entity_type),
        "entity_type_zh": mention.entity_type,
        "mention": mention.mention,
        "normalized_name": mention.normalized_name,
        "evidence": mention.evidence,
        "chunk_ids": mention.chunk_ids,
        "neighbor_tokens": sorted(mention.neighbor_tokens),
        "cluster_id": mention_to_cluster.get(mention.mention_id, ""),
        "embedding_dim": len(mention.embedding or []),
    }


def relation_to_dict(relation: RawRelation) -> dict[str, Any]:
    return {
        "relation_id": relation.relation_id,
        "file_id": relation.file_id,
        "sample_id": relation.sample_id,
        "source_mention_id": relation.source_mention_id,
        "target_mention_id": relation.target_mention_id,
        "source_text": relation.source_text,
        "target_text": relation.target_text,
        "relation_type": relation.relation_type,
        "cross_chunk": relation.cross_chunk,
        "involved_chunk_ids": relation.involved_chunk_ids,
        "evidence": relation.evidence,
        "polarity": relation.polarity,
        "certainty": relation.certainty,
    }


def cluster_to_dict(cluster: Cluster) -> dict[str, Any]:
    return {
        "cluster_id": cluster.cluster_id,
        "file_id": cluster.file_id,
        "entity_type": entity_type_code(cluster.entity_type),
        "entity_type_code": entity_type_code(cluster.entity_type),
        "entity_type_zh": cluster.entity_type,
        "canonical_name": cluster.canonical_name,
        "mention_ids": cluster.mention_ids,
        "aliases": sorted(cluster.aliases),
        "evidence": cluster.evidence,
        "chunk_ids": evidence_chunk_ids(cluster.evidence),
        "neighbor_tokens": sorted(cluster.neighbor_tokens),
        "merge_reasons": sorted(cluster.merge_reasons),
        "mention_count": len(cluster.mention_ids),
        "embedding_dim": len(cluster.embedding or []),
    }


def export_results(
    output_dir: Path,
    mentions: list[Mention],
    clusters: list[Cluster],
    relations: list[RawRelation],
    clustered_relations: list[dict[str, Any]],
    review_candidates: list[dict[str, Any]],
    mention_to_cluster: dict[str, str] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        output_dir / "entity_mentions.csv",
        [
            {
                "mention_id": m.mention_id,
                "file_id": m.file_id,
                "sample_id": m.sample_id,
                "entity_type": entity_type_code(m.entity_type),
                "entity_type_zh": m.entity_type,
                "mention": m.mention,
                "normalized_name": m.normalized_name,
                "chunk_ids": join_list(m.chunk_ids),
                "neighbor_tokens": join_list(m.neighbor_tokens),
                "evidence": json_dumps(m.evidence),
            }
            for m in mentions
        ],
        ["mention_id", "file_id", "sample_id", "entity_type", "entity_type_zh", "mention", "normalized_name", "chunk_ids", "neighbor_tokens", "evidence"],
    )
    if any(m.embedding for m in mentions):
        embedding_rows = []
        mention_to_cluster = mention_to_cluster or {}
        for mention in mentions:
            if not mention.embedding:
                continue
            embedding_rows.append(
                json.dumps(
                    {
                        "mention_id": mention.mention_id,
                        "cluster_id": mention_to_cluster.get(mention.mention_id, ""),
                        "file_id": mention.file_id,
                        "sample_id": mention.sample_id,
                        "entity_type": entity_type_code(mention.entity_type),
                        "entity_type_zh": mention.entity_type,
                        "normalized_name": mention.normalized_name,
                        "embedding": mention.embedding,
                    },
                    ensure_ascii=False,
                )
            )
        (output_dir / "entity_embeddings.jsonl").write_text("\n".join(embedding_rows), encoding="utf-8")
    write_csv(
        output_dir / "entity_clusters.csv",
        [
            {
                "cluster_id": c.cluster_id,
                "file_id": c.file_id,
                "entity_type": entity_type_code(c.entity_type),
                "entity_type_zh": c.entity_type,
                "canonical_name": c.canonical_name,
                "mention_count": len(c.mention_ids),
                "aliases": join_list(c.aliases),
                "mention_ids": join_list(c.mention_ids),
                "neighbor_tokens": join_list(c.neighbor_tokens),
                "merge_reasons": join_list(c.merge_reasons),
                "evidence": json_dumps(c.evidence),
            }
            for c in clusters
        ],
        ["cluster_id", "file_id", "entity_type", "entity_type_zh", "canonical_name", "mention_count", "aliases", "mention_ids", "neighbor_tokens", "merge_reasons", "evidence"],
    )
    write_csv(
        output_dir / "clustered_relations.csv",
        [
            {
                **relation,
                "evidence": json_dumps(relation["evidence"]),
                "source_relation_ids": join_list(relation["source_relation_ids"]),
            }
            for relation in clustered_relations
        ],
        [
            "source_cluster_id",
            "relation_type",
            "target_cluster_id",
            "polarity",
            "certainty",
            "cross_chunk",
            "involved_chunk_ids",
            "evidence",
            "source_relation_ids",
        ],
    )
    diagnostics_dir = output_dir / "diagnostics"
    write_csv(
        diagnostics_dir / "cluster_candidate_diagnostics.csv",
        review_candidates,
        ["mention_id", "mention", "normalized_name", "entity_type", "candidate_cluster_id", "candidate_canonical_name", "score", "reasons", "mention_evidence", "cluster_aliases"],
    )
    (diagnostics_dir / "cluster_candidate_diagnostics.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in review_candidates),
        encoding="utf-8",
    )
    summary = {
        "schema_version": "smartfta_entity_clustering_v1",
        "mentions": len(mentions),
        "raw_relations": len(relations),
        "clusters": len(clusters),
        "clustered_relations": len(clustered_relations),
        "diagnostic_candidates": len(review_candidates),
        "entity_type_mentions": dict(Counter(m.entity_type for m in mentions)),
        "entity_type_clusters": dict(Counter(c.entity_type for c in clusters)),
        "relation_types_raw": dict(Counter(r.relation_type for r in relations)),
        "relation_types_clustered": dict(Counter(r["relation_type"] for r in clustered_relations)),
        "mentions_per_cluster": dict(Counter(str(len(c.mention_ids)) for c in clusters)),
    }
    (output_dir / "cluster_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    intermediate = {
        "schema_version": "smartfta_cluster_intermediate_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_id": mentions[0].file_id if mentions else (clusters[0].file_id if clusters else ""),
        "summary": summary,
        "mentions": [mention_to_dict(mention, mention_to_cluster or {}) for mention in mentions],
        "raw_relations": [relation_to_dict(relation) for relation in relations],
        "clusters": [cluster_to_dict(cluster) for cluster in clusters],
        "mention_to_cluster": mention_to_cluster or {},
        "clustered_relations": clustered_relations,
        "diagnostic_candidates": review_candidates,
        "notes": {
            "embedding_vectors": "Large embedding vectors are stored separately in entity_embeddings.jsonl when available.",
            "neo4j_import": "Use import_cluster_intermediate_to_kb.py to import this artifact into Neo4j and MongoDB.",
        },
    }
    (output_dir / "cluster_intermediate.json").write_text(
        json.dumps(intermediate, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prototype mention-level entity clustering for SmartFTA annotations.")
    parser.add_argument("--env-file", default=str(CURRENT_DIR / ".env"))
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--auto-threshold", type=float, default=0.90)
    parser.add_argument("--review-threshold", type=float, default=0.72)
    parser.add_argument("--embedding-backend", choices=["none", "hash", "sentence-transformers", "openai-compatible"], default="none")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-api-key", default="")
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-batch-size", type=int, default=10)
    parser.add_argument("--embedding-dim", type=int, default=256)
    parser.add_argument("--reuse-embeddings-jsonl", default="")
    parser.add_argument("--name-weight", type=float, default=0.55)
    parser.add_argument("--embedding-weight", type=float, default=0.20)
    parser.add_argument("--neighbor-weight", type=float, default=0.20)
    parser.add_argument("--chunk-weight", type=float, default=0.05)
    parser.add_argument("--graph-boost-threshold", type=float, default=0.75)
    parser.add_argument("--graph-boost-min-name", type=float, default=0.45)
    parser.add_argument("--graph-boost-score", type=float, default=0.90)
    parser.add_argument("--semantic-boost-min-name", type=float, default=0.90)
    parser.add_argument("--semantic-boost-min-embedding", type=float, default=0.95)
    parser.add_argument("--semantic-boost-score", type=float, default=0.90)
    parser.add_argument("--candidate-top-k", type=int, default=20)
    parser.add_argument("--candidate-min-name", type=float, default=0.45)
    parser.add_argument("--candidate-min-embedding", type=float, default=0.88)
    parser.add_argument("--candidate-min-neighbor", type=float, default=0.35)
    parser.add_argument("--disable-refinement", action="store_true")
    parser.add_argument("--refinement-max-rounds", type=int, default=50)
    parser.add_argument("--refinement-merge-mode", choices=["single", "batch", "strong-batch"], default="single")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    mentions, relations = load_mentions_and_relations(Path(args.input_csv), args.file_id)
    reused_embeddings = 0
    if args.reuse_embeddings_jsonl:
        reused_embeddings = apply_existing_embeddings(mentions, Path(args.reuse_embeddings_jsonl))
    if reused_embeddings <= 0:
        build_embeddings(
            mentions,
            args.embedding_backend,
            args.embedding_model or os.getenv("EMBEDDING_MODEL", ""),
            args.embedding_dim,
            api_key=args.embedding_api_key or os.getenv("EMBEDDING_API_KEY", ""),
            base_url=args.embedding_base_url or os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            batch_size=args.embedding_batch_size or int(os.getenv("EMBEDDING_BATCH_SIZE", "10")),
        )
    clusters, mention_to_cluster, review_candidates = cluster_mentions(
        mentions,
        args.auto_threshold,
        args.review_threshold,
        name_weight=args.name_weight,
        embedding_weight=args.embedding_weight,
        neighbor_weight=args.neighbor_weight,
        chunk_weight=args.chunk_weight,
        graph_boost_threshold=args.graph_boost_threshold,
        graph_boost_min_name=args.graph_boost_min_name,
        graph_boost_score=args.graph_boost_score,
        semantic_boost_min_name=args.semantic_boost_min_name,
        semantic_boost_min_embedding=args.semantic_boost_min_embedding,
        semantic_boost_score=args.semantic_boost_score,
        enable_refinement=(not args.disable_refinement),
        refinement_max_rounds=args.refinement_max_rounds,
        refinement_merge_mode=args.refinement_merge_mode,
        candidate_top_k=args.candidate_top_k,
        candidate_min_name=args.candidate_min_name,
        candidate_min_embedding=args.candidate_min_embedding,
        candidate_min_neighbor=args.candidate_min_neighbor,
    )
    clustered_relations = build_clustered_relations(relations, mention_to_cluster)
    export_results(Path(args.output_dir), mentions, clusters, relations, clustered_relations, review_candidates, mention_to_cluster)

    print(f"file_id: {args.file_id}")
    print(f"mentions: {len(mentions)}")
    print(f"raw_relations: {len(relations)}")
    print(f"clusters: {len(clusters)}")
    print(f"clustered_relations: {len(clustered_relations)}")
    print(f"diagnostic_candidates: {len(review_candidates)}")
    print(f"reused_embeddings: {reused_embeddings}")
    print(f"output_dir: {args.output_dir}")


if __name__ == "__main__":
    main()
