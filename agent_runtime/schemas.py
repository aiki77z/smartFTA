from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str = "generate_fault_tree"
    prompt: str = Field(..., min_length=1)
    selected_file_version_ids: List[str] = Field(default_factory=list)
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    canvas_id: Optional[str] = None
    tree_id: Optional[str] = None
    tree_version: Optional[int] = None
    max_depth: Optional[int] = Field(default=None, ge=1, le=10)
    sync: bool = False
    options: Dict[str, Any] = Field(default_factory=dict)


class AgentConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = Field(..., min_length=1)
    confirmation_type: str = Field(default="top_event", min_length=1)
    candidate_ref: str = Field(..., min_length=1)
    note: Optional[str] = None


class AgentErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Optional[Dict[str, Any]] = None


class AgentErrorResponse(BaseModel):
    error: AgentErrorBody

