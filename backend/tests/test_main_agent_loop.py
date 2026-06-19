from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.schemas import ChatMessageRequest
from app.services import AppServices

from helpers import ScriptedLLMClient, create_project, make_settings, tool_call, wait_until_idle


def section_by_title(disclosure: dict[str, Any], title: str) -> dict[str, Any]:
    for section in disclosure["sections"]:
        if section["title"]["text"] == title:
            return section
    raise AssertionError(f"section not found: {title}")


@pytest.mark.anyio
async def test_main_agent_loop_full_flow(tmp_path: Path) -> None:
    """覆盖 disclosure_read_section -> disclosure_edit -> respond 的主链路。"""

    def step_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_read_section",
                    {"section_id": "sec_000008", "limit": 20},
                    "call_1",
                )
            ],
        }

    def step_edit(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [m for m in messages if m.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert last_tool["tool_call_id"] == "call_1"
        assert result["status"] == "success"
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000008",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "正文占位。"},
                    },
                    "call_2",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "respond", "text": "已更新关键创新点及权利要求建议。"}

    llm = ScriptedLLMClient([step_read, step_edit, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充关键创新点及权利要求建议。"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    event_types = [event.type for event in events]
    main_tool_calls = [
        event.payload.get("tool")
        for event in events
        if event.type == "tool_call" and event.scope == "main"
    ]
    assert main_tool_calls.count("disclosure_read_section") == 1
    assert main_tool_calls.count("disclosure_edit") == 1
    assert all(not event.scope.startswith("subagent:") for event in events)
    assert "agent_output" in event_types
    assert event_types[-1] == "agent_output"

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    bus_event_names = [name for name, _ in bus_events]
    assert "document_changed" in bus_event_names
    assert bus_event_names[-1] == "round_finished"
    round_finished_payload = bus_events[-1][1]
    assert round_finished_payload["reply"] == "已更新关键创新点及权利要求建议。"
    assert round_finished_payload["changed"] is True

    disclosure = services.store.get_disclosure(project_id)
    innovation_claims = section_by_title(disclosure, "关键创新点及权利要求建议")
    assert len(innovation_claims["blocks"]) >= 1


@pytest.mark.anyio
async def test_main_agent_loop_allows_document_write_without_innovation_kernel(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000008",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "should not write"},
                    },
                    "call_write",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        return {"type": "respond", "text": "Document updated."}

    llm = ScriptedLLMClient([step_write, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="Write the disclosure."))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    tool_result = next(event for event in events if event.type == "tool_result" and event.call_id == "call_write")
    assert tool_result.payload["status"] == "success"

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
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
                tool_call("disclosure_read_section", {"section_id": "sec_000002"}, "call_a"),
                tool_call("disclosure_read_section", {"section_id": "sec_000003"}, "call_b"),
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
        if event.type == "tool_call" and event.scope == "main" and event.payload.get("tool") == "disclosure_read_section"
    ]
    assert len(main_reads) == 2

@pytest.mark.anyio
async def test_main_agent_loop_persists_tool_call_preamble(tmp_path: Path) -> None:
    def step_read_with_preamble(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("disclosure_read_section", {"section_id": "sec_000002"}, "call_preamble")
            ],
            "assistant_message": {
                "role": "assistant",
                "content": "我先读取技术领域，然后继续判断。",
                "tool_calls": [
                    {
                        "id": "call_preamble",
                        "type": "function",
                        "function": {
                            "name": "disclosure_read_section",
                            "arguments": json.dumps(
                                {"section_id": "sec_000002"},
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

    for _ in range(50):
        events = services.store.read_session_events(project_id, response.session_id)
        if any(event.type == "session_title" for event in events):
            break
        await asyncio.sleep(0.01)
    else:
        events = services.store.read_session_events(project_id, response.session_id)
    round_events = [event for event in events if event.round_id == response.round_id]
    title_events = [event for event in round_events if event.type == "session_title"]
    loop_events = [event for event in round_events if event.type != "session_title"]
    assert title_events[-1].payload["title"] == "低算力实时保护"
    assert [event.type for event in loop_events] == [
        "user_input",
        "agent_message",
        "agent_output",
        "tool_call",
        "tool_result",
        "agent_message",
        "agent_output",
    ]
    assert loop_events[1].payload["message"]["content"] == "我先读取技术领域，然后继续判断。"
    assert loop_events[2].payload["text"] == "我先读取技术领域，然后继续判断。"
    assert loop_events[-1].payload["text"] == "读取完成。"

@pytest.mark.anyio
async def test_main_agent_loop_recovers_invalid_tool_arguments_json(tmp_path: Path) -> None:
    error_message = "disclosure_edit 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_invalid_tool_arguments_json(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "tool": "disclosure_edit",
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
        "tool": "disclosure_edit",
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
    error_message = "disclosure_edit 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_mixed_calls(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("disclosure_read_section", {"section_id": "sec_000002"}, "valid_call_1"),
                {
                    "tool": "disclosure_edit",
                    "arguments": {},
                    "tool_call_id": "bad_call",
                    "arguments_error": error_message,
                },
                tool_call("disclosure_read_section", {"section_id": "sec_000003"}, "valid_call_2"),
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
            "tool_calls": [tool_call("disclosure_read_section", {"section_id": "not_exist_section"}, "call_fail")],
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
