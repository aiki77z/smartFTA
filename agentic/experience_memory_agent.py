from __future__ import annotations

import json
from typing import Any, Dict, List

from diff_analyzer import get_relevant_corrections
from tools.correction_tools import find_active_repair_patterns

from .tool_events import (
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_STARTED,
    EVENT_TOOL_COMPLETED,
    EVENT_TOOL_FAILED,
    EVENT_TOOL_SELECTED,
    EVENT_TOOL_STARTED,
    append_agentic_event,
    append_tool_event,
)
from .tool_registry import ToolRegistry, build_default_registry


class ExperienceMemoryAgent:
    name = "ExperienceMemoryAgent"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def run(
        self,
        state: Dict[str, Any],
        draft_tree_artifact: Dict[str, Any],
        validation_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        repairable_issues = [
            issue
            for issue in (validation_report.get("issues") or [])
            if isinstance(issue, dict) and issue.get("repairable") and issue.get("severity") == "error"
        ]
        issue_codes = [str(issue.get("issue_code") or "").upper() for issue in repairable_issues]
        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent=self.name,
            stage="repair",
            message="ExperienceMemoryAgent started repair experience retrieval.",
            progress=71,
            details={
                "issue_codes": issue_codes,
                "scope_key": state.get("scope_key"),
                "repairable_issue_count": len(repairable_issues),
            },
        )

        patterns = self._find_patterns(run_id, issue_codes, str(state.get("scope_key") or ""))
        corrections = self._get_corrections(run_id, _extract_tree_data(draft_tree_artifact))
        memory = {
            "patterns": patterns,
            "corrections": corrections,
            "issue_codes": issue_codes,
            "repairable_issue_count": len(repairable_issues),
        }
        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent=self.name,
            stage="repair",
            message=(
                "ExperienceMemoryAgent retrieved "
                f"{len(patterns)} repair patterns and {len(corrections)} correction records."
            ),
            progress=74,
            details={
                "pattern_count": len(patterns),
                "correction_count": len(corrections),
                "issue_codes": issue_codes,
            },
        )
        return memory

    def _find_patterns(self, run_id: str, issue_codes: List[str], scope_key: str) -> List[Dict[str, Any]]:
        tool = "find_repair_patterns"
        self.registry.assert_allowed(self.name, tool)
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="repair",
            message="ExperienceMemoryAgent selected find_repair_patterns.",
            progress=72,
            tool=tool,
            details={"issue_codes": issue_codes},
        )
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message="ExperienceMemoryAgent started tool: find_repair_patterns.",
            progress=72,
        )
        try:
            patterns = find_active_repair_patterns(issue_codes=issue_codes, scope_key=scope_key, limit=10)
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool=tool,
                stage="repair",
                message="ExperienceMemoryAgent tool failed: find_repair_patterns.",
                reason=str(exc),
            )
            raise
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message=f"ExperienceMemoryAgent retrieved {len(patterns)} repair patterns.",
            progress=73,
            details={"pattern_count": len(patterns), "sample": _compact_sample(patterns)},
        )
        return patterns

    def _get_corrections(self, run_id: str, tree_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        tool = "get_relevant_corrections"
        self.registry.assert_allowed(self.name, tool)
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="repair",
            message="ExperienceMemoryAgent selected get_relevant_corrections.",
            progress=73,
            tool=tool,
        )
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message="ExperienceMemoryAgent started tool: get_relevant_corrections.",
            progress=73,
        )
        try:
            corrections = get_relevant_corrections(tree_data, max_distinct=20)
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool=tool,
                stage="repair",
                message="ExperienceMemoryAgent tool failed: get_relevant_corrections.",
                reason=str(exc),
            )
            raise
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message=f"ExperienceMemoryAgent retrieved {len(corrections)} correction records.",
            progress=74,
            details={"correction_count": len(corrections), "sample": _compact_sample(corrections)},
        )
        return corrections


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


def _compact_sample(items: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    sample = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compact = {
            key: item.get(key)
            for key in ("issue_code", "description", "summary", "status", "source", "tree_id", "top_event")
            if item.get(key) not in (None, "")
        }
        if not compact:
            text = json.dumps(item, ensure_ascii=False, default=str)
            compact = {"preview": text[:160]}
        sample.append(compact)
    return sample
