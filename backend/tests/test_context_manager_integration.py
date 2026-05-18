from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import ChatMessageRequest
from app.services import AppServices

from helpers import ScriptedLLMClient, create_project, make_settings, tool_call, wait_until_idle


@pytest.mark.anyio
async def test_context_manager_compresses_old_session_history(tmp_path: Path) -> None:
    long_text = "历史技术细节" * 80

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["content"] == long_text
        return {"type": "respond", "text": "已记录历史技术细节。" + long_text}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("我前面已经提供了" in content for content in contents)
        assert not any(content == long_text for content in contents[:-1])
        assert messages[-1] == {"role": "user", "content": "继续完善"}
        return {"type": "respond", "text": "继续处理。"}

    llm = ScriptedLLMClient([first_round, second_round])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 200
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.5
    settings.context_recent_full_rounds = 1
    settings.context_compression_timeout = 123
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message=long_text))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=first.session_id, message="继续完善"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    summary_event = next(event for event in events if event.type == "context_summary")
    compressed_messages = summary_event.payload["compressed_messages"]
    bus_events, _ = await services.bus.subscribe((project_id, first.session_id))
    bus_event_names = [name for name, _payload in bus_events]
    assert "context_compression_started" in bus_event_names
    assert "context_compression_completed" in bus_event_names
    compression_payload = llm.generated_json_payloads[-1]
    assert compression_payload["_timeout"] == 123
    assert "target_estimated_tokens" not in compression_payload
    assert "summary" not in summary_event.payload
    assert compressed_messages[-1]["content"].startswith("【上下文说明】")
    usage = services.context_manager.context_usage(project_id, first.session_id)
    assert usage is not None
    assert usage.used_tokens > 0

@pytest.mark.anyio
async def test_context_manager_compresses_before_temporary_fit_hides_over_limit(tmp_path: Path) -> None:
    long_text = "历史技术细节" * 1200

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["content"] == long_text
        return {"type": "respond", "text": "已记录历史技术细节。" + long_text}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("我前面已经提供了" in content for content in contents)
        assert messages[-1] == {"role": "user", "content": "继续完善"}
        return {"type": "respond", "text": "继续处理。"}

    llm = ScriptedLLMClient([first_round, second_round])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 1000
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.5
    settings.context_recent_full_rounds = 1
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message=long_text))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=first.session_id, message="继续完善"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    summary_event = next(event for event in events if event.type == "context_summary")
    assert summary_event.payload["compressed_messages"][-1]["content"].startswith("【上下文说明】")
