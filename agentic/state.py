from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class FaultTreeAgentState(TypedDict, total=False):
    run_id: str
    session_id: Optional[str]
    project_id: Optional[str]
    canvas_id: Optional[str]

    task_type: str
    prompt: str
    requirements: str
    options: Dict[str, Any]

    selected_file_version_ids: List[str]
    scope_key: str

    requested_top_event: Optional[str]
    resolved_top_event: Optional[str]
    normalized_top_event: Optional[str]
    graph_node_id: Optional[str]

    status: str
    current_stage: str
    progress: Dict[str, Any]
    confirmation: Optional[Dict[str, Any]]

    retrieval_context_artifact_id: Optional[str]
    retrieval_context: Optional[Dict[str, Any]]
    draft_tree_artifact_id: Optional[str]
    draft_tree: Optional[Dict[str, Any]]
    validation_report_artifact_id: Optional[str]
    validation_report: Optional[Dict[str, Any]]
    repair_patch_artifact_id: Optional[str]
    repair_summary: Dict[str, Any]
    experience_memory_summary: Dict[str, Any]
    optimization_done: bool
    final_tree_artifact_id: Optional[str]

    retrieval_attempt_count: int
    draft_attempt_count: int
    repair_attempt_count: int

    retrieval_summary: Dict[str, Any]
    validation_summary: Dict[str, Any]
    supervisor_decision: Optional[Dict[str, Any]]
    phase2_result: Optional[Dict[str, Any]]
    memory_context: Dict[str, Any]
    memory_summary_artifact_id: Optional[str]

    tree_id: Optional[str]
    tree_version: Optional[int]
    error: Optional[Dict[str, Any]]


def state_from_run(run: Dict[str, Any]) -> FaultTreeAgentState:
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    return {
        "run_id": str(run.get("run_id") or ""),
        "session_id": run.get("session_id"),
        "project_id": run.get("project_id"),
        "canvas_id": run.get("canvas_id"),
        "task_type": str(run.get("task_type") or "generate_fault_tree"),
        "prompt": str(run.get("prompt") or ""),
        "requirements": str(run.get("requirements") or ""),
        "options": run.get("options") if isinstance(run.get("options"), dict) else {},
        "selected_file_version_ids": list(run.get("selected_file_version_ids") or []),
        "scope_key": str(run.get("scope_key") or ""),
        "requested_top_event": run.get("requested_top_event"),
        "resolved_top_event": run.get("resolved_top_event"),
        "normalized_top_event": run.get("normalized_top_event"),
        "graph_node_id": run.get("graph_node_id"),
        "status": str(run.get("status") or ""),
        "current_stage": str(run.get("current_stage") or ""),
        "progress": run.get("progress") or {"completed": 0, "total": 0},
        "confirmation": run.get("confirmation"),
        "retrieval_context_artifact_id": result.get("retrieval_context_artifact_id"),
        "retrieval_context": None,
        "draft_tree_artifact_id": result.get("draft_tree_artifact_id"),
        "draft_tree": None,
        "validation_report_artifact_id": result.get("validation_artifact_id"),
        "validation_report": None,
        "repair_patch_artifact_id": result.get("repair_patch_artifact_id"),
        "repair_summary": {},
        "experience_memory_summary": {},
        "optimization_done": False,
        "final_tree_artifact_id": result.get("final_tree_artifact_id"),
        "retrieval_attempt_count": int(run.get("retrieval_attempt_count") or 0),
        "draft_attempt_count": int(run.get("draft_attempt_count") or 0),
        "repair_attempt_count": int(run.get("repair_attempt_count") or 0),
        "retrieval_summary": result.get("retrieval") or {},
        "validation_summary": result.get("validation") or {},
        "supervisor_decision": None,
        "phase2_result": None,
        "memory_context": {},
        "memory_summary_artifact_id": result.get("memory_summary_artifact_id"),
        "tree_id": run.get("tree_id"),
        "tree_version": run.get("tree_version"),
        "error": run.get("error"),
    }
