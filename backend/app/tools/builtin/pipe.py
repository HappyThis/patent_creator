from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...domain.document_tool_results import tool_failed, tool_success
from ..metadata import agent_tool

MAX_PIPE_WRITES = 10
MAX_PIPE_TOTAL_CHARS = 4000


def invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
    return tool_failed("invalid_tool_arguments_json", message)


class WritePipeArguments(BaseModel):
    content: str = Field(
        description=(
            "要追加到 pipe 的 Markdown 或纯文本内容。pipe 是交付给主 agent 的唯一内容通道；"
            "每个子 agent run 最多调用 write_pipe 10 次，累计最多写入 4000 字，不限制单次字符数。"
        )
    )


class FinishArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(slots=True)
class SubagentPipe:
    parts: list[str] = field(default_factory=list)
    max_writes: int = MAX_PIPE_WRITES
    max_total_chars: int = MAX_PIPE_TOTAL_CHARS
    auto_finished: bool = False
    auto_finish_reason: str | None = None

    @agent_tool(
        name="write_pipe",
        args_model=WritePipeArguments,
    )
    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把一小段需要展示给主 agent 的内容写入本次子 agent 内存管道。

        Returns:
            成功时返回当前 pipe 状态、剩余写入次数和剩余字符数；content 非字符串时返回 invalid_pipe_content；超过累计字符预算时返回 pipe_budget_exceeded。

        Rules:
            - pipe 内容会按写入顺序拼接后进入主 agent 上下文；所有要展示给主 agent 的业务内容都先写入 pipe。
            - 形成一段可独立阅读的结论、候选正文、问题清单、风险或待确认点时写入 pipe；不要写入过程性思考、重复内容或工具调用计划。
            - 每个子 agent run 最多调用 write_pipe 10 次，累计最多写入 4000 字，不限制单次字符数；开始写入前先规划额度，把最关键内容优先写入。
            - content 使用 Markdown 或纯文本，不要使用复杂嵌套 JSON。
            - 每次写入后根据 remaining_writes 和 remaining_chars 判断是否还需要继续；本次 goal 的最小完整结果已经交付时立即调用 finish。
            - 达到最大写入次数或累计字符数后，执行器会自动结束本次子任务。

        Examples:
            - 写入一小段结果: {"content":"## 局部候选正文\\n\\n这里写入一小段要交给主 agent 的内容。"}
        """
        if self.auto_finished:
            return tool_failed(
                "pipe_already_finished",
                "pipe 已经结束，不能继续写入。",
                **self._budget_state(),
                auto_finished=True,
                reason=self.auto_finish_reason,
            )
        content = payload.get("content")
        if not isinstance(content, str):
            return tool_failed("invalid_pipe_content", "write_pipe.content 必须是字符串。")

        if len(self.parts) >= self.max_writes:
            self._mark_auto_finished("pipe_budget_exhausted")
            return tool_failed(
                "pipe_budget_exhausted",
                "pipe 写入次数已达到上限，不能继续写入。",
                **self._budget_state(),
                auto_finished=True,
                reason=self.auto_finish_reason,
            )

        next_total = self.total_chars + len(content)
        if next_total > self.max_total_chars:
            return tool_failed(
                "pipe_budget_exceeded",
                "本次写入会超过 pipe 累计字符数限制，请压缩内容后重写。",
                **self._budget_state(),
                attempted_chars=len(content),
            )

        self.parts.append(content)
        if self.remaining_writes == 0 or self.remaining_chars == 0:
            self._mark_auto_finished("pipe_budget_exhausted")
        return tool_success(
            {
                "status": "ok",
                "part_index": len(self.parts),
                "written_chars": len(content),
                **self._budget_state(),
                "should_finish": self.auto_finished,
                "auto_finished": self.auto_finished,
                "reason": self.auto_finish_reason,
                "stored_preview": _preview(content),
                "next": (
                    "pipe 额度已耗尽，执行器将自动结束本次子任务。"
                    if self.auto_finished
                    else "如果本次 goal 的最小完整结果已经交付，请调用 finish({})；否则只继续写入必要内容。"
                ),
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
            - finish 不承载业务内容；调用前先把要交给主 agent 的内容写入 pipe。
            - 已经交付本次 goal 的最小完整结果、信息不足但已说明缺口，或继续分析只能带来局部增益时，立即调用 finish。
            - 如果 write_pipe 返回 should_finish=true，执行器会自动结束本次子任务，不需要再补充业务内容。

        Examples:
            - 结束本次 run: {}
        """
        if payload:
            return tool_failed("invalid_finish_arguments", "finish 不接收任何参数，必须调用 finish({})。")
        return tool_success(
            {
                "status": "done",
                "parts": len(self.parts),
                **self._budget_state(),
            }
        )

    @property
    def total_chars(self) -> int:
        return sum(len(part) for part in self.parts)

    @property
    def remaining_writes(self) -> int:
        return max(0, self.max_writes - len(self.parts))

    @property
    def remaining_chars(self) -> int:
        return max(0, self.max_total_chars - self.total_chars)

    def content(self) -> str:
        return "\n".join(self.parts)

    def _budget_state(self) -> dict[str, int]:
        return {
            "parts": len(self.parts),
            "total_chars": self.total_chars,
            "remaining_writes": self.remaining_writes,
            "remaining_chars": self.remaining_chars,
            "max_writes": self.max_writes,
            "max_total_chars": self.max_total_chars,
        }

    def _mark_auto_finished(self, reason: str) -> None:
        self.auto_finished = True
        self.auto_finish_reason = reason


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
