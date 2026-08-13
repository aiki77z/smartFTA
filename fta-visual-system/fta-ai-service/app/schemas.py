from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SelectedFile(BaseModel):
    id: str
    name: Optional[str] = None
    file_version_id: Optional[str] = None


class GraphDiff(BaseModel):
    added: List[str] = Field(default_factory=list)
    modified: List[str] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)
    added_edges: List[str] = Field(default_factory=list)
    removed_edges: List[str] = Field(default_factory=list)


class EditRequest(BaseModel):
    instruction: str = Field(..., description="Natural language edit instruction")
    tree_json: Any = Field(..., description="Current fault tree JSON (any supported shape)")
    selected_files: List[SelectedFile] = Field(default_factory=list)


class EditResponse(BaseModel):
    updated_tree_json: Any
    diff: GraphDiff
    rationale: str = ""


class AssistantMessageRequest(BaseModel):
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    canvas_id: Optional[str] = None
    message: str
    current_tree: Any = None
    current_tree_id: Optional[str] = None
    selected_file_version_ids: List[str] = Field(default_factory=list)
    selected_files: List[SelectedFile] = Field(default_factory=list)
    workspace_kb_epoch: Optional[int] = None
    baseline_file_version_ids: List[str] = Field(default_factory=list)
    force_generate: bool = False
    frontend_context: Dict[str, Any] = Field(default_factory=dict)
    frontend_message_id: Optional[str] = None


class AssistantMessageResponse(BaseModel):
    session_id: str
    intent: str
    action: str
    assistant_message: str
    result: Dict[str, Any] = Field(default_factory=dict)
    pending_action: Optional[Dict[str, Any]] = None
    memory: Dict[str, Any] = Field(default_factory=dict)
    agent_trace: List[Dict[str, Any]] = Field(default_factory=list)


class AssistantSessionResponse(BaseModel):
    session_id: str
    memory: Dict[str, Any] = Field(default_factory=dict)
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    pending_actions: List[Dict[str, Any]] = Field(default_factory=list)
    messages_path: Optional[str] = None


class AssistantTruncateRequest(BaseModel):
    frontend_message_id: Optional[str] = None
    keep_before_index: Optional[int] = None


class AssistantTruncateResponse(BaseModel):
    session_id: str
    removed_count: int
    remaining_count: int
    messages_path: Optional[str] = None


class AssistantAgentRunConfirmRequest(BaseModel):
    session_id: Optional[str] = None
    confirmation_id: str = Field(..., min_length=1)
    confirmation_type: str = Field(default="top_event", min_length=1)
    candidate_ref: str = Field(..., min_length=1)
    note: Optional[str] = None
