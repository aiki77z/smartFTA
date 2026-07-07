import json
from typing import Any, Dict, List, Optional

from app.config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class ConversationSummaryAgent:
    name = "ConversationSummaryAgent"

    def summarize(
        self,
        *,
        recent_messages: List[Dict[str, Any]],
        previous_summary: Any,
        tree_summary: Dict[str, Any],
        latest_intent: str,
        latest_assistant_message: str,
        selected_file_version_ids: List[str],
    ) -> Dict[str, Any]:
        try:
            return self._call_llm_summary(
                recent_messages=recent_messages,
                previous_summary=previous_summary,
                tree_summary=tree_summary,
                latest_intent=latest_intent,
                latest_assistant_message=latest_assistant_message,
                selected_file_version_ids=selected_file_version_ids,
            )
        except Exception as exc:
            return self._fallback_summary(
                recent_messages=recent_messages,
                tree_summary=tree_summary,
                latest_intent=latest_intent,
                latest_assistant_message=latest_assistant_message,
                selected_file_version_ids=selected_file_version_ids,
                error=str(exc),
            )

    def _call_llm_summary(
        self,
        *,
        recent_messages: List[Dict[str, Any]],
        previous_summary: Any,
        tree_summary: Dict[str, Any],
        latest_intent: str,
        latest_assistant_message: str,
        selected_file_version_ids: List[str],
    ) -> Dict[str, Any]:
        if OpenAI is None:
            raise RuntimeError("openai package not available")
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is missing")

        system = (
            "你是 ConversationSummaryAgent，负责为故障树画布 AI 助手维护稳定、可压缩的会话记忆。\n"
            "请根据 previous_summary、最近消息、当前故障树摘要，输出一个 JSON 对象。\n"
            "不要照抄长消息，不要包含无关寒暄，不要写 markdown。\n"
            "摘要要保留后续对话和意图判断真正需要的信息：用户目标、当前树、最近一次生成/修改/校验、"
            "待确认动作、用户偏好、未解决问题、知识库来源。\n"
            "如果最近出现误判或空编辑草案（新增0/修改0/删除0），不要把它当作有效修改；可以作为注意事项记录。\n"
            "JSON 字段必须包含：active_goal, current_tree, last_user_intent, last_assistant_action, "
            "last_meaningful_edit, pending_action, user_preferences, unresolved_questions, source_scope, cautions, brief。"
        )
        payload = {
            "previous_summary": previous_summary,
            "recent_messages": self._compact_messages(recent_messages),
            "tree_summary": tree_summary,
            "latest_intent": latest_intent,
            "latest_assistant_message": latest_assistant_message,
            "selected_file_version_ids": selected_file_version_ids,
        }
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content or "{}"
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise RuntimeError("summary LLM returned non-object JSON")
        return self._normalize_summary(parsed)

    def _fallback_summary(
        self,
        *,
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        latest_intent: str,
        latest_assistant_message: str,
        selected_file_version_ids: List[str],
        error: str,
    ) -> Dict[str, Any]:
        last_user = ""
        for row in reversed(recent_messages):
            if row.get("role") == "user":
                last_user = str(row.get("content") or "")
                break
        top = tree_summary.get("top_event") or ""
        brief = (
            f"最近意图：{latest_intent}；"
            f"当前顶事件：{top or '未确定'}；"
            f"节点数：{tree_summary.get('node_count', 0)}；"
            f"最近用户：{last_user[:120]}；"
            f"最近回复：{latest_assistant_message[:160]}"
        )
        return self._normalize_summary({
            "active_goal": "",
            "current_tree": {
                "top_event": top,
                "node_count": tree_summary.get("node_count", 0),
                "edge_count": tree_summary.get("edge_count", 0),
            },
            "last_user_intent": latest_intent,
            "last_assistant_action": latest_assistant_message,
            "last_meaningful_edit": {},
            "pending_action": "",
            "user_preferences": [],
            "unresolved_questions": [],
            "source_scope": selected_file_version_ids,
            "cautions": [f"summary_fallback: {error}"],
            "brief": brief,
        })

    def _normalize_summary(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "active_goal": raw.get("active_goal") or "",
            "current_tree": raw.get("current_tree") if isinstance(raw.get("current_tree"), dict) else {},
            "last_user_intent": raw.get("last_user_intent") or "",
            "last_assistant_action": raw.get("last_assistant_action") or "",
            "last_meaningful_edit": raw.get("last_meaningful_edit") if isinstance(raw.get("last_meaningful_edit"), dict) else {},
            "pending_action": raw.get("pending_action") or "",
            "user_preferences": raw.get("user_preferences") if isinstance(raw.get("user_preferences"), list) else [],
            "unresolved_questions": raw.get("unresolved_questions") if isinstance(raw.get("unresolved_questions"), list) else [],
            "source_scope": raw.get("source_scope") if isinstance(raw.get("source_scope"), list) else [],
            "cautions": raw.get("cautions") if isinstance(raw.get("cautions"), list) else [],
            "brief": raw.get("brief") or "",
        }

    def _compact_messages(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compact: List[Dict[str, Any]] = []
        for row in rows[-20:]:
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            item: Dict[str, Any] = {
                "role": row.get("role"),
                "content": str(row.get("content") or "")[:900],
                "intent": meta.get("intent"),
            }
            if meta.get("pending_id"):
                item["pending_id"] = meta.get("pending_id")
            decision = meta.get("intent_decision") if isinstance(meta.get("intent_decision"), dict) else {}
            if decision:
                item["requirements"] = decision.get("requirements")
                item["reason"] = decision.get("reason")
            result = meta.get("result") if isinstance(meta.get("result"), dict) else {}
            if result:
                item["result_mode"] = result.get("mode")
                item["tree_id"] = result.get("tree_id")
                item["requested_top_event"] = result.get("requested_top_event")
            compact.append(item)
        return compact


conversation_summary_agent = ConversationSummaryAgent()
