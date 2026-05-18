from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...domain.document_tools import tool_failed, tool_success


def invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
    return tool_failed("invalid_tool_arguments_json", message)


@dataclass(slots=True)
class SubagentPipe:
    parts: list[str] = field(default_factory=list)

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, str):
            return tool_failed("invalid_pipe_content", "write_pipe.content 必须是字符串。")

        self.parts.append(content)
        return tool_success(
            {
                "status": "ok",
                "part_index": len(self.parts),
                "written_chars": len(content),
                "total_chars": self.total_chars,
                "stored_preview": _preview(content),
                "next": "如果还有内容，继续调用 write_pipe；如果已经完成，调用 finish({})。",
            }
        )

    def finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            return tool_failed("invalid_finish_arguments", "finish 不接收任何参数，必须调用 finish({})。")
        return tool_success(
            {
                "status": "done",
                "parts": len(self.parts),
                "total_chars": self.total_chars,
            }
        )

    @property
    def total_chars(self) -> int:
        return sum(len(part) for part in self.parts)

    def content(self) -> str:
        return "\n".join(self.parts)


def subagent_tool_summary(agent_id: str, tool: str, result: dict[str, Any]) -> str:
    if tool == "write_pipe":
        if result.get("status") == "success":
            output = result.get("output") if isinstance(result.get("output"), dict) else {}
            return f"{agent_id} 写入 pipe #{output.get('part_index', '?')}"
        return f"{agent_id} 写入 pipe 失败"
    if tool == "finish":
        if result.get("status") == "success":
            return f"{agent_id} 结束子任务"
        return f"{agent_id} 结束子任务失败"
    if result.get("status") == "failed":
        return f"{agent_id} 执行 {tool} 失败"
    return f"{agent_id} 完成 {tool}"


def _preview(content: str, limit: int = 200) -> str:
    if len(content) <= limit:
        return content
    return f"{content[:limit]}..."
