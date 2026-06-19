from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ...core import ApiError
from ...schemas import ChatMessageRequest, ContextUsageSummary, SessionEventsResponse, SessionListResponse
from ...services.app_services import AppServices
from ...services.chat_protocol import format_sse_event


def create_chat_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/chat/messages")
    async def post_chat_message(project_id: str, payload: ChatMessageRequest, request: Request) -> StreamingResponse:
        response, state = await services.chat.prepare_round(project_id, payload)
        key = (project_id, response.session_id)
        queue = await services.bus.subscribe_live(key)
        services.chat.launch_round(project_id, payload, state)

        async def event_generator() -> AsyncIterator[str]:
            try:
                yield format_sse_event("round_started", response.model_dump())
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event_name, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield format_sse_event(event_name, payload)
                    if event_name in {"round_finished", "round_failed", "round_cancelled"}:
                        break
            finally:
                await services.bus.unsubscribe(key, queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.post("/sessions/{session_id}/rounds/{round_id}/cancel")
    async def cancel_round(project_id: str, session_id: str, round_id: str) -> dict:
        return await services.chat.cancel_round(project_id, session_id, round_id)

    @router.get("/sessions/{session_id}/stream")
    async def stream_session_events(project_id: str, session_id: str, request: Request) -> StreamingResponse:
        services.store.get_project(project_id)
        if not services.store.session_exists(project_id, session_id):
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")
        key = (project_id, session_id)
        snapshot, queue = await services.bus.subscribe(key)

        async def event_generator() -> AsyncIterator[str]:
            try:
                latest_project = services.store.get_project(project_id)
                if not latest_project.is_busy or latest_project.running_session_id != session_id:
                    yield format_sse_event(
                        "stream_closed",
                        {
                            "project_id": project_id,
                            "session_id": session_id,
                            "reason": "not_running",
                        },
                    )
                    return

                yield format_sse_event(
                    "stream_attached",
                    {
                        "project_id": project_id,
                        "session_id": session_id,
                        "round_id": latest_project.running_round_id,
                    },
                )
                for event_name, payload in snapshot:
                    if payload.get("round_id") != latest_project.running_round_id:
                        continue
                    yield format_sse_event(event_name, payload)
                    if event_name in {"round_finished", "round_failed", "round_cancelled"}:
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
                    if event_name in {"round_finished", "round_failed", "round_cancelled"}:
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
        sessions = services.store.list_sessions(project_id, active_session_id=project.active_session_id)
        for session in sessions:
            if session.session_id != project.active_session_id:
                continue
            usage = services.context_manager.context_usage(project_id, session.session_id)
            session.context_usage = ContextUsageSummary.model_validate(usage.model_dump()) if usage else None
        return SessionListResponse(sessions=sessions)

    return router
