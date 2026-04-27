from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ...core import ApiError
from ...schemas import ChatMessageRequest, ChatMessageResponse, SessionEventsResponse, SessionListResponse
from ...services.app_services import AppServices
from ...services.chat import format_sse_event


def create_chat_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/chat/messages", response_model=ChatMessageResponse)
    async def post_chat_message(project_id: str, payload: ChatMessageRequest) -> ChatMessageResponse:
        services.store.get_project(project_id)
        return await services.chat.start_round(project_id, payload)

    @router.get("/chat/stream")
    async def chat_stream(project_id: str, session_id: str, request: Request) -> StreamingResponse:
        services.store.get_project(project_id)
        key = (project_id, session_id)
        if not services.store.session_exists(project_id, session_id) and not services.bus.has_history(key):
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")

        async def event_generator() -> AsyncIterator[str]:
            snapshot, queue = await services.bus.subscribe(key)
            try:
                for event_name, payload in snapshot:
                    yield format_sse_event(event_name, payload)
                    if event_name in {"round_finished", "round_failed"}:
                        return

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event_name, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield format_sse_event(event_name, payload)
                    if event_name in {"round_finished", "round_failed"}:
                        break
            finally:
                await services.bus.unsubscribe(key, queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.get("/sessions/{session_id}/events", response_model=SessionEventsResponse)
    async def get_session_events(project_id: str, session_id: str) -> SessionEventsResponse:
        services.store.get_project(project_id)
        return SessionEventsResponse(events=services.store.read_session_events(project_id, session_id))

    @router.get("/sessions", response_model=SessionListResponse)
    async def list_sessions(project_id: str) -> SessionListResponse:
        project = services.store.get_project(project_id)
        return SessionListResponse(
            sessions=services.store.list_sessions(project_id, active_session_id=project.active_session_id)
        )

    return router
