from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_action: str = Field(..., min_length=1)
    reason: str = Field(default="")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    tool: str
    progress: Optional[int] = None
    type: Optional[str] = None
    artifact_id: Optional[str] = None
    reason: Optional[str] = None
    memory_type: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class AgentHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_agent: str
    to_agent: str
    reason: str = ""
    stage: str = ""

