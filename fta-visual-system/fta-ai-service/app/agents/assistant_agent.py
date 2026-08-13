import json
import time
from typing import Any, Dict, List, Optional

from app.config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL
from app.agents.conversation_summary_agent import conversation_summary_agent
from app.memory.session_store import session_store
from app.schemas import AssistantMessageRequest, AssistantMessageResponse, EditRequest
from app.services.edit_plan_service import plan_edit_fault_tree
from app.services.tree_utils import summarize_tree
from app.tools.gnr_client import gnr_client
from app.tools.kb_tools import knowledge_reader_tool

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

VALID_INTENTS = {
    "chat",
    "generate_tree",
    "regenerate_tree",
    "edit_tree",
    "validate_tree",
    "query_knowledge",
    "explain_evidence",
    "need_clarification",
}


class AssistantAgent:
    name = "AssistantAgent"

    def handle_message(self, req: AssistantMessageRequest) -> AssistantMessageResponse:
        started = time.time()
        message = (req.message or "").strip()
        if not message:
            raise ValueError("message is required")

        session_id = req.session_id or session_store.make_session_id(req.project_id, req.canvas_id)
        previous_memory = session_store.get_session(session_id)
        recent_messages = session_store.list_messages(session_id, limit=12)
        tree_summary = summarize_tree(req.current_tree)
        has_tree = bool(tree_summary.get("node_count"))
        scope_ids = self._normalize_ids(req.selected_file_version_ids)
        baseline_ids = self._normalize_ids(req.baseline_file_version_ids)
        source_changed = bool(scope_ids and baseline_ids and sorted(scope_ids) != sorted(baseline_ids))

        trace: List[Dict[str, Any]] = []
        trace.append(self._step("ContextAgent", "success", {
            "has_tree": has_tree,
            "source_changed": source_changed,
            "tree_summary": tree_summary,
            "selected_file_version_ids": scope_ids,
        }))

        intent_decision = self._resolve_intent(
            req=req,
            previous_memory=previous_memory,
            recent_messages=recent_messages,
            tree_summary=tree_summary,
            has_tree=has_tree,
            source_changed=source_changed,
        )
        intent = intent_decision["intent"]
        trace.append(self._step("IntentAgent", "success", {"intent": intent}))

        session_store.append_message(
            session_id,
            "user",
            message,
            {
                "intent": intent,
                "intent_decision": intent_decision,
                "frontend_message_id": req.frontend_message_id,
            },
        )

        try:
            if intent in {"generate_tree", "regenerate_tree"}:
                response = self._handle_generate(req, session_id, message, scope_ids, intent, trace)
            elif intent == "need_clarification":
                response = self._handle_need_clarification(req, session_id, intent_decision, tree_summary, scope_ids, trace)
            elif intent == "validate_tree":
                response = self._handle_validate(req, session_id, trace)
            elif intent == "edit_tree":
                response = self._handle_edit(req, session_id, message, trace)
            elif intent in {"query_knowledge", "explain_evidence"}:
                response = self._handle_knowledge_query(req, session_id, previous_memory, recent_messages, tree_summary, scope_ids, intent_decision, trace)
            else:
                response = self._handle_chat(req, session_id, previous_memory, recent_messages, tree_summary, intent_decision, trace)
        except Exception as exc:
            trace.append(self._step("AssistantAgent", "failed", {"error": str(exc)}))
            assistant_message = f"处理失败：{exc}"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": intent, "error": str(exc)})
            memory = self._update_memory(req, session_id, intent, tree_summary, scope_ids, assistant_message)
            return AssistantMessageResponse(
                session_id=session_id,
                intent=intent,
                action="error",
                assistant_message=assistant_message,
                result={"error": str(exc)},
                memory=memory,
                agent_trace=trace,
            )

        response.agent_trace.append(self._step("AssistantAgent", "success", {
            "duration_seconds": round(time.time() - started, 3),
        }))
        return response

    def _handle_generate(
        self,
        req: AssistantMessageRequest,
        session_id: str,
        message: str,
        scope_ids: List[str],
        intent: str,
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        if not scope_ids:
            assistant_message = "请先选择至少一个已导入完成的知识库依据文件，我才能调度故障树生成。"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": intent})
            memory = self._update_memory(req, session_id, intent, summarize_tree(req.current_tree), scope_ids, assistant_message)
            return AssistantMessageResponse(
                session_id=session_id,
                intent=intent,
                action="need_source_files",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        trace.append(self._step("GenerationDispatchAgent", "running", {
            "target": "FTA-GNR",
            "mode": "agent_run" if self._has_agent_run_path() else "tree_generate_api",
        }))
        result = gnr_client.run_generation_agent(
            prompt=message,
            selected_file_version_ids=scope_ids,
            session_id=session_id,
            project_id=req.project_id,
            canvas_id=req.canvas_id,
        )
        trace.append(self._step("GenerationDispatchAgent", "success", {
            "response_mode": result.get("mode"),
            "job_id": result.get("job_id"),
            "item_id": result.get("item_id"),
            "tree_id": result.get("tree_id"),
        }))

        if result.get("mode") == "need_confirmation":
            assistant_message = "我找到了多个相似顶事件候选，需要你确认一个候选后再继续生成。"
            action = "need_top_event_confirmation"
        elif result.get("mode") == "queued":
            assistant_message = "已调度故障树生成任务。后续可根据返回的 job/item 继续轮询生成进度。"
            action = "generation_queued"
        elif result.get("tree_id"):
            assistant_message = f"已完成故障树生成并返回 tree_id={result.get('tree_id')}。"
            action = "generation_finished"
        else:
            assistant_message = "已提交故障树生成请求，但返回格式需要前端进一步处理。"
            action = "generation_submitted"

        session_store.append_message(session_id, "assistant", assistant_message, {"intent": intent, "result": result})
        memory = self._update_memory(req, session_id, intent, summarize_tree(req.current_tree), scope_ids, assistant_message, result)
        return AssistantMessageResponse(
            session_id=session_id,
            intent=intent,
            action=action,
            assistant_message=assistant_message,
            result=result,
            memory=memory,
            agent_trace=trace,
        )

    def _handle_edit(
        self,
        req: AssistantMessageRequest,
        session_id: str,
        message: str,
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        if req.current_tree is None:
            assistant_message = "当前没有可编辑的故障树。你可以先让我生成一棵故障树。"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": "edit_tree"})
            memory = self._update_memory(req, session_id, "edit_tree", {}, self._normalize_ids(req.selected_file_version_ids), assistant_message)
            return AssistantMessageResponse(
                session_id=session_id,
                intent="edit_tree",
                action="need_current_tree",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        trace.append(self._step("EditPlannerAgent", "success", {"strategy": "llm_edit_plan_with_legacy_fallback"}))
        edit_resp = plan_edit_fault_tree(EditRequest(
            instruction=message,
            tree_json=req.current_tree,
            selected_files=req.selected_files,
        ))
        pending = session_store.save_pending_action(session_id, {
            "kind": "edit_diff",
            "before_tree": req.current_tree,
            "after_tree": edit_resp.updated_tree_json,
            "diff": edit_resp.diff.dict(),
            "rationale": edit_resp.rationale,
        })
        trace.append(self._step("DiffAgent", "success", {"diff": edit_resp.diff.dict()}))

        assistant_message = (
            f"已生成编辑草案：新增 {len(edit_resp.diff.added)}，修改 {len(edit_resp.diff.modified)}，"
            f"删除 {len(edit_resp.diff.removed)}。请在前端确认 Accept 或 Undo。"
        )
        session_store.append_message(session_id, "assistant", assistant_message, {
            "intent": "edit_tree",
            "pending_id": pending.get("pending_id"),
        })
        memory = self._update_memory(
            req,
            session_id,
            "edit_tree",
            summarize_tree(edit_resp.updated_tree_json),
            self._normalize_ids(req.selected_file_version_ids),
            assistant_message,
        )
        return AssistantMessageResponse(
            session_id=session_id,
            intent="edit_tree",
            action="edit_draft_created",
            assistant_message=assistant_message,
            result=edit_resp.dict(),
            pending_action=pending,
            memory=memory,
            agent_trace=trace,
        )

    def _handle_need_clarification(
        self,
        req: AssistantMessageRequest,
        session_id: str,
        intent_decision: Dict[str, Any],
        tree_summary: Dict[str, Any],
        scope_ids: List[str],
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        assistant_message = str(
            intent_decision.get("clarifying_question")
            or "你想让我生成故障树的话，请告诉我明确的顶事件，例如“设备突然重启”或“通信模块异常”。"
        )
        session_store.append_message(session_id, "assistant", assistant_message, {
            "intent": "need_clarification",
            "intent_decision": intent_decision,
        })
        memory = self._update_memory(req, session_id, "need_clarification", tree_summary, scope_ids, assistant_message)
        return AssistantMessageResponse(
            session_id=session_id,
            intent="need_clarification",
            action="need_clarification",
            assistant_message=assistant_message,
            result={"intent_decision": intent_decision},
            memory=memory,
            agent_trace=trace,
        )

    def _handle_validate(
        self,
        req: AssistantMessageRequest,
        session_id: str,
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        if req.current_tree is None:
            assistant_message = "当前没有可校验的故障树。"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": "validate_tree"})
            memory = self._update_memory(req, session_id, "validate_tree", {}, self._normalize_ids(req.selected_file_version_ids), assistant_message)
            return AssistantMessageResponse(
                session_id=session_id,
                intent="validate_tree",
                action="need_current_tree",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        result = gnr_client.validate_tree(tree_data=req.current_tree)
        trace.append(self._step("ValidationDispatchAgent", "success", {
            "passed": result.get("passed"),
            "error_count": result.get("error_count"),
            "warning_count": result.get("warning_count"),
        }))
        assistant_message = (
            f"校验完成：{'通过' if result.get('passed') else '未通过'}，"
            f"错误 {result.get('error_count', 0)} 个，警告 {result.get('warning_count', 0)} 个。"
        )
        session_store.append_message(session_id, "assistant", assistant_message, {"intent": "validate_tree", "result": result})
        memory = self._update_memory(req, session_id, "validate_tree", summarize_tree(req.current_tree), self._normalize_ids(req.selected_file_version_ids), assistant_message, result)
        return AssistantMessageResponse(
            session_id=session_id,
            intent="validate_tree",
            action="validation_finished",
            assistant_message=assistant_message,
            result=result,
            memory=memory,
            agent_trace=trace,
        )

    def _handle_chat(
        self,
        req: AssistantMessageRequest,
        session_id: str,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        intent_decision: Dict[str, Any],
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        top_event = tree_summary.get("top_event") or previous_memory.get("current_top_event") or "当前故障树"
        assistant_message = self._build_chat_reply(
            req.message,
            top_event,
            tree_summary,
            previous_memory,
            recent_messages,
            intent_decision,
        )
        session_store.append_message(session_id, "assistant", assistant_message, {"intent": "chat"})
        memory = self._update_memory(req, session_id, "chat", tree_summary, self._normalize_ids(req.selected_file_version_ids), assistant_message)
        return AssistantMessageResponse(
            session_id=session_id,
            intent="chat",
            action="chat",
            assistant_message=assistant_message,
            memory=memory,
            agent_trace=trace,
        )

    def _handle_knowledge_query(
        self,
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
            memory = self._update_memory(req, session_id, intent, tree_summary, scope_ids, assistant_message)
            return AssistantMessageResponse(
                session_id=session_id,
                intent=intent,
                action="need_source_files",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        kb_context: Dict[str, Any] = {"top_events": {}, "chunks": {}}
        trace.append(self._step("KnowledgeReaderTool", "running", {"scope_count": len(scope_ids)}))
        try:
            kb_context["top_events"] = knowledge_reader_tool.top_event_candidates(scope_ids, limit=10)
            kb_context["chunks"] = knowledge_reader_tool.chunks_preview(scope_ids, limit=6)
            if intent == "explain_evidence":
                node_query = self._extract_node_query(req.message, intent_decision, tree_summary)
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
            trace.append(self._step("KnowledgeReaderTool", "success", {
                "top_event_count": len(kb_context["top_events"].get("items") or []),
                "chunk_count": len(kb_context["chunks"].get("chunks") or []),
                "has_node_evidence": bool(kb_context.get("node_evidence")),
                "has_graph_context": bool(kb_context.get("graph_context")),
            }))
        except Exception as exc:
            trace.append(self._step("KnowledgeReaderTool", "failed", {"error": str(exc)}))
            kb_context["error"] = str(exc)

        assistant_message = self._build_knowledge_reply(
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
        memory = self._update_memory(req, session_id, intent, tree_summary, scope_ids, assistant_message)
        return AssistantMessageResponse(
            session_id=session_id,
            intent=intent,
            action="knowledge_read",
            assistant_message=assistant_message,
            result={"knowledge_context": kb_context},
            memory=memory,
            agent_trace=trace,
        )

    def _resolve_intent(
        self,
        *,
        req: AssistantMessageRequest,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        has_tree: bool,
        source_changed: bool,
    ) -> Dict[str, Any]:
        try:
            decision = self._call_llm_intent(
                message=req.message,
                previous_memory=previous_memory,
                recent_messages=recent_messages,
                tree_summary=tree_summary,
                has_tree=has_tree,
                source_changed=source_changed,
                force_generate=bool(req.force_generate),
            )
        except Exception as exc:
            return {
                "intent": "chat",
                "confidence": 0.0,
                "reason": f"LLM intent parsing failed, using safe chat fallback: {exc}",
                "top_event": "",
                "clarifying_question": "",
            }

        intent = str(decision.get("intent") or "chat").strip()
        if intent not in VALID_INTENTS:
            intent = "chat"

        top_event = str(decision.get("top_event") or "").strip()
        if intent in {"generate_tree", "regenerate_tree"}:
            # Guardrail: do not dispatch generation unless the model found a real top event
            # or this is an explicit regenerate request for an existing tree.
            if not top_event and not (intent == "regenerate_tree" and has_tree):
                intent = "need_clarification"
                decision["clarifying_question"] = (
                    decision.get("clarifying_question")
                    or "你想生成哪一个顶事件的故障树？请给出明确顶事件名称。"
                )
        if intent == "edit_tree" and not has_tree:
            intent = "need_clarification"
            decision["clarifying_question"] = (
                decision.get("clarifying_question")
                or "当前画布还没有可修改的故障树。你是想先生成一棵新故障树吗？如果是，请告诉我顶事件。"
            )

        decision["intent"] = intent
        decision.setdefault("top_event", top_event)
        decision.setdefault("confidence", 0.0)
        decision.setdefault("reason", "")
        return decision

    def _call_llm_intent(
        self,
        *,
        message: str,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        has_tree: bool,
        source_changed: bool,
        force_generate: bool,
    ) -> Dict[str, Any]:
        if OpenAI is None:
            raise RuntimeError("openai package not available")
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is missing")

        system = (
            "你是工业设备故障树可视化系统的 AssistantAgent 意图解析器。\n"
            "你的任务是理解用户这句话到底想让系统执行什么，而不是按关键词触发工具。\n"
            "只返回一个 JSON 对象，不要 markdown。\n\n"
            "可选 intent：\n"
            "- chat: 普通对话、询问能力、询问怎么使用、让你给示例、问如何表达需求、解释流程。\n"
            "  也包括询问历史上下文，例如“刚才做了什么”“你刚才帮我改了什么”“复述刚才的修改”。\n"
            "- generate_tree: 用户明确要求现在生成一棵新故障树，并且给出了明确顶事件或分析对象。\n"
            "- regenerate_tree: 用户明确要求基于当前树重新生成/按新知识库重建。\n"
            "- edit_tree: 用户明确要求现在修改当前故障树，例如增删节点、改名、调整连线或逻辑门。\n"
            "- validate_tree: 用户明确要求检查/校验/评估当前故障树。\n"
            "- explain_evidence: 用户询问节点依据、溯源、为什么这样生成。\n"
            "- need_clarification: 用户似乎想执行任务，但缺少必要信息，例如想生成但没有说明顶事件。\n\n"
            "关键边界：\n"
            "1. 用户问“我要怎么让你生成故障树”“如何表达生成需求”“给我一个生成示例”，这是 chat，不是 generate_tree。\n"
            "2. 只有当用户是在命令你现在生成，例如“为设备突然重启生成三层故障树”，才是 generate_tree。\n"
            "3. 不要因为句子里出现“生成”“故障树”就判定为生成。\n"
            "4. 如果用户想生成但没有明确顶事件，返回 need_clarification。\n"
            "5. 如果用户想修改但当前没有树，返回 need_clarification。\n\n"
            "6. 用户问“你刚才帮我做了什么修改”“刚才改了哪些地方”时，必须返回 chat；"
            "这是让你复述历史操作，不是让你再次修改故障树。\n"
            "7. edit_tree 只用于用户要求对当前树产生新的结构变化，例如“把A改成B”“新增一个节点”。\n\n"
            "JSON 字段：intent, confidence, top_event, requirements, reason, clarifying_question。"
        )
        system += (
            "\nAdditional intent: query_knowledge means the user asks about selected knowledge-base "
            "content independent of the current canvas tree, such as available top events, generation "
            "candidates, source files, or chunks. Return query_knowledge for questions like "
            "'知识库中有哪些顶事件可以让我生成故障树？'. This must not trigger generation or editing."
            "\nUse explain_evidence only when the user asks for evidence, source chunks, or rationale "
            "of the current fault tree or a specific node in the current tree. For explain_evidence, "
            "if a node/event name is mentioned, include node_label in the JSON."
        )
        user = {
            "message": message,
            "context": {
                "has_tree": has_tree,
                "source_changed": source_changed,
                "force_generate_requested_by_frontend": force_generate,
                "tree_summary": tree_summary,
                "previous_memory": {
                    "current_top_event": previous_memory.get("current_top_event"),
                    "last_user_intent": previous_memory.get("last_user_intent"),
                    "conversation_summary": previous_memory.get("conversation_summary"),
                },
                "recent_messages": self._compact_recent_messages(recent_messages),
                "last_meaningful_edit": self._last_meaningful_edit_summary(recent_messages),
            },
        }
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content or "{}"
        try:
            parsed = json.loads(content)
        except Exception as exc:
            raise RuntimeError(f"intent LLM returned non-JSON: {exc}; raw={content[:800]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("intent LLM returned non-object JSON")
        return parsed

    def _build_chat_reply(
        self,
        message: str,
        top_event: str,
        tree_summary: Dict[str, Any],
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        intent_decision: Dict[str, Any],
    ) -> str:
        try:
            return self._call_llm_chat_reply(
                message,
                top_event,
                tree_summary,
                previous_memory,
                recent_messages,
                intent_decision,
            )
        except Exception:
            if tree_summary.get("node_count"):
                prefix = f"当前画布里有 {tree_summary.get('node_count')} 个节点，顶事件看起来是「{top_event}」。"
            else:
                prefix = "当前画布还没有故障树。"
            return (
                f"{prefix}我可以帮你生成、修改、校验故障树，也可以解释如何描述生成需求。"
                "例如你可以说：“请基于当前知识库，为设备突然重启生成一棵三层故障树”。"
            )

    def _build_knowledge_reply(
        self,
        *,
        message: str,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        intent_decision: Dict[str, Any],
        kb_context: Dict[str, Any],
    ) -> str:
        try:
            return self._call_llm_knowledge_reply(
                message=message,
                previous_memory=previous_memory,
                recent_messages=recent_messages,
                tree_summary=tree_summary,
                intent_decision=intent_decision,
                kb_context=kb_context,
            )
        except Exception:
            if kb_context.get("error"):
                return f"我尝试读取当前选择范围内的知识库，但读取失败：{kb_context.get('error')}"
            tops = (kb_context.get("top_events") or {}).get("items") or []
            chunks = (kb_context.get("chunks") or {}).get("chunks") or []
            lines = ["我读取了当前已选择文件的知识库摘要，先给你一个有限范围的预览："]
            if tops:
                names = [str(x.get("name") or "") for x in tops[:5] if isinstance(x, dict) and x.get("name")]
                if names:
                    lines.append("候选顶事件：" + "；".join(names))
            if chunks:
                titles = [str(x.get("title") or x.get("source") or x.get("id") or "") for x in chunks[:4] if isinstance(x, dict)]
                titles = [x for x in titles if x]
                if titles:
                    lines.append("相关 chunks：" + "；".join(titles))
            if len(lines) == 1:
                lines.append("当前选择范围内没有读到可用的顶事件或 chunk 摘要。")
            return "\n".join(lines)

    def _extract_node_query(
        self,
        message: str,
        intent_decision: Dict[str, Any],
        tree_summary: Dict[str, Any],
    ) -> str:
        for key in ("node_label", "node_name", "target_node", "target_label", "event_name"):
            value = str(intent_decision.get(key) or "").strip()
            if value:
                return value
        requirements = intent_decision.get("requirements")
        if isinstance(requirements, dict):
            for key in ("node_label", "node_name", "target_node", "target_label", "event_name"):
                value = str(requirements.get(key) or "").strip()
                if value:
                    return value
        text = str(message or "").strip()
        for marker in ("节点", "事件", "“", "「", "\""):
            if marker in text:
                return text
        return str(tree_summary.get("top_event") or text or "").strip()

    def _call_llm_knowledge_reply(
        self,
        *,
        message: str,
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        tree_summary: Dict[str, Any],
        intent_decision: Dict[str, Any],
        kb_context: Dict[str, Any],
    ) -> str:
        if OpenAI is None:
            raise RuntimeError("openai package not available")
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is missing")
        system = (
            "你是工业设备故障树系统里的知识库只读问答助手。"
            "你只能根据给定的 kb_context、当前画布摘要和会话记忆回答。"
            "不要声称已经生成、修改或校验故障树；这一步只是读取知识库用于对话辅助与意图澄清。"
            "当 intent 是 query_knowledge 时，回答应围绕知识库中可用的候选顶事件、文件和 chunks；不要说这是在解释当前故障树。"
            "当 intent 是 explain_evidence 时，才围绕当前故障树或当前节点的依据进行说明。"
            "当 intent 是 explain_evidence 且 kb_context.node_evidence.chunks 为空时，必须明确说明没有找到直接证据 chunk；"
            "不要把候选顶事件、常识推断或相似故障域说成该节点的来源依据。"
            "如果 node_evidence.chunks 非空，优先引用这些 chunks 中的原文线索、chunk_id 和 source。"
            "如果证据不足，要明确说只是有限预览，并提示用户选择更具体的文件、节点或顶事件。"
            "回答要简洁，优先列出可用于后续生成故障树的候选顶事件、相关 chunks 或依据线索。"
        )
        user = {
            "message": message,
            "intent_decision": intent_decision,
            "context": {
                "tree_summary": tree_summary,
                "conversation_summary": previous_memory.get("conversation_summary"),
                "recent_messages": self._compact_recent_messages(recent_messages),
                "kb_context": kb_context,
            },
        }
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip() or "我读取了当前知识库范围，但没有得到可总结的内容。"

    def _call_llm_chat_reply(
        self,
        message: str,
        top_event: str,
        tree_summary: Dict[str, Any],
        previous_memory: Dict[str, Any],
        recent_messages: List[Dict[str, Any]],
        intent_decision: Dict[str, Any],
    ) -> str:
        if OpenAI is None:
            raise RuntimeError("openai package not available")
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is missing")
        system = (
            "你是工业设备故障树可视化系统右侧面板中的 AI 对话助手。"
            "你已经确认本轮只是 chat/help，不要声称已经开始生成、修改或校验。"
            "回答要简洁、自然、面向工程用户。"
            "如果用户询问如何让你生成故障树，请给出 2-3 个可直接复制的中文示例，"
            "并说明只有当用户明确给出顶事件并要求生成时，你才会调用生成流程。"
            "如果用户询问刚才做了什么修改，请根据 recent_messages 复述最近一次 edit_tree 用户请求和助手草案结果；"
            "不要调用修改工具，不要说又生成了新草案。"
        )
        user = {
            "message": message,
            "intent_decision": intent_decision,
            "context": {
                "top_event": top_event,
                "tree_summary": tree_summary,
                "conversation_summary": previous_memory.get("conversation_summary"),
                "recent_messages": self._compact_recent_messages(recent_messages),
                "last_meaningful_edit": self._last_meaningful_edit_summary(recent_messages),
            },
        }
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip() or "我可以帮你生成、编辑和校验故障树。"

    def _compact_recent_messages(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compact: List[Dict[str, Any]] = []
        for row in rows[-12:]:
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            item: Dict[str, Any] = {
                "role": row.get("role"),
                "content": str(row.get("content") or "")[:800],
                "intent": meta.get("intent"),
            }
            if meta.get("pending_id"):
                item["pending_id"] = meta.get("pending_id")
            decision = meta.get("intent_decision") if isinstance(meta.get("intent_decision"), dict) else {}
            if decision:
                item["intent_reason"] = decision.get("reason")
                item["requirements"] = decision.get("requirements")
            result = meta.get("result") if isinstance(meta.get("result"), dict) else {}
            if result:
                item["result_mode"] = result.get("mode")
                item["tree_id"] = result.get("tree_id")
            compact.append(item)
        return compact

    def _last_meaningful_edit_summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        last_user: Optional[Dict[str, Any]] = None
        pairs: List[Dict[str, Any]] = []
        for row in rows:
            if row.get("role") == "user":
                last_user = row
                continue
            if row.get("role") != "assistant":
                continue
            content = str(row.get("content") or "")
            if "已生成编辑草案" not in content:
                continue
            if "新增 0" in content and "修改 0" in content and "删除 0" in content:
                continue
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            pairs.append({
                "user_request": str((last_user or {}).get("content") or ""),
                "assistant_result": content,
                "pending_id": meta.get("pending_id"),
            })
        return pairs[-1] if pairs else {}

    def _update_memory(
        self,
        req: AssistantMessageRequest,
        session_id: str,
        intent: str,
        tree_summary: Dict[str, Any],
        scope_ids: List[str],
        assistant_message: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = result or {}
        agent_run_payload = result.get("agent_run") if isinstance(result.get("agent_run"), dict) else {}
        if not agent_run_payload and isinstance(result, dict) and result.get("run_id"):
            agent_run_payload = result
        if not isinstance(agent_run_payload, dict):
            agent_run_payload = {}

        previous_memory = session_store.get_session(session_id)
        recent_messages = session_store.list_messages(session_id, limit=20)
        conversation_summary = conversation_summary_agent.summarize(
            recent_messages=recent_messages,
            previous_summary=previous_memory.get("conversation_summary"),
            tree_summary=tree_summary,
            latest_intent=intent,
            latest_assistant_message=assistant_message,
            selected_file_version_ids=scope_ids,
        )
        agent_run_id = agent_run_payload.get("run_id") or result.get("run_id") or previous_memory.get("agent_run_id")
        agent_run_status = str(agent_run_payload.get("status") or result.get("status") or "").lower()
        agent_run_mode = str(agent_run_payload.get("mode") or result.get("mode") or "").lower()
        agent_run_confirmation = agent_run_payload.get("confirmation") or result.get("confirmation")
        if agent_run_status in {"completed", "failed", "cancelled"}:
            pending_confirmation = None
        elif agent_run_status == "waiting_confirmation" or agent_run_mode == "need_confirmation":
            pending_confirmation = agent_run_confirmation
        elif intent == "generate_tree":
            pending_confirmation = None
        else:
            pending_confirmation = previous_memory.get("pending_confirmation")

        last_generation_result = (
            result
            if intent == "generate_tree"
            else previous_memory.get("last_generation_result")
        )
        patch = {
            "project_id": req.project_id,
            "canvas_id": req.canvas_id,
            "current_tree_id": (
                agent_run_payload.get("tree_id")
                or result.get("tree_id")
                or req.current_tree_id
            ),
            "current_tree_version": (
                agent_run_payload.get("tree_version")
                if agent_run_payload.get("tree_version") is not None
                else (
                    result.get("tree_version")
                    if result.get("tree_version") is not None
                    else previous_memory.get("current_tree_version")
                )
            ),
            "current_top_event": (
                tree_summary.get("top_event")
                or agent_run_payload.get("resolved_top_event")
                or result.get("resolved_top_event")
            ),
            "selected_file_version_ids": scope_ids,
            "source_scope_key": "|".join(sorted(scope_ids)),
            "agent_run_id": agent_run_id,
            "agent_run_status": agent_run_status or previous_memory.get("agent_run_status"),
            "agent_run_current_stage": (
                agent_run_payload.get("current_stage")
                or result.get("current_stage")
                or previous_memory.get("agent_run_current_stage")
            ),
            "agent_run_last_event_seq": (
                agent_run_payload.get("last_event_seq")
                if agent_run_payload.get("last_event_seq") is not None
                else result.get("last_event_seq", previous_memory.get("agent_run_last_event_seq"))
            ),
            "pending_confirmation": pending_confirmation,
            "last_generation_result": last_generation_result,
            "workspace_kb_epoch": req.workspace_kb_epoch,
            "last_user_intent": intent,
            "last_assistant_message": assistant_message,
            "tree_summary": tree_summary,
            "conversation_summary": conversation_summary,
            "conversation_summary_brief": conversation_summary.get("brief") or self._build_conversation_summary(
                intent,
                tree_summary,
                assistant_message,
            ),
        }
        return session_store.upsert_session(session_id, patch)

    def handle_agent_run_status(
        self,
        *,
        run_id: str,
        session_id: Optional[str] = None,
        after_event_seq: int = 0,
        include_tree_data: bool = False,
    ) -> AssistantMessageResponse:
        payload = gnr_client.poll_agent_run(
            run_id=run_id,
            after_event_seq=after_event_seq,
            include_tree_data=include_tree_data,
        )
        resolved_session_id = session_id or payload.get("session_id") or run_id
        assistant_message, action = self._agent_run_message_action(payload)
        memory = self._update_memory(
            AssistantMessageRequest(session_id=resolved_session_id, message="agent run status"),
            resolved_session_id,
            "generate_tree",
            {},
            self._normalize_ids(payload.get("selected_file_version_ids") or []),
            assistant_message,
            payload,
        )
        return AssistantMessageResponse(
            session_id=resolved_session_id,
            intent="generate_tree",
            action=action,
            assistant_message=assistant_message,
            result={"agent_run": payload},
            memory=memory,
            agent_trace=[self._step("AgentRunPoller", "success", {
                "run_id": payload.get("run_id") or run_id,
                "status": payload.get("status"),
                "current_stage": payload.get("current_stage"),
                "last_event_seq": payload.get("last_event_seq"),
            })],
        )

    def confirm_agent_run(
        self,
        *,
        run_id: str,
        confirmation_id: str,
        candidate_ref: str,
        confirmation_type: str = "top_event",
        note: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AssistantMessageResponse:
        payload = gnr_client.confirm_agent_run(
            run_id=run_id,
            confirmation_id=confirmation_id,
            candidate_ref=candidate_ref,
            confirmation_type=confirmation_type,
            note=note,
        )
        resolved_session_id = session_id or payload.get("session_id") or run_id
        assistant_message, action = self._agent_run_message_action(payload)
        memory = self._update_memory(
            AssistantMessageRequest(session_id=resolved_session_id, message="agent run confirmation"),
            resolved_session_id,
            "generate_tree",
            {},
            self._normalize_ids(payload.get("selected_file_version_ids") or []),
            assistant_message,
            payload,
        )
        return AssistantMessageResponse(
            session_id=resolved_session_id,
            intent="generate_tree",
            action=action,
            assistant_message=assistant_message,
            result={"agent_run": payload},
            memory=memory,
            agent_trace=[self._step("AgentRunConfirmation", "success", {
                "run_id": payload.get("run_id") or run_id,
                "status": payload.get("status"),
                "confirmation_id": confirmation_id,
                "candidate_ref": candidate_ref,
            })],
        )

    def _agent_run_message_action(self, payload: Dict[str, Any]) -> tuple[str, str]:
        status = str(payload.get("status") or "").lower()
        mode = str(payload.get("mode") or "").lower()
        if status == "waiting_confirmation" or mode == "need_confirmation":
            return "请选择一个顶事件候选，以继续生成故障树。", "need_top_event_confirmation"
        if status == "completed" or mode == "completed":
            return "多智能体故障树生成已完成。", "generation_finished"
        if status == "human_review_required" or mode == "human_review_required":
            return "生成结果需要人工复核。", "human_review_required"
        if status == "failed" or mode == "failed":
            return "多智能体故障树生成失败。", "generation_failed"
        return "多智能体故障树生成任务正在执行。", "generation_running"

    def _build_conversation_summary(self, intent: str, tree_summary: Dict[str, Any], assistant_message: str) -> str:
        top = tree_summary.get("top_event") or "未确定顶事件"
        return f"最近意图：{intent}；当前顶事件：{top}；节点数：{tree_summary.get('node_count', 0)}；最近回复：{assistant_message}"

    def _normalize_ids(self, ids: List[str]) -> List[str]:
        out: List[str] = []
        for item in ids or []:
            value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
        return out

    def _step(self, agent: str, status: str, output: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "agent": agent,
            "status": status,
            "output": output,
            "ts": int(time.time()),
        }

    def _has_agent_run_path(self) -> bool:
        from app.config import FTA_GNR_AGENT_RUN_PATH

        return bool(FTA_GNR_AGENT_RUN_PATH)


assistant_agent = AssistantAgent()
