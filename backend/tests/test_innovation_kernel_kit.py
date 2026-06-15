from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.runtime import ContextManager, ExecutorEngine
from app.runtime.executor import ToolRuntimeContext
from app.storage.workspace_store import WorkspaceStore


def make_runtime(tmp_path: Path) -> tuple[WorkspaceStore, ExecutorEngine, ContextManager, str, str, Settings]:
    settings = Settings(data_dir=tmp_path / "data", git_user_name="Test User", git_user_email="test@example.com")
    store = WorkspaceStore(settings.data_dir, settings.git_user_name, settings.git_user_email)
    project = store.create_project("创新内核测试")
    session_id = "sess_kernel"
    store.append_session_event(
        project.project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        payload={"text": "基于材料生成交底书。"},
    )
    return store, ExecutorEngine(store), ContextManager(store, settings), project.project_id, session_id, settings


def run_kernel_tool(
    executor: ExecutorEngine,
    project_id: str,
    session_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return asyncio.run(
        executor.execute_tool(
            project_id,
            "innovation_kernel_kit",
            arguments,
            runtime_context=ToolRuntimeContext(
                session_id=session_id,
                round_id="round_1",
                message_id="msg_1",
                parent_call_id="call_kernel",
            ),
        )
    )


def test_innovation_kernel_kit_write_and_read_current_kernel(tmp_path: Path) -> None:
    store, executor, _manager, project_id, session_id, _settings = make_runtime(tmp_path)

    first = run_kernel_tool(
        executor,
        project_id,
        session_id,
        {"action": "write", "kernel_markdown": "  # 创新内核\n\n## 1. 核心问题\n旧问题  "},
    )
    assert first["status"] == "success"
    assert first["output"]["source"] == "write"
    assert first["output"]["kernel_markdown"] == "# 创新内核\n\n## 1. 核心问题\n旧问题"
    assert "user_confirmation_reminder" not in first["output"]

    read = run_kernel_tool(executor, project_id, session_id, {"action": "read"})
    assert read["status"] == "success"
    assert read["output"]["kernel_markdown"] == first["output"]["kernel_markdown"]

    second = run_kernel_tool(
        executor,
        project_id,
        session_id,
        {"action": "write", "kernel_markdown": "# 创新内核\n\n## 1. 核心问题\n新问题"},
    )
    assert second["status"] == "success"
    assert "新问题" in second["output"]["kernel_markdown"]
    assert "旧问题" not in store.get_innovation_kernel(project_id, session_id).kernel_markdown

    kernel_files = list((store.project_dir(project_id) / "sessions").glob("*.innovation_kernel.json"))
    assert len(kernel_files) == 1


def test_innovation_kernel_kit_read_requires_current_kernel(tmp_path: Path) -> None:
    _store, executor, _manager, project_id, session_id, _settings = make_runtime(tmp_path)

    result = run_kernel_tool(executor, project_id, session_id, {"action": "read"})

    assert result["status"] == "failed"
    assert result["output"]["code"] == "innovation_kernel_not_found"


def test_innovation_kernel_kit_write_requires_non_empty_kernel_markdown(tmp_path: Path) -> None:
    _store, executor, _manager, project_id, session_id, _settings = make_runtime(tmp_path)

    missing = run_kernel_tool(executor, project_id, session_id, {"action": "write"})
    blank = run_kernel_tool(executor, project_id, session_id, {"action": "write", "kernel_markdown": " \n\t "})

    assert missing["status"] == "failed"
    assert missing["output"]["code"] == "innovation_kernel_empty_content"
    assert blank["status"] == "failed"
    assert blank["output"]["code"] == "innovation_kernel_empty_content"


def test_innovation_kernel_kit_rejects_old_actions(tmp_path: Path) -> None:
    _store, executor, _manager, project_id, session_id, _settings = make_runtime(tmp_path)

    for action in ("create", "recreate", "read_all"):
        result = run_kernel_tool(executor, project_id, session_id, {"action": action})
        assert result["status"] == "failed"
        assert result["output"]["code"] == "invalid_action"
        assert "read 或 write" in result["output"]["message"]


def test_innovation_kernel_kit_write_preserves_caller_content_without_parsing(tmp_path: Path) -> None:
    _store, executor, _manager, project_id, session_id, _settings = make_runtime(tmp_path)
    content = "<analysis>x</analysis><innovation_kernel># K</innovation_kernel>"

    result = run_kernel_tool(executor, project_id, session_id, {"action": "write", "kernel_markdown": content})

    assert result["status"] == "success"
    assert result["output"]["kernel_markdown"] == content


def test_context_manager_does_not_inject_current_kernel_without_tool_history(tmp_path: Path) -> None:
    store, _executor, manager, project_id, session_id, _settings = make_runtime(tmp_path)
    store.save_innovation_kernel(
        project_id,
        session_id,
        kernel_markdown="# 创新内核\n\n## 1. 核心问题\n当前问题",
        source="write",
    )

    messages = manager.build_main_agent_messages(
        project_id,
        session_id,
        user_message="继续写交底书。",
        active_section_id=None,
        active_block_id=None,
        current_message_id="msg_2",
    )

    assert messages[-1] == {"role": "user", "content": "继续写交底书。"}
    assert all("[当前创新内核]" not in str(message.get("content") or "") for message in messages)
    assert all("当前问题" not in str(message.get("content") or "") for message in messages)
    events = store.read_session_events(project_id, session_id)
    assert [event.type for event in events] == ["user_input"]
