from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...agents.tool_metadata import agent_tool
from ...domain.document_tools import tool_failed, tool_success


def invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
    return tool_failed("invalid_tool_arguments_json", message)


class WritePipeArguments(BaseModel):
    content: str = Field(description="要追加到 pipe 的 Markdown 或纯文本内容。避免一次写入巨大内容。")


class FinishArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(slots=True)
class SubagentPipe:
    parts: list[str] = field(default_factory=list)

    @agent_tool(
        name="write_pipe",
        args_model=WritePipeArguments,
    )
    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把一小段需要展示给主 agent 的内容写入本次子 agent 内存管道。

        Returns:
            成功时返回当前 pipe 状态；content 非字符串时返回 invalid_pipe_content。

        Rules:
            - 所有要展示给主 agent 的业务内容都先写入 pipe。
            - 鼓励少量多次，避免一次写入巨大内容。
            - content 使用 Markdown 或纯文本，不要使用复杂嵌套 JSON。

        Examples:
            - 写入一小段结果: {"content":"## 局部候选正文\\n\\n这里写入一小段要交给主 agent 的内容。"}
        """
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

    @agent_tool(
        name="finish",
        args_model=FinishArguments,
    )
    def finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """结束当前子 agent run。finish 不承载任何业务内容。

        Returns:
            成功时返回完成状态和最终 pipe 内容；携带任何参数时返回 invalid_finish_arguments。

        Rules:
            - 工作完成后调用；arguments 必须是空对象。

        Examples:
            - 结束本次 run: {}
        """
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
