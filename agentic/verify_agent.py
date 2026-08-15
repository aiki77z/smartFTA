from __future__ import annotations

import json
from typing import Any, Dict

from openai import OpenAI

from agents.verify_agent import VerifyAgent as LegacyVerifyAgent
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PARSE_MAX_TOKENS

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


client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


class VerifyAgent:
    name = "VerifyAgent"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.legacy = LegacyVerifyAgent()

    def run(self, state: Dict[str, Any], draft_tree_artifact: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent=self.name,
            stage="validate",
            message="VerifyAgent started validation planning.",
            progress=55,
            details={"draft_artifact_id": draft_tree_artifact.get("artifact_id")},
        )
        selected_tool = self._select_validation_tool(draft_tree_artifact)
        final_tool = "validate_tree_full"
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="validate",
            message=f"VerifyAgent selected validation tool: {selected_tool}.",
            progress=56,
            tool=selected_tool,
            details={
                "selected_tool": selected_tool,
                "executed_tool": final_tool,
                "quality_gate": "full validation is mandatory before repair or commit",
            },
        )
        verification = self._call_validation_tool(
            run_id,
            final_tool,
            draft_tree_artifact,
            scope_key=str(state.get("scope_key") or ""),
        )
        report = verification.get("payload") or {}
        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent=self.name,
            stage="validate",
            message="VerifyAgent completed required validation quality gate.",
            progress=68,
            details={
                "passed": bool(report.get("passed")),
                "error_count": int(report.get("error_count") or 0),
                "warning_count": int(report.get("warning_count") or 0),
                "artifact_id": report.get("artifact_id"),
                "next_stage": verification.get("next_stage"),
            },
        )
        return verification

    def _select_validation_tool(self, draft_tree_artifact: Dict[str, Any]) -> str:
        allowed = [tool.name for tool in self.registry.list_for_agent(self.name)]
        fallback = "validate_tree_full"
        if not LLM_API_KEY:
            return fallback
        payload = draft_tree_artifact.get("payload") or draft_tree_artifact.get("content") or {}
        prompt = {
            "role": "user",
            "content": (
                "You are VerifyAgent for a fault-tree generation system. "
                "Choose one validation tool from the allowed list. "
                "The final quality gate must include full validation before commit. "
                f"Allowed tools: {allowed}\n"
                f"Draft node count: {len(payload.get('nodeList') or []) if isinstance(payload, dict) else 0}\n"
                f"Draft link count: {len(payload.get('linkList') or []) if isinstance(payload, dict) else 0}\n"
                "Return only JSON: {\"tool\": \"...\"}"
            ),
        }
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[prompt],
                temperature=0.0,
                max_tokens=min(LLM_PARSE_MAX_TOKENS, 120),
            )
            parsed = json.loads(response.choices[0].message.content or "{}")
            tool = str(parsed.get("tool") or "")
            if tool in allowed and tool in {"validate_tree", "validate_tree_full", "validate_tree_structural"}:
                return tool
        except Exception:
            return fallback
        return fallback

    def _call_validation_tool(
        self,
        run_id: str,
        tool: str,
        draft_tree_artifact: Dict[str, Any],
        *,
        scope_key: str,
    ) -> Dict[str, Any]:
        self.registry.assert_allowed(self.name, tool)
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage="validate",
            message=f"VerifyAgent started tool: {tool}.",
            progress=58,
        )
        try:
            verification = self.legacy.run(
                draft_tree_artifact,
                run_id=run_id,
                scope_key=scope_key,
                skip_semantic=False,
                persist_artifact=True,
            )
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool=tool,
                stage="validate",
                message=f"VerifyAgent tool failed: {tool}.",
                reason=str(exc),
            )
            raise
        report = verification.get("payload") or {}
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage="validate",
            message=f"VerifyAgent completed tool: {tool}.",
            progress=66,
            artifact_type="validation_report",
            artifact_id=report.get("artifact_id"),
            details={
                "passed": bool(report.get("passed")),
                "error_count": int(report.get("error_count") or 0),
                "warning_count": int(report.get("warning_count") or 0),
                "human_review_required": bool(verification.get("human_review_required")),
            },
        )
        return verification
