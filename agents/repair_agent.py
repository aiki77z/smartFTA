from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_runtime.artifact_store import put_agent_artifact
from agent_runtime.event_store import append_agent_event
from agent_runtime.policies import (
    ARTIFACT_REPAIR_PATCH,
    ARTIFACT_TREE_DRAFT,
    ARTIFACT_VALIDATION_REPORT,
    EVENT_AGENT_MESSAGE,
    EVENT_DRAFT_GENERATED,
    EVENT_VALIDATION_DONE,
    STAGE_REPAIR,
    STAGE_VALIDATE,
)
from diff_analyzer import format_corrections_for_repair, get_relevant_corrections
from generator import repair_fault_tree
from tools.correction_tools import find_active_repair_patterns
from tools.repair_patch_tools import apply_repair_patch, build_repair_patch_from_validation
from tools.validator_tools import normalize_validation_report
from validator import validate_full


class RepairAgent:
    """Repair validation issues with auditable RepairPatch artifacts."""

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
        apply_patch: bool = True,
        revalidate: bool = True,
        max_operations: int = 50,
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
        legacy_repaired_tree = None
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
                legacy_repaired_tree = result.get("tree")
            except Exception as exc:
                repair_error = str(exc)

        patch = build_repair_patch_from_validation(
            tree_data,
            validation_report,
            base_artifact_id=draft_tree_artifact.get("artifact_id"),
            scope_key=scope_key,
            repair_attempt=repair_attempt,
            patterns=patterns,
            corrections=corrections,
        )
        if max_operations > 0 and len(patch.get("operations") or []) > max_operations:
            patch["operations"] = patch["operations"][:max_operations]
            patch["truncated"] = True
        patch.setdefault("sources", {})
        patch["sources"]["legacy_repair_fault_tree"] = {
            "attempted": bool(apply_legacy_repair and repairable_issues),
            "succeeded": legacy_repaired_tree is not None,
            "error": repair_error,
        }

        patched_tree = None
        patch_application: Optional[Dict[str, Any]] = None
        patched_validation_report = None
        if apply_patch and patch.get("operations"):
            patch_application = apply_repair_patch(tree_data, patch)
            patch["application"] = {
                "changed": bool(patch_application.get("changed")),
                "applied_count": len(patch_application.get("applied_operations") or []),
                "skipped_count": len(patch_application.get("skipped_operations") or []),
                "applied_operations": patch_application.get("applied_operations") or [],
                "skipped_operations": patch_application.get("skipped_operations") or [],
            }
            if patch_application.get("changed"):
                patched_tree = patch_application.get("tree_data")
            if revalidate and patched_tree is not None:
                raw_validation = validate_full(patched_tree, skip_semantic=True, include_meta=True)
                patched_validation_report = normalize_validation_report(
                    raw_validation,
                    scope_key=scope_key,
                    draft_artifact_id=None,
                    run_id=run_id,
                    source="repair_patch_revalidate",
                )

        if legacy_repaired_tree is not None and patched_tree is None:
            patched_tree = legacy_repaired_tree

        draft_payload = None
        if patched_tree is not None:
            draft_payload = {
                "tree_data": patched_tree,
                "source": "repair_patch" if patch_application else "legacy_repair_fault_tree",
                "repair_attempt": int(repair_attempt or 1),
                "parent_artifact_id": draft_tree_artifact.get("artifact_id"),
            }
        patch["repaired_tree"] = patched_tree
        if patched_tree is not None:
            patch["status"] = "patched" if patch_application else "legacy_repaired"
        elif patch_application:
            patch["status"] = "patch_not_applied"
        elif patch.get("operations"):
            patch["status"] = "patch_ready"
        else:
            patch["status"] = "no_reliable_patch"

        patch_artifact = None
        draft_artifact = None
        validation_artifact = None
        if persist_artifact and run_id:
            patch_artifact = put_agent_artifact(
                run_id=run_id,
                artifact_type=ARTIFACT_REPAIR_PATCH,
                content=patch,
                metadata={"producer": self.name},
                producer=self.name,
                parent_artifact_id=str(draft_tree_artifact.get("artifact_id") or "") or None,
            )
            patch["artifact_id"] = patch_artifact.get("artifact_id")
            if patched_tree is not None:
                draft_payload["repair_patch_artifact_id"] = patch_artifact.get("artifact_id")
                draft_artifact = put_agent_artifact(
                    run_id=run_id,
                    artifact_type=ARTIFACT_TREE_DRAFT,
                    content=draft_payload,
                    producer=self.name,
                    parent_artifact_id=draft_tree_artifact.get("artifact_id"),
                    metadata={"producer": self.name, "source": "repair_patch"},
                )
                patch["draft_artifact_id"] = draft_artifact.get("artifact_id")
                append_agent_event(
                    run_id,
                    EVENT_DRAFT_GENERATED,
                    stage=STAGE_REPAIR,
                    message="RepairPatch created a new draft tree",
                    payload={
                        "artifact_id": draft_artifact.get("artifact_id"),
                        "repair_patch_artifact_id": patch_artifact.get("artifact_id"),
                    },
                )
            if patched_validation_report is not None:
                if draft_artifact:
                    patched_validation_report["draft_artifact_id"] = draft_artifact.get("artifact_id")
                validation_artifact = put_agent_artifact(
                    run_id=run_id,
                    artifact_type=ARTIFACT_VALIDATION_REPORT,
                    content=patched_validation_report,
                    producer=self.name,
                    parent_artifact_id=(
                        str((draft_artifact or draft_tree_artifact).get("artifact_id") or "") or None
                    ),
                    metadata={"producer": self.name, "source": "repair_patch_revalidate"},
                )
                patched_validation_report["artifact_id"] = validation_artifact.get("artifact_id")
                append_agent_event(
                    run_id,
                    EVENT_VALIDATION_DONE,
                    stage=STAGE_VALIDATE,
                    message=_repair_validation_message(patched_validation_report),
                    payload={
                        "artifact_id": validation_artifact.get("artifact_id"),
                        "passed": patched_validation_report.get("passed"),
                        "error_count": patched_validation_report.get("error_count"),
                        "warning_count": patched_validation_report.get("warning_count"),
                        "info_count": patched_validation_report.get("info_count"),
                    },
                )
            append_agent_event(
                run_id,
                EVENT_AGENT_MESSAGE,
                stage=STAGE_REPAIR,
                message=_repair_message(patch, repairable_issues),
                payload={
                    "artifact_id": patch_artifact.get("artifact_id"),
                    "status": patch["status"],
                    "repairable_issue_count": len(repairable_issues),
                    "pattern_count": len(patterns),
                    "correction_count": len(corrections),
                    "draft_artifact_id": patch.get("draft_artifact_id"),
                    "validation_artifact_id": (validation_artifact or {}).get("artifact_id"),
                },
            )
        blocking_errors = [
            issue
            for issue in (validation_report.get("issues") or [])
            if issue.get("severity") == "error" and not issue.get("repairable")
        ]
        remaining_error_count = (
            int(patched_validation_report.get("error_count") or 0)
            if patched_validation_report is not None
            else int(validation_report.get("error_count") or 0)
        )
        return {
            "agent": self.name,
            "artifact_type": ARTIFACT_REPAIR_PATCH,
            "payload": patch,
            "draft_tree_artifact": draft_artifact
            or (
                {
                    "artifact_id": None,
                    "type": ARTIFACT_TREE_DRAFT,
                    "payload": draft_payload,
                    "content": draft_payload,
                    "parent_artifact_id": draft_tree_artifact.get("artifact_id"),
                }
                if draft_payload is not None
                else None
            ),
            "validation_report": patched_validation_report,
            "event_type": EVENT_AGENT_MESSAGE,
            "event_payload": {
                "artifact_id": patch.get("artifact_id"),
                "status": patch["status"],
                "repairable_issue_count": len(repairable_issues),
                "draft_artifact_id": patch.get("draft_artifact_id"),
            },
            "next_stage": "validate" if patched_tree is not None else None,
            "requires_confirmation": False,
            "human_review_required": bool(blocking_errors) or (apply_patch and remaining_error_count > 0),
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


def _repair_message(patch: Dict[str, Any], repairable_issues: List[Dict[str, Any]]) -> str:
    status = patch.get("status")
    app = patch.get("application") or {}
    if status == "patched":
        return (
            "RepairPatch applied "
            f"{app.get('applied_count', 0)} operations for {len(repairable_issues)} repairable issues"
        )
    if status == "patch_ready":
        return f"RepairPatch prepared for {len(repairable_issues)} repairable issues"
    if status == "legacy_repaired":
        return "Legacy repair produced a repaired draft"
    return f"No reliable RepairPatch was produced for {len(repairable_issues)} repairable issues"


def _repair_validation_message(report: Dict[str, Any]) -> str:
    if report.get("passed"):
        return "Repaired draft validation passed"
    return (
        "Repaired draft validation found "
        f"{report.get('error_count', 0)} errors, "
        f"{report.get('warning_count', 0)} warnings"
    )


def run_repair_agent(*args, **kwargs) -> Dict[str, Any]:
    return RepairAgent().run(*args, **kwargs)
