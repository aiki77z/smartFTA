from typing import Any, Dict, List

from app.memory.session_store import session_store
from app.schemas import AssistantMessageRequest, AssistantMessageResponse
from app.tools.kb_tools import knowledge_reader_tool


class ChatAgent:
    name = "ChatAgent"

    def handle_chat(
        self,
        *,
        owner: Any,
        req: AssistantMessageRequest,
        session_id: str,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        intent_decision: Dict[str, Any],
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        trace.append(owner._step(self.name, "running", {"mode": "chat"}))
        top_event = tree_summary.get("top_event") or previous_memory.get("current_top_event") or "当前故障树"
        assistant_message = owner._build_chat_reply(
            req.message,
            top_event,
            tree_summary,
            previous_memory,
            recent_messages,
            intent_decision,
        )
        session_store.append_message(session_id, "assistant", assistant_message, {"intent": "chat"})
        memory = owner._update_memory(
            req,
            session_id,
            "chat",
            tree_summary,
            owner._normalize_ids(req.selected_file_version_ids),
            assistant_message,
        )
        trace.append(owner._step(self.name, "success", {"mode": "chat"}))
        return AssistantMessageResponse(
            session_id=session_id,
            intent="chat",
            action="chat",
            assistant_message=assistant_message,
            memory=memory,
            agent_trace=trace,
        )

    def handle_knowledge_query(
        self,
        *,
        owner: Any,
        req: AssistantMessageRequest,
        session_id: str,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        scope_ids: List[str],
        intent_decision: Dict[str, Any],
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        intent = str(intent_decision.get("intent") or "query_knowledge")
        if intent not in {"query_knowledge", "explain_evidence"}:
            intent = "query_knowledge"
        if not scope_ids:
            assistant_message = "我可以读取已选择文件的知识库摘要和 chunks，但当前还没有选中的知识库文件。请先在页面里选择一个或多个已导入文件。"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": intent})
            memory = owner._update_memory(req, session_id, intent, tree_summary, scope_ids, assistant_message)
            return AssistantMessageResponse(
                session_id=session_id,
                intent=intent,
                action="need_source_files",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        kb_context: Dict[str, Any] = {"top_events": {}, "chunks": {}}
        trace.append(owner._step("KnowledgeReaderTool", "running", {"scope_count": len(scope_ids)}))
        try:
            kb_context["top_events"] = knowledge_reader_tool.top_event_candidates(scope_ids, limit=10)
            kb_context["chunks"] = knowledge_reader_tool.chunks_preview(scope_ids, limit=6)
            if intent == "explain_evidence":
                node_query = owner._extract_node_query(req.message, intent_decision, tree_summary)
                kb_context["node_evidence"] = knowledge_reader_tool.node_evidence(
                    tree_json=req.current_tree,
                    node_query=node_query,
                    selected_file_version_ids=scope_ids,
                    max_chunks=5,
                )
            elif intent == "query_knowledge":
                kb_context["graph_context"] = knowledge_reader_tool.graph_question(
                    question=req.message,
                    selected_file_version_ids=scope_ids,
                    limit=20,
                )
            trace.append(owner._step("KnowledgeReaderTool", "success", {
                "top_event_count": len(kb_context["top_events"].get("items") or []),
                "chunk_count": len(kb_context["chunks"].get("chunks") or []),
                "has_node_evidence": bool(kb_context.get("node_evidence")),
                "has_graph_context": bool(kb_context.get("graph_context")),
            }))
        except Exception as exc:
            trace.append(owner._step("KnowledgeReaderTool", "failed", {"error": str(exc)}))
            kb_context["error"] = str(exc)

        assistant_message = owner._build_knowledge_reply(
            message=req.message,
            previous_memory=previous_memory,
            recent_messages=recent_messages,
            tree_summary=tree_summary,
            intent_decision=intent_decision,
            kb_context=kb_context,
        )
        session_store.append_message(session_id, "assistant", assistant_message, {
            "intent": intent,
            "kb_context": kb_context,
        })
        memory = owner._update_memory(req, session_id, intent, tree_summary, scope_ids, assistant_message)
        return AssistantMessageResponse(
            session_id=session_id,
            intent=intent,
            action="knowledge_read",
            assistant_message=assistant_message,
            result={"knowledge_context": kb_context},
            memory=memory,
            agent_trace=trace,
        )


chat_agent = ChatAgent()
