from typing import Any, Dict, List

from app.memory.session_store import session_store
from app.schemas import AssistantMessageRequest, AssistantMessageResponse, EditRequest
from app.services.edit_plan_service import plan_edit_fault_tree
from app.services.tree_utils import summarize_tree


class EditAgent:
    name = "EditAgent"

    def handle(
        self,
        *,
        owner: Any,
        req: AssistantMessageRequest,
        session_id: str,
        message: str,
        trace: List[Dict[str, Any]],
    ) -> AssistantMessageResponse:
        if req.current_tree is None:
            assistant_message = "当前没有可编辑的故障树。你可以先让我生成一棵故障树。"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": "edit_tree"})
            memory = owner._update_memory(
                req,
                session_id,
                "edit_tree",
                {},
                owner._normalize_ids(req.selected_file_version_ids),
                assistant_message,
            )
            return AssistantMessageResponse(
                session_id=session_id,
                intent="edit_tree",
                action="need_current_tree",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        trace.append(owner._step(self.name, "running", {"strategy": "llm_edit_plan_with_legacy_fallback"}))
        edit_resp = plan_edit_fault_tree(EditRequest(
            instruction=message,
            tree_json=req.current_tree,
            selected_files=req.selected_files,
        ))
        diff = edit_resp.diff
        if not any([diff.added, diff.modified, diff.removed, diff.added_edges, diff.removed_edges]):
            assistant_message = "我理解了你的编辑请求，但没有找到可以安全修改的目标节点或连线。请尝试直接写出节点名称，例如“把节点A重命名为节点B”。"
            session_store.append_message(session_id, "assistant", assistant_message, {"intent": "edit_tree", "no_change": True})
            memory = owner._update_memory(
                req,
                session_id,
                "edit_tree",
                summarize_tree(req.current_tree),
                owner._normalize_ids(req.selected_file_version_ids),
                assistant_message,
            )
            trace.append(owner._step(self.name, "success", {"diff": diff.dict(), "no_change": True}))
            return AssistantMessageResponse(
                session_id=session_id,
                intent="edit_tree",
                action="edit_noop",
                assistant_message=assistant_message,
                result=edit_resp.dict(),
                memory=memory,
                agent_trace=trace,
            )
        pending = session_store.save_pending_action(session_id, {
            "kind": "edit_diff",
            "before_tree": req.current_tree,
            "after_tree": edit_resp.updated_tree_json,
            "diff": diff.dict(),
            "rationale": edit_resp.rationale,
        })
        trace.append(owner._step(self.name, "success", {"diff": diff.dict()}))

        assistant_message = (
            f"已生成编辑草案：新增 {len(diff.added)}，修改 {len(diff.modified)}，"
            f"删除 {len(diff.removed)}。请在前端确认 Accept 或 Undo。"
        )
        session_store.append_message(session_id, "assistant", assistant_message, {
            "intent": "edit_tree",
            "pending_id": pending.get("pending_id"),
        })
        memory = owner._update_memory(
            req,
            session_id,
            "edit_tree",
            summarize_tree(edit_resp.updated_tree_json),
            owner._normalize_ids(req.selected_file_version_ids),
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


edit_agent = EditAgent()
