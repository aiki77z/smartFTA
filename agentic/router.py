from __future__ import annotations

from typing import Any, Dict


def is_agentic_v2_enabled(options: Dict[str, Any]) -> bool:
    return isinstance(options, dict) and str(options.get("agentic_version") or "").lower() == "v2"


def is_waiting_human_confirmation(state: Dict[str, Any]) -> bool:
    confirmation = state.get("confirmation")
    if not isinstance(confirmation, dict):
        return False
    confirmation_status = str(confirmation.get("status") or "").lower()
    run_status = str(state.get("status") or "").lower()
    return confirmation_status == "waiting" or run_status == "waiting_confirmation"


def guard_supervisor_action(state: Dict[str, Any], proposed_action: str) -> str:
    """Keep v2 deterministic by allowing only known global routes."""
    allowed = {"run_agentic_workflow", "wait_human_confirmation", "human_review"}
    if proposed_action not in allowed:
        return "run_agentic_workflow"
    if is_waiting_human_confirmation(state):
        return "wait_human_confirmation"
    return proposed_action
