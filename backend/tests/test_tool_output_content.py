from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.runtime.context import ContextManager
from app.runtime.context.tool_output_content import (
    FIGURE_VISUAL_REVIEW_PROMPT,
    hydrate_tool_output_content,
)
from app.storage.workspace_store import WorkspaceStore


PNG_BYTES = b"\x89PNG\r\n\x1a\nsmall-test-image"


class CompressionShouldNotRun:
    async def generate_text(self, **_: Any) -> str:
        raise AssertionError("context compression should not run")


def _store_with_project(tmp_path: Path) -> tuple[WorkspaceStore, str]:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project("一种图像检测方法")
    return store, project.project_id


def _write_render_png(
    store: WorkspaceStore,
    project_id: str,
    figure_id: str,
    data: bytes = PNG_BYTES,
) -> None:
    path = store.figure_render_file(project_id, figure_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _figure_tool_message(
    *,
    round_id: str = "round_1",
    figure_id: str = "fig_000001",
    message_id: str = "msg_1",
    drawio_updated_at: str | None = None,
) -> dict[str, Any]:
    attachment = {"type": "render_image", "ref": f"figure:{figure_id}", "purpose": "visual_review"}
    if drawio_updated_at is not None:
        attachment["drawio_updated_at"] = drawio_updated_at
    return {
        "role": "tool",
        "tool_call_id": "call_figure",
        "tool_name": "figure_kit",
        "round_id": round_id,
        "message_id": message_id,
        "content": json.dumps(
            {
                "status": "success",
                "output": {
                    "figure": {"figure_id": figure_id, "ref": f"figure:{figure_id}", "title": "系统结构示意图"},
                    "review": {
                        "attempt": 3,
                        "limit": 8,
                        "remaining": 5,
                        "successful_renders": 2,
                        "consecutive_failures": 0,
                        "stable_version_available": True,
                    },
                    "warnings": [
                        {"code": "drawio_edge_curved", "message": "连线 edge-1 使用曲线。"}
                    ],
                    "attachments": [attachment],
                },
            },
            ensure_ascii=False,
        ),
    }


def test_hydrate_tool_output_content_adds_current_round_figure_image_after_tool_block(tmp_path: Path) -> None:
    store, project_id = _store_with_project(tmp_path)
    _write_render_png(store, project_id, "fig_000001")
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        _figure_tool_message(),
        {
            "role": "tool",
            "tool_call_id": "call_other",
            "tool_name": "disclosure_outline",
            "round_id": "round_1",
            "content": "{}",
        },
        {"role": "user", "content": "下一条用户消息"},
    ]

    hydrated = hydrate_tool_output_content(store, project_id, messages, round_id="round_1")

    assert [message["role"] for message in hydrated] == ["assistant", "tool", "tool", "user", "user"]
    review_message = hydrated[3]
    assert review_message["tool_output_attachment"] is True
    assert review_message["content"][0]["type"] == "input_text"
    assert FIGURE_VISUAL_REVIEW_PROMPT in review_message["content"][0]["text"]
    review_text = review_message["content"][0]["text"]
    assert "read 复用已有截图，不占 write/update 尝试次数" in review_text
    assert "最多尝试 8 次 write/update" in review_text
    assert "预检或渲染失败也计数" in review_text
    assert "技术表达" in review_text
    assert "图解逻辑" in review_text
    assert "文字" in review_text
    assert "连线" in review_text
    assert "缩略图构图" in review_text
    assert "对齐与节奏" in review_text
    assert "视觉系统" in review_text
    assert "同一 visualRole 的节点" in review_text
    assert "首尾是否接触正确节点" in review_text
    assert "悬空、微小折返、共线覆盖" in review_text
    assert "错误共享路径和汇聚错位" in review_text
    assert "统一无衬线字体、黑白灰度、边框和主线线宽、箭头" in review_text
    assert "多对多关系是否应收束为总线、汇聚点、中间层或集合" in review_text
    assert "视觉中心、主阅读方向、语义分区和层级" in review_text
    assert "不像可直接使用的正式工程图" in review_text
    assert "轻微不对称和纯个人偏好留给用户" in review_text
    assert "不做无变化编辑" in review_message["content"][0]["text"]
    assert "已尝试 3/8 次，成功渲染 2 次，剩余 5 次" in review_message["content"][0]["text"]
    assert "自动检查的非阻断 warnings" in review_text
    assert "连线 edge-1 使用曲线" in review_text
    assert "硬失败条件" not in review_message["content"][0]["text"]
    assert "大外框包住整张主画面" not in review_message["content"][0]["text"]
    assert "fig_000001" in review_message["content"][0]["text"]
    assert review_message["content"][1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}",
        "detail": "high",
    }


def test_hydrate_tool_output_content_skips_old_round_and_missing_image(tmp_path: Path) -> None:
    store, project_id = _store_with_project(tmp_path)
    _write_render_png(store, project_id, "fig_000001")
    messages = [_figure_tool_message(round_id="round_old")]

    assert hydrate_tool_output_content(store, project_id, messages, round_id="round_new") is messages
    missing_image_messages = [_figure_tool_message(figure_id="fig_999999")]
    hydrated = hydrate_tool_output_content(store, project_id, missing_image_messages, round_id="round_1")
    assert [message["role"] for message in hydrated] == ["tool", "user"]
    assert "附件读取失败" in hydrated[1]["content"][0]["text"]
    assert "不要声称已经查看" in hydrated[1]["content"][0]["text"]


def test_hydrate_tool_output_content_reports_attachment_limit(tmp_path: Path) -> None:
    store, project_id = _store_with_project(tmp_path)
    _write_render_png(store, project_id, "fig_000001")
    messages = [_figure_tool_message()]

    hydrated = hydrate_tool_output_content(
        store,
        project_id,
        messages,
        round_id="round_1",
        max_image_attachments=0,
    )

    assert [message["role"] for message in hydrated] == ["tool", "user"]
    assert "早于最近图片窗口" in hydrated[1]["content"][0]["text"]


def test_hydrate_tool_output_content_keeps_newest_eight_distinct_images(tmp_path: Path) -> None:
    store, project_id = _store_with_project(tmp_path)
    messages: list[dict[str, Any]] = []
    for index in range(1, 10):
        figure_id = f"fig_{index:06d}"
        _write_render_png(store, project_id, figure_id, data=f"png-{index}".encode())
        messages.append(_figure_tool_message(figure_id=figure_id, message_id=f"msg_{index}"))

    hydrated = hydrate_tool_output_content(store, project_id, messages, round_id="round_1")
    reviews = [message for message in hydrated if message.get("tool_output_attachment")]
    image_reviews = [
        message
        for message in reviews
        if any(part.get("type") == "input_image" for part in message.get("content", []))
    ]

    assert len(image_reviews) == 8
    assert "早于最近图片窗口" in reviews[0]["content"][0]["text"]
    assert "fig_000001" not in "\n".join(message["content"][0]["text"] for message in image_reviews)
    assert "fig_000002" in image_reviews[0]["content"][0]["text"]
    assert "fig_000009" in image_reviews[-1]["content"][0]["text"]


def test_hydrate_tool_output_content_deduplicates_same_revision_and_keeps_latest_occurrence(tmp_path: Path) -> None:
    store, project_id = _store_with_project(tmp_path)
    _write_render_png(store, project_id, "fig_000001")
    messages = [
        _figure_tool_message(message_id="msg_old"),
        _figure_tool_message(message_id="msg_new"),
    ]

    hydrated = hydrate_tool_output_content(store, project_id, messages, round_id="round_1")
    reviews = [message for message in hydrated if message.get("tool_output_attachment")]

    assert len(reviews) == 1
    assert reviews[0]["message_id"] == "msg_new"
    assert any(part.get("type") == "input_image" for part in reviews[0]["content"])


def test_context_manager_prepares_current_round_figure_visual_review(tmp_path: Path) -> None:
    store, project_id = _store_with_project(tmp_path)
    _write_render_png(store, project_id, "fig_000001")
    session_id = "sess_figure"

    store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        payload={"text": "画一张结构图"},
    )
    store.append_session_event(
        project_id,
        session_id,
        event_type="agent_message",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        payload={
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_figure",
                        "type": "function",
                        "function": {"name": "figure_kit", "arguments": "{}"},
                    }
                ],
            },
            "model": "test",
        },
    )
    store.append_session_event(
        project_id,
        session_id,
        event_type="tool_result",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        call_id="call_figure",
        payload={
            "tool": "figure_kit",
            "status": "success",
            "output": {
                "figure": {"figure_id": "fig_000001", "ref": "figure:fig_000001", "title": "系统结构示意图"},
                "attachments": [
                    {"type": "render_image", "ref": "figure:fig_000001", "purpose": "visual_review"}
                ],
            },
        },
    )
    manager = ContextManager(
        store,
        Settings(data_dir=tmp_path / "data", git_user_name="Test User", git_user_email="test@example.com"),
    )

    messages = asyncio.run(
        manager.prepare_main_agent_messages(
            project_id,
            session_id,
            user_message="继续",
            current_message_id=None,
            round_id="round_1",
            system_prompt="system",
            llm_client=CompressionShouldNotRun(),
        )
    )

    review_messages = [
        message
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
    ]
    assert len(review_messages) == 1
    assert review_messages[0]["content"][1]["type"] == "input_image"
    assert review_messages[0]["content"][1]["image_url"].startswith("data:image/png;base64,")
