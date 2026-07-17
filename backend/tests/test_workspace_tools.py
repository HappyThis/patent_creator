from __future__ import annotations

import json
import sys
from pathlib import Path

import app.tools.builtin.filesystem as filesystem_tools
from app.core.command_platform import current_command_platform
from app.tools.builtin.shell import EXEC_COMMAND_DISABLED_ENV

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
    assert invalid_timeout["output"]["code"] == "invalid_tool_arguments"
    assert "timeout" in invalid_timeout["output"]["message"]
    assert invalid_timeout["output"]["retry_hint"] == "请严格按照当前工具的 parameters schema 重新调用。"

    zero_timeout = run_builtin_tool(executor, project_id, "exec_command", {"command": "pwd", "timeout": 0})
    assert zero_timeout["status"] == "failed"
    assert zero_timeout["output"]["code"] == "invalid_tool_arguments"
    assert "timeout" in zero_timeout["output"]["message"]

    extra_field = run_builtin_tool(executor, project_id, "exec_command", {"command": "pwd", "unused": True})
    assert extra_field["status"] == "failed"
    assert extra_field["output"]["code"] == "invalid_tool_arguments"
    assert "unused" in extra_field["output"]["message"]


def test_exec_command_can_be_hard_disabled_by_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    marker = executor.store.project_dir(project_id) / "must-not-exist.txt"
    monkeypatch.setenv(EXEC_COMMAND_DISABLED_ENV, "true")
    script = "from pathlib import Path; Path('must-not-exist.txt').write_text('bad')"
    command = f"{json.dumps(sys.executable)} -c {json.dumps(script)}"
    if current_command_platform().platform == "windows":
        command = f"& {command}"

    result = run_builtin_tool(
        executor,
        project_id,
        "exec_command",
        {"command": command},
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "tool_disabled"
    assert not marker.exists()


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
    assert read_result["output"]["total_lines_is_lower_bound"] is True

    final_read_result = run_builtin_tool(
        executor,
        project_id,
        "file_read",
        {"path": "src/alpha.py", "start_line": 2, "limit": 10},
    )
    assert final_read_result["status"] == "success"
    assert final_read_result["output"]["content"] == "2 |     return 'needle'"
    assert final_read_result["output"]["next_start_line"] is None
    assert final_read_result["output"]["total_lines_is_lower_bound"] is False

    beyond_eof_result = run_builtin_tool(
        executor,
        project_id,
        "file_read",
        {"path": "src/alpha.py", "start_line": 10, "limit": 10},
    )
    assert beyond_eof_result["status"] == "success"
    assert beyond_eof_result["output"]["content"] == ""
    assert beyond_eof_result["output"]["end_line"] == 2
    assert beyond_eof_result["output"]["next_start_line"] is None
    assert beyond_eof_result["output"]["total_lines"] == 2

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


def test_file_tools_reject_relative_paths_outside_project(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    external_file = workspace.parent / "outside.txt"
    external_file.write_text("outside needle\n", encoding="utf-8")

    read_result = run_builtin_tool(executor, project_id, "file_read", {"path": "../outside.txt"})
    assert read_result["status"] == "failed"
    assert read_result["output"]["code"] == "invalid_operation"
    assert "project workspace" in read_result["output"]["message"]

    glob_result = run_builtin_tool(executor, project_id, "file_glob", {"path": "../*.txt"})
    assert glob_result["status"] == "failed"
    assert glob_result["output"]["code"] == "invalid_operation"
    assert "project workspace" in glob_result["output"]["message"]

    absolute_read = run_builtin_tool(executor, project_id, "file_read", {"path": str(external_file)})
    assert absolute_read["status"] == "success"
    assert "outside needle" in absolute_read["output"]["content"]


def test_file_tools_can_restrict_external_absolute_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    internal_file = workspace / "internal.txt"
    internal_file.write_text("internal\n", encoding="utf-8")
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    allowed_file = allowed_dir / "allowed.txt"
    allowed_file.write_text("allowed\n", encoding="utf-8")
    denied_dir = tmp_path / "denied"
    denied_dir.mkdir()
    denied_file = denied_dir / "hidden.txt"
    denied_file.write_text("hidden\n", encoding="utf-8")
    denied_symlink = allowed_dir / "hidden-link.txt"
    denied_symlink.symlink_to(denied_file)
    denied_dir_symlink = allowed_dir / "hidden-dir"
    denied_dir_symlink.symlink_to(denied_dir, target_is_directory=True)
    monkeypatch.setenv("PATENT_CREATOR_AGENT_EXTERNAL_READ_ROOTS", str(allowed_dir))

    internal = run_builtin_tool(executor, project_id, "file_read", {"path": str(internal_file)})
    allowed = run_builtin_tool(executor, project_id, "file_read", {"path": str(allowed_file)})
    denied = run_builtin_tool(executor, project_id, "file_read", {"path": str(denied_file)})
    denied_glob = run_builtin_tool(
        executor,
        project_id,
        "file_glob",
        {"path": str(denied_dir), "pattern": "*.txt"},
    )
    allowed_glob = run_builtin_tool(
        executor,
        project_id,
        "file_glob",
        {"path": str(allowed_dir), "pattern": "*"},
    )
    denied_symlink_search = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"path": str(allowed_dir), "pattern": "hidden"},
    )

    assert internal["status"] == "success"
    assert allowed["status"] == "success"
    assert denied["status"] == "failed"
    assert "outside configured external read roots" in denied["output"]["message"]
    assert denied_glob["status"] == "failed"
    assert allowed_glob["status"] == "success"
    assert allowed_glob["output"]["matches"] == [str(allowed_file)]
    assert denied_symlink_search["status"] == "success"
    assert denied_symlink_search["output"]["matches"] == []


def test_file_tools_reject_arguments_outside_schema(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    (workspace / "sample.txt").write_text("needle\n", encoding="utf-8")

    zero_limit = run_builtin_tool(executor, project_id, "file_glob", {"limit": 0})
    bad_limit = run_builtin_tool(executor, project_id, "file_read", {"path": "sample.txt", "limit": "bad"})
    extra_field = run_builtin_tool(executor, project_id, "file_search", {"pattern": "needle", "unused": True})

    for result in (zero_limit, bad_limit, extra_field):
        assert result["status"] == "failed"
        assert result["output"]["code"] == "invalid_tool_arguments"
        assert result["output"]["retry_hint"] == "请严格按照当前工具的 parameters schema 重新调用。"
    assert "limit" in zero_limit["output"]["message"]
    assert "limit" in bad_limit["output"]["message"]
    assert "unused" in extra_field["output"]["message"]


def test_file_glob_stops_at_internal_scan_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(filesystem_tools, "_DEFAULT_GLOB_SCANNED_PATHS", 3)
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
        {"path": "many", "pattern": "*", "limit": 100},
    )

    assert glob_result["status"] == "success"
    assert glob_result["output"]["stop_reason"] == "scan_budget_exceeded"
    assert glob_result["output"]["truncated"] is True
    assert glob_result["output"]["scanned"] == 3
    assert glob_result["output"]["scan_budget"] == 3
    assert glob_result["output"]["total_is_lower_bound"] is True
    assert glob_result["output"]["next_offset"] is None


def test_file_search_stops_at_internal_scan_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(filesystem_tools, "_DEFAULT_GLOB_SCANNED_PATHS", 3)
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    many_dir = workspace / "many"
    many_dir.mkdir()
    for index in range(10):
        (many_dir / f"{index:02d}.txt").write_text("needle\n", encoding="utf-8")

    search_result = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"path": "many", "pattern": "needle", "limit": 100},
    )

    assert search_result["status"] == "success"
    assert search_result["output"]["stop_reason"] == "scan_budget_exceeded"
    assert search_result["output"]["truncated"] is True
    assert search_result["output"]["scanned"] == 3
    assert search_result["output"]["scan_budget"] == 3
    assert search_result["output"]["returned"] == 3
    assert search_result["output"]["total_is_lower_bound"] is True
    assert search_result["output"]["next_offset"] is None


def test_file_search_files_mode_stops_after_page_is_filled(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    many_dir = workspace / "many"
    many_dir.mkdir()
    for index in range(10):
        (many_dir / f"{index:02d}.txt").write_text("needle\n", encoding="utf-8")

    search_result = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"path": "many", "pattern": "needle", "mode": "files", "limit": 3},
    )

    assert search_result["status"] == "success"
    assert search_result["output"]["stop_reason"] == "limit_reached"
    assert search_result["output"]["truncated"] is True
    assert search_result["output"]["scanned"] == 3
    assert search_result["output"]["returned"] == 3
    assert search_result["output"]["total_is_lower_bound"] is True
    assert search_result["output"]["next_offset"] == 3


def test_file_search_lines_mode_does_not_collect_past_limit(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    log_file = workspace / "many-lines.txt"
    log_file.write_text("\n".join("needle" for _ in range(20)) + "\n", encoding="utf-8")

    search_result = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"path": "many-lines.txt", "pattern": "needle", "limit": 3},
    )

    assert search_result["status"] == "success"
    assert search_result["output"]["stop_reason"] == "limit_reached"
    assert search_result["output"]["truncated"] is True
    assert search_result["output"]["returned"] == 3
    assert search_result["output"]["total"] == 3
    assert search_result["output"]["total_line_matches"] == 3
    assert search_result["output"]["total_is_lower_bound"] is True


def test_file_search_context_lines_mode_does_not_collect_past_limit(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    log_file = workspace / "many-lines.txt"
    log_file.write_text("\n".join(f"line {index} needle" for index in range(20)) + "\n", encoding="utf-8")

    search_result = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"path": "many-lines.txt", "pattern": "needle", "context_lines": 1, "limit": 3},
    )

    assert search_result["status"] == "success"
    assert search_result["output"]["stop_reason"] == "limit_reached"
    assert search_result["output"]["truncated"] is True
    assert search_result["output"]["returned"] == 3
    assert search_result["output"]["total_line_matches"] == 3
    assert [match["line"] for match in search_result["output"]["matches"]] == [1, 2, 3]
    assert search_result["output"]["matches"][0]["context"] == [
        {"line": 1, "text": "line 0 needle"},
        {"line": 2, "text": "line 1 needle"},
    ]


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


def test_file_search_skips_heavy_directories_during_scan(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "visible.py").write_text("needle\n", encoding="utf-8")
    vendor_dir = workspace / "node_modules"
    vendor_dir.mkdir()
    (vendor_dir / "hidden.py").write_text("needle\n", encoding="utf-8")

    search_result = run_builtin_tool(executor, project_id, "file_search", {"pattern": "needle", "limit": 100})

    assert search_result["status"] == "success"
    assert [match["path"] for match in search_result["output"]["matches"]] == ["src/visible.py"]
    assert "node_modules" in search_result["output"]["skipped_dirs"]


def test_file_search_double_star_include_glob_matches_root_files(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    (workspace / "root.py").write_text("needle\n", encoding="utf-8")
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "nested.py").write_text("needle\n", encoding="utf-8")

    search_result = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"pattern": "needle", "include_glob": "**/*.py", "limit": 100},
    )

    assert search_result["status"] == "success"
    assert [match["path"] for match in search_result["output"]["matches"]] == ["root.py", "src/nested.py"]


def test_file_search_include_glob_supports_brace_expansion(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "a.ts").write_text("needle\n", encoding="utf-8")
    (source_dir / "b.tsx").write_text("needle\n", encoding="utf-8")
    (source_dir / "c.js").write_text("needle\n", encoding="utf-8")
    (source_dir / "d.md").write_text("needle\n", encoding="utf-8")
    (source_dir / "e.py").write_text("needle\n", encoding="utf-8")

    search_result = run_builtin_tool(
        executor,
        project_id,
        "file_search",
        {"pattern": "needle", "include_glob": "**/*.{ts,tsx,js,md}", "limit": 100},
    )

    assert search_result["status"] == "success"
    assert [match["path"] for match in search_result["output"]["matches"]] == [
        "src/a.ts",
        "src/b.tsx",
        "src/c.js",
        "src/d.md",
    ]
    assert search_result["output"]["scanned"] == 4


def test_file_glob_pattern_supports_brace_expansion(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    workspace = executor.store.project_dir(project_id)
    source_dir = workspace / "src"
    nested_dir = source_dir / "nested"
    nested_dir.mkdir(parents=True)
    (source_dir / "a.ts").write_text("ok", encoding="utf-8")
    (source_dir / "b.tsx").write_text("ok", encoding="utf-8")
    (nested_dir / "c.md").write_text("ok", encoding="utf-8")
    (source_dir / "d.py").write_text("ok", encoding="utf-8")

    glob_result = run_builtin_tool(
        executor,
        project_id,
        "file_glob",
        {"pattern": "src/**/*.{ts,md}", "limit": 100},
    )

    assert glob_result["status"] == "success"
    assert glob_result["output"]["matches"] == ["src/a.ts", "src/nested/c.md"]
