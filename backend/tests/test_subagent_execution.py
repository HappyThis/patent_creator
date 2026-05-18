from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import ChatMessageRequest
from app.services import AppServices

from helpers import ScriptedLLMClient, create_project, make_settings, tool_call, wait_until_idle


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
async def test_main_agent_restores_execute_subagent_result_without_subagent_internal_tools(tmp_path: Path) -> None:
    def main_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "goal": "读取并总结背景技术。",
                    },
                    "call_restore_subagent",
                )
            ],
        }

    def subagent_read(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages[-1]["role"] == "user"
        assert "【任务说明】" in messages[-1]["content"]
        assert "读取并总结背景技术" in messages[-1]["content"]
        assert {"role": "user", "content": "让子 agent 读背景技术"} in messages
        assert not any(
            call.get("id") == "call_restore_subagent"
            for message in messages
            for call in (message.get("tool_calls") or [])
        )
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "document_read",
                    {"action": "get_section", "section_id": "sec_000003"},
                    "sub_internal_read",
                )
            ],
        }

    def subagent_finish(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert any(
            message.get("role") == "tool" and message.get("tool_call_id") == "sub_internal_read"
            for message in messages
        )
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("write_pipe", {"content": "背景技术已读取。"}, "sub_write_restore"),
                tool_call("finish", {}, "sub_finish_restore"),
            ],
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
        assert json.loads(restored_main_tool[0]["content"])["output"]["content"] == "背景技术已读取。"
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
        [main_call_subagent, subagent_read, subagent_finish, main_respond, second_round],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    first = await services.chat.start_round(project_id, ChatMessageRequest(message="让子 agent 读背景技术"))
    await wait_until_idle(services, project_id)

    await services.chat.start_round(project_id, ChatMessageRequest(session_id=first.session_id, message="继续"))
    await wait_until_idle(services, project_id)

@pytest.mark.anyio
async def test_execute_subagent_compresses_run_local_messages(tmp_path: Path) -> None:
    def subagent_finish(messages: list[dict[str, Any]]) -> dict[str, Any]:
        contents = [str(message.get("content") or "") for message in messages]
        assert any("## 已确认事实" in content for content in contents)
        assert any("【上下文说明】" in content for content in contents)
        assert messages[-1]["role"] == "user"
        assert "【任务说明】" in messages[-1]["content"]
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("write_pipe", {"content": "历史内容已被压缩保留。"}, "sub_write_compressed"),
                tool_call("finish", {}, "sub_finish_compressed"),
            ],
        }

    settings = make_settings(tmp_path)
    settings.context_max_tokens = 500
    settings.context_reserved_output_tokens = 0
    settings.context_compress_threshold_ratio = 0.5
    settings.context_compression_timeout = 123
    llm = ScriptedLLMClient([subagent_finish], script_subagents=True)
    services = AppServices(settings, llm_client=llm)
    project_id = await create_project(services)
    session_id = "sess_subagent_compress"

    result = await services.executor.execute_subagent(
        project_id,
        {"agent_id": "material_analyst", "goal": "提炼压缩后的历史事实。"},
        session_id=session_id,
        round_id="round_subagent_compress",
        message_id="msg_subagent_compress",
        parent_call_id="call_parent",
        caller_messages=[
            {"role": "user", "content": "历史技术细节" * 120},
            {"role": "assistant", "content": "历史处理结论" * 120},
        ],
    )

    assert result["status"] == "success"
    events = services.store.read_session_events(project_id, session_id)
    summary_event = next(event for event in events if event.type == "context_summary")
    compression_payload = llm.generated_text_prompts[-1]
    assert compression_payload["_timeout"] == 123
    assert "target_estimated_tokens" not in compression_payload
    assert summary_event.scope == "subagent:material_analyst"
    assert summary_event.payload["compression_mode"] == "markdown_memory"
    assert summary_event.payload["compressed_markdown"].startswith("## 已确认事实")

@pytest.mark.anyio
async def test_subagent_plain_response_fails_without_protocol_retry(tmp_path: Path) -> None:
    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "goal": "补充背景技术",
                    },
                    "call_bad_json",
                )
            ],
        }

    def step_subagent_plain_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert messages
        return {"type": "respond", "text": "这不是 JSON"}

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "failed"
        assert result["output"]["code"] == "subagent_plain_response"
        return {"type": "respond", "text": "子 agent 未按 pipe 协议提交。"}

    llm = ScriptedLLMClient(
        [step_call_subagent, step_subagent_plain_response, step_main_recover],
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
    assert main_tool_results[-1].payload["status"] == "failed"
    assert [event.payload.get("text") for event in events if event.type == "agent_output"][-1] == "子 agent 未按 pipe 协议提交。"

@pytest.mark.anyio
async def test_subagent_repeated_plain_response_fails_fast(tmp_path: Path) -> None:
    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {"agent_id": "section_writer", "goal": "补充背景技术章节"},
                    "call_subagent_plain_repeat",
                )
            ],
        }

    def step_subagent_plain_response(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "respond", "text": "这不是 pipe。"}

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "failed"
        assert result["output"]["code"] == "subagent_plain_response"
        return {"type": "respond", "text": "子 agent 未按协议提交。"}

    llm = ScriptedLLMClient(
        [step_call_subagent, step_subagent_plain_response, step_main_recover],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="测试子 agent 连续直接回复。"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    main_tool_results = [
        event
        for event in events
        if event.type == "tool_result" and event.scope == "main" and event.payload.get("tool") == "execute_subagent"
    ]
    assert main_tool_results[-1].payload["status"] == "failed"
    assert main_tool_results[-1].payload["output"]["code"] == "subagent_plain_response"

@pytest.mark.anyio
async def test_subagent_write_pipe_validation_failure_can_retry(tmp_path: Path) -> None:
    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "goal": "补充背景技术",
                    },
                    "call_bad_pipe",
                )
            ],
        }

    def step_subagent_bad_write(_: list[dict[str, Any]]) -> dict[str, Any]:
        arguments = {"content": {"text": "不是字符串"}}
        return {
            "type": "tool_calls",
            "tool_calls": [tool_call("write_pipe", arguments, "sub_write_bad")],
        }

    def step_subagent_retry(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "sub_write_bad"
        result = json.loads(last_tool["content"])
        assert result["status"] == "failed"
        assert result["output"]["code"] == "invalid_pipe_content"
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("write_pipe", {"content": "背景技术正文。"}, "sub_write_ok"),
                tool_call("finish", {}, "sub_finish_ok"),
            ],
        }

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        assert result["output"]["content"] == "背景技术正文。"
        return {"type": "respond", "text": "子 agent pipe 写入失败后已重试成功。"}

    llm = ScriptedLLMClient(
        [step_call_subagent, step_subagent_bad_write, step_subagent_retry, step_main_recover],
        script_subagents=True,
    )
    services = AppServices(make_settings(tmp_path), llm_client=llm)
    project_id = await create_project(services)

    response = await services.chat.start_round(project_id, ChatMessageRequest(message="补充背景技术"))
    await wait_until_idle(services, project_id)

    events = services.store.read_session_events(project_id, response.session_id)
    sub_write_results = [
        event
        for event in events
        if event.type == "tool_result" and event.scope == "subagent:section_writer" and event.payload.get("tool") == "write_pipe"
    ]
    assert [event.payload["status"] for event in sub_write_results] == ["failed", "success"]

@pytest.mark.anyio
async def test_subagent_write_pipe_invalid_arguments_json_can_retry(tmp_path: Path) -> None:
    error_message = "write_pipe 的 arguments 不是合法 JSON：Expecting ',' delimiter: line 1 column 48 (char 47)"

    def step_call_subagent(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call(
                    "execute_subagent",
                    {
                        "agent_id": "section_writer",
                        "goal": "补充背景技术",
                    },
                    "call_bad_pipe_json",
                )
            ],
        }

    def step_subagent_bad_arguments(_: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "tool": "write_pipe",
                    "arguments": {},
                    "tool_call_id": "sub_write_invalid_json",
                    "arguments_error": error_message,
                }
            ],
        }

    def step_subagent_retry(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        assert last_tool["tool_call_id"] == "sub_write_invalid_json"
        result = json.loads(last_tool["content"])
        assert result == {
            "status": "failed",
            "output": {
                "code": "invalid_tool_arguments_json",
                "message": error_message,
            },
        }
        return {
            "type": "tool_calls",
            "tool_calls": [
                tool_call("write_pipe", {"content": "背景技术正文。"}, "sub_write_json_retry"),
                tool_call("finish", {}, "sub_finish_json_retry"),
            ],
        }

    def step_main_recover(messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_tool = [message for message in messages if message.get("role") == "tool"][-1]
        result = json.loads(last_tool["content"])
        assert result["status"] == "success"
        assert result["output"]["content"] == "背景技术正文。"
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
    sub_write_results = [
        event
        for event in events
        if event.type == "tool_result" and event.scope == "subagent:section_writer" and event.payload.get("tool") == "write_pipe"
    ]
    assert [event.payload["status"] for event in sub_write_results] == ["failed", "success"]
