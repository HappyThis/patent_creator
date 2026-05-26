from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.core.config import Settings
from app.services import AppServices


class StubLLMClient:
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> str:
        context = json.loads(user_prompt[user_prompt.index("{") :])
        message_count = len(context.get("messages_to_merge") or [])
        return (
            "<analysis>测试压缩。</analysis>\n"
            "<summary>\n"
            "## 当前任务\n\n"
            "- 继续沿用压缩前的用户要求。\n\n"
            "## 执行进度\n\n"
            f"- 已滚动压缩 {message_count} 条新增消息相关的信息。\n\n"
            "## 已完成事项\n\n"
            "- 当前任务继续沿用压缩前的上下文。\n\n"
            "## 关键事实与证据\n\n"
            "- 暂无。\n\n"
            "## 待办与下一步\n\n"
            "- 后续如信息不足，应重新读取必要上下文。\n"
            "\n## 风险与约束\n\n"
            "- 暂无。\n"
            "</summary>"
        )

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        response_format_json: bool = False,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """模拟 main-only agent loop：document write tools -> respond。"""
        last_user_index = max(
            (index for index, message in enumerate(messages) if message.get("role") == "user"),
            default=-1,
        )
        tool_results = [
            msg
            for msg in messages[last_user_index + 1 :]
            if msg.get("role") == "tool"
        ]

        if not tool_results:
            user_messages = [
                str(message.get("content") or "")
                for message in messages
                if message.get("role") == "user"
            ]
            user_message = user_messages[-1] if user_messages else ""
            if "技术效果" in user_message:
                tool_calls = [
                    {
                        "tool": "document_replace_section_blocks",
                        "arguments": {
                            "section_id": "sec_000010",
                            "blocks": [
                                {
                                    "type": "paragraph",
                                    "text": "本方案通过减少无效推理和复用时序信息，降低了低算力终端上的单帧处理开销。",
                                },
                                {
                                    "type": "list",
                                    "ordered": False,
                                    "items": [
                                        "缩短端到端检测时延，提升实时响应能力。",
                                        "在有限算力预算下保持检测稳定性。",
                                        "降低持续运行时的能耗和温升压力。",
                                    ],
                                },
                            ],
                        },
                        "tool_call_id": "stub_call_2",
                    }
                ]
            else:
                tool_calls = [
                    {
                        "tool": "document_replace_section_blocks",
                        "arguments": {
                            "section_id": "sec_000007",
                            "blocks": [
                                {
                                    "type": "paragraph",
                                    "text": "本节补充适用于低算力终端的整体方案说明。",
                                },
                                {
                                    "type": "paragraph",
                                    "text": "系统通过候选区域筛选、轻量特征提取和时序校正协同完成实时检测。",
                                },
                            ],
                        },
                        "tool_call_id": "stub_call_2",
                    },
                    {
                        "tool": "document_append_child_section",
                        "arguments": {
                            "parent_section_id": "sec_000007",
                            "title": "整体架构",
                            "blocks": [
                                {
                                    "type": "table",
                                    "columns": ["模块", "职责"],
                                    "rows": [
                                        ["预处理模块", "完成缩放、归一化和候选区域粗筛"],
                                        ["推理模块", "仅对高价值候选区域执行完整检测"],
                                    ],
                                }
                            ],
                        },
                        "tool_call_id": "stub_call_3",
                    },
                    {
                        "tool": "document_append_child_section",
                        "arguments": {
                            "parent_section_id": "sec_000007",
                            "title": "处理流程",
                            "blocks": [
                                {
                                    "type": "list",
                                    "ordered": True,
                                    "items": [
                                        "获取当前帧图像并复用上一帧稳定特征。",
                                        "筛出满足阈值的候选区域。",
                                        "对候选区域执行轻量化检测与结果校正。",
                                    ],
                                }
                            ],
                        },
                        "tool_call_id": "stub_call_4",
                    },
                ]
            return {
                "type": "tool_calls",
                "tool_calls": tool_calls,
                "assistant_message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "主 agent 直接生成最终态正文并写入文档。",
                    "tool_calls": [
                        {
                            "id": call["tool_call_id"],
                            "type": "function",
                            "function": {
                                "name": call["tool"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                        for call in tool_calls
                    ],
                },
            }

        reply = "已完成本轮修改。"
        if on_text_delta is not None:
            await on_text_delta(reply)
        return {
            "type": "respond",
            "text": reply,
            "assistant_message": {
                "role": "assistant",
                "content": reply,
                "reasoning_content": "已完成工具调用，准备回复用户。",
            },
        }


async def collect_stream_events(
    client: httpx.AsyncClient,
    project_id: str,
    payload: dict[str, Any],
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/chat/messages",
        json=payload,
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ").strip()
                continue
            if line.startswith("data: ") and current_event:
                payload = json.loads(line.removeprefix("data: ").strip())
                events.append((current_event, payload))
                if current_event in {"round_finished", "round_failed", "round_cancelled"}:
                    break
    return events


async def collect_session_stream_events(
    client: httpx.AsyncClient,
    project_id: str,
    session_id: str,
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    async with client.stream(
        "GET",
        f"/api/projects/{project_id}/sessions/{session_id}/stream",
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ").strip()
                continue
            if line.startswith("data: ") and current_event:
                payload = json.loads(line.removeprefix("data: ").strip())
                events.append((current_event, payload))
                if current_event in {"round_finished", "round_failed", "round_cancelled", "stream_closed"}:
                    break
    return events


@pytest.mark.anyio
async def test_project_chat_and_export(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
        round_step_delay=0.01,
        round_finish_delay=0.01,
    )
    services = AppServices(settings, llm_client=StubLLMClient())
    app = create_app(settings, services=services)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        create_response = await client.post("/api/projects", json={"title": "一种图像检测方法"})
        assert create_response.status_code == 405

        projects_response = await client.get("/api/projects")
        assert projects_response.status_code == 200
        projects = projects_response.json()["projects"]
        assert len(projects) == 1
        project = projects[0]
        project_id = project["project_id"]

        second_projects_response = await client.get("/api/projects")
        assert second_projects_response.status_code == 200
        assert second_projects_response.json()["projects"][0]["project_id"] == project_id

        outline_response = await client.get(f"/api/projects/{project_id}/outline")
        assert outline_response.status_code == 200
        assert len(outline_response.json()["sections"]) == 12

        render_response = await client.get(f"/api/projects/{project_id}/render")
        assert render_response.status_code == 200
        assert render_response.json()["render_ast"]["title"] == "一种图像检测方法"

        sse_events = await collect_stream_events(
            client,
            project_id,
            {"message": "请补充技术效果章节，强调低算力实时性的收益。"},
        )
        event_names = [name for name, _ in sse_events]
        assert event_names[0] == "round_started"
        assert "document_changed" in event_names
        assert event_names[-1] == "round_finished"
        session_id = sse_events[0][1]["session_id"]

        session_events = await client.get(f"/api/projects/{project_id}/sessions/{session_id}/events")
        assert session_events.status_code == 200
        event_payloads = session_events.json()["events"]
        event_types = [event["type"] for event in event_payloads]
        scopes = [event["scope"] for event in event_payloads]
        assert event_types[0] == "user_input"
        assert "references" not in event_payloads[0]["payload"]
        assert "tool_call" in event_types
        assert "tool_result" in event_types
        assert all(not scope.startswith("subagent:") for scope in scopes)
        assert event_types[-1] == "agent_output"

        sessions_response = await client.get(f"/api/projects/{project_id}/sessions")
        assert sessions_response.status_code == 200
        sessions = sessions_response.json()["sessions"]
        assert sessions[0]["session_id"] == session_id
        assert sessions[0]["is_active"] is True

        document_response = await client.get(f"/api/projects/{project_id}/document")
        assert document_response.status_code == 200
        technical_effects = next(
            section for section in document_response.json()["sections"] if section["type"] == "technical_effects"
        )
        assert len(technical_effects["blocks"]) == 2
        technical_solution = next(
            section for section in document_response.json()["sections"] if section["type"] == "technical_solution"
        )
        assert technical_solution["blocks"] == []

        export_response = await client.post(f"/api/projects/{project_id}/export/markdown")
        assert export_response.status_code == 200
        export_path = Path(export_response.json()["path"])
        assert export_path.exists()
        assert "技术效果" in export_path.read_text(encoding="utf-8")

        second_stream_events = await collect_stream_events(
            client,
            project_id,
            {
                "session_id": session_id,
                "message": "继续完善这里的处理流程和整体架构。",
                "active_section_id": technical_solution["id"],
            },
        )
        assert second_stream_events[0][0] == "round_started"
        assert second_stream_events[-1][0] == "round_finished"

        document_response = await client.get(f"/api/projects/{project_id}/document")
        assert document_response.status_code == 200
        technical_solution = next(
            section for section in document_response.json()["sections"] if section["type"] == "technical_solution"
        )
        assert len(technical_solution["blocks"]) == 2
        assert technical_solution["children"][0]["type"] == "custom"
        assert technical_solution["children"][0]["title"] == "整体架构"

        missing_session_response = await client.post(
            f"/api/projects/{project_id}/chat/messages",
            json={"session_id": "sess_missing", "message": "继续写。"},
        )
        assert missing_session_response.status_code == 404


@pytest.mark.anyio
async def test_session_stream_can_attach_to_running_round(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
    )
    services = AppServices(settings, llm_client=StubLLMClient())
    app = create_app(settings, services=services)
    transport = httpx.ASGITransport(app=app)

    project = services.store.ensure_current_project()
    project_id = project.project_id
    session_id = "sess_resume"
    round_id = "round_resume"
    message_id = "msg_resume"
    services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id=round_id,
        message_id=message_id,
        payload={"text": "继续写。"},
    )
    project.active_session_id = session_id
    project.running_session_id = session_id
    project.running_round_id = round_id
    project.is_busy = True
    services.store.save_project(project)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stream_task = asyncio.create_task(collect_session_stream_events(client, project_id, session_id))
        await asyncio.sleep(0.05)
        await services.bus.publish(
            (project_id, session_id),
            "assistant_delta",
            {
                "round_id": round_id,
                "message_id": message_id,
                "text": "恢复后的流式文本",
            },
        )
        await services.bus.publish(
            (project_id, session_id),
            "round_failed",
            {
                "round_id": round_id,
                "message_id": message_id,
                "reply": "测试结束。",
            },
        )

        events = await asyncio.wait_for(stream_task, timeout=2.0)

    assert events[0][0] == "stream_attached"
    assert ("assistant_delta", {"round_id": round_id, "message_id": message_id, "text": "恢复后的流式文本"}) in events
    assert events[-1][0] == "round_failed"


@pytest.mark.anyio
async def test_running_round_can_be_cancelled(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
        round_step_delay=1.0,
    )
    services = AppServices(settings, llm_client=StubLLMClient())
    app = create_app(settings, services=services)
    transport = httpx.ASGITransport(app=app)

    project = services.store.ensure_current_project()
    project_id = project.project_id

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stream_task = asyncio.create_task(
            collect_stream_events(client, project_id, {"message": "请补充技术效果章节。"})
        )

        running_project = services.store.get_project(project_id)
        for _ in range(100):
            running_project = services.store.get_project(project_id)
            if running_project.running_session_id and running_project.running_round_id:
                break
            await asyncio.sleep(0.01)
        assert running_project.running_session_id is not None
        assert running_project.running_round_id is not None

        cancel_response = await client.post(
            f"/api/projects/{project_id}/sessions/{running_project.running_session_id}/rounds/{running_project.running_round_id}/cancel"
        )
        assert cancel_response.status_code == 200
        assert cancel_response.json()["cancelled"] is True
        events = await asyncio.wait_for(stream_task, timeout=2.0)

    assert events[0][0] == "round_started"
    assert events[-1][0] == "round_cancelled"
    cancelled_payload = events[-1][1]
    assert cancelled_payload["reply"] == "本轮任务已取消。"

    unlocked_project = services.store.get_project(project_id)
    assert unlocked_project.is_busy is False
    assert unlocked_project.running_session_id is None
    assert unlocked_project.running_round_id is None

    session_id = events[0][1]["session_id"]
    session_events = services.store.read_session_events(project_id, session_id)
    assert session_events[-1].payload["code"] == "round_cancelled"
