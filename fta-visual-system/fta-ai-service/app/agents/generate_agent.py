from typing import Any, Dict, List

from app.memory.session_store import session_store
from app.schemas import AssistantMessageRequest, AssistantMessageResponse
from app.services.tree_utils import summarize_tree
from app.tools.gnr_client import gnr_client


class GenerateAgent:
    name = "GenerateAgent"

    def handle(
        self,
        *,
        owner: Any,
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
            memory = owner._update_memory(
                req,
                session_id,
                intent,
                summarize_tree(req.current_tree),
                scope_ids,
                assistant_message,
            )
            return AssistantMessageResponse(
                session_id=session_id,
                intent=intent,
                action="need_source_files",
                assistant_message=assistant_message,
                memory=memory,
                agent_trace=trace,
            )

        trace.append(owner._step(self.name, "running", {
            "target": "FTA-GNR",
            "mode": "agent_run" if owner._has_agent_run_path() else "tree_generate_api",
        }))
        result = gnr_client.run_generation_agent(
            prompt=message,
            selected_file_version_ids=scope_ids,
            session_id=session_id,
            project_id=req.project_id,
            canvas_id=req.canvas_id,
        )
        trace.append(owner._step(self.name, "success", {
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
        memory = owner._update_memory(
            req,
            session_id,
            intent,
            summarize_tree(req.current_tree),
            scope_ids,
            assistant_message,
            result,
        )
        return AssistantMessageResponse(
            session_id=session_id,
            intent=intent,
            action=action,
            assistant_message=assistant_message,
            result=result,
            memory=memory,
            agent_trace=trace,
        )


generate_agent = GenerateAgent()
