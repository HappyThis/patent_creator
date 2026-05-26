from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings
from app.services import AppServices


class ScriptedLLMClient:
    """驱动主 agent loop 的脚本化 stub。

    - generate_with_tools_stream 按外部提供的 script 顺序返回 action。
    """

    def __init__(
        self,
        script: list[Callable[[list[dict[str, Any]]], dict[str, Any]]],
    ) -> None:
        self._script = list(script)
        self._cursor = 0
        self.generated_text_prompts: list[dict[str, Any]] = []

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        response_format_json: bool = False,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._cursor >= len(self._script):
            raise AssertionError("ScriptedLLMClient script exhausted")
        step = self._script[self._cursor]
        self._cursor += 1
        result = step(messages)
        if result.get("type") == "respond" and on_text_delta is not None:
            await on_text_delta(str(result.get("text") or ""))
        if "assistant_message" not in result:
            result = {**result, "assistant_message": self._build_assistant_message(result)}
        return result

    @staticmethod
    def _build_assistant_message(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("type") == "respond":
            message = {
                "role": "assistant",
                "content": str(result.get("text") or ""),
                "reasoning_content": "测试推理内容。",
            }
            if "usage" in result:
                message["usage"] = result["usage"]
            return message
        if result.get("type") in {"tool_call", "tool_calls"}:
            raw_calls = result.get("tool_calls")
            if not isinstance(raw_calls, list):
                raw_calls = [result]
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "测试工具调用推理内容。",
                "tool_calls": [
                    {
                        "id": str(call.get("tool_call_id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("tool") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for call in raw_calls
                ],
            }
            if "usage" in result:
                message["usage"] = result["usage"]
            return message
        return {"role": "assistant", "content": ""}

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> str:
        if "上下文" in system_prompt and "压缩" in system_prompt:
            context = json.loads(user_prompt[user_prompt.index("{") :])
            context["_timeout"] = timeout
            self.generated_text_prompts.append(context)
            previous = str((context.get("previous_compressed_markdown") or {}).get("content") or "")
            messages_to_merge = context.get("messages_to_merge") or []
            message_count = len(messages_to_merge)
            last_user = next(
                (
                    str(message.get("content") or "")
                    for message in reversed(messages_to_merge)
                    if isinstance(message, dict) and message.get("role") == "user"
                ),
                "",
            )
            return (
                "<analysis>\n"
                f"需要把上一轮摘要和 {message_count} 条新增消息合并成专利写作状态。\n"
                "</analysis>\n"
                "<summary>\n"
                "## 当前任务\n\n"
                "- 继续沿用压缩前的用户要求。\n\n"
                "## 执行进度\n\n"
                f"- 已滚动压缩 {message_count} 条新增消息；最新用户输入：{last_user or '暂无'}。\n\n"
                "## 已完成事项\n\n"
                "- 当前任务继续沿用压缩前的上下文。\n\n"
                "## 关键事实与证据\n\n"
                f"- 上一轮摘要长度：{len(previous)}。\n\n"
                "## 待办与下一步\n\n"
                "- 后续如信息不足，应重新读取必要上下文。\n"
                "\n## 风险与约束\n\n"
                "- 不做工具结果轻量化/投影。\n"
                "</summary>"
            )
        return (
            "<analysis>暂无。</analysis>\n"
            "<summary>\n"
            "## 当前任务\n\n- 暂无。\n\n"
            "## 执行进度\n\n- 暂无。\n\n"
            "## 已完成事项\n\n- 暂无。\n\n"
            "## 关键事实与证据\n\n- 压缩后的历史。\n\n"
            "## 待办与下一步\n\n- 暂无。\n\n"
            "## 风险与约束\n\n- 暂无。\n"
            "</summary>"
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
        round_step_delay=0.0,
        round_finish_delay=0.0,
    )


async def wait_until_idle(services: AppServices, project_id: str, timeout: float = 2.0) -> None:
    elapsed = 0.0
    step = 0.01
    while elapsed < timeout:
        if not services.store.get_project(project_id).is_busy:
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError("round did not finish in time")


async def create_project(services: AppServices, title: str = "测试项目") -> str:
    project = services.store.create_project(title)
    return project.project_id


def tool_call(tool: str, arguments: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments, "tool_call_id": tool_call_id}
