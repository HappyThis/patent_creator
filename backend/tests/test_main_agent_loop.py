from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.core.config import Settings
from app.schemas import ChatMessageRequest
from app.services import AppServices


class ScriptedLLMClient:
    """驱动主 agent loop 的脚本化 stub。

    - generate_with_tools_stream 按外部提供的 script 顺序返回 action。
    - 未脚本化子 agent 时，默认通过 submit_result 提交 section_writer 结果。
    """

    def __init__(
        self,
        script: list[Callable[[list[dict[str, Any]]], dict[str, Any]]],
        *,
        script_subagents: bool = False,
    ) -> None:
        self._script = list(script)
        self._cursor = 0
        self._script_subagents = script_subagents

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        response_format_json: bool = False,
    ) -> dict[str, Any]:
        if "子 agent：" in system_prompt and not self._script_subagents:
            context = json.loads(messages[0]["content"])
            target_section_id = context["task"]["target_section_id"]
            arguments = {
                "summary": "已生成候选正文。",
                "reply": "已补充目标章节。",
                "rationale": "根据用户输入生成候选。",
                "proposal_type": "document_edit_proposal",
                "proposal": {
                    "operations": [
                        {
                            "op": "replace_section_blocks",
                            "section_id": target_section_id,
                            "blocks": [{"type": "paragraph", "text": "正文占位。"}],
                        }
                    ],
                },
                "questions": [],
                "warnings": [],
            }
            return {
                "type": "tool_calls",
                "tool_calls": [tool_call("submit_result", arguments, "sub_submit_1")],
                "assistant_message": self._build_assistant_message(
                    {"type": "tool_calls", "tool_calls": [tool_call("submit_result", arguments, "sub_submit_1")]}
                ),
            }
        if self._cursor >= len(self._script):
            raise AssertionError("ScriptedLLMClient script exhausted")
        step = self._script[self._cursor]
        self._cursor += 1
        result = step(messages)
        if result.get("type") == "respond" and on_text_delta is not None:
            await on_text_delta(str(result.get("text") or ""))
        if "assistant_message" not in result:
            result = {**result, "assistant_message": self._build_assistant_message(result)}
        return result

    @staticmethod
    def _build_assistant_message(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("type") == "respond":
            return {
                "role": "assistant",
                "content": str(result.get("text") or ""),
                "reasoning_content": "测试推理内容。",
            }
        if result.get("type") in {"tool_call", "tool_calls"}:
            raw_calls = result.get("tool_calls")
            if not isinstance(raw_calls, list):
                raw_calls = [result]
            return {
                "role": "assistant",
                "content": "",
                "reasoning_content": "测试工具调用推理内容。",
                "tool_calls": [
                    {
                        "id": str(call.get("tool_call_id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("tool") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for call in raw_calls
                ],
            }
        return {"role": "assistant", "content": ""}

    async def generate_json(self, *, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> dict[str, Any]:
        if "上下文压缩 agent" in system_prompt:
            context = json.loads(user_prompt)
            event_count = len(context.get("events") or [])
            return {
                "summary": f"系统压缩摘要：此前共有 {event_count} 条主 agent 历史消息，用户正在延续同一任务。",
                "warnings": [],
            }
        context = json.loads(user_prompt)
        target_section_id = context["task"]["target_section_id"]
        return {
            "summary": "已生成候选正文。",
            "reply": "已补充目标章节。",
            "rationale": "根据用户输入生成候选。",
            "operations": [
                {
                    "op": "replace_section_blocks",
                    "section_id": target_section_id,
                    "blocks": [{"type": "paragraph", "text": "正文占位。"}],
                }
            ],
            "questions": [],
            "warnings": [],
        }


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
        round_step_delay=0.0,
        round_finish_delay=0.0,
        main_agent_max_steps=10,
    )


async def wait_until_idle(services: AppServices, project_id: str, timeout: float = 2.0) -> None:
    elapsed = 0.0
    step = 0.01
    while elapsed < timeout:
        if not services.store.get_project(project_id).is_busy:
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError("round did not finish in time")


async def create_project(services: AppServices, title: str = "测试项目") -> str:
    project = services.store.create_project(title)
    return project.project_id


def tool_call(tool: str, arguments: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments, "tool_call_id": tool_call_id}


def test_recover_interrupted_project_marks_round_failed_and_unlocks_project(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_interrupted"
    round_id = "round_interrupted"
    message_id = "msg_interrupted"
    services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id=round_id,
        message_id=message_id,
        payload={"text": "开始长任务"},
    )
    project = services.store.get_project(project_id)
    project.active_session_id = session_id
    project.running_session_id = session_id
    project.running_round_id = round_id
    project.is_busy = True
    services.store.save_project(project)

    recovered = services.store.recover_interrupted_projects()

    assert [item.project_id for item in recovered] == [project_id]
    unlocked = services.store.get_project(project_id)
    assert unlocked.is_busy is False
    assert unlocked.running_session_id is None
    assert unlocked.running_round_id is None

    events = services.store.read_session_events(project_id, session_id)
    failure_event = events[-1]
    assert failure_event.type == "agent_output"
    assert failure_event.round_id == round_id
    assert failure_event.message_id == message_id
    assert failure_event.payload["code"] == "round_interrupted_by_restart"
    assert "后端重启" in failure_event.payload["text"]

    recovered_again = services.store.recover_interrupted_projects()
    assert recovered_again == []
    assert len(services.store.read_session_events(project_id, session_id)) == len(events)


def test_recover_interrupted_project_does_not_infer_missing_running_round(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_stale"
    services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_stale",
        message_id="msg_stale",
        payload={"text": "开始长任务"},
    )
    project = services.store.get_project(project_id)
    project.active_session_id = session_id
    project.running_session_id = None
    project.running_round_id = None
    project.is_busy = True
    services.store.save_project(project)

    recovered = services.store.recover_interrupted_projects()

    assert [item.project_id for item in recovered] == [project_id]
    unlocked = services.store.get_project(project_id)
    assert unlocked.is_busy is False
    assert unlocked.running_session_id is None
    assert unlocked.running_round_id is None
    events = services.store.read_session_events(project_id, session_id)
    assert len(events) == 1


@pytest.mark.anyio
async def test_main_agent_loop_full_flow(tmp_path: Path) -> None:
    """覆盖 read -> execute_subagent -> document_edit -> respond 的完整链路。"""

    def step_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_read",
                    {"action": "get_section", "section_id": "technical_effects", "include_children": True},
                    "call_1",
                )
            ],
        }

    def step_subagent(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "call_type": "rich_context_specialist",
                        "goal": "补充技术效果章节",
                        "target_section_id": "technical_effects",
                        "user_message": "请补充技术效果。",
                    },
                    "call_2",
                )
            ],
        }

    def step_edit(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [m for m in messages if m.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        operations = result["output"]["result"]["proposal"]["operations"]
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("document_edit", {"operations": operations}, "call_3")],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "respond", "text": "已更新技术效果。"}

    llm = ScriptedLLMClient([step_read, step_subagent, step_edit, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充技术效果。"),
    )
    session_id = response.session_id
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, session_id)
    event_types = [event.type for event in events]
    main_tool_calls = [
        event.payload.get("tool")
        for event in events
        if event.type == "tool_call" and event.scope == "main"
    ]
    assert main_tool_calls.count("document_read") == 1
    assert main_tool_calls.count("execute_subagent") == 1
    assert main_tool_calls.count("document_edit") == 1
    # 子 agent 作用域应额外产生 document_read 事件（文档规定的子 agent 读文档行为）
    sub_tool_calls = [
        event.payload.get("tool")
        for event in events
        if event.type == "tool_call" and event.scope.startswith("subagent:")
    ]
    assert "document_read" in sub_tool_calls
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
    technical_effects = next(s for s in disclosure["sections"] if s["id"] == "technical_effects")
    assert len(technical_effects["blocks"]) >= 1


@pytest.mark.anyio
async def test_main_agent_loop_handles_multiple_tool_calls_in_one_assistant_message(tmp_path: Path) -> None:
    def step_multi_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("document_read", {"action": "get_section", "section_id": "technical_field"}, "call_a"),
                tool_call("document_read", {"action": "get_section", "section_id": "background_technology"}, "call_b"),
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
                tool_call("document_read", {"action": "get_section", "section_id": "technical_field"}, "call_preamble")
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
                                {"action": "get_section", "section_id": "technical_field"},
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
        "agent_output",
        "tool_call",
        "tool_result",
        "agent_output",
    ]
    assert round_events[1].payload["text"] == "我先读取技术领域，然后继续判断。"
    assert round_events[-1].payload["text"] == "读取完成。"


@pytest.mark.anyio
async def test_execute_subagent_unknown_agent_returns_tool_failure(tmp_path: Path) -> None:
    def step_unknown_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "missing_writer",
                        "call_type": "rich_context_specialist",
                        "goal": "测试不存在的子 agent。",
                    },
                    "call_missing_subagent",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert last_tool["tool_call_id"] == "call_missing_subagent"
        assert result["status"] == "failed"
        assert result["output"]["code"] == "subagent_not_found"
        return {"type": "respond", "text": "已识别不存在的子 agent。"}

    llm = ScriptedLLMClient([step_unknown_subagent, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="调用不存在的子 agent。"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    failed_tool_result = next(
        event
        for event in events
        if event.type == "tool_result" and event.call_id == "call_missing_subagent"
    )
    assert failed_tool_result.payload["status"] == "failed"
    assert failed_tool_result.payload["output"]["code"] == "subagent_not_found"

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    assert "round_failed" not in [name for name, _ in bus_events]
    assert bus_events[-1][0] == "round_finished"


@pytest.mark.anyio
async def test_main_agent_loop_recovers_invalid_tool_arguments_json(tmp_path: Path) -> None:
    error_message = "document_edit 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_invalid_tool_arguments_json(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "tool": "document_edit",
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
        "tool": "document_edit",
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
    error_message = "document_edit 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_mixed_calls(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("document_read", {"action": "get_section", "section_id": "technical_field"}, "valid_call_1"),
                {
                    "tool": "document_edit",
                    "arguments": {},
                    "tool_call_id": "bad_call",
                    "arguments_error": error_message,
                },
                tool_call("document_read", {"action": "get_section", "section_id": "background_technology"}, "valid_call_2"),
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
async def test_main_agent_loop_max_steps_limit_response(tmp_path: Path) -> None:
    """主 agent 持续 read 不 respond，达到 max_steps 后用兜底回复结束。"""

    def step_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("document_read", {"action": "get_section", "section_id": "technical_effects"}, "call_infinite")],
        }

    max_steps = 3
    llm = ScriptedLLMClient([step_read] * (max_steps + 2))
    settings = make_settings(tmp_path)
    settings.main_agent_max_steps = max_steps
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="hi"),
    )
    await wait_until_idle(services, project_id)

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert names[-1] == "round_finished"
    round_finished_payload = bus_events[-1][1]
    assert "本轮步数已达上限" in round_finished_payload["reply"]
    assert round_finished_payload["changed"] is False


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
async def test_main_agent_restores_main_tool_results_between_rounds(tmp_path: Path) -> None:
    def first_round_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_read",
                    {"action": "get_section", "section_id": "technical_field"},
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


@pytest.mark.anyio
async def test_main_agent_restores_execute_subagent_result_without_subagent_internal_tools(tmp_path: Path) -> None:
    def main_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "call_type": "task_only_specialist",
                        "goal": "读取并总结背景技术。",
                        "target_section_id": "background_technology",
                        "user_message": "读取背景技术",
                    },
                    "call_restore_subagent",
                )
            ],
        }

    def subagent_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_read",
                    {"action": "get_section", "section_id": "background_technology"},
                    "sub_internal_read",
                )
            ],
        }

    def subagent_submit(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(
            message.get("role") == "tool" and message.get("tool_call_id") == "sub_internal_read"
            for message in messages
        )
        arguments = {
            "summary": "已读取背景技术。",
            "reply": "已读取背景技术。",
            "rationale": "测试子 agent 内部工具过程隔离。",
            "proposal_type": "analysis_result",
            "proposal": {"facts": [{"kind": "section_read", "text": "背景技术已读取。"}]},
            "questions": [],
            "warnings": [],
        }
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("submit_result", arguments, "sub_submit_restore")],
        }

    def main_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "call_restore_subagent"
        assert json.loads(last_tool["content"])["status"] == "success"
        return {"type": "respond", "text": "主流程已收到子 agent 最终结果。"}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        restored_main_tool = [
            message
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_restore_subagent"
        ]
        assert restored_main_tool
        assert json.loads(restored_main_tool[0]["content"])["output"]["agent_id"] == "section_writer"
        assert not any(
            message.get("role") == "tool" and message.get("tool_call_id") == "sub_internal_read"
            for message in messages
        )
        assert not any(
            call.get("id") == "sub_internal_read"
            for message in messages
            for call in (message.get("tool_calls") or [])
        )
        return {"type": "respond", "text": "第二轮只看到了子 agent 最终结果。"}

    llm = ScriptedLLMClient(
        [main_call_subagent, subagent_read, subagent_submit, main_respond, second_round],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="让子 agent 读背景技术"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="继续"))
    await wait_until_idle(services, project_id)


@pytest.mark.anyio
async def test_context_manager_compresses_old_session_history(tmp_path: Path) -> None:
    long_text = "历史技术细节" * 80

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["content"] == long_text
        return {"type": "respond", "text": "已记录历史技术细节。" + long_text}

    def second_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [message["content"] for message in messages]
        assert any("系统压缩摘要" in content for content in contents)
        assert not any(content == long_text for content in contents[:-1])
        assert messages[-1] == {"role": "user", "content": "继续完善"}
        return {"type": "respond", "text": "继续处理。"}

    llm = ScriptedLLMClient([first_round, second_round])
    settings = make_settings(tmp_path)
    settings.context_max_tokens = 900
    settings.context_reserved_output_tokens = 100
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
    assert any(event.type == "context_summary" for event in events)
    usage = services.context_manager.context_usage(project_id, first.session_id)
    assert usage is not None
    assert usage.used_tokens > 0


@pytest.mark.anyio
async def test_subagent_plain_response_is_corrected_to_submit_result(tmp_path: Path) -> None:
    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "call_type": "rich_context_specialist",
                        "goal": "补充背景技术",
                        "target_section_id": "background_technology",
                        "user_message": "补充背景技术",
                    },
                    "call_bad_json",
                )
            ],
        }

    def step_subagent_bad_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages
        return {"type": "respond", "text": "这不是 JSON"}

    def step_subagent_submit_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["role"] == "user"
        assert "submit_result" in messages[-1]["content"]
        arguments = {
            "summary": "已生成候选正文。",
            "reply": "已补充背景技术。",
            "rationale": "根据用户输入生成候选。",
            "proposal_type": "document_edit_proposal",
            "proposal": {
                "operations": [
                    {
                        "op": "replace_section_blocks",
                        "section_id": "background_technology",
                        "blocks": [{"type": "paragraph", "text": "背景技术正文。"}],
                    }
                ],
            },
            "questions": [],
            "warnings": [],
        }
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("submit_result", arguments, "sub_submit_retry")],
        }

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        assert result["output"]["result"]["proposal"]["operations"][0]["section_id"] == "background_technology"
        return {"type": "respond", "text": "子 agent 已按 submit_result 重新提交。"}

    llm = ScriptedLLMClient(
        [step_call_subagent, step_subagent_bad_json, step_subagent_submit_result, step_main_recover],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="补充背景技术"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    main_tool_results = [
        event for event in events if event.type == "tool_result" and event.scope == "main" and event.payload.get("tool") == "execute_subagent"
    ]
    assert main_tool_results[-1].payload["status"] == "success"
    assert [event.payload.get("text") for event in events if event.type == "agent_output"][-1] == "子 agent 已按 submit_result 重新提交。"


@pytest.mark.anyio
async def test_subagent_submit_result_validation_failure_can_retry(tmp_path: Path) -> None:
    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "call_type": "rich_context_specialist",
                        "goal": "补充背景技术",
                        "target_section_id": "background_technology",
                        "user_message": "补充背景技术",
                    },
                    "call_bad_submit",
                )
            ],
        }

    def step_subagent_bad_submit(_: list[dict[str, Any]]) -> dict[str, Any]:
        arguments = {
            "summary": "缺少 proposal。",
            "reply": "缺少 proposal。",
            "rationale": "测试校验失败。",
            "proposal_type": "document_edit_proposal",
            "proposal": {},
            "questions": [],
            "warnings": [],
        }
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("submit_result", arguments, "sub_submit_bad")],
        }

    def step_subagent_retry(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "sub_submit_bad"
        result = json.loads(last_tool["content"])
        assert result["status"] == "failed"
        assert result["output"]["code"] == "subagent_invalid_submit_result"
        arguments = {
            "summary": "已生成候选正文。",
            "reply": "已补充背景技术。",
            "rationale": "根据校验反馈补齐 operations。",
            "proposal_type": "document_edit_proposal",
            "proposal": {
                "operations": [
                    {
                        "op": "replace_section_blocks",
                        "section_id": "background_technology",
                        "blocks": [{"type": "paragraph", "text": "背景技术正文。"}],
                    }
                ],
            },
            "questions": [],
            "warnings": [],
        }
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("submit_result", arguments, "sub_submit_ok")],
        }

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        return {"type": "respond", "text": "子 agent 校验失败后已重试成功。"}

    llm = ScriptedLLMClient(
        [step_call_subagent, step_subagent_bad_submit, step_subagent_retry, step_main_recover],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="补充背景技术"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    sub_submit_results = [
        event
        for event in events
        if event.type == "tool_result" and event.scope == "subagent:section_writer" and event.payload.get("tool") == "submit_result"
    ]
    assert [event.payload["status"] for event in sub_submit_results] == ["failed", "success"]


@pytest.mark.anyio
async def test_subagent_submit_result_invalid_arguments_json_can_retry(tmp_path: Path) -> None:
    error_message = "submit_result 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "call_type": "rich_context_specialist",
                        "goal": "补充背景技术",
                        "target_section_id": "background_technology",
                        "user_message": "补充背景技术",
                    },
                    "call_bad_submit_json",
                )
            ],
        }

    def step_subagent_bad_arguments(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "tool": "submit_result",
                    "arguments": {},
                    "tool_call_id": "sub_submit_invalid_json",
                    "arguments_error": error_message,
                }
            ],
        }

    def step_subagent_retry(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "sub_submit_invalid_json"
        result = json.loads(last_tool["content"])
        assert result == {
            "status": "failed",
            "output": {
                "code": "invalid_tool_arguments_json",
                "message": error_message,
            },
        }
        arguments = {
            "summary": "已生成候选正文。",
            "reply": "已补充背景技术。",
            "rationale": "根据参数错误反馈重交合法 JSON。",
            "proposal_type": "document_edit_proposal",
            "proposal": {
                "operations": [
                    {
                        "op": "replace_section_blocks",
                        "section_id": "background_technology",
                        "blocks": [{"type": "paragraph", "text": "背景技术正文。"}],
                    }
                ],
            },
            "questions": [],
            "warnings": [],
        }
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("submit_result", arguments, "sub_submit_json_retry")],
        }

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        return {"type": "respond", "text": "子 agent 参数 JSON 错误后已重试成功。"}

    llm = ScriptedLLMClient(
        [step_call_subagent, step_subagent_bad_arguments, step_subagent_retry, step_main_recover],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="补充背景技术"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    sub_submit_results = [
        event
        for event in events
        if event.type == "tool_result" and event.scope == "subagent:section_writer" and event.payload.get("tool") == "submit_result"
    ]
    assert [event.payload["status"] for event in sub_submit_results] == ["failed", "success"]
