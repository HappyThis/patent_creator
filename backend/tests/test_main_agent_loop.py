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
    - generate_json 给 section_writer 用，直接返回可解析的 operations。
    """

    def __init__(self, script: list[Callable[[list[dict[str, Any]]], dict[str, Any]]]) -> None:
        self._script = list(script)
        self._cursor = 0

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        if self._cursor >= len(self._script):
            raise AssertionError("ScriptedLLMClient script exhausted")
        step = self._script[self._cursor]
        self._cursor += 1
        result = step(messages)
        if result.get("type") == "respond" and on_text_delta is not None:
            await on_text_delta(str(result.get("text") or ""))
        return result

    async def generate_json(self, *, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> dict[str, Any]:
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


@pytest.mark.anyio
async def test_main_agent_loop_full_flow(tmp_path: Path) -> None:
    """覆盖 read -> execute_subagent -> document_edit -> respond 的完整链路。"""

    def step_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_call",
            "tool": "document_read",
            "arguments": {"action": "get_section", "section_id": "technical_effects", "include_children": True},
            "tool_call_id": "call_1",
        }

    def step_subagent(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_call",
            "tool": "execute_subagent",
            "arguments": {
                "agent_id": "section_writer",
                "call_type": "rich_context_specialist",
                "goal": "补充技术效果章节",
                "target_section_id": "technical_effects",
                "user_message": "请补充技术效果。",
            },
            "tool_call_id": "call_2",
        }

    def step_edit(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [m for m in messages if m.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        operations = result["output"]["result"]["proposal"]["operations"]
        return {
            "type": "tool_call",
            "tool": "document_edit",
            "arguments": {"operations": operations},
            "tool_call_id": "call_3",
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
async def test_main_agent_loop_max_steps_limit_response(tmp_path: Path) -> None:
    """主 agent 持续 read 不 respond，达到 max_steps 后用兜底回复结束。"""

    def step_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_call",
            "tool": "document_read",
            "arguments": {"action": "get_section", "section_id": "technical_effects"},
            "tool_call_id": "call_infinite",
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
    """主 agent 调用失败的 document_read 会走 round_failed。"""

    def step_bad_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_call",
            "tool": "document_read",
            "arguments": {"action": "get_section", "section_id": "not_exist_section"},
            "tool_call_id": "call_fail",
        }

    llm = ScriptedLLMClient([step_bad_read])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="读一下不存在的章节"),
    )
    await wait_until_idle(services, project_id)

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert names[-1] == "round_failed"
    failed_payload = bus_events[-1][1]
    assert failed_payload["code"] == "round_runtime_error"
    assert failed_payload["reply"].startswith("本轮未完成")
