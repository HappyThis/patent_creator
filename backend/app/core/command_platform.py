from __future__ import annotations

import locale
import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandPlatform:
    platform: str
    shell: str
    executable: str
    command_prefix: tuple[str, ...]
    prelude: str | None
    examples: tuple[str, ...]


def current_command_platform() -> CommandPlatform:
    if os.name == "nt":
        executable = shutil.which("powershell.exe") or "powershell.exe"
        return CommandPlatform(
            platform="windows",
            shell="powershell",
            executable=executable,
            command_prefix=(
                executable,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
            ),
            prelude=(
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)"
            ),
            examples=(
                "Get-ChildItem -Force",
                "Get-ChildItem -Recurse -Filter *.ts",
                "Get-Content -Raw -Encoding UTF8 .\\src\\file.ts",
                "Select-String -Path .\\src\\*.ts -Pattern \"OpenCode\"",
                "python -c \"print('ok')\"",
            ),
        )

    executable = shutil.which("sh") or "/bin/sh"
    return CommandPlatform(
        platform="posix",
        shell="sh",
        executable=executable,
        command_prefix=(executable, "-c"),
        prelude=None,
        examples=(
            "ls -la",
            "find . -name '*.ts'",
            "cat ./src/file.ts",
            "grep -R \"OpenCode\" ./src",
            "python -c \"print('ok')\"",
        ),
    )


def command_arguments(command: str, profile: CommandPlatform | None = None) -> list[str]:
    active_profile = profile or current_command_platform()
    shell_command = command
    if active_profile.prelude:
        shell_command = f"{active_profile.prelude}; {command}"
    return [*active_profile.command_prefix, shell_command]


def decode_command_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data

    encodings = ["utf-8", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.extend(["gbk", "cp936"])

    seen: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode(encodings[0], errors="replace")


def exec_command_tool_description() -> str:
    profile = current_command_platform()
    examples = "；".join(profile.examples)
    return (
        f"在项目工作区内执行命令字符串，cwd 为当前 project 工作区。"
        f"当前运行平台是 {profile.platform}，命令会通过 {profile.shell} 执行。"
        f"示例：{examples}。"
    )


def exec_command_prompt_examples() -> str:
    profile = current_command_platform()
    examples = "\n".join(f"  {example}" for example in profile.examples)
    return (
        f"当前 exec_command 运行平台：{profile.platform}；shell：{profile.shell}。\n"
        f"命令示例：\n{examples}"
    )
