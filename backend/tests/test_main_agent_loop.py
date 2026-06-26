from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.core import ApiError
from app.schemas import ChatMessageRequest
from app.runtime.context.history import INTERRUPTED_OUTPUT_CONTEXT_NOTE
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
    assert "context_usage_updated" not in event_types
    assert "agent_output" in event_types
    assert event_types[-1] == "agent_output"

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    bus_event_names = [name for name, _ in bus_events]
    assert "document_changed" in bus_event_names
    usage_payloads = [payload for name, payload in bus_events if name == "context_usage_updated"]
    assert {payload["reason"] for payload in usage_payloads} >= {
        "before_main_agent_call",
        "after_main_agent_call",
        "after_tool_results",
    }
    assert all(payload["session_id"] == response.session_id for payload in usage_payloads)
    assert all(payload["status"] in {"ok", "over_limit"} for payload in usage_payloads)
    assert all(isinstance(payload["used_tokens"], int) for payload in usage_payloads)
    assert bus_event_names[-1] == "round_finished"
    round_finished_payload = bus_events[-1][1]
    assert round_finished_payload["reply"] == "已更新关键创新点及权利要求建议。"
    assert round_finished_payload["changed"] is True
    disclosure = services.store.get_disclosure(project_id)
    innovation_claims = section_by_title(disclosure, "关键创新点及权利要求建议")
    assert len(innovation_claims["blocks"]) >= 1


@pytest.mark.anyio
async def test_main_agent_loop_marks_interrupted_partial_output(tmp_path: Path) -> None:
    def step_interrupted(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "respond",
            "text": "这是一段已经输出给用户的半截内容",
            "interrupted": True,
            "interrupted_message": "stream broke",
        }

    llm = ScriptedLLMClient([step_interrupted])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请生成技术方案。"),
    )
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    agent_output = next(event for event in events if event.type == "agent_output")
    assert agent_output.payload["text"] == "这是一段已经输出给用户的半截内容"
    assert agent_output.payload["status"] == "interrupted"
    assert agent_output.payload["detail"] == "输出中断，已保留当前内容。"

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    assert bus_events[-1][0] == "round_finished"
    assert bus_events[-1][1]["reply_status"] == "interrupted"
    assert bus_events[-1][1]["reply_detail"] == "输出中断，已保留当前内容。"
    assert "round_failed" not in [name for name, _ in bus_events]

    restored = services.context_manager.build_main_agent_messages(
        project_id,
        response.session_id,
        user_message="继续。",
    )
    assistant_messages = [message for message in restored if message.get("role") == "assistant"]
    assert assistant_messages
    assert INTERRUPTED_OUTPUT_CONTEXT_NOTE in str(assistant_messages[-1]["content"])


@pytest.mark.anyio
async def test_main_agent_loop_finalizes_llm_retry_status_on_success(tmp_path: Path) -> None:
    class RetryThenRespondLLM(ScriptedLLMClient):
        async def generate_with_tools_stream(
            self,
            *,
            system_prompt: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            on_text_delta: Any = None,
            on_audit_event: Any = None,
            on_retry_event: Any = None,
            response_format_json: bool = False,
            trace_context: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if on_retry_event is not None:
                await on_retry_event(
                    {
                        "status": "waiting",
                        "reason": "模型连接失败",
                        "attempt": 2,
                        "max_attempts": 6,
                        "retry_index": 1,
                        "max_retries": 5,
                        "retry_after_seconds": 5,
                        "retry_at_ms": 123456,
                        "error_type": "APIStatusError",
                        "error_message": "server overloaded",
                        "kind": "generate_with_tools_stream",
                    }
                )
                await on_retry_event(
                    {
                        "status": "retrying",
                        "reason": "模型连接失败",
                        "attempt": 2,
                        "max_attempts": 6,
                        "retry_index": 1,
                        "max_retries": 5,
                        "retry_after_seconds": 0,
                        "retry_at_ms": None,
                        "error_type": "APIStatusError",
                        "error_message": "server overloaded",
                        "kind": "generate_with_tools_stream",
                    }
                )
            if on_text_delta is not None:
                await on_text_delta("已恢复。")
            return {
                "type": "respond",
                "text": "已恢复。",
                "assistant_message": {"role": "assistant", "content": "已恢复。"},
            }

    llm = RetryThenRespondLLM([])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="请继续。"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    retry_payloads = [event.payload for event in events if event.type == "llm_retry_status"]
    assert [payload["status"] for payload in retry_payloads] == ["waiting", "retrying", "done"]
    assert retry_payloads[-1]["retry_after_seconds"] == 0
    assert retry_payloads[-1]["retry_at_ms"] is None


@pytest.mark.anyio
async def test_main_agent_loop_finalizes_llm_retry_status_on_failure(tmp_path: Path) -> None:
    class RetryThenFailLLM(ScriptedLLMClient):
        async def generate_with_tools_stream(
            self,
            *,
            system_prompt: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            on_text_delta: Any = None,
            on_audit_event: Any = None,
            on_retry_event: Any = None,
            response_format_json: bool = False,
            trace_context: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if on_retry_event is not None:
                await on_retry_event(
                    {
                        "status": "retrying",
                        "reason": "模型连接失败",
                        "attempt": 6,
                        "max_attempts": 6,
                        "retry_index": 5,
                        "max_retries": 5,
                        "retry_after_seconds": 0,
                        "retry_at_ms": None,
                        "error_type": "APIStatusError",
                        "error_message": "server overloaded",
                        "kind": "generate_with_tools_stream",
                    }
                )
            raise ApiError(502, "llm_http_error", "模型调用失败：server overloaded")

    llm = RetryThenFailLLM([])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="请继续。"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    retry_payloads = [event.payload for event in events if event.type == "llm_retry_status"]
    assert [payload["status"] for payload in retry_payloads] == ["retrying", "failed"]
    assert retry_payloads[-1]["error_message"] == "模型调用失败：server overloaded"

    failed_output = next(event for event in events if event.type == "agent_output")
    assert failed_output.payload["status"] == "failed"
    assert failed_output.payload["message"] == "模型调用失败：server overloaded"


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
async def test_technical_solution_normal_mode_does_not_run_enhancement(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "系统通过状态识别和规则调度完成处理。"},
                    },
                    "call_write_solution",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_write_solution" for message in messages)
        return {"type": "respond", "text": "已更新技术方案。"}

    llm = ScriptedLLMClient([step_write, step_respond])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="请补充技术方案。"))
    await wait_until_idle(services, project_id)

    assert len(llm.assessment_prompts) == 0
    assert len(llm.advice_prompts) == 0
    assert len(llm.summary_prompts) == 0
    events = services.store.read_session_events(project_id, response.session_id)
    event_types = [event.type for event in events]
    assert not any(event_type.startswith("technical_solution_enhancement") for event_type in event_types)
    assert "technical_solution_change_assessment" not in event_types
    assert "technical_solution_improvement_advice" not in event_types


@pytest.mark.anyio
async def test_technical_solution_enhanced_mode_runs_one_followup(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "系统通过状态识别和规则调度完成处理。"},
                    },
                    "call_write_solution",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_write_solution" for message in messages)
        return {"type": "respond", "text": "已更新技术方案。"}

    def step_followup(messages: list[dict[str, Any]]) -> dict[str, Any]:
        feedback_message = messages[-1]
        assert feedback_message["role"] == "user"
        assert "系统正在增强模式下继续完善“技术方案”章节" in feedback_message["content"]
        assert "技术人员式技术抽象" in feedback_message["content"]
        assert "不要把它们包装成权利要求式或正式专利说明书式语言" in feedback_message["content"]
        assert "不要把建议中的工程变量、字段清单、状态枚举、公式、接口名或伪代码直接堆入正文" in feedback_message["content"]
        assert "补充处理阶段迁移规则和冲突边界" in feedback_message["content"]
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {
                            "type": "paragraph",
                            "text": "状态识别模块输出任务状态和异常标记，规则调度模块据此选择处理路径并记录处理结果。",
                        },
                    },
                    "call_followup_solution",
                )
            ],
        }

    def step_final_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_followup_solution" for message in messages)
        return {"type": "respond", "text": "已参考评审意见完成处理。"}

    llm = ScriptedLLMClient(
        [step_write, step_respond, step_followup, step_final_respond],
        assessment_json=[
            {"should_review": True, "reason": "本轮新增技术方案核心处理内容，需要进一步增强。"},
        ],
        advice_json=[
            {
                "review_markdown": "## 技术方案评审意见\n\n### 技术深度修订点\n1. 补充处理阶段迁移规则和冲突边界。",
            },
        ],
        summary_json=[
            {"applied_summary": "已补充处理阶段迁移规则和冲突边界。"},
        ],
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充技术方案。", quality_mode="enhanced"),
    )
    await wait_until_idle(services, project_id)

    assert len(llm.assessment_prompts) == 1
    assert len(llm.advice_prompts) == 1
    assert len(llm.summary_prompts) == 1
    events = services.store.read_session_events(project_id, response.session_id)
    event_types = [event.type for event in events]
    assert event_types.count("technical_solution_change_assessment") == 1
    assert event_types.count("technical_solution_improvement_advice") == 1
    assert event_types.count("technical_solution_enhancement_feedback") == 1
    assert event_types.count("technical_solution_enhancement_summary") == 1
    assert "technical_solution_check_result" not in event_types
    advice = next(event for event in events if event.type == "technical_solution_improvement_advice")
    assert "gate_pass" not in advice.payload
    stored_history = services.store.recent_technical_solution_enhancement_history(project_id, limit=3)
    assert len(stored_history) == 1
    assert stored_history[0]["summary"]["applied_summary"] == "已补充处理阶段迁移规则和冲突边界。"

    disclosure = services.store.get_disclosure(project_id)
    technical_solution = section_by_title(disclosure, "技术方案")
    assert len(technical_solution["blocks"]) == 2

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert names.count("quality_enhancement_status") == 4
    assert "technical_solution_improvement_advice" in names
    assert bus_events[-1][0] == "round_finished"
    assert bus_events[-1][1]["reply"] == "已参考评审意见完成处理。"


@pytest.mark.anyio
async def test_technical_solution_enhancement_runs_one_followup_without_second_advice(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "系统根据策略处理任务。"},
                    },
                    "call_initial_solution",
                )
            ],
        }

    def step_initial_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_initial_solution" for message in messages)
        return {"type": "respond", "text": "已初步更新技术方案。"}

    def step_followup(messages: list[dict[str, Any]]) -> dict[str, Any]:
        feedback_message = messages[-1]
        assert feedback_message["role"] == "user"
        assert "系统正在增强模式下继续完善" in feedback_message["content"]
        assert "关键机制不足" in feedback_message["content"]
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {
                            "type": "paragraph",
                            "text": "状态识别模块输出任务状态和异常标记，规则调度模块据此选择处理路径并记录处理结果。",
                        },
                    },
                    "call_followup_solution",
                )
            ],
        }

    def step_final_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_followup_solution" for message in messages)
        return {"type": "respond", "text": "已参考检查意见完成处理。"}

    llm = ScriptedLLMClient(
        [step_write, step_initial_respond, step_followup, step_final_respond],
        assessment_json=[
            {"should_review": True, "reason": "本轮新增核心技术方案，需要进一步增强。"},
        ],
        advice_json=[
            {
                "review_markdown": "## 技术方案评审意见\n\n### 关键机制闭合性修订点\n1. 关键机制不足。",
            },
        ],
        summary_json=[
            {"applied_summary": "已补充关键机制闭合内容。"},
        ],
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充技术方案。", quality_mode="enhanced"),
    )
    await wait_until_idle(services, project_id)

    assert len(llm.assessment_prompts) == 1
    assert len(llm.advice_prompts) == 1
    assert len(llm.summary_prompts) == 1
    events = services.store.read_session_events(project_id, response.session_id)
    event_types = [event.type for event in events]
    assert event_types.count("technical_solution_change_assessment") == 1
    assert event_types.count("technical_solution_improvement_advice") == 1
    assert event_types.count("technical_solution_enhancement_feedback") == 1
    assert event_types.count("technical_solution_enhancement_summary") == 1

    disclosure = services.store.get_disclosure(project_id)
    technical_solution = section_by_title(disclosure, "技术方案")
    assert len(technical_solution["blocks"]) == 2

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert names.count("technical_solution_improvement_advice") == 1
    assert names.count("quality_enhancement_status") == 4
    assert bus_events[-1][0] == "round_finished"
    assert bus_events[-1][1]["reply"] == "已参考检查意见完成处理。"


@pytest.mark.anyio
async def test_technical_solution_enhanced_mode_skips_advice_when_assessment_false(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "系统根据策略处理任务。"},
                    },
                    "call_solution",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_solution" for message in messages)
        return {"type": "respond", "text": "已更新技术方案。"}

    llm = ScriptedLLMClient(
        [step_write, step_respond],
        assessment_json=[
            {"should_review": False, "reason": "本轮只是局部补充，不需要进一步增强。"},
        ],
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充一句技术方案。", quality_mode="enhanced"),
    )
    await wait_until_idle(services, project_id)

    assert len(llm.assessment_prompts) == 1
    assert len(llm.advice_prompts) == 0
    assert len(llm.summary_prompts) == 0
    events = services.store.read_session_events(project_id, response.session_id)
    event_types = [event.type for event in events]
    assert event_types.count("technical_solution_change_assessment") == 1
    assert "technical_solution_improvement_advice" not in event_types
    assert "technical_solution_enhancement_feedback" not in event_types
    assert "technical_solution_enhancement_summary" not in event_types


@pytest.mark.anyio
async def test_technical_solution_change_assessment_validation_failure_reports_failed_status(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "系统根据策略处理任务。"},
                    },
                    "call_solution",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_solution" for message in messages)
        return {"type": "respond", "text": "已更新技术方案。"}

    llm = ScriptedLLMClient(
        [step_write, step_respond],
        assessment_json=[
            {"gate_pass": True, "reason": "旧门禁格式不应被接受。"},
        ],
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充技术方案。", quality_mode="enhanced"),
    )
    await wait_until_idle(services, project_id)

    assert len(llm.assessment_prompts) == 1
    assert len(llm.advice_prompts) == 0
    assert len(llm.summary_prompts) == 0
    events = services.store.read_session_events(project_id, response.session_id)
    assessment_result = next(event for event in events if event.type == "technical_solution_change_assessment")
    assert assessment_result.payload["status"] == "failed"
    assert assessment_result.payload["code"] == "technical_solution_change_assessment_validation_failed"
    assert "technical_solution_improvement_advice" not in [event.type for event in events]

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    quality_statuses = [payload for name, payload in bus_events if name == "quality_enhancement_status"]
    assert quality_statuses[-1]["status"] == "failed"
    assert quality_statuses[-1]["summary"] == "增强模式：技术方案增强未完成"
    assert bus_events[-1][0] == "round_finished"
    assert bus_events[-1][1]["reply"] == "已更新技术方案。"


@pytest.mark.anyio
async def test_technical_solution_improvement_advice_validation_failure_reports_error(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "系统根据策略处理任务。"},
                    },
                    "call_solution",
                )
            ],
        }

    def step_respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(message.get("tool_call_id") == "call_solution" for message in messages)
        return {"type": "respond", "text": "已更新技术方案。"}

    llm = ScriptedLLMClient(
        [step_write, step_respond],
        assessment_json=[
            {"should_review": True, "reason": "本轮新增核心技术方案，需要进一步增强。"},
        ],
        advice_json=[
            {"gate_pass": False, "review_markdown": "意见"},
            {"review_markdown": "意见", "score": 80},
        ],
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(
        project_id,
        ChatMessageRequest(message="请补充技术方案。", quality_mode="enhanced"),
    )
    await wait_until_idle(services, project_id)

    assert len(llm.assessment_prompts) == 1
    assert len(llm.advice_prompts) == 2
    assert len(llm.summary_prompts) == 0
    events = services.store.read_session_events(project_id, response.session_id)
    event_types = [event.type for event in events]
    assert event_types.count("technical_solution_change_assessment") == 1
    assert event_types.count("technical_solution_improvement_advice") == 1
    assert "technical_solution_enhancement_feedback" not in event_types
    advice_result = next(event for event in events if event.type == "technical_solution_improvement_advice")
    assert advice_result.payload["status"] == "failed"
    assert advice_result.payload["code"] == "technical_solution_improvement_advice_validation_failed"
    assert advice_result.payload["attempts"] == 2
    assert "does not match schema" in advice_result.payload["message"]

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    assert bus_events[-1][0] == "round_finished"
    assert bus_events[-1][1]["reply"] == "已更新技术方案。"


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


@pytest.mark.anyio
async def test_main_agent_loop_closes_tool_event_when_executor_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def step_read(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("disclosure_read_section", {"section_id": "sec_000002"}, "call_boom")],
        }

    async def broken_execute_tool(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    llm = ScriptedLLMClient([step_read])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    monkeypatch.setattr(services.executor, "execute_tool", broken_execute_tool)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="读一下章节"))
    await wait_until_idle(services, project_id)

    events = [
        event
        for event in services.store.read_session_events(project_id, response.session_id)
        if event.call_id == "call_boom"
    ]
    assert [event.type for event in events] == ["tool_call", "tool_result"]
    assert events[1].payload == {
        "tool": "disclosure_read_section",
        "status": "failed",
        "output": {"code": "tool_runtime_error", "message": "boom"},
    }

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    names = [name for name, _ in bus_events]
    assert "tool_call_started" in names
    assert "tool_call_finished" in names
    assert names[-1] == "round_failed"
    assert bus_events[-1][1]["reply"] == "本轮未完成，请重试或补充信息。"


@pytest.mark.anyio
async def test_main_agent_loop_marks_partial_write_when_model_fails_after_tool(tmp_path: Path) -> None:
    def step_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "disclosure_edit",
                    {
                        "section_id": "sec_000006",
                        "operation": "insert_block",
                        "position": {"mode": "end"},
                        "block": {"type": "paragraph", "text": "已经写入的技术方案内容。"},
                    },
                    "call_write_solution",
                )
            ],
        }

    def step_fail_after_tool(_: list[dict[str, Any]]) -> dict[str, Any]:
        raise ApiError(502, "llm_http_error", "模型调用失败：server overloaded")

    llm = ScriptedLLMClient([step_write, step_fail_after_tool])
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="请修改技术方案。"))
    await wait_until_idle(services, project_id)

    disclosure = services.store.get_disclosure(project_id)
    technical_solution = section_by_title(disclosure, "技术方案")
    assert any(block.get("text") == "已经写入的技术方案内容。" for block in technical_solution["blocks"])

    events = services.store.read_session_events(project_id, response.session_id)
    failed_output = [event for event in events if event.type == "agent_output"][-1]
    assert failed_output.payload["text"] == "已完成部分修改，但模型连接失败，未生成最终回复。请重试继续处理。"
    assert failed_output.payload["status"] == "failed"
    assert failed_output.payload["changed"] is True

    bus_events, _ = await services.bus.subscribe((project_id, response.session_id))
    assert bus_events[-1][0] == "round_failed"
    assert bus_events[-1][1]["reply"] == "已完成部分修改，但模型连接失败，未生成最终回复。请重试继续处理。"
    assert bus_events[-1][1]["changed"] is True
    assert bus_events[-1][1]["committed"] is True
