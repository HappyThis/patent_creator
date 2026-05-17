from __future__ import annotations

import json
import sys
from pathlib import Path

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


def test_document_read_and_edit_protocol(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")

    meta_result = executor.document_read(project_id, {"action": "get_meta"})
    assert meta_result["status"] == "success"
    assert meta_result["output"]["meta"]["title"] == "一种图像检测方法"

    context_result = executor.document_read(project_id, {"action": "get_project_context"})
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

    edit_result = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": technical_problem_id,
                    "block": {"type": "paragraph", "text": "低算力终端上的实时检测时延较高。"},
                }
            ]
        },
    )
    assert edit_result["status"] == "success"
    block_id = edit_result["output"]["primary_block_id"]

    block_result = executor.document_read(project_id, {"action": "get_block", "block_id": block_id})
    assert block_result["status"] == "success"
    assert block_result["output"]["section_id"] == technical_problem_id

    search_result = executor.document_read(project_id, {"action": "search_blocks", "query": "低算力"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"][0]["block_id"] == block_id


def test_document_edit_is_atomic_and_permission_checked(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")

    denied_result = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": technical_problem_id,
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


def test_document_edit_accepts_stringified_operations(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "technical_problem")
    operations = [
        {
            "op": "append_block",
            "section_id": technical_problem_id,
            "block": {"type": "paragraph", "text": "字符串化 operations 也应能被安全解析。"},
        }
    ]

    edit_result = executor.document_edit(project_id, {"operations": json.dumps(operations, ensure_ascii=False)})

    assert edit_result["status"] == "success"
    block_id = edit_result["output"]["primary_block_id"]
    block_result = executor.document_read(project_id, {"action": "get_block", "block_id": block_id})
    assert block_result["output"]["block"]["text"] == "字符串化 operations 也应能被安全解析。"


def test_document_edit_rejects_invalid_stringified_operations(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)

    invalid_json = executor.document_edit(project_id, {"operations": "[not-json]"})
    assert invalid_json["status"] == "failed"
    assert invalid_json["output"]["code"] == "invalid_operation"
    assert "无法解析为 JSON 数组" in invalid_json["output"]["message"]

    object_json = executor.document_edit(project_id, {"operations": json.dumps({"op": "append_block"})})
    assert object_json["status"] == "failed"
    assert object_json["output"]["code"] == "invalid_operation"
    assert object_json["output"]["message"] == "document_edit.operations 必须是非空数组。"

    scalar_items = executor.document_edit(project_id, {"operations": json.dumps(["append_block"])})
    assert scalar_items["status"] == "failed"
    assert scalar_items["output"]["code"] == "invalid_operation"
    assert scalar_items["output"]["message"] == "document_edit.operations 中的每一项都必须是对象。"


def test_append_child_section_requires_parent_section_id_and_section(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_solution_id = section_id(executor, project_id, "technical_solution")

    missing_parent = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_child_section",
                    "section_id": technical_solution_id,
                    "section": {"type": "custom", "title": "子章节", "blocks": [], "children": []},
                }
            ]
        },
    )
    assert missing_parent["status"] == "failed"
    assert missing_parent["output"]["message"] == "append_child_section 需要 parent_section_id 和 section。"

    missing_section = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_child_section",
                    "parent_section_id": technical_solution_id,
                    "child_section": {"type": "custom", "title": "子章节", "blocks": [], "children": []},
                }
            ]
        },
    )
    assert missing_section["status"] == "failed"
    assert missing_section["output"]["message"] == "append_child_section 需要 parent_section_id 和 section。"


def test_document_edit_generates_section_ids_and_rejects_agent_ids(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_solution_id = section_id(executor, project_id, "technical_solution")

    append_result = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_child_section",
                    "parent_section_id": technical_solution_id,
                    "section": {
                        "type": "custom",
                        "title": "整体架构",
                        "blocks": [{"type": "paragraph", "text": "整体架构正文。"}],
                        "children": [],
                    },
                }
            ]
        },
    )

    assert append_result["status"] == "success"
    child_id = append_result["output"]["primary_section_id"]
    assert child_id == "sec_000013"
    disclosure = executor.store.get_disclosure(project_id)
    technical_solution = next(section for section in disclosure["sections"] if section["type"] == "technical_solution")
    assert technical_solution["children"][0]["id"] == child_id
    assert technical_solution["children"][0]["type"] == "custom"

    rejected_id = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_child_section",
                    "parent_section_id": technical_solution_id,
                    "section": {"id": "agent_made_id", "type": "custom", "title": "错误子章节", "blocks": [], "children": []},
                }
            ]
        },
    )
    assert rejected_id["status"] == "failed"
    assert rejected_id["output"]["message"] == "section 不允许携带 id；section_id 由系统生成或保留。"

    rejected_standard_child = executor.document_edit(
        project_id,
        {
            "operations": [
                {
                    "op": "append_child_section",
                    "parent_section_id": technical_solution_id,
                    "section": {"type": "technical_effects", "title": "技术效果", "blocks": [], "children": []},
                }
            ]
        },
    )
    assert rejected_standard_child["status"] == "failed"
    assert rejected_standard_child["output"]["message"] == "子章节 section.type 必须为 custom。"


def test_exec_command_runs_shell_commands_and_permission_checked(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    profile = current_command_platform()

    def python_command(script: str) -> str:
        executable = json.dumps(sys.executable)
        script_arg = json.dumps(script)
        if profile.platform == "windows":
            return f"& {executable} -c {script_arg}"
        return f"{executable} -c {script_arg}"

    pwd_result = executor.exec_command(project_id, {"command": python_command("import os; print(os.getcwd())")})
    assert pwd_result["status"] == "success"
    assert pwd_result["output"]["exit_code"] == 0
    assert pwd_result["output"]["platform"] == profile.platform
    assert pwd_result["output"]["shell"] == profile.shell

    output_result = executor.exec_command(project_id, {"command": python_command("print(len('ok'))")})
    assert output_result["status"] == "success"
    assert output_result["output"]["exit_code"] == 0
    assert output_result["output"]["stdout"].strip() == "2"

    write_result = executor.exec_command(
        project_id,
        {"command": python_command("from pathlib import Path; Path('unsafe.txt').write_text('ok', encoding='utf-8')")},
    )
    assert write_result["status"] == "success"
    assert write_result["output"]["exit_code"] == 0
    assert (executor.store.project_dir(project_id) / "unsafe.txt").exists()

    workspace = executor.store.project_dir(project_id)
    (workspace / "utf8.txt").write_text("中文测试\n", encoding="utf-8")
    read_command = "Get-Content -Raw -Encoding UTF8 utf8.txt" if profile.platform == "windows" else "cat utf8.txt"
    read_result = executor.exec_command(project_id, {"command": read_command})
    assert read_result["status"] == "success"
    assert read_result["output"]["exit_code"] == 0
    assert "中文测试" in read_result["output"]["stdout"]

    denied_scope = executor.exec_command(project_id, {"command": "pwd"}, scope="unknown")  # type: ignore[arg-type]
    assert denied_scope["status"] == "failed"
    assert denied_scope["output"]["code"] == "permission_denied"
