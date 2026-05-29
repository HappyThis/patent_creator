from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from ...core.command_platform import command_arguments, current_command_platform, decode_command_output
from ...domain.document_tool_results import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool
from ..output_storage import EXEC_COMMAND_INLINE_LIMIT_CHARS, EXEC_COMMAND_PREVIEW_CHARS, head_tail_preview, write_tool_output


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
) -> dict[str, Any]:
    """在项目工作区内执行命令字符串，cwd 为当前 project 工作区。

    Returns:
        返回 exit_code、stdout 和 stderr；stdout/stderr 过长时只返回 preview，并提供完整输出的 runtime 文件路径。

    Rules:
        - 当前运行平台和 shell 会在工具结果中返回。
        - 查找文件优先用 file_glob；搜索代码优先用 file_search；读取文件优先用 file_read。
        - stdout/stderr 不保证完整；当 *_truncated 为 true 时，需要用 file_read 读取 *_path。
        - 命令超时时返回 command_timeout。

    Examples:
        - 执行诊断命令: {"command":"git status --short","timeout":30}
    """
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
            **_stream_output_fields(
                store,
                project_id,
                command=command,
                name="stdout",
                text=decode_command_output(exc.stdout),
            ),
            **_stream_output_fields(
                store,
                project_id,
                command=command,
                name="stderr",
                text=decode_command_output(exc.stderr),
            ),
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
            **_stream_output_fields(
                store,
                project_id,
                command=command,
                name="stdout",
                text=decode_command_output(completed.stdout),
            ),
            **_stream_output_fields(
                store,
                project_id,
                command=command,
                name="stderr",
                text=decode_command_output(completed.stderr),
            ),
        }
    )


def _stream_output_fields(
    store: WorkspaceStore,
    project_id: str,
    *,
    command: str,
    name: str,
    text: str,
) -> dict[str, Any]:
    chars = len(text)
    fields: dict[str, Any] = {
        name: text,
        f"{name}_chars": chars,
        f"{name}_truncated": False,
        f"{name}_path": None,
    }
    if chars <= EXEC_COMMAND_INLINE_LIMIT_CHARS:
        return fields

    fields[name] = head_tail_preview(text, EXEC_COMMAND_PREVIEW_CHARS)
    fields[f"{name}_truncated"] = True
    fields[f"{name}_path"] = write_tool_output(
        store,
        project_id,
        text,
        stem=f"exec_command_{name}",
        suffix=".txt",
    )
    fields["preview_policy"] = "head_tail"
    fields["preview_hint"] = (
        f"{name} 已截断；完整输出已保存到 {fields[f'{name}_path']}，"
        "如需查看请调用 file_read 读取该路径的片段。"
    )
    return fields
