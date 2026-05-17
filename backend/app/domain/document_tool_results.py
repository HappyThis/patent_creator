from __future__ import annotations

from typing import Any

ToolResult = dict[str, Any]


def tool_success(output: dict[str, Any]) -> ToolResult:
    return {"status": "success", "output": output}


def tool_failed(code: str, message: str, **extra: Any) -> ToolResult:
    return {"status": "failed", "output": {"code": code, "message": message, **extra}}
