from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.schemas import ChatMessageRequest
from app.services import AppServices

from helpers import ScriptedLLMClient, create_project, make_settings, tool_call, wait_until_idle


@pytest.mark.anyio
async def test_main_agent_restores_session_history_between_rounds(tmp_path: Path) -> None:
    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1] == {"role": "user", "content": "first question"}
        return {"type": "respond", "text": "first reply"}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert {"role": "user", "content": "first question"} in messages
        assert {"role": "assistant", "content": "first reply"} in messages
        assert messages[-1] == {"role": "user", "content": "continue"}
        return {"type": "respond", "text": "second reply"}

    llm = ScriptedLLMClient([first_round, second_round])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="first question"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="continue"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    assistant_outputs = [event.payload.get("text") for event in events if event.type == "agent_output"]
    assert assistant_outputs == ["first reply", "second reply"]


@pytest.mark.anyio
async def test_main_agent_restores_main_tool_results_between_rounds(tmp_path: Path) -> None:
    def first_round_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_read_section",
                    {"section_id": "sec_000002"},
                    "call_restore_read",
                )
            ],
        }

    def first_round_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "call_restore_read"
        assert json.loads(last_tool["content"])["status"] == "success"
        return {"type": "respond", "text": "read done"}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assistant_tool_messages = [
            message
            for message in messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        restored_tool_calls = [
            call
            for message in assistant_tool_messages
            for call in message["tool_calls"]
            if call.get("id") == "call_restore_read"
        ]
        assert restored_tool_calls
        assert restored_tool_calls[0]["function"]["name"] == "disclosure_read_section"

        restored_tool = next(
            message
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_restore_read"
        )
        restored_result = json.loads(restored_tool["content"])
        assert restored_result["status"] == "success"
        assert "output" in restored_result
        assert messages[-1] == {"role": "user", "content": "continue judging"}
        return {"type": "respond", "text": "history tool result visible"}

    llm = ScriptedLLMClient([first_round_read, first_round_respond, second_round])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="read technical field"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=first.session_id, message="continue judging"),
    )
    await wait_until_idle(services, project_id)
