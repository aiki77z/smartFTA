from __future__ import annotations

from typing import Final, Set


CONTRACT_VERSION: Final[int] = 1

RUN_STATUS_QUEUED: Final[str] = "queued"
RUN_STATUS_RUNNING: Final[str] = "running"
RUN_STATUS_WAITING_CONFIRMATION: Final[str] = "waiting_confirmation"
RUN_STATUS_COMPLETED: Final[str] = "completed"
RUN_STATUS_FAILED: Final[str] = "failed"
RUN_STATUS_CANCELLED: Final[str] = "cancelled"
RUN_STATUS_HUMAN_REVIEW_REQUIRED: Final[str] = "human_review_required"

TERMINAL_RUN_STATUSES: Final[Set[str]] = {
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_HUMAN_REVIEW_REQUIRED,
}

STAGE_CREATED: Final[str] = "created"
STAGE_SCOPE: Final[str] = "scope"
STAGE_RETRIEVAL: Final[str] = "retrieval"
STAGE_PLANNING: Final[str] = "planning"
STAGE_GENERATION: Final[str] = "generation"
STAGE_VALIDATION: Final[str] = "validation"
STAGE_REPAIR: Final[str] = "repair"
STAGE_COMMIT: Final[str] = "commit"
STAGE_CURATE: Final[str] = "curate"
STAGE_DONE: Final[str] = "done"

EVENT_RUN_CREATED: Final[str] = "RUN_CREATED"
EVENT_STAGE_STARTED: Final[str] = "STAGE_STARTED"
EVENT_STAGE_COMPLETED: Final[str] = "STAGE_COMPLETED"
EVENT_AGENT_MESSAGE: Final[str] = "AGENT_MESSAGE"
EVENT_CONFIRMATION_REQUIRED: Final[str] = "CONFIRMATION_REQUIRED"
EVENT_CONFIRMATION_RECEIVED: Final[str] = "CONFIRMATION_RECEIVED"
EVENT_ARTIFACT_CREATED: Final[str] = "ARTIFACT_CREATED"
EVENT_SCOPE_RESOLVED: Final[str] = "SCOPE_RESOLVED"
EVENT_RETRIEVAL_DONE: Final[str] = "RETRIEVAL_DONE"
EVENT_DRAFT_GENERATED: Final[str] = "DRAFT_GENERATED"
EVENT_VALIDATION_DONE: Final[str] = "VALIDATION_DONE"
EVENT_TREE_COMMITTED: Final[str] = "TREE_COMMITTED"
EVENT_RUN_COMPLETED: Final[str] = "RUN_COMPLETED"
EVENT_RUN_FAILED: Final[str] = "RUN_FAILED"

ARTIFACT_REQUIREMENT: Final[str] = "requirement"
ARTIFACT_SCOPE: Final[str] = "scope"
ARTIFACT_RETRIEVAL_CONTEXT: Final[str] = "retrieval_context"
ARTIFACT_TREE_DRAFT: Final[str] = "tree_draft"
ARTIFACT_REPAIR_PATCH: Final[str] = "repair_patch"
ARTIFACT_VALIDATION_REPORT: Final[str] = "validation_report"
ARTIFACT_FINAL_TREE: Final[str] = "final_tree"

ERROR_INVALID_REQUEST: Final[str] = "INVALID_REQUEST"
ERROR_RUN_NOT_FOUND: Final[str] = "RUN_NOT_FOUND"
ERROR_INVALID_RUN_STATE: Final[str] = "INVALID_RUN_STATE"
ERROR_CONFIRMATION_NOT_FOUND: Final[str] = "CONFIRMATION_NOT_FOUND"
ERROR_INVALID_CANDIDATE_REF: Final[str] = "INVALID_CANDIDATE_REF"
ERROR_WORKFLOW_FAILED: Final[str] = "WORKFLOW_FAILED"

