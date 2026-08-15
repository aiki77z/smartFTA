from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from agent_runtime.artifact_store import put_agent_artifact
from agent_runtime.event_store import append_agent_event
from agent_runtime.policies import (
    ARTIFACT_FINAL_TREE,
    EVENT_AGENT_MESSAGE,
    EVENT_RUN_COMPLETED,
    EVENT_STAGE_STARTED,
    EVENT_TREE_COMMITTED,
    RUN_STATUS_COMPLETED,
    STAGE_COMMIT,
    STAGE_CURATE,
)
from agent_runtime.run_store import get_agent_run, update_agent_run
from database import create_tree, save_version

from .tool_events import (
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_STARTED,
    EVENT_TOOL_COMPLETED,
    EVENT_TOOL_SELECTED,
    EVENT_TOOL_STARTED,
    append_agentic_event,
    append_tool_event,
)


class CommitAgent:
    name = "CommitAgent"

    def run(self, run_id: str, draft_artifact: Dict[str, Any], validation: Dict[str, Any]) -> Dict[str, Any]:
        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent=self.name,
            stage="commit",
            message="CommitAgent started persistence workflow.",
            progress=88,
            details={"draft_artifact_id": draft_artifact.get("artifact_id")},
        )
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="persistence",
            message="CommitAgent selected tool: save_tree_version.",
            progress=89,
            tool="save_tree_version",
        )
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool="save_tree_version",
            stage="persistence",
            message="CommitAgent started tool: save_tree_version.",
            progress=90,
        )
        result = self._commit(run_id, draft_artifact, validation)
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool="save_tree_version",
            stage="persistence",
            message="CommitAgent completed tool: save_tree_version.",
            progress=98,
            artifact_type=ARTIFACT_FINAL_TREE,
            artifact_id=result.get("final_tree_artifact_id"),
            details={"tree_id": result.get("tree_id"), "tree_version": result.get("tree_version")},
        )
        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent=self.name,
            stage="curate",
            message="CommitAgent completed final tree persistence.",
            progress=100,
            details={"tree_id": result.get("tree_id"), "tree_version": result.get("tree_version")},
        )
        return get_agent_run(run_id) or result

    def _commit(self, run_id: str, draft_artifact: Dict[str, Any], validation: Dict[str, Any]) -> Dict[str, Any]:
        run = get_agent_run(run_id) or {}
        artifact_content = draft_artifact.get("payload") or draft_artifact.get("content") or {}
        tree_data = (artifact_content.get("tree_data") or artifact_content).copy()
        tree_data["validation"] = validation
        retrieval = tree_data.get("retrieval") or {}
        tree_id = f"ft_{uuid.uuid4().hex[:8]}"
        append_agent_event(run_id, EVENT_STAGE_STARTED, stage=STAGE_COMMIT, message="Saving validated draft as a tree version.")
        append_agent_event(
            run_id,
            EVENT_AGENT_MESSAGE,
            stage="persistence",
            message="Persisting final fault tree version.",
            payload={"progress": 90, "draft_artifact_id": draft_artifact.get("artifact_id"), "agent": self.name},
        )
        create_tree(
            tree_id=tree_id,
            top_event=run.get("resolved_top_event") or run.get("requested_top_event") or "",
            requested_top_event=run.get("requested_top_event"),
            resolved_top_event=run.get("resolved_top_event"),
            catalog_name=run.get("resolved_top_event"),
            normalized_top_event=run.get("normalized_top_event"),
            graph_node_id=run.get("graph_node_id"),
            source_chunk_ids=retrieval.get("evidence_chunk_ids") or retrieval.get("chunk_ids") or [],
            source_file_version_ids=run.get("selected_file_version_ids") or [],
            source_scope_key=run.get("scope_key"),
        )
        version = save_version(
            tree_id=tree_id,
            tree_data=tree_data,
            editor="AI",
            description=f"AI agent workflow generation for top event: {run.get('resolved_top_event') or ''}",
            is_ai=True,
            requested_top_event=run.get("requested_top_event"),
            resolved_top_event=run.get("resolved_top_event"),
            normalized_top_event=run.get("normalized_top_event"),
            source_file_version_ids=run.get("selected_file_version_ids") or [],
            evidence_chunk_ids=retrieval.get("evidence_chunk_ids") or retrieval.get("chunk_ids") or [],
            subgraph_node_ids=retrieval.get("subgraph_node_ids") or [],
        )
        final_artifact = put_agent_artifact(
            run_id=run_id,
            artifact_type=ARTIFACT_FINAL_TREE,
            content={"tree_id": tree_id, "tree_version": version, "draft_artifact_id": draft_artifact.get("artifact_id")},
            producer=self.name,
            parent_artifact_id=draft_artifact.get("artifact_id"),
            metadata={"producer": self.name},
        )
        update_agent_run(
            run_id,
            {
                "status": RUN_STATUS_COMPLETED,
                "current_stage": STAGE_CURATE,
                "progress": {"completed": 100, "total": 100},
                "tree_id": tree_id,
                "tree_version": version,
                "result": {
                    "mode": "completed",
                    "tree_id": tree_id,
                    "tree_version": version,
                    "final_tree_artifact_id": final_artifact.get("artifact_id"),
                    "draft_tree_artifact_id": draft_artifact.get("artifact_id"),
                    "validation": validation,
                },
                "error": None,
                "finished_at": datetime.utcnow(),
            },
        )
        append_agent_event(run_id, EVENT_TREE_COMMITTED, stage=STAGE_COMMIT, message="Validated draft committed.", payload={"tree_id": tree_id, "tree_version": version})
        append_agent_event(run_id, EVENT_RUN_COMPLETED, stage=STAGE_CURATE, message="Agent run completed.", payload={"tree_id": tree_id, "tree_version": version})
        return {
            "tree_id": tree_id,
            "tree_version": version,
            "final_tree_artifact_id": final_artifact.get("artifact_id"),
        }
