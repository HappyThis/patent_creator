from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings
from app.runtime import ExecutorEngine
from app.runtime.context.compression import COMPRESSED_MEMORY_PREFIX
from app.services import AppServices
from app.storage.workspace_store import WorkspaceStore


class ScriptedLLMClient:
    """驱动主 agent loop 的脚本化 stub。

    - generate_with_tools_stream 按外部提供的 script 顺序返回 action。
    """

    def __init__(
        self,
        script: list[Callable[[list[dict[str, Any]]], dict[str, Any]]],
        *,
        assessment_json: list[dict[str, Any]] | None = None,
        checker_json: list[dict[str, Any]] | None = None,
        summary_json: list[dict[str, Any]] | None = None,
    ) -> None:
        self._script = list(script)
        self._cursor = 0
        self._assessment_json = list(assessment_json or [])
        self._assessment_cursor = 0
        self._checker_json = list(checker_json or [])
        self._checker_cursor = 0
        self._summary_json = list(summary_json or [])
        self._summary_cursor = 0
        self.assessment_prompts: list[dict[str, Any]] = []
        self.checker_prompts: list[dict[str, Any]] = []
        self.summary_prompts: list[dict[str, Any]] = []
        self.generated_text_prompts: list[dict[str, Any]] = []

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        on_audit_event: Any = None,
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

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt_record = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "temperature": temperature,
            "timeout": timeout,
            "trace_context": trace_context,
        }
        if "变更影响评估器" in system_prompt:
            self.assessment_prompts.append(prompt_record)
            if self._assessment_cursor >= len(self._assessment_json):
                return {"should_review": False, "reason": "默认不进入增强。"}
            payload = self._assessment_json[self._assessment_cursor]
            self._assessment_cursor += 1
            return payload
        if "技术方案”章节的质量检查器" in system_prompt:
            self.checker_prompts.append(prompt_record)
            if self._checker_cursor >= len(self._checker_json):
                return {"title": "低算力实时保护"}
            payload = self._checker_json[self._checker_cursor]
            self._checker_cursor += 1
            return payload
        if "增强总结器" in system_prompt:
            self.summary_prompts.append(prompt_record)
            if self._summary_cursor >= len(self._summary_json):
                return {"applied_summary": "已完成技术方案增强。"}
            payload = self._summary_json[self._summary_cursor]
            self._summary_cursor += 1
            return payload
        return {"title": "低算力实时保护"}

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> str:
        if "上下文滚动压缩" in user_prompt:
            prompt_messages = list(messages or [])
            context = {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "messages": prompt_messages,
                "_timeout": timeout,
            }
            self.generated_text_prompts.append(context)
            previous = next(
                (
                    str(message.get("content") or "")
                    for message in prompt_messages
                    if isinstance(message, dict)
                    and message.get("role") == "user"
                    and str(message.get("content") or "").startswith(COMPRESSED_MEMORY_PREFIX)
                ),
                "",
            )
            message_count = len(prompt_messages)
            last_user = next(
                (
                    str(message.get("content") or "")
                    for message in reversed(prompt_messages)
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
        log_dir=tmp_path / "logs",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_api_key="test-key",
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


def make_tool_executor(tmp_path: Path, title: str = "一种图像检测方法") -> tuple[ExecutorEngine, str]:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project(title)
    return ExecutorEngine(store), project.project_id


def section_id_by_title(executor: ExecutorEngine, project_id: str, title: str) -> str:
    disclosure = executor.store.get_disclosure(project_id)
    section = next(section for section in disclosure["sections"] if section["title"]["text"] == title)
    return section["id"]


def run_builtin_tool(
    executor: ExecutorEngine,
    project_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return asyncio.run(executor.execute_tool(project_id, tool_name, arguments))
