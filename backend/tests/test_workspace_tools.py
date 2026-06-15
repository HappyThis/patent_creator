from __future__ import annotations

import json
import sys
from pathlib import Path

from app.core.command_platform import current_command_platform

from helpers import make_tool_executor, run_builtin_tool


def test_exec_command_runs_shell_commands(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    profile = current_command_platform()

    def python_command(script: str) -> str:
        executable = json.dumps(sys.executable)
        script_arg = json.dumps(script)
        if profile.platform == "windows":
            return f"& {executable} -c {script_arg}"
        return f"{executable} -c {script_arg}"

    pwd_result = run_builtin_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("import os; print(os.getcwd())")},
    )
    assert pwd_result["status"] == "success"
    assert pwd_result["output"]["exit_code"] == 0
    assert pwd_result["output"]["platform"] == profile.platform
    assert pwd_result["output"]["shell"] == profile.shell

    output_result = run_builtin_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("print(len('ok'))")},
    )
    assert output_result["status"] == "success"
    assert output_result["output"]["exit_code"] == 0
    assert output_result["output"]["stdout"].strip() == "2"
    assert output_result["output"]["stdout_truncated"] is False

    write_result = run_builtin_tool(
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
    read_result = run_builtin_tool(executor, project_id, "exec_command", {"command": read_command})
    assert read_result["status"] == "success"
    assert read_result["output"]["exit_code"] == 0
    assert "中文测试" in read_result["output"]["stdout"]

    large_result = run_builtin_tool(
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

    persisted_read = run_builtin_tool(
        executor,
        project_id,
        "file_read",
        {"path": large_result["output"]["stdout_path"], "start_line": 1, "limit": 1},
    )
    assert persisted_read["status"] == "success"
    assert persisted_read["output"]["content"].startswith("1 | ")

    null_timeout = run_builtin_tool(
        executor,
        project_id,
        "exec_command",
        {"command": python_command("print('ok')"), "timeout": None},
    )
    assert null_timeout["status"] == "success"
    assert null_timeout["output"]["stdout"].strip() == "ok"

    invalid_timeout = run_builtin_tool(executor, project_id, "exec_command", {"command": "pwd", "timeout": "bad"})
    assert invalid_timeout["status"] == "failed"
    assert invalid_timeout["output"]["code"] == "invalid_operation"
    assert invalid_timeout["output"]["message"] == "timeout 必须是数字。"

    zero_timeout = run_builtin_tool(executor, project_id, "exec_command", {"command": "pwd", "timeout": 0})
    assert zero_timeout["status"] == "failed"
    assert zero_timeout["output"]["code"] == "invalid_operation"
    assert zero_timeout["output"]["message"] == "timeout 必须大于 0。"


def test_file_exploration_tools_page_results(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "alpha.py").write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")
    (source_dir / "beta.py").write_text("def beta():\n    return 'other'\n", encoding="utf-8")

    glob_result = run_builtin_tool(executor, project_id, "file_glob", {"pattern": "src/*.py", "limit": 1})
    assert glob_result["status"] == "success"
    assert glob_result["output"]["matches"] == ["src/alpha.py"]
    assert glob_result["output"]["truncated"] is True
    assert glob_result["output"]["next_offset"] == 1

    glob_in_path_result = run_builtin_tool(
        executor,
        project_id,
        "file_glob",
        {"path": "src/*.py", "pattern": "**/*.py", "limit": 10},
    )
    assert glob_in_path_result["status"] == "success"
    assert glob_in_path_result["output"]["matches"] == ["src/alpha.py", "src/beta.py"]
    assert glob_in_path_result["output"]["effective_path"] == "src"
    assert glob_in_path_result["output"]["effective_pattern"] == "*.py"

    search_result = run_builtin_tool(executor, project_id, "file_search", {"pattern": "needle", "path": "src"})
    assert search_result["status"] == "success"
    assert search_result["output"]["matches"][0]["path"] == "src/alpha.py"
    assert search_result["output"]["matches"][0]["line"] == 2

    read_result = run_builtin_tool(executor, project_id, "file_read", {"path": "src/alpha.py", "start_line": 1, "limit": 1})
    assert read_result["status"] == "success"
    assert read_result["output"]["content"] == "1 | def alpha():"
    assert read_result["output"]["next_start_line"] == 2

    external_dir = tmp_path / "external_repo"
    external_dir.mkdir()
    external_file = external_dir / "gamma.py"
    external_file.write_text("print('external needle')\n", encoding="utf-8")

    external_glob = run_builtin_tool(
        executor,
        project_id,
        "file_glob",
        {"path": str(external_dir), "pattern": "*.py", "limit": 10},
    )
    assert external_glob["status"] == "success"
    assert external_glob["output"]["matches"] == [str(external_file)]

    external_glob_in_path = run_builtin_tool(
        executor,
        project_id,
        "file_glob",
        {"path": str(external_dir / "*.py"), "pattern": "**/*.py", "limit": 10},
    )
    assert external_glob_in_path["status"] == "success"
    assert external_glob_in_path["output"]["matches"] == [str(external_file)]

    external_search = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"path": str(external_dir), "pattern": "external needle"},
    )
    assert external_search["status"] == "success"
    assert external_search["output"]["matches"][0]["path"] == str(external_file)


def test_file_glob_stops_at_scan_budget(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    many_dir = workspace / "many"
    many_dir.mkdir()
    for index in range(10):
        (many_dir / f"{index:02d}.txt").write_text("ok", encoding="utf-8")

    glob_result = run_builtin_tool(
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
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "visible.py").write_text("print('visible')\n", encoding="utf-8")
    vendor_dir = workspace / "node_modules"
    vendor_dir.mkdir()
    (vendor_dir / "hidden.py").write_text("print('hidden')\n", encoding="utf-8")

    glob_result = run_builtin_tool(executor, project_id, "file_glob", {"pattern": "**/*.py", "limit": 100})

    assert glob_result["status"] == "success"
    assert glob_result["output"]["matches"] == ["src/visible.py"]
    assert "node_modules" in glob_result["output"]["skipped_dirs"]
