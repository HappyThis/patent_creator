from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.core.command_platform import current_command_platform
from app.runtime import ExecutorEngine
from app.storage.workspace_store import WorkspaceStore


def make_executor(tmp_path: Path) -> tuple[ExecutorEngine, str]:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project("一种图像检测方法")
    return ExecutorEngine(store), project.project_id


def section_id(executor: ExecutorEngine, project_id: str, title: str) -> str:
    disclosure = executor.store.get_disclosure(project_id)
    section = next(section for section in disclosure["sections"] if section["title"]["text"] == title)
    return section["id"]


def run_tool(
    executor: ExecutorEngine,
    project_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return asyncio.run(executor.execute_tool(project_id, tool_name, arguments))


def test_disclosure_v3_initial_structure(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    disclosure = executor.store.get_disclosure(project_id)

    assert disclosure["meta"]["schema_version"] == "v3"
    assert "id_counters" not in disclosure["meta"]
    assert "title" not in disclosure["meta"]
    assert disclosure["sections"][0]["title"] == {"id": "blk_000001", "type": "title", "text": "发明名称"}
    assert disclosure["sections"][0]["blocks"][0] == {
        "id": "blk_000002",
        "type": "paragraph",
        "text": "一种图像检测方法",
    }
    assert "type" not in disclosure["sections"][0]
    assert "children" not in disclosure["sections"][0]


def test_disclosure_outline_search_and_read_section(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    technical_problem_id = section_id(executor, project_id, "要解决的技术问题")

    edit_result = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": technical_problem_id,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "低算力终端上的实时检测时延较高。"},
        },
    )
    assert edit_result["status"] == "success"
    block_id = edit_result["output"]["primary_block_id"]

    outline = run_tool(executor, project_id, "disclosure_outline", {"limit": 5, "offset": 0})
    assert outline["status"] == "success"
    assert outline["output"]["returned"] == 5
    assert outline["output"]["truncated"] is True
    first_item = outline["output"]["items"][0]
    assert first_item["kind"] == "section"
    assert first_item["title"]["locator"]["block_type"] == "title"

    search = run_tool(executor, project_id, "disclosure_search", {"query": "低算力"})
    assert search["status"] == "success"
    assert search["output"]["matches"][0]["locator"]["block_id"] == block_id
    assert search["output"]["matches"][0]["locator"]["section_id"] == technical_problem_id

    read = run_tool(
        executor,
        project_id,
        "disclosure_read_section",
        {"section_id": technical_problem_id, "block_ids": [block_id]},
    )
    assert read["status"] == "success"
    assert read["output"]["section"]["blocks"][0]["text"] == "低算力终端上的实时检测时延较高。"
    assert read["output"]["section"]["blocks"][0]["locator"]["index"] == 1


def test_disclosure_search_supports_regex_and_pagination(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "背景技术")
    for text in ["Alpha 创新内核", "beta 创新内核", "其他内容"]:
        result = run_tool(
            executor,
            project_id,
            "disclosure_edit",
            {
                "section_id": section,
                "operation": "insert_block",
                "position": {"mode": "end"},
                "block": {"type": "paragraph", "text": text},
            },
        )
        assert result["status"] == "success"

    search = run_tool(
        executor,
        project_id,
        "disclosure_search",
        {"query": "alpha|BETA", "regex": True, "limit": 1, "offset": 0},
    )
    assert search["status"] == "success"
    assert search["output"]["returned"] == 1
    assert search["output"]["total"] == 2
    assert search["output"]["truncated"] is True
    assert search["output"]["next_offset"] == 1


def test_disclosure_read_section_paginates_title_and_direct_blocks(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "技术方案")
    for text in ["第一段", "第二段"]:
        assert run_tool(
            executor,
            project_id,
            "disclosure_edit",
            {
                "section_id": section,
                "operation": "insert_block",
                "position": {"mode": "end"},
                "block": {"type": "paragraph", "text": text},
            },
        )["status"] == "success"

    first_page = run_tool(executor, project_id, "disclosure_read_section", {"section_id": section, "limit": 2})
    assert first_page["status"] == "success"
    assert first_page["output"]["returned"] == 2
    assert first_page["output"]["total"] == 3
    assert first_page["output"]["section"]["blocks"][0]["type"] == "title"
    assert first_page["output"]["section"]["blocks"][1]["text"] == "第一段"
    assert first_page["output"]["next_offset"] == 2

    second_page = run_tool(
        executor,
        project_id,
        "disclosure_read_section",
        {"section_id": section, "limit": 2, "offset": first_page["output"]["next_offset"]},
    )
    assert second_page["status"] == "success"
    assert second_page["output"]["section"]["blocks"][0]["text"] == "第二段"
    assert second_page["output"]["truncated"] is False


def test_disclosure_edit_block_and_section_operations(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "技术方案")

    inserted = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_section",
            "position": {"mode": "end"},
            "section": {"title": "创新内核门禁机制"},
        },
    )
    assert inserted["status"] == "success"
    child_section_id = inserted["output"]["primary_section_id"]
    child_title_block_id = inserted["output"]["primary_block_id"]

    renamed = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": child_section_id,
            "operation": "replace_block",
            "block_id": child_title_block_id,
            "block": {"type": "title", "text": "创新内核前置门禁机制"},
        },
    )
    assert renamed["status"] == "success"

    deleted = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {"section_id": section, "operation": "delete_section", "target_section_id": child_section_id},
    )
    assert deleted["status"] == "success"
    disclosure = executor.store.get_disclosure(project_id)
    parent = next(item for item in disclosure["sections"] if item["id"] == section)
    assert parent["sections"] == []


def test_disclosure_edit_rejects_cross_section_block_operation(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    background = section_id(executor, project_id, "背景技术")
    solution = section_id(executor, project_id, "技术方案")
    inserted = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": background,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "背景段落。"},
        },
    )
    block_id = inserted["output"]["primary_block_id"]

    rejected = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": solution,
            "operation": "replace_block",
            "block_id": block_id,
            "block": {"type": "paragraph", "text": "不应跨 section 替换。"},
        },
    )
    assert rejected["status"] == "failed"
    assert rejected["output"]["code"] == "block_not_found"


def test_disclosure_edit_rejects_title_delete_and_before_title_insert(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "技术方案")
    read = run_tool(executor, project_id, "disclosure_read_section", {"section_id": section, "limit": 1})
    title_block_id = read["output"]["section"]["title"]["locator"]["block_id"]

    delete_title = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {"section_id": section, "operation": "delete_block", "block_id": title_block_id},
    )
    assert delete_title["status"] == "failed"

    before_title = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "before", "block_id": title_block_id},
            "block": {"type": "paragraph", "text": "不允许插入。"},
        },
    )
    assert before_title["status"] == "failed"


def test_disclosure_edit_rejects_overlong_single_edit(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "要解决的技术问题")

    edit_result = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "超" * 1501},
        },
    )

    assert edit_result["status"] == "failed"
    assert edit_result["output"]["code"] == "edit_too_large"
    assert "不能超过 1500 字" in edit_result["output"]["message"]


def test_disclosure_edit_normalizes_stringified_json_containers(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "要解决的技术问题")

    appended = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": json.dumps({"mode": "end"}, ensure_ascii=False),
            "block": json.dumps({"type": "paragraph", "text": "字符串化 block 会被还原。"}, ensure_ascii=False),
        },
    )
    assert appended["status"] == "success"

    disclosure = executor.store.get_disclosure(project_id)
    problem = next(item for item in disclosure["sections"] if item["id"] == section)
    assert problem["blocks"][0]["text"] == "字符串化 block 会被还原。"


def test_disclosure_edit_do_not_parse_text_as_json(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    section = section_id(executor, project_id, "要解决的技术问题")
    json_like_text = '{"保留为正文": true}'

    result = run_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": json_like_text},
        },
    )

    assert result["status"] == "success"
    disclosure = executor.store.get_disclosure(project_id)
    problem = next(item for item in disclosure["sections"] if item["id"] == section)
    assert problem["blocks"][0]["text"] == json_like_text


def test_disclosure_edit_rejects_legacy_document_tool_names(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    result = run_tool(executor, project_id, "document_append_block", {})
    assert result["status"] == "failed"
    assert result["output"]["code"] == "unsupported_tool"


def test_exec_command_runs_shell_commands(tmp_path: Path) -> None:
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
    assert output_result["output"]["stdout_truncated"] is False

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

    large_result = run_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("print('x' * 35000)")},
    )
    assert large_result["status"] == "success"
    assert large_result["output"]["stdout_truncated"] is True
    assert len(large_result["output"]["stdout"]) < 10000
    stdout_path = executor.store.project_dir(project_id) / large_result["output"]["stdout_path"]
    assert stdout_path.exists()
    assert stdout_path.read_text(encoding="utf-8").startswith("x" * 100)

    persisted_read = run_tool(
        executor,
        project_id,
        "file_read",
        {"path": large_result["output"]["stdout_path"], "start_line": 1, "limit": 1},
    )
    assert persisted_read["status"] == "success"
    assert persisted_read["output"]["content"].startswith("1 | ")

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


def test_file_exploration_tools_page_results(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "alpha.py").write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")
    (source_dir / "beta.py").write_text("def beta():\n    return 'other'\n", encoding="utf-8")

    glob_result = run_tool(executor, project_id, "file_glob", {"pattern": "src/*.py", "limit": 1})
    assert glob_result["status"] == "success"
    assert glob_result["output"]["matches"] == ["src/alpha.py"]
    assert glob_result["output"]["truncated"] is True
    assert glob_result["output"]["next_offset"] == 1

    glob_in_path_result = run_tool(
        executor,
        project_id,
        "file_glob",
        {"path": "src/*.py", "pattern": "**/*.py", "limit": 10},
    )
    assert glob_in_path_result["status"] == "success"
    assert glob_in_path_result["output"]["matches"] == ["src/alpha.py", "src/beta.py"]
    assert glob_in_path_result["output"]["effective_path"] == "src"
    assert glob_in_path_result["output"]["effective_pattern"] == "*.py"

    search_result = run_tool(executor, project_id, "file_search", {"pattern": "needle", "path": "src"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"][0]["path"] == "src/alpha.py"
    assert search_result["output"]["matches"][0]["line"] == 2

    read_result = run_tool(executor, project_id, "file_read", {"path": "src/alpha.py", "start_line": 1, "limit": 1})
    assert read_result["status"] == "success"
    assert read_result["output"]["content"] == "1 | def alpha():"
    assert read_result["output"]["next_start_line"] == 2

    external_dir = tmp_path / "external_repo"
    external_dir.mkdir()
    external_file = external_dir / "gamma.py"
    external_file.write_text("print('external needle')\n", encoding="utf-8")

    external_glob = run_tool(
        executor,
        project_id,
        "file_glob",
        {"path": str(external_dir), "pattern": "*.py", "limit": 10},
    )
    assert external_glob["status"] == "success"
    assert external_glob["output"]["matches"] == [str(external_file)]

    external_glob_in_path = run_tool(
        executor,
        project_id,
        "file_glob",
        {"path": str(external_dir / "*.py"), "pattern": "**/*.py", "limit": 10},
    )
    assert external_glob_in_path["status"] == "success"
    assert external_glob_in_path["output"]["matches"] == [str(external_file)]

    external_search = run_tool(
        executor,
        project_id,
        "file_search",
        {"path": str(external_dir), "pattern": "external needle"},
    )
    assert external_search["status"] == "success"
    assert external_search["output"]["matches"][0]["path"] == str(external_file)


def test_file_glob_stops_at_scan_budget(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    many_dir = workspace / "many"
    many_dir.mkdir()
    for index in range(10):
        (many_dir / f"{index:02d}.txt").write_text("ok", encoding="utf-8")

    glob_result = run_tool(
        executor,
        project_id,
        "file_glob",
        {"path": "many", "pattern": "*", "limit": 100, "max_scanned_paths": 3},
    )

    assert glob_result["status"] == "success"
    assert glob_result["output"]["stop_reason"] == "scan_budget_exceeded"
    assert glob_result["output"]["truncated"] is True
    assert glob_result["output"]["scanned"] == 3
    assert glob_result["output"]["scan_budget"] == 3
    assert glob_result["output"]["total_is_lower_bound"] is True


def test_file_glob_skips_heavy_directories_during_scan(tmp_path: Path) -> None:
    executor, project_id = make_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "visible.py").write_text("print('visible')\n", encoding="utf-8")
    vendor_dir = workspace / "node_modules"
    vendor_dir.mkdir()
    (vendor_dir / "hidden.py").write_text("print('hidden')\n", encoding="utf-8")

    glob_result = run_tool(executor, project_id, "file_glob", {"pattern": "**/*.py", "limit": 100})

    assert glob_result["status"] == "success"
    assert glob_result["output"]["matches"] == ["src/visible.py"]
    assert "node_modules" in glob_result["output"]["skipped_dirs"]
