from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_local_env

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None  # type: ignore


CAUSE_RELATION = "CLUSTERED_CAUSES"


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [x for x in value if x not in (None, "")]
    return [value] if value not in ("",) else []


def node_name(props: dict[str, Any]) -> str:
    return clean_scalar(props.get("canonical_name") or props.get("name") or props.get("cluster_id"))


def compact_text(value: Any) -> str:
    return "".join(str(value or "").split())


def direction_evidence_score(rel: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    props = rel.get("props") or {}
    source_name = compact_text(node_name(rel.get("source_props") or {}))
    target_name = compact_text(node_name(rel.get("target_props") or {}))
    evidence = parse_json_list(props.get("evidence_json"))
    cues = ("导致", "触发", "引起", "造成", "致使", "使", "从而", "因此", "保护", "停机")
    if not source_name or not target_name:
        return 0.0, {"direction_signal": "missing_name"}

    best = 0.0
    signal = "none"
    same_sentence_count = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        text = compact_text(item.get("text"))
        if not text:
            continue
        source_pos = text.find(source_name)
        target_pos = text.find(target_name)
        if source_pos >= 0 and target_pos >= 0:
            same_sentence_count += 1
            left, right = sorted([source_pos, target_pos])
            between = text[left:right + max(len(source_name), len(target_name))]
            has_cue = any(cue in between for cue in cues)
            if source_pos < target_pos and has_cue:
                if best < 4.0:
                    best = 4.0
                    signal = "source_before_target_with_cue"
            elif source_pos < target_pos:
                if best < 2.0:
                    best = 2.0
                    signal = "source_before_target"
            elif target_pos < source_pos and has_cue:
                if best > -2.5:
                    best = -2.5
                    signal = "target_before_source_with_cue"
            elif target_pos < source_pos:
                if best > -1.0:
                    best = -1.0
                    signal = "target_before_source"

    if same_sentence_count == 0 and clean_scalar(props.get("cross_chunk")).lower() == "true":
        best -= 1.2
        signal = "cross_chunk_without_same_sentence_direction"
    return best, {"direction_signal": signal, "same_sentence_evidence_count": same_sentence_count}


def relation_strength(rel: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    props = rel.get("props") or {}
    evidence = parse_json_list(props.get("evidence_json"))
    source_relation_ids = as_list(props.get("source_relation_ids"))
    source_file_version_ids = as_list(props.get("source_file_version_ids") or props.get("file_version_id"))
    certainty = clean_scalar(props.get("certainty")) or "certain"
    polarity = clean_scalar(props.get("polarity")) or "positive"

    score = 0.0
    score += 3.0 if certainty == "certain" else 1.0
    score += 1.0 if polarity == "positive" else 0.0
    score += min(len(evidence), 5) * 0.6
    score += min(len(source_relation_ids), 5) * 0.4
    score += min(len(source_file_version_ids), 5) * 0.5
    if clean_scalar(props.get("cross_chunk")).lower() == "true":
        score -= 0.8
    direction_score, direction_details = direction_evidence_score(rel)
    score += direction_score

    details = {
        "certainty": certainty,
        "polarity": polarity,
        "evidence_count": len(evidence),
        "source_relation_count": len(source_relation_ids),
        "source_file_version_count": len(source_file_version_ids),
        "cross_chunk": clean_scalar(props.get("cross_chunk")),
        "direction_score": round(direction_score, 4),
        **direction_details,
        "score": round(score, 4),
    }
    return score, details


def relation_sort_key(rel: dict[str, Any]) -> tuple[Any, ...]:
    score, _ = relation_strength(rel)
    props = rel.get("props") or {}
    return (
        score,
        clean_scalar(props.get("relation_id")),
        clean_scalar(rel.get("element_id")),
    )


def pick_weakest_relation(relations: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(relations, key=relation_sort_key)[0]


def fetch_causal_relations(session: Any, selected_file_version_ids: list[str]) -> list[dict[str, Any]]:
    scope_clause = ""
    params: dict[str, Any] = {}
    if selected_file_version_ids:
        scope_clause = """
          AND any(fid IN coalesce(r.source_file_version_ids, CASE WHEN r.file_version_id IS NULL THEN [] ELSE [r.file_version_id] END)
                  WHERE fid IN $selected_file_version_ids)
        """
        params["selected_file_version_ids"] = selected_file_version_ids

    query = f"""
    MATCH (a:EntityCluster:FaultEvent)-[r:{CAUSE_RELATION}]->(b:EntityCluster:FaultEvent)
    WHERE coalesce(r.is_active, true) = true
      AND a <> b
      {scope_clause}
    RETURN
      elementId(r) AS element_id,
      type(r) AS type,
      properties(r) AS props,
      elementId(a) AS source_element_id,
      elementId(b) AS target_element_id,
      properties(a) AS source_props,
      properties(b) AS target_props
    """
    return session.run(query, **params).data()


def fetch_self_loops(session: Any, selected_file_version_ids: list[str]) -> list[dict[str, Any]]:
    scope_clause = ""
    params: dict[str, Any] = {}
    if selected_file_version_ids:
        scope_clause = """
          AND any(fid IN coalesce(r.source_file_version_ids, CASE WHEN r.file_version_id IS NULL THEN [] ELSE [r.file_version_id] END)
                  WHERE fid IN $selected_file_version_ids)
        """
        params["selected_file_version_ids"] = selected_file_version_ids
    query = f"""
    MATCH (a:EntityCluster:FaultEvent)-[r:{CAUSE_RELATION}]->(a)
    WHERE coalesce(r.is_active, true) = true
      {scope_clause}
    RETURN
      elementId(r) AS element_id,
      type(r) AS type,
      properties(r) AS props,
      elementId(a) AS source_element_id,
      elementId(a) AS target_element_id,
      properties(a) AS source_props,
      properties(a) AS target_props
    """
    return session.run(query, **params).data()


def find_cycles(relations: list[dict[str, Any]], max_cycles: int) -> list[list[dict[str, Any]]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for rel in relations:
        outgoing.setdefault(rel["source_element_id"], []).append(rel)

    cycles: list[list[dict[str, Any]]] = []
    seen_keys: set[tuple[str, ...]] = set()

    def dfs(start: str, current: str, path: list[dict[str, Any]], visiting: set[str]) -> None:
        if len(cycles) >= max_cycles:
            return
        for rel in outgoing.get(current, []):
            nxt = rel["target_element_id"]
            if nxt == start:
                cycle = path + [rel]
                key = tuple(sorted(clean_scalar(item["element_id"]) for item in cycle))
                if key not in seen_keys:
                    seen_keys.add(key)
                    cycles.append(cycle)
                continue
            if nxt in visiting:
                continue
            visiting.add(nxt)
            dfs(start, nxt, path + [rel], visiting)
            visiting.remove(nxt)

    for node_id in sorted(outgoing):
        dfs(node_id, node_id, [], {node_id})
        if len(cycles) >= max_cycles:
            break
    return cycles


def relation_summary(rel: dict[str, Any]) -> dict[str, Any]:
    _, strength = relation_strength(rel)
    props = rel.get("props") or {}
    return {
        "element_id": rel.get("element_id"),
        "relation_id": props.get("relation_id"),
        "source": node_name(rel.get("source_props") or {}),
        "target": node_name(rel.get("target_props") or {}),
        "source_cluster_id": (rel.get("source_props") or {}).get("cluster_id"),
        "target_cluster_id": (rel.get("target_props") or {}).get("cluster_id"),
        "source_file_version_ids": as_list(props.get("source_file_version_ids") or props.get("file_version_id")),
        "certainty": props.get("certainty"),
        "polarity": props.get("polarity"),
        "evidence_json": props.get("evidence_json"),
        "source_relation_ids": as_list(props.get("source_relation_ids")),
        "strength": strength,
    }


def build_cleanup_plan(relations: list[dict[str, Any]], self_loops: list[dict[str, Any]], max_cycles: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    removal_by_id: dict[str, dict[str, Any]] = {}
    cycle_reports: list[dict[str, Any]] = []

    for rel in self_loops:
        rel_id = clean_scalar(rel["element_id"])
        removal_by_id[rel_id] = {
            "reason": "self_loop",
            "remove": relation_summary(rel),
            "cycle": [relation_summary(rel)],
        }

    active = [rel for rel in relations if clean_scalar(rel["element_id"]) not in removal_by_id]
    for cycle in find_cycles(active, max_cycles=max_cycles):
        removable_candidates = [rel for rel in cycle if clean_scalar(rel["element_id"]) not in removal_by_id]
        if not removable_candidates:
            continue
        weakest = pick_weakest_relation(removable_candidates)
        cycle_report = {
            "reason": "directed_cycle",
            "cycle": [relation_summary(rel) for rel in cycle],
            "remove": relation_summary(weakest),
        }
        cycle_reports.append(cycle_report)
        removal_by_id[clean_scalar(weakest["element_id"])] = cycle_report

    removals = list(removal_by_id.values())
    return removals, cycle_reports


def apply_delete(session: Any, element_ids: list[str]) -> int:
    if not element_ids:
        return 0
    record = session.run(
        """
        MATCH ()-[r]->()
        WHERE elementId(r) IN $element_ids
        WITH collect(r) AS rels
        FOREACH (rel IN rels | DELETE rel)
        RETURN size(rels) AS deleted
        """,
        element_ids=element_ids,
    ).single()
    return int(record["deleted"]) if record else 0


def apply_deactivate(session: Any, element_ids: list[str], reason: str) -> int:
    if not element_ids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    record = session.run(
        """
        MATCH ()-[r]->()
        WHERE elementId(r) IN $element_ids
        SET r.is_active = false,
            r.deactivated_reason = $reason,
            r.deactivated_at = $now
        RETURN count(r) AS updated
        """,
        element_ids=element_ids,
        reason=reason,
        now=now,
    ).single()
    return int(record["updated"]) if record else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect and clean directed cycles among FaultEvent EntityCluster CAUSES edges.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--selected-file-version-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="Disabled for global causal cycles; kept only for backward CLI compatibility.")
    parser.add_argument("--mode", choices=["delete", "deactivate"], default="delete")
    parser.add_argument("--max-cycles", type=int, default=200)
    parser.add_argument("--report-file", default="")
    args = parser.parse_args()

    if GraphDatabase is None:
        raise RuntimeError("neo4j package is not installed")
    if args.apply:
        raise RuntimeError(
            "Global causal-cycle cleanup is disabled. Only self-loops should be deleted in KB. "
            "Ordinary causal cycles are handled temporarily inside GNR selected-file views."
        )

    load_local_env(args.env_file, override=True)
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is empty")

    selected_file_version_ids = [clean_scalar(x) for x in args.selected_file_version_id if clean_scalar(x)]
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            relations = fetch_causal_relations(session, selected_file_version_ids)
            self_loops = fetch_self_loops(session, selected_file_version_ids)
            removals, cycle_reports = build_cleanup_plan(relations, self_loops, args.max_cycles)
            element_ids = [clean_scalar(item["remove"].get("element_id")) for item in removals if clean_scalar(item["remove"].get("element_id"))]
            changed = 0
            if args.apply and element_ids:
                if args.mode == "delete":
                    changed = apply_delete(session, element_ids)
                else:
                    changed = apply_deactivate(session, element_ids, "causal_cycle_cleanup")
    finally:
        driver.close()

    report = {
        "database": database,
        "selected_file_version_ids": selected_file_version_ids,
        "dry_run": not args.apply,
        "mode": args.mode,
        "active_causal_relations": len(relations),
        "self_loops": len(self_loops),
        "cycles_detected": len(cycle_reports),
        "planned_removals": len(removals),
        "changed": changed,
        "removals": removals,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report_file:
        Path(args.report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_file).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
