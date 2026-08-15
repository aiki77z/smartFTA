from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from agent_runtime.artifact_store import list_agent_artifacts, put_agent_artifact
from agent_runtime.event_store import list_agent_events
from agent_runtime.policies import ARTIFACT_MEMORY_SUMMARY
from agent_runtime.run_store import get_agent_run, update_agent_run

from .tool_events import (
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_STARTED,
    append_agentic_event,
)


def collect_runtime_memory(run_id: str) -> Dict[str, Any]:
    run = get_agent_run(run_id) or {}
    events = list_agent_events(run_id, after_event_seq=0, limit=1000)
    artifacts = list_agent_artifacts(run_id, limit=1000)
    return {
        "run": _summarize_run_memory(run),
        "events": _summarize_event_memory(events),
        "artifacts": _summarize_artifact_memory(artifacts),
    }


def curate_run_memory(run_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    append_agentic_event(
        run_id,
        EVENT_AGENT_STARTED,
        agent="MemoryCurator",
        stage="curate",
        message="MemoryCurator started runtime memory curation.",
        progress=99,
    )
    memory = collect_runtime_memory(run_id)
    summary = {
        "run_id": run_id,
        "outcome": {
            "status": state.get("status") or (memory.get("run") or {}).get("status"),
            "tree_id": state.get("tree_id") or (memory.get("run") or {}).get("tree_id"),
            "tree_version": state.get("tree_version") or (memory.get("run") or {}).get("tree_version"),
            "repair_attempt_count": state.get("repair_attempt_count"),
        },
        "memory": memory,
        "agentic_version": "v2",
    }
    artifact = put_agent_artifact(
        run_id=run_id,
        artifact_type=ARTIFACT_MEMORY_SUMMARY,
        content=summary,
        producer="MemoryCurator",
        metadata={"producer": "MemoryCurator", "agentic_version": "v2"},
    )
    run = get_agent_run(run_id) or {}
    result = dict(run.get("result") or {})
    result["memory_summary_artifact_id"] = artifact.get("artifact_id")
    update_agent_run(run_id, {"result": result})
    append_agentic_event(
        run_id,
        EVENT_AGENT_COMPLETED,
        agent="MemoryCurator",
        stage="curate",
        message="MemoryCurator persisted runtime memory summary.",
        progress=100,
        artifact_type=ARTIFACT_MEMORY_SUMMARY,
        artifact_id=artifact.get("artifact_id"),
        details={
            "event_count": (memory.get("events") or {}).get("event_count"),
            "artifact_count": (memory.get("artifacts") or {}).get("artifact_count"),
        },
    )
    return {"memory_summary_artifact_id": artifact.get("artifact_id"), "memory_context": memory}


def _summarize_run_memory(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "task_type": run.get("task_type"),
        "status": run.get("status"),
        "current_stage": run.get("current_stage"),
        "prompt": run.get("prompt"),
        "requested_top_event": run.get("requested_top_event"),
        "resolved_top_event": run.get("resolved_top_event"),
        "graph_node_id": run.get("graph_node_id"),
        "scope_key": run.get("scope_key"),
        "selected_file_version_ids": run.get("selected_file_version_ids") or [],
        "repair_attempt_count": run.get("repair_attempt_count"),
        "tree_id": run.get("tree_id"),
        "tree_version": run.get("tree_version"),
        "error": run.get("error"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "finished_at": run.get("finished_at"),
    }


def _summarize_event_memory(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    stage_counts = Counter(str(event.get("stage") or "") for event in events)
    type_counts = Counter(str(event.get("type") or "") for event in events)
    agent_counts = Counter(str((event.get("payload") or {}).get("agent") or "") for event in events)
    return {
        "event_count": len(events),
        "stage_counts": {key: value for key, value in stage_counts.items() if key},
        "type_counts": {key: value for key, value in type_counts.items() if key},
        "agent_counts": {key: value for key, value in agent_counts.items() if key},
        "recent_events": [
            {
                "event_seq": event.get("event_seq"),
                "type": event.get("type"),
                "stage": event.get("stage"),
                "message": event.get("message"),
                "agent": (event.get("payload") or {}).get("agent"),
                "tool": (event.get("payload") or {}).get("tool"),
                "created_at": event.get("created_at"),
            }
            for event in events[-30:]
        ],
    }


def _summarize_artifact_memory(artifacts: List[Dict[str, Any]]) -> Dict[str, Any]:
    type_counts = Counter(str(artifact.get("type") or "") for artifact in artifacts)
    return {
        "artifact_count": len(artifacts),
        "type_counts": {key: value for key, value in type_counts.items() if key},
        "latest_by_type": _latest_artifacts_by_type(artifacts),
    }


def _latest_artifacts_by_type(artifacts: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest: Dict[str, Dict[str, Any]] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("type") or "")
        if artifact_type:
            latest[artifact_type] = _summarize_artifact(artifact)
    return latest


def _summarize_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    payload = artifact.get("payload") or artifact.get("content") or {}
    summary: Dict[str, Any] = {
        "artifact_id": artifact.get("artifact_id"),
        "type": artifact.get("type"),
        "version": artifact.get("version"),
        "producer": artifact.get("producer"),
        "parent_artifact_id": artifact.get("parent_artifact_id"),
        "created_at": artifact.get("created_at"),
    }
    if isinstance(payload, dict):
        if "nodeList" in payload or "linkList" in payload:
            summary["node_count"] = len(payload.get("nodeList") or [])
            summary["link_count"] = len(payload.get("linkList") or [])
        if "tree_data" in payload and isinstance(payload.get("tree_data"), dict):
            tree_data = payload["tree_data"]
            summary["node_count"] = len(tree_data.get("nodeList") or [])
            summary["link_count"] = len(tree_data.get("linkList") or [])
        if "matched" in payload:
            matched = payload.get("matched") or {}
            subgraph = payload.get("subgraph_bundle") or {}
            summary["matched_top_event"] = matched.get("matched_name")
            summary["matched_node_id"] = matched.get("matched_node_id")
            summary["subgraph_node_count"] = len(subgraph.get("nodes") or [])
            summary["subgraph_edge_count"] = len(subgraph.get("edges") or [])
            summary["evidence_chunk_count"] = len(payload.get("raw_chunks") or [])
        if "passed" in payload:
            summary["passed"] = bool(payload.get("passed"))
            summary["error_count"] = int(payload.get("error_count") or 0)
            summary["warning_count"] = int(payload.get("warning_count") or 0)
        if "status" in payload:
            summary["status"] = payload.get("status")
        if "tree_id" in payload:
            summary["tree_id"] = payload.get("tree_id")
            summary["tree_version"] = payload.get("tree_version")
    return summary
