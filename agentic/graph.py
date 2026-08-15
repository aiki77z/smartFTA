from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict

from agent_runtime.artifact_store import get_latest_agent_artifact, put_agent_artifact
from agent_runtime.policies import (
    ARTIFACT_RETRIEVAL_CONTEXT,
    ARTIFACT_TREE_DRAFT,
    ARTIFACT_VALIDATION_REPORT,
    EVENT_ARTIFACT_CREATED,
    EVENT_DRAFT_GENERATED,
    EVENT_RETRIEVAL_DONE,
    MAX_DRAFT_REBUILD_ATTEMPTS,
    RUN_STATUS_HUMAN_REVIEW_REQUIRED,
    STAGE_VALIDATE,
)
from agent_runtime.run_store import get_agent_run, update_agent_run
from langgraph.graph import END, START, StateGraph

from .commit_agent import CommitAgent
from .memory import collect_runtime_memory, curate_run_memory
from .repair_agent import RepairAgent
from .retrieval_agent import RetrievalAgent
from .state import FaultTreeAgentState, state_from_run
from .supervisor import SupervisorAgent
from .tree_draft_agent import TreeDraftAgent
from .verify_agent import VerifyAgent
from .tool_events import (
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_DECISION,
    EVENT_AGENT_STARTED,
    EVENT_HANDOFF_REQUESTED,
    EVENT_TOOL_COMPLETED,
    append_agentic_event,
)


Phase2Runner = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def _build_agentic_v2_graph(catalog: Dict[str, Any], phase2_runner: Phase2Runner):
    supervisor = SupervisorAgent()

    def supervisor_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent="SupervisorAgent",
            stage="scope",
            message="SupervisorAgent started LangGraph workflow.",
            progress=11,
            details={"graph": "agentic_v2"},
        )
        decision = supervisor.decide(state)
        memory_context = collect_runtime_memory(run_id)
        append_agentic_event(
            run_id,
            EVENT_AGENT_DECISION,
            agent="SupervisorAgent",
            stage=str(state.get("current_stage") or "scope"),
            message=f"SupervisorAgent selected next action: {decision.next_action}.",
            progress=12,
            reason=decision.reason,
            details=decision.model_dump(),
        )
        return {"supervisor_decision": decision.model_dump(), "memory_context": memory_context}

    def route_after_supervisor(state: FaultTreeAgentState) -> str:
        decision = state.get("supervisor_decision") or {}
        next_action = str(decision.get("next_action") or "")
        if next_action == "wait_human_confirmation":
            return "pause_for_confirmation"
        if next_action == "human_review":
            return "human_review"
        return "retrieval_agent"

    def pause_for_confirmation_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        append_agentic_event(
            str(state.get("run_id") or ""),
            EVENT_AGENT_COMPLETED,
            agent="SupervisorAgent",
            stage="scope",
            message="SupervisorAgent paused for human confirmation.",
            progress=12,
        )
        return {}

    def human_review_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        latest_draft = get_latest_agent_artifact(run_id, ARTIFACT_TREE_DRAFT) or {}
        validation = state.get("validation_report") or {}
        validation_summary = state.get("validation_summary") or {}
        repair_summary = state.get("repair_summary") or {}
        reason = (
            repair_summary.get("reason")
            or validation_summary.get("reason")
            or "Automatic generation could not produce a verified final tree."
        )
        update_agent_run(
            run_id,
            {
                "status": RUN_STATUS_HUMAN_REVIEW_REQUIRED,
                "current_stage": STAGE_VALIDATE,
                "review_tree_artifact_id": latest_draft.get("artifact_id") or state.get("draft_tree_artifact_id"),
                "result": {
                    "mode": "human_review_required",
                    "reason": reason,
                    "review_tree_artifact_id": latest_draft.get("artifact_id") or state.get("draft_tree_artifact_id"),
                    "validation": validation,
                    "validation_summary": validation_summary,
                    "repair_summary": repair_summary,
                },
                "error": None,
                "finished_at": datetime.utcnow(),
            },
        )
        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent="SupervisorAgent",
            stage="human_review",
            message="SupervisorAgent routed the run to human review.",
            progress=None,
            reason=reason,
            details={
                "draft_artifact_id": latest_draft.get("artifact_id") or state.get("draft_tree_artifact_id"),
                "validation_artifact_id": state.get("validation_report_artifact_id"),
                "validation_summary": validation_summary,
                "repair_summary": repair_summary,
            },
        )
        return {"phase2_result": get_agent_run(run_id) or {}}

    def retrieval_agent_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        append_agentic_event(
            run_id,
            EVENT_HANDOFF_REQUESTED,
            agent="SupervisorAgent",
            stage="retrieval",
            message="SupervisorAgent handed off to RetrievalAgent.",
            progress=13,
            reason="retrieval context is required before draft generation",
            details={"to_agent": "RetrievalAgent"},
        )
        retrieval_context = RetrievalAgent().run(state, catalog)
        artifact = put_agent_artifact(
            run_id=run_id,
            artifact_type=ARTIFACT_RETRIEVAL_CONTEXT,
            content=retrieval_context,
            producer="RetrievalAgent",
            metadata={"agentic_version": "v2", "contains_runtime_context": True},
        )
        matched = retrieval_context.get("matched") or {}
        subgraph = retrieval_context.get("subgraph_bundle") or {}
        append_agentic_event(
            run_id,
            EVENT_RETRIEVAL_DONE,
            agent="RetrievalAgent",
            stage="graph_chunks",
            message="RetrievalAgent persisted retrieval context artifact.",
            progress=43,
            artifact_type=ARTIFACT_RETRIEVAL_CONTEXT,
            artifact_id=artifact.get("artifact_id"),
            details={
                "matched_top_event": matched.get("matched_name"),
                "matched_node_id": matched.get("matched_node_id"),
                "subgraph_node_count": len(subgraph.get("nodes") or []),
                "subgraph_edge_count": len(subgraph.get("edges") or []),
                "evidence_chunk_count": len(retrieval_context.get("raw_chunks") or []),
            },
        )
        return {
            "retrieval_context_artifact_id": artifact.get("artifact_id"),
            "retrieval_context": retrieval_context,
            "retrieval_summary": {
                "matched_top_event": matched.get("matched_name"),
                "matched_node_id": matched.get("matched_node_id"),
                "subgraph_node_count": len(subgraph.get("nodes") or []),
                "subgraph_edge_count": len(subgraph.get("edges") or []),
                "evidence_chunk_count": len(retrieval_context.get("raw_chunks") or []),
            },
        }

    def tree_draft_agent_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        draft_attempt = int(state.get("draft_attempt_count") or 0) + 1
        retrieval_context = state.get("retrieval_context") or {}
        if not isinstance(retrieval_context, dict) or not retrieval_context:
            artifact = get_agent_run(run_id) or {}
            raise ValueError(f"Missing retrieval context for TreeDraftAgent: {artifact.get('run_id') or run_id}")
        validation_summary = state.get("validation_summary") or {}
        is_rebuild = bool(validation_summary and not validation_summary.get("passed"))
        append_agentic_event(
            run_id,
            EVENT_HANDOFF_REQUESTED,
            agent="SupervisorAgent",
            stage="generate_draft",
            message=(
                "SupervisorAgent requested TreeDraftAgent 重新生成 draft."
                if is_rebuild
                else "SupervisorAgent handed off to TreeDraftAgent."
            ),
            progress=44,
            reason=(
                "previous draft failed validation; rebuild a new draft"
                if is_rebuild
                else "retrieval context is ready for draft generation"
            ),
            details={
                "to_agent": "TreeDraftAgent",
                "draft_attempt": draft_attempt,
                "rebuild": is_rebuild,
                "previous_validation": validation_summary if is_rebuild else {},
            },
        )
        draft_tree = TreeDraftAgent().run(state, catalog, retrieval_context)
        artifact = put_agent_artifact(
            run_id=run_id,
            artifact_type=ARTIFACT_TREE_DRAFT,
            content=draft_tree,
            producer="TreeDraftAgent",
            metadata={"agentic_version": "v2"},
            parent_artifact_id=state.get("retrieval_context_artifact_id"),
        )
        append_agentic_event(
            run_id,
            EVENT_ARTIFACT_CREATED,
            agent="TreeDraftAgent",
            stage="generate_draft",
            message="Artifact created: tree_draft",
            progress=50,
            artifact_type=ARTIFACT_TREE_DRAFT,
            artifact_id=artifact.get("artifact_id"),
        )
        append_agentic_event(
            run_id,
            EVENT_DRAFT_GENERATED,
            agent="TreeDraftAgent",
            stage="generate_draft",
            message="TreeDraftAgent persisted draft tree artifact.",
            progress=51,
            artifact_type=ARTIFACT_TREE_DRAFT,
            artifact_id=artifact.get("artifact_id"),
            details={
                "node_count": len(draft_tree.get("nodeList") or []),
                "link_count": len(draft_tree.get("linkList") or []),
            },
        )
        return {
            "draft_tree_artifact_id": artifact.get("artifact_id"),
            "draft_tree": draft_tree,
            "draft_attempt_count": draft_attempt,
            "optimization_done": False,
            "validation_report": None,
            "validation_report_artifact_id": None,
            "validation_summary": {},
        }

    def verify_agent_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        draft_artifact = get_latest_agent_artifact(run_id, ARTIFACT_TREE_DRAFT)
        if not draft_artifact:
            raise ValueError(f"Missing tree_draft artifact for VerifyAgent: {run_id}")
        append_agentic_event(
            run_id,
            EVENT_HANDOFF_REQUESTED,
            agent="SupervisorAgent",
            stage="validate",
            message="SupervisorAgent handed off to VerifyAgent.",
            progress=54,
            reason="draft tree is ready for required quality gate",
            details={"to_agent": "VerifyAgent", "draft_artifact_id": draft_artifact.get("artifact_id")},
        )
        verification = VerifyAgent().run(state, draft_artifact)
        report = verification.get("payload") or {}
        return {
            "validation_report_artifact_id": report.get("artifact_id"),
            "validation_report": report,
            "validation_summary": {
                "passed": bool(report.get("passed")),
                "error_count": int(report.get("error_count") or 0),
                "warning_count": int(report.get("warning_count") or 0),
                "human_review_required": bool(verification.get("human_review_required")),
                "next_stage": verification.get("next_stage"),
            },
        }

    def route_after_verify(state: FaultTreeAgentState) -> str:
        summary = state.get("validation_summary") or {}
        if summary.get("passed"):
            return "commit_agent" if state.get("optimization_done") else "repair_agent"
        if int(state.get("draft_attempt_count") or 0) >= MAX_DRAFT_REBUILD_ATTEMPTS:
            return "human_review" if int(state.get("repair_attempt_count") or 0) > 0 else "repair_agent"
        return "tree_draft_agent"

    def repair_agent_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        draft_artifact = get_latest_agent_artifact(run_id, ARTIFACT_TREE_DRAFT)
        validation_artifact = get_latest_agent_artifact(run_id, ARTIFACT_VALIDATION_REPORT)
        if not draft_artifact:
            raise ValueError(f"Missing tree_draft artifact for RepairAgent: {run_id}")
        validation_report = state.get("validation_report") or (validation_artifact or {}).get("payload") or (validation_artifact or {}).get("content") or {}
        retrieval_context = state.get("retrieval_context") or {}
        chunks = retrieval_context.get("raw_chunks") if isinstance(retrieval_context, dict) else []
        append_agentic_event(
            run_id,
            EVENT_HANDOFF_REQUESTED,
            agent="SupervisorAgent",
            stage="repair",
            message="SupervisorAgent handed off to RepairAgent.",
            progress=70,
            reason=(
                "draft failed after rebuild limit; try experience-memory repair once"
                if not bool((state.get("validation_summary") or {}).get("passed"))
                else "validated draft is ready for experience-memory optimization"
            ),
            details={"to_agent": "RepairAgent", "draft_artifact_id": draft_artifact.get("artifact_id")},
        )
        repair = RepairAgent().run(state, draft_artifact, validation_report, chunks=chunks or [])
        patch = repair.get("payload") or {}
        repaired_draft = repair.get("draft_tree_artifact") or {}
        experience_memory = repair.get("experience_memory") or {}
        experience_memory_summary = {
            "pattern_count": len(experience_memory.get("patterns") or []),
            "correction_count": len(experience_memory.get("corrections") or []),
            "issue_codes": experience_memory.get("issue_codes") or [],
            "repairable_issue_count": experience_memory.get("repairable_issue_count"),
        }
        if not isinstance((patch or {}).get("repaired_tree"), dict) or not repaired_draft.get("artifact_id"):
            validation_passed = bool((state.get("validation_summary") or {}).get("passed"))
            return {
                "draft_tree_artifact_id": draft_artifact.get("artifact_id"),
                "draft_tree": draft_artifact.get("payload") or draft_artifact.get("content"),
                "repair_patch_artifact_id": patch.get("artifact_id"),
                "repair_attempt_count": int(state.get("repair_attempt_count") or 0) + 1,
                "experience_memory_summary": experience_memory_summary,
                "optimization_done": validation_passed,
                "repair_summary": {
                    "status": patch.get("status"),
                    "human_review_required": not validation_passed,
                    "reason": (
                        "Experience-memory optimization produced no structural changes."
                        if validation_passed
                        else "No reliable optimized draft was produced."
                    ),
                    "changed": False,
                    "experience_memory": experience_memory_summary,
                },
            }
        return {
            "draft_tree_artifact_id": repaired_draft.get("artifact_id"),
            "draft_tree": repaired_draft.get("payload") or repaired_draft.get("content"),
            "repair_patch_artifact_id": patch.get("artifact_id"),
            "repair_attempt_count": int(state.get("repair_attempt_count") or 0) + 1,
            "validation_report": None,
            "validation_report_artifact_id": None,
            "validation_summary": {},
            "experience_memory_summary": experience_memory_summary,
            "optimization_done": True,
            "repair_summary": {
                "status": patch.get("status"),
                "human_review_required": False,
                "draft_artifact_id": repaired_draft.get("artifact_id"),
                "changed": True,
                "experience_memory": experience_memory_summary,
            },
        }

    def route_after_repair(state: FaultTreeAgentState) -> str:
        summary = state.get("repair_summary") or {}
        if summary.get("human_review_required"):
            return "human_review"
        if not summary.get("changed"):
            return "commit_agent"
        return "verify_agent"

    def commit_agent_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        draft_artifact = get_latest_agent_artifact(run_id, ARTIFACT_TREE_DRAFT)
        validation = state.get("validation_report") or {}
        if not draft_artifact:
            raise ValueError(f"Missing tree_draft artifact for CommitAgent: {run_id}")
        append_agentic_event(
            run_id,
            EVENT_HANDOFF_REQUESTED,
            agent="SupervisorAgent",
            stage="commit",
            message="SupervisorAgent handed off to CommitAgent.",
            progress=87,
            reason="validation passed",
            details={"to_agent": "CommitAgent", "draft_artifact_id": draft_artifact.get("artifact_id")},
        )
        result = CommitAgent().run(run_id, draft_artifact, validation)
        return {
            "phase2_result": result,
            "status": str((result or {}).get("status") or "completed"),
            "current_stage": str((result or {}).get("current_stage") or "curate"),
            "tree_id": (result or {}).get("tree_id"),
            "tree_version": (result or {}).get("tree_version"),
        }

    def complete_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        result = state.get("phase2_result") or {}
        status = str(result.get("status") or state.get("status") or "")
        append_agentic_event(
            str(state.get("run_id") or ""),
            EVENT_AGENT_COMPLETED,
            agent="SupervisorAgent",
            stage=str(result.get("current_stage") or state.get("current_stage") or "curate"),
            message=(
                "SupervisorAgent completed LangGraph multi-agent workflow."
                if status == "completed"
                else f"SupervisorAgent stopped LangGraph workflow with status: {status}."
            ),
            progress=100 if status == "completed" else None,
            details={"status": status},
        )
        return {}

    def memory_curator_node(state: FaultTreeAgentState) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        return curate_run_memory(run_id, state)

    builder = StateGraph(FaultTreeAgentState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("pause_for_confirmation", pause_for_confirmation_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("retrieval_agent", retrieval_agent_node)
    builder.add_node("tree_draft_agent", tree_draft_agent_node)
    builder.add_node("verify_agent", verify_agent_node)
    builder.add_node("repair_agent", repair_agent_node)
    builder.add_node("commit_agent", commit_agent_node)
    builder.add_node("memory_curator", memory_curator_node)
    builder.add_node("complete", complete_node)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "pause_for_confirmation": "pause_for_confirmation",
            "human_review": "human_review",
            "retrieval_agent": "retrieval_agent",
        },
    )
    builder.add_edge("pause_for_confirmation", END)
    builder.add_edge("human_review", "memory_curator")
    builder.add_edge("retrieval_agent", "tree_draft_agent")
    builder.add_edge("tree_draft_agent", "verify_agent")
    builder.add_conditional_edges(
        "verify_agent",
        route_after_verify,
        {
            "commit_agent": "commit_agent",
            "repair_agent": "repair_agent",
            "tree_draft_agent": "tree_draft_agent",
            "human_review": "human_review",
        },
    )
    builder.add_conditional_edges(
        "repair_agent",
        route_after_repair,
        {
            "verify_agent": "verify_agent",
            "commit_agent": "commit_agent",
            "human_review": "human_review",
        },
    )
    builder.add_edge("commit_agent", "memory_curator")
    builder.add_edge("memory_curator", "complete")
    builder.add_edge("complete", END)
    return builder.compile(name="fta_agentic_v2")


def run_agentic_v2_workflow(
    run_id: str,
    catalog: Dict[str, Any],
    *,
    phase2_runner: Phase2Runner,
) -> Dict[str, Any]:
    """Run the controlled agentic v2 workflow through LangGraph.

    The v2 graph now owns the main multi-agent path: retrieval, draft,
    validation, bounded repair loop, commit, and human-review routing.
    """
    run = get_agent_run(run_id)
    if not run:
        return {}
    state = state_from_run(run)
    graph = _build_agentic_v2_graph(catalog, phase2_runner)
    final_state = graph.invoke(state, config={"configurable": {"thread_id": run_id}})
    result = (final_state or {}).get("phase2_result")
    if isinstance(result, dict):
        return result
    return get_agent_run(run_id) or {}
