from __future__ import annotations

from typing import Any, Dict, Optional

from agent_runtime.artifact_store import put_agent_artifact
from agent_runtime.event_store import append_agent_event
from agent_runtime.policies import ARTIFACT_VALIDATION_REPORT, EVENT_VALIDATION_DONE, STAGE_VALIDATE
from tools.validator_tools import normalize_validation_report, supplement_structural_issues
from validator import validate_full


class VerifyAgent:
    """Adapter that turns the existing validator output into a stable ValidationReport."""

    name = "VerifyAgent"

    def run(
        self,
        draft_tree_artifact: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
        scope_key: str = "",
        skip_semantic: bool = True,
        persist_artifact: bool = False,
    ) -> Dict[str, Any]:
        tree_data = _extract_tree_data(draft_tree_artifact)
        raw_report = validate_full(tree_data, skip_semantic=skip_semantic, include_meta=True)
        raw_report = supplement_structural_issues(tree_data, raw_report)
        report = normalize_validation_report(
            raw_report,
            scope_key=scope_key,
            draft_artifact_id=str(draft_tree_artifact.get("artifact_id") or "") or None,
            run_id=run_id,
            source="validate_full",
        )
        artifact = None
        if persist_artifact and run_id:
            artifact = put_agent_artifact(
                run_id=run_id,
                artifact_type=ARTIFACT_VALIDATION_REPORT,
                content=report,
                metadata={"producer": self.name},
                producer=self.name,
                parent_artifact_id=str(draft_tree_artifact.get("artifact_id") or "") or None,
            )
            report["artifact_id"] = artifact.get("artifact_id")
            append_agent_event(
                run_id,
                EVENT_VALIDATION_DONE,
                stage=STAGE_VALIDATE,
                message=_validation_message(report),
                payload={
                    "artifact_id": artifact.get("artifact_id"),
                    "passed": report["passed"],
                    "error_count": report["error_count"],
                    "warning_count": report["warning_count"],
                    "info_count": report["info_count"],
                },
            )
        return {
            "agent": self.name,
            "artifact_type": ARTIFACT_VALIDATION_REPORT,
            "payload": report,
            "event_type": EVENT_VALIDATION_DONE,
            "event_payload": {
                "passed": report["passed"],
                "error_count": report["error_count"],
                "warning_count": report["warning_count"],
                "info_count": report["info_count"],
                "artifact_id": report.get("artifact_id"),
            },
            "next_stage": "commit" if report["passed"] else "repair",
            "requires_confirmation": False,
            "human_review_required": bool(report["summary"]["blocking_error_count"]),
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


def _validation_message(report: Dict[str, Any]) -> str:
    if report.get("passed"):
        return "Validation passed"
    return (
        "Validation found "
        f"{report.get('error_count', 0)} errors, "
        f"{report.get('warning_count', 0)} warnings"
    )


def run_verify_agent(*args, **kwargs) -> Dict[str, Any]:
    return VerifyAgent().run(*args, **kwargs)
