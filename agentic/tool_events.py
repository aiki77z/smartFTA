from __future__ import annotations

from typing import Any, Dict, Optional

from agent_runtime.event_store import append_agent_event


EVENT_AGENT_STARTED = "AGENT_STARTED"
EVENT_AGENT_DECISION = "AGENT_DECISION"
EVENT_AGENT_COMPLETED = "AGENT_COMPLETED"
EVENT_TOOL_SELECTED = "TOOL_SELECTED"
EVENT_TOOL_STARTED = "TOOL_STARTED"
EVENT_TOOL_COMPLETED = "TOOL_COMPLETED"
EVENT_TOOL_FAILED = "TOOL_FAILED"
EVENT_HANDOFF_REQUESTED = "HANDOFF_REQUESTED"


def append_agentic_event(
    run_id: str,
    event_type: str,
    *,
    agent: str,
    stage: str,
    message: str,
    progress: Optional[int] = None,
    tool: Optional[str] = None,
    artifact_type: Optional[str] = None,
    artifact_id: Optional[str] = None,
    reason: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "agent": agent,
        "details": details or {},
    }
    if progress is not None:
        payload["progress"] = int(progress)
    if tool:
        payload["tool"] = tool
    if artifact_type:
        payload["type"] = artifact_type
    if artifact_id:
        payload["artifact_id"] = artifact_id
    if reason:
        payload["reason"] = reason
    return append_agent_event(
        run_id,
        event_type,
        stage=stage,
        message=message,
        payload=payload,
    )


def append_tool_event(
    run_id: str,
    event_type: str,
    *,
    agent: str,
    tool: str,
    stage: str,
    message: str,
    progress: Optional[int] = None,
    artifact_type: Optional[str] = None,
    artifact_id: Optional[str] = None,
    reason: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return append_agentic_event(
        run_id,
        event_type,
        agent=agent,
        tool=tool,
        stage=stage,
        message=message,
        progress=progress,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        reason=reason,
        details=details,
    )

