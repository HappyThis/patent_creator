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
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": "call_figure",
        "tool_name": "figure_kit",
        "round_id": round_id,
        "message_id": "msg_1",
        "content": json.dumps(
            {
                "status": "success",
                "output": {"figure": {"figure_id": figure_id, "title": "系统结构示意图"}},
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
    assert "geometry_report" in review_message["content"][0]["text"]
    assert "优先处理 issues 中 severity=error 的问题" in review_message["content"][0]["text"]
    assert "semantic_* 结构语义问题" in review_message["content"][0]["text"]
    assert "硬失败条件" in review_message["content"][0]["text"]
    assert "只要命中任一条，就必须读取 diagram.html 并调用 figure_kit.update 修正" in review_message["content"][0]["text"]
    assert "不要用“基本还行”放过明显瑕疵" in review_message["content"][0]["text"]
    assert "业务节点没有参与任何关系" in review_message["content"][0]["text"]
    assert "同一分组内同类节点的连接关系不一致" in review_message["content"][0]["text"]
    assert "大外框包住整张主画面" in review_message["content"][0]["text"]
    assert "独立线型示例、图例盒或说明卡" in review_message["content"][0]["text"]
    assert "跨越主画面的长斜线" in review_message["content"][0]["text"]
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
    missing_image_messages = [_figure_tool_message(figure_id="fig_missing")]
    assert (
        hydrate_tool_output_content(store, project_id, missing_image_messages, round_id="round_1")
        == missing_image_messages
    )


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
            "output": {"figure": {"figure_id": "fig_000001", "title": "系统结构示意图"}},
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
