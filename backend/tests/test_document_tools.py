from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.agents.runtime.openai_compat import OpenAICompatibleClient
from app.core.command_platform import current_command_platform
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


def section_id(executor: ExecutorEngine, project_id: str, section_type: str) -> str:
    disclosure = executor.store.get_disclosure(project_id)
    section = next(section for section in disclosure["sections"] if section["type"] == section_type)
    return section["id"]


def run_tool(
    executor: ExecutorEngine,
    project_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    scope: str = "main_agent",
) -> dict[str, Any]:
    return asyncio.run(executor.execute_tool(project_id, tool_name, arguments, scope=scope))  # type: ignore[arg-type]


def test_document_read_and_edit_protocol(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")

    meta_result = run_tool(executor, project_id, "document_read", {"action": "get_meta"})
    assert meta_result["status"] == "success"
    assert meta_result["output"]["meta"]["title"] == "一种图像检测方法"

    context_result = run_tool(executor, project_id, "document_read", {"action": "get_project_context"})
    assert context_result["status"] == "success"
    context = context_result["output"]["context"]
    assert context["kind"] == "project_context"
    assert context["document"]["title"] == "一种图像检测方法"
    assert context["document"]["outline"][0] == {
        "id": "sec_000001",
        "type": "title",
        "title": "发明名称",
        "children": [],
    }
    assert "blocks" not in context["document"]["outline"][0]
    assert "anchor" not in context["document"]["outline"][0]

    edit_result = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "section_id": technical_problem_id,
            "block": {"type": "paragraph", "text": "低算力终端上的实时检测时延较高。"},
        },
    )
    assert edit_result["status"] == "success"
    block_id = edit_result["output"]["primary_block_id"]

    block_result = run_tool(executor, project_id, "document_read", {"action": "get_block", "block_id": block_id})
    assert block_result["status"] == "success"
    assert block_result["output"]["section_id"] == technical_problem_id

    search_result = run_tool(executor, project_id, "document_read", {"action": "search_blocks", "query": "低算力"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"][0]["block_id"] == block_id


def test_document_write_tools_are_atomic_and_permission_checked(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")

    denied_result = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "section_id": technical_problem_id,
            "block": {"type": "paragraph", "text": "不应写入。"},
        },
        scope="subagent",
    )
    assert denied_result["status"] == "failed"
    assert denied_result["output"]["code"] == "permission_denied"

    invalid_result = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "section_id": "missing_section",
            "block": {"type": "paragraph", "text": "不应写入。"},
        },
    )
    assert invalid_result["status"] == "failed"

    search_result = run_tool(executor, project_id, "document_read", {"action": "search_blocks", "query": "不应写入"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"] == []


def test_document_write_tools_reject_legacy_operations_shape(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")

    edit_result = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": technical_problem_id,
                    "block": {"type": "paragraph", "text": "旧 operations 结构不应被兼容。"},
                }
            ]
        },
    )

    assert edit_result["status"] == "failed"
    assert edit_result["output"]["code"] == "invalid_tool_arguments"
    assert edit_result["output"]["retry_hint"] == "请严格按照当前工具的 parameters schema 重新调用。"


def test_document_write_tools_reject_overlong_single_edit(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")

    overlong_text = "超" * 1501
    edit_result = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "section_id": technical_problem_id,
            "block": {"type": "paragraph", "text": overlong_text},
        },
    )

    assert edit_result["status"] == "failed"
    assert edit_result["output"]["code"] == "edit_too_large"
    assert "不能超过 1500 字" in edit_result["output"]["message"]

    search_result = run_tool(executor, project_id, "document_read", {"action": "search_blocks", "query": "过长正文"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"] == []


def test_document_write_tools_normalize_stringified_json_containers(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")
    technical_solution_id = section_id(executor, project_id, "technical_solution")

    append_block = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "section_id": technical_problem_id,
            "block": json.dumps({"type": "paragraph", "text": "字符串化 block 会被还原。"}, ensure_ascii=False),
        },
    )
    assert append_block["status"] == "success"

    append_child = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "parent_section_id": technical_solution_id,
            "title": "参数归一化",
            "blocks": json.dumps(
                [
                    {"type": "paragraph", "text": "字符串化 blocks 会被还原。"},
                    {"type": "list", "ordered": False, "items": json.dumps(["A", "B"], ensure_ascii=False)},
                ],
                ensure_ascii=False,
            ),
        },
    )
    assert append_child["status"] == "success"

    disclosure = executor.store.get_disclosure(project_id)
    problem = next(section for section in disclosure["sections"] if section["type"] == "technical_problem")
    solution = next(section for section in disclosure["sections"] if section["type"] == "technical_solution")
    assert problem["blocks"][0]["text"] == "字符串化 block 会被还原。"
    assert solution["children"][0]["blocks"][0]["text"] == "字符串化 blocks 会被还原。"
    assert solution["children"][0]["blocks"][1]["items"] == ["A", "B"]


def test_document_write_tools_do_not_parse_text_as_json(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")
    json_like_text = '{"保留为正文": true}'

    append_block = run_tool(
        executor,
        project_id,
        "document_append_block",
        {
            "section_id": technical_problem_id,
            "block": {"type": "paragraph", "text": json_like_text},
        },
    )

    assert append_block["status"] == "success"
    disclosure = executor.store.get_disclosure(project_id)
    problem = next(section for section in disclosure["sections"] if section["type"] == "technical_problem")
    assert problem["blocks"][0]["text"] == json_like_text


def test_document_write_tools_reject_stringified_json_with_wrong_container_type(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_solution_id = section_id(executor, project_id, "technical_solution")

    append_child = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "parent_section_id": technical_solution_id,
            "title": "错误容器",
            "blocks": json.dumps({"type": "paragraph", "text": "blocks 需要数组。"}, ensure_ascii=False),
        },
    )

    assert append_child["status"] == "failed"
    assert append_child["output"]["code"] == "invalid_tool_arguments"
    assert "blocks" in append_child["output"]["message"]


def test_append_child_section_rejects_legacy_section_object_shape(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_solution_id = section_id(executor, project_id, "technical_solution")

    missing_parent = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "section_id": technical_solution_id,
            "section": {"type": "custom", "title": "子章节", "blocks": [], "children": []},
        },
    )
    assert missing_parent["status"] == "failed"
    assert missing_parent["output"]["code"] == "invalid_tool_arguments"

    missing_section = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "parent_section_id": technical_solution_id,
            "child_section": {"type": "custom", "title": "子章节", "blocks": [], "children": []},
        },
    )
    assert missing_section["status"] == "failed"
    assert missing_section["output"]["code"] == "invalid_tool_arguments"


def test_document_write_tools_generate_section_ids_and_reject_agent_ids(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_solution_id = section_id(executor, project_id, "technical_solution")

    append_result = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "parent_section_id": technical_solution_id,
            "title": "整体架构",
            "blocks": [{"type": "paragraph", "text": "整体架构正文。"}],
        },
    )

    assert append_result["status"] == "success"
    child_id = append_result["output"]["primary_section_id"]
    assert child_id == "sec_000013"
    disclosure = executor.store.get_disclosure(project_id)
    technical_solution = next(section for section in disclosure["sections"] if section["type"] == "technical_solution")
    assert technical_solution["children"][0]["id"] == child_id
    assert technical_solution["children"][0]["type"] == "custom"

    rejected_id = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "parent_section_id": technical_solution_id,
            "id": "agent_made_id",
            "title": "错误子章节",
            "blocks": [],
        },
    )
    assert rejected_id["status"] == "failed"
    assert rejected_id["output"]["code"] == "invalid_tool_arguments"

    rejected_standard_child = run_tool(
        executor,
        project_id,
        "document_append_child_section",
        {
            "parent_section_id": technical_solution_id,
            "type": "technical_effects",
            "title": "技术效果",
            "blocks": [],
        },
    )
    assert rejected_standard_child["status"] == "failed"
    assert rejected_standard_child["output"]["code"] == "invalid_tool_arguments"


def test_exec_command_runs_shell_commands_and_permission_checked(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    profile = current_command_platform()

    def python_command(script: str) -> str:
        executable = json.dumps(sys.executable)
        script_arg = json.dumps(script)
        if profile.platform == "windows":
            return f"& {executable} -c {script_arg}"
        return f"{executable} -c {script_arg}"

    pwd_result = run_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("import os; print(os.getcwd())")},
    )
    assert pwd_result["status"] == "success"
    assert pwd_result["output"]["exit_code"] == 0
    assert pwd_result["output"]["platform"] == profile.platform
    assert pwd_result["output"]["shell"] == profile.shell

    output_result = run_tool(executor, project_id, "exec_command", {"command": python_command("print(len('ok'))")})
    assert output_result["status"] == "success"
    assert output_result["output"]["exit_code"] == 0
    assert output_result["output"]["stdout"].strip() == "2"

    write_result = run_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("from pathlib import Path; Path('unsafe.txt').write_text('ok', encoding='utf-8')")},
    )
    assert write_result["status"] == "success"
    assert write_result["output"]["exit_code"] == 0
    assert (executor.store.project_dir(project_id) / "unsafe.txt").exists()

    workspace = executor.store.project_dir(project_id)
    (workspace / "utf8.txt").write_text("中文测试\n", encoding="utf-8")
    read_command = "Get-Content -Raw -Encoding UTF8 utf8.txt" if profile.platform == "windows" else "cat utf8.txt"
    read_result = run_tool(executor, project_id, "exec_command", {"command": read_command})
    assert read_result["status"] == "success"
    assert read_result["output"]["exit_code"] == 0
    assert "中文测试" in read_result["output"]["stdout"]

    null_timeout = run_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("print('ok')"), "timeout": None},
    )
    assert null_timeout["status"] == "success"
    assert null_timeout["output"]["stdout"].strip() == "ok"

    invalid_timeout = run_tool(executor, project_id, "exec_command", {"command": "pwd", "timeout": "bad"})
    assert invalid_timeout["status"] == "failed"
    assert invalid_timeout["output"]["code"] == "invalid_operation"
    assert invalid_timeout["output"]["message"] == "timeout 必须是数字。"

    zero_timeout = run_tool(executor, project_id, "exec_command", {"command": "pwd", "timeout": 0})
    assert zero_timeout["status"] == "failed"
    assert zero_timeout["output"]["code"] == "invalid_operation"
    assert zero_timeout["output"]["message"] == "timeout 必须大于 0。"

    denied_scope = run_tool(executor, project_id, "exec_command", {"command": "pwd"}, scope="unknown")
    assert denied_scope["status"] == "failed"
    assert denied_scope["output"]["code"] == "permission_denied"
