from __future__ import annotations

import subprocess
from typing import Any

from ....domain.document_tools import tool_failed, tool_success
from ....storage.workspace_store import WorkspaceStore
from ..registry import can_use_tool
from ..types import AgentScope


def exec_command(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    scope: AgentScope,
) -> dict[str, Any]:
    if not can_use_tool(scope, "exec_command"):
        return tool_failed("permission_denied", "当前调用方不允许执行命令。")
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return tool_failed("invalid_operation", "command 字段缺失。")

    timeout = float(arguments.get("timeout", 30))
    try:
        completed = subprocess.run(
            command,
            cwd=store.project_dir(project_id),
            capture_output=True,
            shell=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return tool_failed(
            "command_timeout",
            f"命令执行超时：{timeout} 秒。",
            command=command,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        )
    except OSError as exc:
        return tool_failed("command_execution_failed", f"命令执行失败：{exc}", command=command)
    return tool_success(
        {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
