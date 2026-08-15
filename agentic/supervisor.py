from __future__ import annotations

from typing import Any, Dict

from .router import guard_supervisor_action, is_waiting_human_confirmation
from .schemas import SupervisorDecision


class SupervisorAgent:
    name = "SupervisorAgent"

    def decide(self, state: Dict[str, Any]) -> SupervisorDecision:
        if is_waiting_human_confirmation(state):
            proposed = "wait_human_confirmation"
            reason = "The run is waiting for a human top-event confirmation."
        else:
            proposed = "run_agentic_workflow"
            reason = "Agentic v2 should execute the controlled LangGraph multi-agent workflow."
        action = guard_supervisor_action(state, proposed)
        return SupervisorDecision(
            next_action=action,
            reason=reason,
            confidence=1.0,
            metadata={
                "current_stage": state.get("current_stage"),
                "status": state.get("status"),
                "agentic_version": "v2",
            },
        )
