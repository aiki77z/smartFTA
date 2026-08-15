from __future__ import annotations

import json
from typing import Any, Dict, List

from openai import OpenAI

from agents.repair_agent import RepairAgent as LegacyRepairAgent
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PARSE_MAX_TOKENS

from .experience_memory_agent import ExperienceMemoryAgent
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


class RepairAgent:
    name = "RepairAgent"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.legacy = LegacyRepairAgent()

    def run(
        self,
        state: Dict[str, Any],
        draft_tree_artifact: Dict[str, Any],
        validation_report: Dict[str, Any],
        *,
        chunks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        repair_attempt = int(state.get("repair_attempt_count") or 0) + 1
        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent=self.name,
            stage="repair",
            message=f"RepairAgent started experience-memory optimization (attempt {repair_attempt}).",
            progress=72,
            details={
                "draft_artifact_id": draft_tree_artifact.get("artifact_id"),
                "validation_artifact_id": validation_report.get("artifact_id"),
                "repair_attempt": repair_attempt,
                "memory": self._memory_hint(state),
            },
        )
        experience_memory = ExperienceMemoryAgent().run(state, draft_tree_artifact, validation_report)
        tool = self._select_repair_tool(validation_report, memory_hint=self._memory_hint(state))
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="repair",
            message=f"RepairAgent selected optimization tool: {tool}.",
            progress=73,
            tool=tool,
            details={"tool": tool, "repair_attempt": repair_attempt},
        )
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="repair",
            message=(
                "RepairAgent used experience memory to build patch "
                f"({len(experience_memory.get('patterns') or [])} patterns, "
                f"{len(experience_memory.get('corrections') or [])} corrections)."
            ),
            progress=74,
            tool="experience_memory",
            details={
                "pattern_count": len(experience_memory.get("patterns") or []),
                "correction_count": len(experience_memory.get("corrections") or []),
            },
        )
        repair = self._call_repair_tool(
            run_id,
            tool,
            draft_tree_artifact,
            validation_report,
            scope_key=str(state.get("scope_key") or ""),
            chunks=chunks,
            repair_attempt=repair_attempt,
            experience_memory=experience_memory,
        )
        payload = repair.get("payload") or {}
        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent=self.name,
            stage="repair",
            message="RepairAgent completed experience-memory optimization.",
            progress=82,
            details={
                "status": payload.get("status"),
                "repair_patch_artifact_id": payload.get("artifact_id"),
                "draft_artifact_id": payload.get("draft_artifact_id"),
                "human_review_required": bool(repair.get("human_review_required")),
            },
        )
        repair["experience_memory"] = experience_memory
        return repair

    def _select_repair_tool(self, validation_report: Dict[str, Any], *, memory_hint: Dict[str, Any]) -> str:
        allowed = [tool.name for tool in self.registry.list_for_agent(self.name)]
        fallback = "apply_repair_patch"
        if not LLM_API_KEY:
            return fallback
        issues = validation_report.get("issues") or []
        prompt = {
            "role": "user",
            "content": (
                "You are RepairAgent for a fault-tree generation system. "
                "Choose one repair tool from the allowed list. "
                "Prefer find_repair_patterns when historical repair knowledge is important; "
                "prefer apply_repair_patch when validator issues are directly repairable. "
                f"Allowed tools: {allowed}\n"
                f"Error count: {validation_report.get('error_count', 0)}\n"
                f"Repairable issues: {len([i for i in issues if isinstance(i, dict) and i.get('repairable')])}\n"
                f"Runtime memory summary: {json.dumps(memory_hint, ensure_ascii=False)}\n"
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
            if tool in allowed and tool in {"find_repair_patterns", "apply_repair_patch", "validate_tree"}:
                return tool
        except Exception:
            return fallback
        return fallback

    def _memory_hint(self, state: Dict[str, Any]) -> Dict[str, Any]:
        memory = state.get("memory_context") if isinstance(state.get("memory_context"), dict) else {}
        return {
            "previous_artifact_types": sorted(((memory.get("artifacts") or {}).get("type_counts") or {}).keys()),
            "previous_event_count": (memory.get("events") or {}).get("event_count", 0),
            "repair_attempt_count": state.get("repair_attempt_count"),
        }

    def _call_repair_tool(
        self,
        run_id: str,
        tool: str,
        draft_tree_artifact: Dict[str, Any],
        validation_report: Dict[str, Any],
        *,
        scope_key: str,
        chunks: List[Dict[str, Any]],
        repair_attempt: int,
        experience_memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.registry.assert_allowed(self.name, tool)
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message=f"RepairAgent started optimization tool: {tool}.",
            progress=75,
        )
        try:
            repair = self.legacy.run(
                draft_tree_artifact,
                validation_report,
                run_id=run_id,
                scope_key=scope_key,
                chunks=chunks,
                repair_attempt=repair_attempt,
                persist_artifact=True,
                apply_legacy_repair=True,
                experience_patterns=experience_memory.get("patterns") or [],
                experience_corrections=experience_memory.get("corrections") or [],
            )
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message=f"RepairAgent optimization tool failed: {tool}.",
                reason=str(exc),
            )
            raise
        payload = repair.get("payload") or {}
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage="repair",
            message=f"RepairAgent completed optimization tool: {tool}.",
            progress=80,
            artifact_type="repair_patch",
            artifact_id=payload.get("artifact_id"),
            details={
                "status": payload.get("status"),
                "draft_artifact_id": payload.get("draft_artifact_id"),
                "human_review_required": bool(repair.get("human_review_required")),
                "experience_pattern_count": len(experience_memory.get("patterns") or []),
                "experience_correction_count": len(experience_memory.get("corrections") or []),
            },
        )
        return repair
