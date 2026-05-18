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
    - 未脚本化子 agent 时，默认通过 write_pipe + finish 提交 section_writer 结果。
    """

    def __init__(
        self,
        script: list[Callable[[list[dict[str, Any]]], dict[str, Any]]],
        *,
        script_subagents: bool = False,
    ) -> None:
        self._script = list(script)
        self._cursor = 0
        self._script_subagents = script_subagents
        self.generated_text_prompts: list[dict[str, Any]] = []

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        response_format_json: bool = False,
    ) -> dict[str, Any]:
        if "子 agent：" in system_prompt and not self._script_subagents:
            task_content = str(messages[-1].get("content") or "")
            target_section_id = "sec_000010" if "技术效果" in task_content else "sec_000007"
            arguments = {"content": f"目标章节：{target_section_id}\n\n正文占位。"}
            return {
                "type": "tool_calls",
                "tool_calls": [
                    tool_call("write_pipe", arguments, "sub_write_1"),
                    tool_call("finish", {}, "sub_finish_1"),
                ],
                "assistant_message": self._build_assistant_message(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            tool_call("write_pipe", arguments, "sub_write_1"),
                            tool_call("finish", {}, "sub_finish_1"),
                        ],
                    }
                ),
            }
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
            return {
                "role": "assistant",
                "content": str(result.get("text") or ""),
                "reasoning_content": "测试推理内容。",
            }
        if result.get("type") in {"tool_call", "tool_calls"}:
            raw_calls = result.get("tool_calls")
            if not isinstance(raw_calls, list):
                raw_calls = [result]
            return {
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
        return {"role": "assistant", "content": ""}

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
    ) -> str:
        if "上下文压缩 agent" in system_prompt:
            marker = "待压缩上下文："
            if marker in user_prompt:
                context = json.loads(user_prompt.split(marker, 1)[1].strip())
            else:
                context = json.loads(user_prompt[user_prompt.index("{") :])
            context["_timeout"] = timeout
            self.generated_text_prompts.append(context)
            message_count = len(context.get("compressible_messages") or [])
            return (
                "## 已确认事实\n\n"
                f"- 已压缩 {message_count} 条历史消息相关的信息。\n\n"
                "## 当前进展\n\n"
                "- 当前任务继续沿用压缩前的上下文。\n\n"
                "## 后续注意\n\n"
                "- 后续如信息不足，应重新读取必要上下文。"
            )
        return "## 已确认事实\n\n- 压缩后的历史。\n\n## 当前进展\n\n- 暂无。\n\n## 后续注意\n\n- 暂无。"


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
