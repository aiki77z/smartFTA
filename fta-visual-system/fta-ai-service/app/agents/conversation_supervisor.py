from typing import Any, Dict


class ConversationSupervisorAgent:
    name = "ConversationSupervisorAgent"

    def route(self, intent_decision: Dict[str, Any]) -> Dict[str, Any]:
        intent = str((intent_decision or {}).get("intent") or "chat")
        if intent in {"generate_tree", "regenerate_tree"}:
            route = "GenerateAgent"
        elif intent == "edit_tree":
            route = "EditAgent"
        elif intent == "need_clarification":
            route = self.name
        else:
            route = "ChatAgent"
        return {
            "route": route,
            "intent": intent,
            "reason": (intent_decision or {}).get("reason") or "",
        }


conversation_supervisor = ConversationSupervisorAgent()
