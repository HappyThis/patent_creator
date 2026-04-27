from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ProjectRecord(BaseModel):
    project_id: str
    title: str
    created_at: str
    updated_at: str
    schema_version: str = "v1"
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


class OutlineItem(BaseModel):
    id: str
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


class SessionEvent(BaseModel):
    id: str
    ts: str
    type: Literal["user_input", "agent_output", "tool_call", "tool_result"]
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
    latest_user_text: str | None = None
    is_active: bool = False


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
