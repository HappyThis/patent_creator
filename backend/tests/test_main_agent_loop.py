from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import ChatMessageRequest
from app.services import AppServices

from helpers import ScriptedLLMClient, create_project, make_settings, tool_call, wait_until_idle


def create_kernel_session(services: AppServices, project_id: str, session_id: str = "sess_with_kernel") -> str:
    services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_seed",
        message_id="msg_seed",
        payload={"text": "seed innovation kernel"},
    )
    services.store.save_innovation_kernel(
        project_id,
        session_id,
        kernel_markdown=(
            "# Innovation Kernel\n\n"
            "## Core Problem\n"
            "A reliable kernel must exist before disclosure text is edited.\n\n"
            "## Technical Direction\n"
            "Use the kernel as the factual basis for document writing."
        ),
        source="create",
    )
    return session_id


@pytest.mark.anyio
async def test_main_agent_loop_full_flow(tmp_path: Path) -> None:
    """覆盖 read -> document_replace_section_blocks -> respond 的主 agent-only 链路。"""

    def step_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_read",
                    {"action": "get_section", "section_id": "sec_000010", "include_children": True},
                    "call_1",
                )
            ],
        }

    def step_edit(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [m for m in messages if m.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_replace_section_blocks",
                    {"section_id": "sec_000010", "blocks": [{"type": "paragraph", "text": "正文占位。"}]},
                    "call_2",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "respond", "text": "已更新技术效果。"}

    llm = ScriptedLLMClient([step_read, step_edit, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)
    session_id = create_kernel_session(services, project_id)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(session_id=session_id, message="请补充技术效果。"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, session_id)
    event_types = [event.type for event in events]
    main_tool_calls = [
        event.payload.get("tool")
        for event in events
        if event.type == "tool_call" and event.scope == "main"
    ]
    assert main_tool_calls.count("document_read") == 1
    assert main_tool_calls.count("document_replace_section_blocks") == 1
    assert all(not event.scope.startswith("subagent:") for event in events)
    assert "agent_output" in event_types
    assert event_types[-1] == "agent_output"

    bus_events, _ = await services.bus.subscribe((project_id, session_id))
    bus_event_names = [name for name, _ in bus_events]
    assert "document_changed" in bus_event_names
    assert bus_event_names[-1] == "round_finished"
    round_finished_payload = bus_events[-1][1]
    assert round_finished_payload["reply"] == "已更新技术效果。"
    assert round_finished_payload["changed"] is True

    disclosure = services.store.get_disclosure(project_id)
    technical_effects = next(s for s in disclosure["sections"] if s["type"] == "technical_effects")
    assert len(technical_effects["blocks"]) >= 1


@pytest.mark.anyio
async def test_main_agent_loop_blocks_document_write_without_innovation_kernel(tmp_path: Path) -> None:
    def step_write_without_kernel(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_replace_section_blocks",
                    {"section_id": "sec_000010", "blocks": [{"type": "paragraph", "text": "should not write"}]},
                    "call_write_without_kernel",
                )
            ],
        }

    def step_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "failed"
        assert result["output"]["code"] == "innovation_kernel_required"
        assert "innovation_kernel_kit" in result["output"]["message"]
        return {"type": "respond", "text": "Need an innovation kernel first."}

    llm = ScriptedLLMClient([step_write_without_kernel, step_recover])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="Write the disclosure."))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    failed_tool_result = next(
        event
        for event in events
        if event.type == "tool_result" and event.call_id == "call_write_without_kernel"
    )
    assert failed_tool_result.payload["status"] == "failed"
    assert failed_tool_result.payload["output"]["code"] == "innovation_kernel_required"

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert "document_changed" not in names
    assert names[-1] == "round_finished"
    assert bus_events[-1][1]["changed"] is False


@pytest.mark.anyio
async def test_main_agent_loop_allows_document_write_with_current_innovation_kernel(tmp_path: Path) -> None:
    def step_write_with_kernel(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_replace_section_blocks",
                    {"section_id": "sec_000010", "blocks": [{"type": "paragraph", "text": "allowed write"}]},
                    "call_write_with_kernel",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        return {"type": "respond", "text": "Updated with current innovation kernel."}

    llm = ScriptedLLMClient([step_write_with_kernel, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)
    session_id = create_kernel_session(services, project_id, "sess_current_kernel")

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=session_id, message="Write the disclosure."))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, session_id)
    tool_result = next(event for event in events if event.type == "tool_result" and event.call_id == "call_write_with_kernel")
    assert tool_result.payload["status"] == "success"

    bus_events, _ = await services.bus.subscribe((project_id, session_id))
    names = [name for name, _ in bus_events]
    assert "document_changed" in names
    assert names[-1] == "round_finished"
    assert bus_events[-1][1]["changed"] is True

@pytest.mark.anyio
async def test_main_agent_loop_handles_multiple_tool_calls_in_one_assistant_message(tmp_path: Path) -> None:
    def step_multi_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("document_read", {"action": "get_section", "section_id": "sec_000002"}, "call_a"),
                tool_call("document_read", {"action": "get_section", "section_id": "sec_000003"}, "call_b"),
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assistant_index = next(index for index, message in enumerate(messages) if message.get("role") == "assistant")
        following_tools = messages[assistant_index + 1 : assistant_index + 3]
        assert [message.get("role") for message in following_tools] == ["tool", "tool"]
        assert [message.get("tool_call_id") for message in following_tools] == ["call_a", "call_b"]
        return {"type": "respond", "text": "已读取两个章节。"}

    llm = ScriptedLLMClient([step_multi_read, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="先读技术领域和背景技术。"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    main_reads = [
        event
        for event in events
        if event.type == "tool_call" and event.scope == "main" and event.payload.get("tool") == "document_read"
    ]
    assert len(main_reads) == 2

@pytest.mark.anyio
async def test_main_agent_loop_persists_tool_call_preamble(tmp_path: Path) -> None:
    def step_read_with_preamble(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("document_read", {"action": "get_section", "section_id": "sec_000002"}, "call_preamble")
            ],
            "assistant_message": {
                "role": "assistant",
                "content": "我先读取技术领域，然后继续判断。",
                "tool_calls": [
                    {
                        "id": "call_preamble",
                        "type": "function",
                        "function": {
                            "name": "document_read",
                            "arguments": json.dumps(
                                {"action": "get_section", "section_id": "sec_000002"},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            },
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("role") == "tool" and message.get("tool_call_id") == "call_preamble" for message in messages)
        return {"type": "respond", "text": "读取完成。"}

    llm = ScriptedLLMClient([step_read_with_preamble, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="先看技术领域。"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    round_events = [event for event in events if event.round_id == response.round_id]
    assert [event.type for event in round_events] == [
        "user_input",
        "agent_message",
        "agent_output",
        "tool_call",
        "tool_result",
        "agent_message",
        "agent_output",
    ]
    assert round_events[1].payload["message"]["content"] == "我先读取技术领域，然后继续判断。"
    assert round_events[2].payload["text"] == "我先读取技术领域，然后继续判断。"
    assert round_events[-1].payload["text"] == "读取完成。"

@pytest.mark.anyio
async def test_main_agent_loop_recovers_invalid_tool_arguments_json(tmp_path: Path) -> None:
    error_message = "document_append_block 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_invalid_tool_arguments_json(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "tool": "document_append_block",
                    "arguments": {},
                    "tool_call_id": "bad_call",
                    "arguments_error": error_message,
                }
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert last_tool["tool_call_id"] == "bad_call"
        assert result == {
            "status": "failed",
            "output": {
                "code": "invalid_tool_arguments_json",
                "message": error_message,
            },
        }
        return {"type": "respond", "text": "已识别参数格式错误并重新规划。"}

    llm = ScriptedLLMClient([step_invalid_tool_arguments_json, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请修改技术方案。"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    failed_tool_result = next(
        event
        for event in events
        if event.type == "tool_result" and event.call_id == "bad_call"
    )
    assert failed_tool_result.payload == {
        "tool": "document_append_block",
        "status": "failed",
        "output": {
            "code": "invalid_tool_arguments_json",
            "message": error_message,
        },
    }

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert "round_failed" not in names
    finished_payload = next(payload for name, payload in bus_events if name == "tool_call_finished")
    assert finished_payload["call_id"] == "bad_call"
    assert finished_payload["summary"] == "执行失败"
    assert finished_payload["result"] == {
        "status": "failed",
        "output": {
            "code": "invalid_tool_arguments_json",
            "message": error_message,
        },
    }
    assert names[-1] == "round_finished"
    assert bus_events[-1][1]["reply"] == "已识别参数格式错误并重新规划。"

@pytest.mark.anyio
async def test_main_agent_loop_handles_invalid_arguments_inside_multiple_tool_calls(tmp_path: Path) -> None:
    error_message = "document_append_block 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_mixed_calls(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("document_read", {"action": "get_section", "section_id": "sec_000002"}, "valid_call_1"),
                {
                    "tool": "document_append_block",
                    "arguments": {},
                    "tool_call_id": "bad_call",
                    "arguments_error": error_message,
                },
                tool_call("document_read", {"action": "get_section", "section_id": "sec_000003"}, "valid_call_2"),
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in tool_messages[-3:]] == [
            "valid_call_1",
            "bad_call",
            "valid_call_2",
        ]
        tool_results = [json.loads(message["content"]) for message in tool_messages[-3:]]
        assert tool_results[0]["status"] == "success"
        assert tool_results[1] == {
            "status": "failed",
            "output": {
                "code": "invalid_tool_arguments_json",
                "message": error_message,
            },
        }
        assert tool_results[2]["status"] == "success"
        return {"type": "respond", "text": "已收到完整工具结果。"}

    llm = ScriptedLLMClient([step_mixed_calls, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="测试混合工具调用。"))
    await wait_until_idle(services, project_id)

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert "round_failed" not in names
    assert names[-1] == "round_finished"
    assert bus_events[-1][1]["reply"] == "已收到完整工具结果。"

@pytest.mark.anyio
async def test_main_agent_loop_tool_failed_triggers_round_failed(tmp_path: Path) -> None:
    """工具返回 failed 时应回填给模型继续决策，而不是直接炸掉整轮。"""

    def step_bad_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("document_read", {"action": "get_section", "section_id": "not_exist_section"}, "call_fail")],
        }

    def step_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "failed"
        assert result["output"]["code"] == "section_not_found"
        return {"type": "respond", "text": "没有找到对应章节，我需要换一个有效章节。"}

    llm = ScriptedLLMClient([step_bad_read, step_recover])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="读一下不存在的章节"),
    )
    await wait_until_idle(services, project_id)

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert "tool_call_finished" in names
    assert names[-1] == "round_finished"
    assert bus_events[-1][1]["reply"] == "没有找到对应章节，我需要换一个有效章节。"

@pytest.mark.anyio
async def test_main_agent_restores_session_history_between_rounds(tmp_path: Path) -> None:
    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1] == {"role": "user", "content": "第一轮问题"}
        return {"type": "respond", "text": "第一轮回答"}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert {"role": "user", "content": "第一轮问题"} in messages
        assert {"role": "assistant", "content": "第一轮回答"} in messages
        assert messages[-1] == {"role": "user", "content": "继续"}
        return {"type": "respond", "text": "第二轮回答"}

    llm = ScriptedLLMClient([first_round, second_round])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="第一轮问题"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="继续"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    assistant_outputs = [event.payload.get("text") for event in events if event.type == "agent_output"]
    assert assistant_outputs == ["第一轮回答", "第二轮回答"]

@pytest.mark.anyio
async def test_main_agent_saves_reasoning_but_filters_mimo_replay(tmp_path: Path) -> None:
    def first_round(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "respond", "text": "第一轮回答"}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        restored_assistant = next(message for message in messages if message.get("role") == "assistant")
        assert restored_assistant["content"] == "第一轮回答"
        assert "reasoning_content" not in restored_assistant
        return {"type": "respond", "text": "第二轮回答"}

    llm = ScriptedLLMClient([first_round, second_round])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="第一轮问题"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="继续"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, first.session_id)
    agent_message = next(event for event in events if event.type == "agent_message")
    assert agent_message.payload["message"]["reasoning_content"] == "测试推理内容。"

@pytest.mark.anyio
async def test_main_agent_replays_reasoning_for_deepseek_enabled(tmp_path: Path) -> None:
    def first_round(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "respond", "text": "第一轮回答"}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        restored_assistant = next(message for message in messages if message.get("role") == "assistant")
        assert restored_assistant["reasoning_content"] == "测试推理内容。"
        return {"type": "respond", "text": "第二轮回答"}

    settings = make_settings(tmp_path)
    settings.openai_compat_provider = "deepseek"
    settings.openai_compat_thinking = "enabled"
    llm = ScriptedLLMClient([first_round, second_round])
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="第一轮问题"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="继续"))
    await wait_until_idle(services, project_id)

@pytest.mark.anyio
async def test_main_agent_restores_main_tool_results_between_rounds(tmp_path: Path) -> None:
    def first_round_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_read",
                    {"action": "get_section", "section_id": "sec_000002"},
                    "call_restore_read",
                )
            ],
        }

    def first_round_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "call_restore_read"
        assert json.loads(last_tool["content"])["status"] == "success"
        return {"type": "respond", "text": "第一轮已读取。"}

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
        assert restored_tool_calls[0]["function"]["name"] == "document_read"

        restored_tool = next(
            message
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_restore_read"
        )
        restored_result = json.loads(restored_tool["content"])
        assert restored_result["status"] == "success"
        assert "output" in restored_result
        assert messages[-1] == {"role": "user", "content": "继续判断"}
        return {"type": "respond", "text": "第二轮看到了历史工具结果。"}

    llm = ScriptedLLMClient([first_round_read, first_round_respond, second_round])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="先读技术领域"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="继续判断"))
    await wait_until_idle(services, project_id)
