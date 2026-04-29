from __future__ import annotations

from pathlib import Path

from app.agents.runtime.openai_compat import OpenAICompatibleClient
from app.core.config import Settings
from app.runtime import ContextManager, ExecutorEngine
from app.storage.workspace_store import WorkspaceStore


class DummyLLMClient(OpenAICompatibleClient):
    def __init__(self) -> None:
        pass


def make_executor(tmp_path: Path) -> tuple[ExecutorEngine, str]:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project("一种图像检测方法")
    settings = Settings(data_dir=tmp_path / "data", git_user_name="Test User", git_user_email="test@example.com")
    return ExecutorEngine(store, ContextManager(store, settings), DummyLLMClient(), settings), project.project_id


def test_document_read_and_edit_protocol(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)

    meta_result = executor.document_read(project_id, {"action": "get_meta"})
    assert meta_result["status"] == "success"
    assert meta_result["output"]["meta"]["title"] == "一种图像检测方法"

    edit_result = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": "technical_problem",
                    "block": {"type": "paragraph", "text": "低算力终端上的实时检测时延较高。"},
                }
            ]
        },
    )
    assert edit_result["status"] == "success"
    block_id = edit_result["output"]["primary_block_id"]

    block_result = executor.document_read(project_id, {"action": "get_block", "block_id": block_id})
    assert block_result["status"] == "success"
    assert block_result["output"]["section_id"] == "technical_problem"

    search_result = executor.document_read(project_id, {"action": "search_blocks", "query": "低算力"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"][0]["block_id"] == block_id


def test_document_edit_is_atomic_and_permission_checked(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)

    denied_result = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": "technical_problem",
                    "block": {"type": "paragraph", "text": "不应写入。"},
                }
            ]
        },
        scope="subagent",
    )
    assert denied_result["status"] == "failed"
    assert denied_result["output"]["code"] == "permission_denied"

    invalid_result = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": "missing_section",
                    "block": {"type": "paragraph", "text": "不应写入。"},
                }
            ]
        },
    )
    assert invalid_result["status"] == "failed"

    search_result = executor.document_read(project_id, {"action": "search_blocks", "query": "不应写入"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"] == []


def test_exec_command_runs_shell_commands_and_permission_checked(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)

    pwd_result = executor.exec_command(project_id, {"command": "pwd"})
    assert pwd_result["status"] == "success"
    assert pwd_result["output"]["exit_code"] == 0

    pipe_result = executor.exec_command(project_id, {"command": "printf ok | wc -c"})
    assert pipe_result["status"] == "success"
    assert pipe_result["output"]["exit_code"] == 0
    assert pipe_result["output"]["stdout"].strip() == "2"

    write_result = executor.exec_command(project_id, {"command": "touch unsafe.txt"})
    assert write_result["status"] == "success"
    assert write_result["output"]["exit_code"] == 0
    assert (executor.store.project_dir(project_id) / "unsafe.txt").exists()

    denied_scope = executor.exec_command(project_id, {"command": "pwd"}, scope="unknown")  # type: ignore[arg-type]
    assert denied_scope["status"] == "failed"
    assert denied_scope["output"]["code"] == "permission_denied"
