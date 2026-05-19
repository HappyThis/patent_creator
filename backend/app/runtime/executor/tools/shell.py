from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from ....agents.tool_metadata import agent_tool
from ....core.command_platform import command_arguments, current_command_platform, decode_command_output
from ....domain.document_tools import tool_failed, tool_success
from ....storage.workspace_store import WorkspaceStore
from ..registry import can_use_tool
from ..types import AgentScope


class ExecCommandArguments(BaseModel):
    command: str = Field(description="要执行的命令字符串，按当前项目工作区作为 cwd 执行。")
    timeout: float = Field(default=30, gt=0, description="超时时间，单位秒，默认 30，必须大于 0。")


@agent_tool(
    args_model=ExecCommandArguments,
)
def exec_command(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    scope: AgentScope,
) -> dict[str, Any]:
    """在项目工作区内执行命令字符串，cwd 为当前 project 工作区。

    Returns:
        返回 exit_code、stdout 和 stderr；命令失败时根据这些字段继续判断下一步。

    Rules:
        - 当前运行平台和 shell 会在工具结果中返回。
        - 命令超时时返回 command_timeout。

    Examples:
        - 执行诊断命令: {"command":"ls -la","timeout":30}
    """
    if not can_use_tool(scope, "exec_command"):
        return tool_failed("permission_denied", "当前调用方不允许执行命令。")
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return tool_failed("invalid_operation", "command 字段缺失。")

    raw_timeout = arguments.get("timeout", 30)
    if raw_timeout is None:
        raw_timeout = 30
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return tool_failed("invalid_operation", "timeout 必须是数字。")
    if timeout <= 0:
        return tool_failed("invalid_operation", "timeout 必须大于 0。")
    profile = current_command_platform()
    try:
        completed = subprocess.run(
            command_arguments(command, profile),
            cwd=store.project_dir(project_id),
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return tool_failed(
            "command_timeout",
            f"命令执行超时：{timeout} 秒。",
            command=command,
            platform=profile.platform,
            shell=profile.shell,
            stdout=decode_command_output(exc.stdout),
            stderr=decode_command_output(exc.stderr),
        )
    except OSError as exc:
        return tool_failed(
            "command_execution_failed",
            f"命令执行失败：{exc}",
            command=command,
            platform=profile.platform,
            shell=profile.shell,
        )
    return tool_success(
        {
            "command": command,
            "platform": profile.platform,
            "shell": profile.shell,
            "exit_code": completed.returncode,
            "stdout": decode_command_output(completed.stdout),
            "stderr": decode_command_output(completed.stderr),
        }
    )
