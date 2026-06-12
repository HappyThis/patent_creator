from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectRecord(BaseModel):
    project_id: str
    title: str
    created_at: str
    updated_at: str
    schema_version: str = "v2"
    active_session_id: str | None = None
    running_session_id: str | None = None
    running_round_id: str | None = None
    is_busy: bool = False


class ProjectSummary(BaseModel):
    project_id: str
    title: str
    created_at: str
    updated_at: str | None = None
    active_session_id: str | None = None
    running_session_id: str | None = None
    running_round_id: str | None = None
    is_busy: bool = False
    active_session_context: "ContextUsageSummary | None" = None


class ContextUsageSummary(BaseModel):
    max_tokens: int
    used_tokens: int
    used_ratio: float
    threshold_tokens: int
    reserved_output_tokens: int
    status: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectSummary]


class OutlineItem(BaseModel):
    id: str
    type: str
    title: str
    level: int
    anchor: str
    children: list["OutlineItem"] = Field(default_factory=list)


class OutlineResponse(BaseModel):
    sections: list[OutlineItem]


class RenderResponse(BaseModel):
    render_ast: dict[str, Any]
    active_section_id: str | None = None
    active_block_id: str | None = None
    updated_at: str


class ChatMessageRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1)
    active_section_id: str | None = None
    active_block_id: str | None = None


class ChatMessageResponse(BaseModel):
    accepted: bool
    session_id: str
    message_id: str
    round_id: str
    first_user_text: str


class SessionEvent(BaseModel):
    id: str
    ts: str
    type: Literal[
        "user_input",
        "agent_message",
        "agent_output",
        "tool_call",
        "tool_result",
        "context_summary",
        "context_pruned",
    ]
    seq: int
    scope: str
    round_id: str
    message_id: str
    call_id: str | None = None
    parent_call_id: str | None = None
    payload: dict[str, Any]


class SessionEventsResponse(BaseModel):
    events: list[SessionEvent]


class SessionSummary(BaseModel):
    session_id: str
    updated_at: str
    event_count: int
    last_round_id: str | None = None
    first_user_text: str | None = None
    is_active: bool = False
    context_usage: ContextUsageSummary | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class ExportResponse(BaseModel):
    path: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


OutlineItem.model_rebuild()
ProjectSummary.model_rebuild()
