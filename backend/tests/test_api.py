from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.core.config import Settings
from app.services import AppServices


class StubLLMClient:
    async def generate_json(self, *, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> dict[str, Any]:
        context = json.loads(user_prompt)
        target_section_id = context["task"]["target_section_id"]
        goal = context["task"]["goal"]
        if target_section_id == "technical_solution":
            return {
                "summary": "已生成技术方案候选正文。",
                "reply": "我已经补充了技术方案里的整体架构和处理流程，并把结果同步到文档中。",
                "rationale": "用户明确要求继续完善技术方案章节。",
                "operations": [
                    {
                        "op": "replace_section",
                        "section_id": "technical_solution",
                        "section": {
                            "id": "technical_solution",
                            "title": "技术方案",
                            "blocks": [
                                {
                                    "type": "paragraph",
                                    "text": f"本节结合用户当前要求“{goal}”，补充适用于低算力终端的整体方案说明。",
                                },
                                {
                                    "type": "paragraph",
                                    "text": "系统通过候选区域筛选、轻量特征提取和时序校正协同完成实时检测。",
                                },
                            ],
                            "children": [
                                {
                                    "id": "overall_architecture",
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
                                    "children": [],
                                },
                                {
                                    "id": "processing_flow",
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
                                    "children": [],
                                },
                            ],
                        },
                    }
                ],
                "questions": [],
                "warnings": [],
            }
        return {
            "summary": "已生成技术效果候选正文。",
            "reply": "我已经补充了技术效果章节，重点强调了低算力实时性的收益。",
            "rationale": "用户明确希望强调实时性收益。",
            "operations": [
                {
                    "op": "replace_section_blocks",
                    "section_id": target_section_id,
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
                }
            ],
            "questions": [],
            "warnings": [],
        }

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
    ) -> dict[str, Any]:
        """模拟主 agent loop：execute_subagent -> document_edit -> respond。"""
        if "子 agent：section_writer" in system_prompt:
            context = json.loads(messages[0]["content"])
            target_section_id = context["task"]["target_section_id"]
            goal = context["task"]["goal"]
            if target_section_id == "technical_solution":
                payload = {
                    "summary": "已生成技术方案候选正文。",
                    "reply": "我已经补充了技术方案里的整体架构和处理流程，并把结果同步到文档中。",
                    "rationale": "用户明确要求继续完善技术方案章节。",
                    "operations": [
                        {
                            "op": "replace_section",
                            "section_id": "technical_solution",
                            "section": {
                                "id": "technical_solution",
                                "title": "技术方案",
                                "blocks": [
                                    {
                                        "type": "paragraph",
                                        "text": f"本节结合用户当前要求“{goal}”，补充适用于低算力终端的整体方案说明。",
                                    },
                                    {
                                        "type": "paragraph",
                                        "text": "系统通过候选区域筛选、轻量特征提取和时序校正协同完成实时检测。",
                                    },
                                ],
                                "children": [
                                    {
                                        "id": "overall_architecture",
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
                                        "children": [],
                                    },
                                    {
                                        "id": "processing_flow",
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
                                        "children": [],
                                    },
                                ],
                            },
                        }
                    ],
                    "questions": [],
                    "warnings": [],
                }
            else:
                payload = {
                    "summary": "已生成技术效果候选正文。",
                    "reply": "我已经补充了技术效果章节，重点强调了低算力实时性的收益。",
                    "rationale": "用户明确希望强调实时性收益。",
                    "operations": [
                        {
                            "op": "replace_section_blocks",
                            "section_id": target_section_id,
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
                        }
                    ],
                    "questions": [],
                    "warnings": [],
                }
            text = json.dumps(payload, ensure_ascii=False)
            return {
                "type": "respond",
                "text": text,
                "assistant_message": {"role": "assistant", "content": text},
            }

        tool_results = [msg for msg in messages if msg.get("role") == "tool"]
        user_payload = json.loads(messages[0]["content"]) if messages else {}

        if not tool_results:
            user_message = user_payload.get("user_message", "")
            target_section_id = user_payload.get("active_section_id")
            if not target_section_id:
                target_section_id = "technical_effects" if "技术效果" in user_message else "technical_solution"
            arguments = {
                "agent_id": "section_writer",
                "call_type": "rich_context_specialist",
                "goal": f"根据用户最新请求完善章节：{user_message}",
                "target_section_id": target_section_id,
                "user_message": user_message,
            }
            return {
                "type": "tool_calls",
                "tool_calls": [{"tool": "execute_subagent", "arguments": arguments, "tool_call_id": "stub_call_1"}],
                "assistant_message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "需要调用 section_writer 完成章节写作。",
                    "tool_calls": [
                        {
                            "id": "stub_call_1",
                            "type": "function",
                            "function": {
                                "name": "execute_subagent",
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }

        if len(tool_results) == 1:
            subagent_result = json.loads(tool_results[0]["content"])
            operations = subagent_result["output"]["result"]["proposal"]["operations"]
            arguments = {"operations": operations}
            return {
                "type": "tool_calls",
                "tool_calls": [{"tool": "document_edit", "arguments": arguments, "tool_call_id": "stub_call_2"}],
                "assistant_message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "需要将子 agent proposal 写入文档。",
                    "tool_calls": [
                        {
                            "id": "stub_call_2",
                            "type": "function",
                            "function": {
                                "name": "document_edit",
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }

        subagent_result = json.loads(tool_results[0]["content"])
        reply = (
            subagent_result["output"]["result"].get("reply")
            or subagent_result["output"]["result"].get("summary")
            or "已完成本轮修改。"
        )
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
                if current_event in {"round_finished", "round_failed"}:
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
        assert create_response.status_code == 200
        project = create_response.json()
        project_id = project["project_id"]

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
        assert "subagent:section_writer" in scopes
        assert event_types[-1] == "agent_output"

        sessions_response = await client.get(f"/api/projects/{project_id}/sessions")
        assert sessions_response.status_code == 200
        sessions = sessions_response.json()["sessions"]
        assert sessions[0]["session_id"] == session_id
        assert sessions[0]["is_active"] is True

        document_response = await client.get(f"/api/projects/{project_id}/document")
        assert document_response.status_code == 200
        technical_effects = next(
            section for section in document_response.json()["sections"] if section["id"] == "technical_effects"
        )
        assert len(technical_effects["blocks"]) == 2
        technical_solution = next(
            section for section in document_response.json()["sections"] if section["id"] == "technical_solution"
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
                "active_section_id": "technical_solution",
            },
        )
        assert second_stream_events[0][0] == "round_started"
        assert second_stream_events[-1][0] == "round_finished"

        document_response = await client.get(f"/api/projects/{project_id}/document")
        assert document_response.status_code == 200
        technical_solution = next(
            section for section in document_response.json()["sections"] if section["id"] == "technical_solution"
        )
        assert len(technical_solution["blocks"]) == 2
        assert technical_solution["children"][0]["id"] == "overall_architecture"

        missing_session_response = await client.post(
            f"/api/projects/{project_id}/chat/messages",
            json={"session_id": "sess_missing", "message": "继续写。"},
        )
        assert missing_session_response.status_code == 404
