from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import ChatMessageRequest
from app.runtime.context.compression import COMPRESSED_MEMORY_PREFIX
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
        assert any("## 当前任务" in content for content in contents)
        assert any("最新用户输入：继续完善" in content for content in contents)
        assert not any(content == long_text for content in contents)
        assert messages[-1]["content"].startswith("【上下文恢复说明】")
        return {"type": "respond", "text": "继续处理。"}

    llm = ScriptedLLMClient([first_round, second_round])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 1000
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.4
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
    bus_events, _ = await services.bus.subscribe((project_id, first.session_id))
    bus_event_names = [name for name, _payload in bus_events]
    assert "context_compression_started" in bus_event_names
    assert "context_compression_completed" in bus_event_names
    compression_payload = llm.generated_text_prompts[-1]
    assert compression_payload["_timeout"] == 123
    assert "你是本系统的主 agent" in compression_payload["system_prompt"]
    assert "上下文滚动压缩 agent" not in compression_payload["system_prompt"]
    assert "请只执行上下文滚动压缩" in compression_payload["user_prompt"]
    assert "target_estimated_tokens" not in compression_payload
    assert "compressible_messages" not in compression_payload
    assert "messages_to_merge" not in compression_payload
    assert len(compression_payload["messages"]) >= 3
    assert "summary" not in summary_event.payload
    assert summary_event.payload["compression_mode"] == "rolling_markdown_memory"
    assert summary_event.payload["compressed_markdown"].startswith("## 当前任务")
    usage = services.context_manager.context_usage(project_id, first.session_id)
    assert usage is not None
    assert usage.used_tokens > 0


@pytest.mark.anyio
async def test_context_manager_rolls_previous_summary_into_next_summary(tmp_path: Path) -> None:
    first_text = "历史技术细节" * 60
    second_reply = "第二轮新增材料" * 120

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["content"] == first_text
        return {"type": "respond", "text": "第一轮记录。" + first_text}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("最新用户输入：继续完善" in content for content in contents)
        return {"type": "respond", "text": second_reply}

    def third_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("上一轮摘要长度：" in content for content in contents)
        assert any("最新用户输入：继续第三轮" in content for content in contents)
        return {"type": "respond", "text": "第三轮继续。"}

    llm = ScriptedLLMClient([first_round, second_round, third_round])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 1000
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.35
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message=first_text))
    await wait_until_idle(services, project_id)
    await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=first.session_id, message="继续完善"),
    )
    await wait_until_idle(services, project_id)
    await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=first.session_id, message="继续第三轮"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    summaries = [event for event in events if event.type == "context_summary"]
    assert len(summaries) == 2
    assert len(llm.generated_text_prompts) == 2
    assert not any(
        str(message.get("content") or "").startswith(COMPRESSED_MEMORY_PREFIX)
        for message in llm.generated_text_prompts[0]["messages"]
    )
    previous_summary_message = llm.generated_text_prompts[1]["messages"][0]
    assert previous_summary_message["role"] == "user"
    assert previous_summary_message["content"] == (
        f"{COMPRESSED_MEMORY_PREFIX}\n\n{summaries[0].payload['compressed_markdown']}"
    )
    assert summaries[1].payload["cursor_seq_after"] > summaries[0].payload["cursor_seq_after"]


@pytest.mark.anyio
async def test_context_manager_compresses_before_emergency_trim_hides_over_limit(tmp_path: Path) -> None:
    long_text = "历史技术细节" * 1200

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["content"] == long_text
        return {"type": "respond", "text": "已记录历史技术细节。" + long_text}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("## 当前任务" in content for content in contents)
        assert any("最新用户输入：继续完善" in content for content in contents)
        assert messages[-1]["content"].startswith("【上下文恢复说明】")
        return {"type": "respond", "text": "继续处理。"}

    llm = ScriptedLLMClient([first_round, second_round])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 10000
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.5
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
    assert summary_event.payload["compressed_markdown"].startswith("## 当前任务")


@pytest.mark.anyio
async def test_context_manager_rechecks_context_before_each_tool_followup(tmp_path: Path) -> None:
    old_text = "历史技术细节" * 80

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["content"] == old_text
        return {"type": "respond", "text": "已记录。"}

    def second_round_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert not any("## 当前任务" in content for content in contents)
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("document_read", {"action": "get_section", "section_id": "sec_000003"}, "call_read")
            ],
        }

    def second_round_after_tool(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("## 当前任务" in content for content in contents)
        assert not any(message.get("role") == "tool" and message.get("tool_call_id") == "call_read" for message in messages)
        assert messages[-1]["content"].startswith("【上下文恢复说明】")
        return {"type": "respond", "text": "继续处理。"}

    llm = ScriptedLLMClient([first_round, second_round_read, second_round_after_tool])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 520
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.5
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message=old_text))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=first.session_id, message="读取背景后继续。"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    assert any(event.type == "context_summary" for event in events)
    assert len(llm.generated_text_prompts) == 1
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call_read"
        for message in llm.generated_text_prompts[0]["messages"]
    )
