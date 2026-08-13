from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_runtime.artifact_store import put_agent_artifact
from agent_runtime.event_store import append_agent_event
from agent_runtime.policies import ARTIFACT_REPAIR_PATCH, EVENT_AGENT_MESSAGE, STAGE_REPAIR
from diff_analyzer import format_corrections_for_repair, get_relevant_corrections
from generator import repair_fault_tree
from tools.correction_tools import find_active_repair_patterns


class RepairAgent:
    """First-phase repair wrapper around active patterns, corrections, and repair_fault_tree."""

    name = "RepairAgent"

    def run(
        self,
        draft_tree_artifact: Dict[str, Any],
        validation_report: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
        scope_key: str = "",
        chunks: Optional[List[Dict[str, Any]]] = None,
        repair_attempt: int = 1,
        persist_artifact: bool = False,
        apply_legacy_repair: bool = False,
    ) -> Dict[str, Any]:
        tree_data = _extract_tree_data(draft_tree_artifact)
        repairable_issues = [
            issue
            for issue in (validation_report.get("issues") or [])
            if issue.get("repairable") and issue.get("severity") == "error"
        ]
        issue_codes = [str(issue.get("issue_code") or "").upper() for issue in repairable_issues]
        patterns = find_active_repair_patterns(issue_codes=issue_codes, scope_key=scope_key, limit=10)
        corrections = get_relevant_corrections(tree_data, max_distinct=20)
        correction_hint = format_corrections_for_repair(corrections)
        repaired_tree = None
        repair_error = None
        # Phase 2 also repairs validator-detected structural issues when no
        # prior correction is available. The validation report supplies the
        # minimum change constraints in that case.
        if apply_legacy_repair and repairable_issues:
            try:
                result = repair_fault_tree(
                    tree_data,
                    _compose_repair_hint(correction_hint, patterns, validation_report),
                    chunks or [],
                    include_meta=True,
                )
                repaired_tree = result.get("tree")
            except Exception as exc:
                repair_error = str(exc)

        patch = {
            "base_artifact_id": draft_tree_artifact.get("artifact_id"),
            "repair_attempt": int(repair_attempt or 1),
            "scope_key": scope_key,
            "issue_codes": issue_codes,
            "operations": _build_fixture_operations(repairable_issues, patterns, corrections),
            "sources": {
                "repair_patterns": _summarize_patterns(patterns),
                "corrections": _summarize_corrections(corrections),
                "legacy_repair_fault_tree": {
                    "attempted": bool(apply_legacy_repair and repairable_issues),
                    "succeeded": repaired_tree is not None,
                    "error": repair_error,
                },
            },
            "repaired_tree": repaired_tree,
            "status": "repaired" if repaired_tree is not None else "summary_only",
        }
        artifact = None
        if persist_artifact and run_id:
            artifact = put_agent_artifact(
                run_id=run_id,
                artifact_type=ARTIFACT_REPAIR_PATCH,
                content=patch,
                metadata={"producer": self.name},
                producer=self.name,
                parent_artifact_id=str(draft_tree_artifact.get("artifact_id") or "") or None,
            )
            patch["artifact_id"] = artifact.get("artifact_id")
            append_agent_event(
                run_id,
                EVENT_AGENT_MESSAGE,
                stage=STAGE_REPAIR,
                message=f"Repair summary prepared for {len(repairable_issues)} repairable issues",
                payload={
                    "artifact_id": artifact.get("artifact_id"),
                    "status": patch["status"],
                    "repairable_issue_count": len(repairable_issues),
                    "pattern_count": len(patterns),
                    "correction_count": len(corrections),
                },
            )
        return {
            "agent": self.name,
            "artifact_type": ARTIFACT_REPAIR_PATCH,
            "payload": patch,
            "event_type": EVENT_AGENT_MESSAGE,
            "event_payload": {
                "artifact_id": patch.get("artifact_id"),
                "status": patch["status"],
                "repairable_issue_count": len(repairable_issues),
            },
            "next_stage": "validate" if repaired_tree is not None else None,
            "requires_confirmation": False,
            "human_review_required": repaired_tree is None and bool(validation_report.get("error_count")),
        }


def _extract_tree_data(draft_tree_artifact: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(draft_tree_artifact, dict):
        return {}
    content = draft_tree_artifact.get("content")
    if isinstance(content, dict):
        if isinstance(content.get("tree_data"), dict):
            return content["tree_data"]
        if "nodeList" in content or "linkList" in content:
            return content
    payload = draft_tree_artifact.get("payload")
    if isinstance(payload, dict):
        if isinstance(payload.get("tree_data"), dict):
            return payload["tree_data"]
        if "nodeList" in payload or "linkList" in payload:
            return payload
    if "nodeList" in draft_tree_artifact or "linkList" in draft_tree_artifact:
        return draft_tree_artifact
    return {}


def _compose_repair_hint(correction_hint: str, patterns: List[Dict[str, Any]], validation_report: Dict[str, Any]) -> str:
    sections = []
    if patterns:
        sections.append("Active repair patterns:\n" + "\n".join(f"- {p.get('issue_code')}: {p.get('description') or p.get('summary')}" for p in patterns))
    if correction_hint:
        sections.append(correction_hint)
    issues = validation_report.get("issues") or []
    if issues:
        sections.append("Validation issues:\n" + "\n".join(f"- {i.get('issue_code')}: {i.get('message')}" for i in issues))
    return "\n\n".join(sections)


def _build_fixture_operations(
    repairable_issues: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
    corrections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    operations = []
    for issue in repairable_issues:
        operations.append(
            {
                "op": "mark_uncertain",
                "node_ids": issue.get("node_ids") or [],
                "reason_issue_code": issue.get("issue_code"),
                "source": "validation_report",
                "message": issue.get("message"),
            }
        )
    for pattern in patterns[:5]:
        operations.append(
            {
                "op": str(pattern.get("operation") or "mark_uncertain"),
                "reason_issue_code": pattern.get("issue_code"),
                "source": "repair_patterns",
                "pattern_id": pattern.get("pattern_id"),
            }
        )
    for correction in corrections[:5]:
        operations.append(
            {
                "op": _operation_from_correction_type(correction.get("correction_type")),
                "reason_issue_code": "HISTORICAL_CORRECTION",
                "source": "corrections",
                "correction_type": correction.get("correction_type"),
                "node_name": correction.get("node_name"),
            }
        )
    return operations


def _operation_from_correction_type(correction_type: Any) -> str:
    mapping = {
        "gate_changed": "replace_gate",
        "link_added": "add_edge",
        "link_deleted": "remove_edge",
        "name_edited": "update_node",
        "node_added": "add_node",
        "node_deleted": "remove_node",
    }
    return mapping.get(str(correction_type or ""), "mark_uncertain")


def _summarize_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "pattern_id": item.get("pattern_id"),
            "issue_code": item.get("issue_code"),
            "scope_key": item.get("scope_key", ""),
            "status": item.get("status"),
        }
        for item in patterns
    ]


def _summarize_corrections(corrections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "tree_id": item.get("tree_id"),
            "correction_type": item.get("correction_type"),
            "node_name": item.get("node_name"),
            "matched_node_name": item.get("matched_node_name"),
            "similarity": item.get("similarity"),
        }
        for item in corrections
    ]


def run_repair_agent(*args, **kwargs) -> Dict[str, Any]:
    return RepairAgent().run(*args, **kwargs)
