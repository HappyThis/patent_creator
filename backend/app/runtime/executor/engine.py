from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from ...agents import get_subagent
from ...agents.prompts import (
    build_consistency_reviewer_system_prompt,
    build_material_analyst_system_prompt,
    build_section_writer_system_prompt,
    build_solution_refiner_system_prompt,
)
from ...agents.runtime.openai_compat import OpenAICompatibleClient
from ...agents.workers import (
    MainAgentToolCall,
)
from ...core import ApiError, Settings
from ...core.command_platform import exec_command_tool_description
from ...domain.document_tools import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..context import ContextManager
from ..context.barrier import render_barrier_message
from ..context.compression import (
    build_compression_payload,
    prepare_compressed_messages_with_warnings,
    restore_compressed_messages_from_messages,
)
from ..context.messages import closed_message_prefix
from ..context.prompts import context_compressor_system_prompt
from ..context.usage import estimate_messages_tokens, usage_for_messages
from .registry import can_use_tool
from .subagent_pipe import SubagentPipe, invalid_tool_arguments_json_result, subagent_tool_summary
from .tools.document import document_edit, document_read
from .tools.shell import exec_command
from .types import AgentScope

ToolEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger("patent_creator.executor")

SUBAGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "document_read",
            "description": "读取当前交底书的元信息、目录、章节、block 或搜索文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get_meta", "get_project_context", "get_outline", "get_section", "get_block", "search_blocks"],
                    },
                    "section_id": {"type": "string"},
                    "block_id": {"type": "string"},
                    "query": {"type": "string"},
                    "include_children": {"type": "boolean"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": exec_command_tool_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_pipe",
            "description": "把一小段需要展示给主 agent 的内容写入本次子 agent 内存管道。可以少量多次调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要追加到 pipe 的 Markdown 或纯文本内容。避免一次写入巨大内容。",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "结束当前子 agent run。finish 不承载任何业务内容；业务内容必须先通过 write_pipe 写入。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]

class ExecutorEngine:
    def __init__(
        self,
        store: WorkspaceStore,
        context_manager: ContextManager,
        llm_client: OpenAICompatibleClient,
        settings: Settings,
    ) -> None:
        self.store = store
        self.context_manager = context_manager
        self.llm_client = llm_client
        self.settings = settings

    def document_read(self, project_id: str, arguments: dict[str, Any], scope: AgentScope = "main_agent") -> dict[str, Any]:
        return document_read(self.store, project_id, arguments, scope)

    def document_edit(self, project_id: str, arguments: dict[str, Any], scope: AgentScope = "main_agent") -> dict[str, Any]:
        return document_edit(self.store, project_id, arguments, scope)

    def exec_command(self, project_id: str, arguments: dict[str, Any], scope: AgentScope = "main_agent") -> dict[str, Any]:
        return exec_command(self.store, project_id, arguments, scope)

    async def execute_subagent(
        self,
        project_id: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
        round_id: str | None = None,
        message_id: str | None = None,
        parent_call_id: str | None = None,
        scope: AgentScope = "main_agent",
        on_tool_event: ToolEventSink | None = None,
        caller_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not can_use_tool(scope, "execute_subagent"):
            return tool_failed("permission_denied", "子 agent 不允许调用 execute_subagent。")

        try:
            declaration = get_subagent(str(arguments.get("agent_id")))
        except ApiError as exc:
            return tool_failed(exc.code, exc.message)

        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return tool_failed("missing_goal", "execute_subagent.goal 不能为空。")

        try:
            inherited_messages = closed_message_prefix(caller_messages or [])
            task_message = render_barrier_message({"kind": "agent_task", "task": goal})
        except (ApiError, ValueError) as exc:
            message = exc.message if isinstance(exc, ApiError) else str(exc)
            code = exc.code if isinstance(exc, ApiError) else "subagent_task_error"
            return tool_failed(code, message)

        try:
            result = await self._run_subagent_loop(
                project_id=project_id,
                agent_id=declaration.id,
                goal=goal,
                initial_messages=[*inherited_messages, task_message],
                session_id=session_id,
                round_id=round_id,
                message_id=message_id,
                parent_call_id=parent_call_id,
                on_tool_event=on_tool_event,
            )
        except ApiError as exc:
            logger.warning("subagent failed agent=%s code=%s message=%s", declaration.id, exc.code, exc.message)
            return tool_failed(exc.code, exc.message)
        except Exception as exc:
            logger.exception("subagent runtime error agent=%s", declaration.id)
            return tool_failed("subagent_runtime_error", f"{declaration.id} 执行失败：{exc}")
        return tool_success(
            {
                "agent_id": declaration.id,
                "content": result.get("content", ""),
            }
        )

    async def _run_subagent_loop(
        self,
        *,
        project_id: str,
        agent_id: str,
        goal: str,
        initial_messages: list[dict[str, Any]],
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: ToolEventSink | None,
    ) -> dict[str, Any]:
        system_prompt = self._subagent_system_prompt(agent_id)
        messages: list[dict[str, Any]] = [dict(message) for message in initial_messages]
        max_steps = max(1, self.settings.subagent_max_steps)
        pipe = SubagentPipe()

        for _step in range(max_steps):
            messages = await self._prepare_subagent_run_messages(
                project_id=project_id,
                agent_id=agent_id,
                goal=goal,
                messages=messages,
                session_id=session_id,
                round_id=round_id,
                message_id=message_id,
                parent_call_id=parent_call_id,
            )
            action = await self.llm_client.generate_with_tools_stream(
                system_prompt=system_prompt,
                messages=messages,
                tools=SUBAGENT_TOOLS,
                on_text_delta=None,
            )
            assistant_message = action.get("assistant_message")
            if not isinstance(assistant_message, dict):
                raise ApiError(502, "subagent_invalid_action", f"{agent_id} 缺少 assistant_message。")
            messages.append(assistant_message)

            if action.get("type") == "respond":
                raise ApiError(
                    502,
                    "subagent_plain_response",
                    f"{agent_id} 直接回复文本，未按协议调用 write_pipe 和 finish。",
                )

            if action.get("type") != "tool_calls":
                raise ApiError(502, "subagent_invalid_action", f"{agent_id} 返回未知动作：{action.get('type')}")

            raw_calls = action.get("tool_calls")
            if not isinstance(raw_calls, list) or not raw_calls:
                raise ApiError(502, "subagent_invalid_action", f"{agent_id} tool_calls 为空。")
            for raw_call in raw_calls:
                tool_call = MainAgentToolCall(
                    tool=str(raw_call.get("tool") or ""),
                    arguments=raw_call.get("arguments") if isinstance(raw_call.get("arguments"), dict) else {},
                    tool_call_id=str(raw_call.get("tool_call_id") or ""),
                    arguments_error=(
                        raw_call.get("arguments_error") if isinstance(raw_call.get("arguments_error"), str) else None
                    ),
                )
                result = await self._execute_subagent_tool(
                    project_id=project_id,
                    agent_id=agent_id,
                    tool_call=tool_call,
                    session_id=session_id,
                    round_id=round_id,
                    message_id=message_id,
                    parent_call_id=parent_call_id,
                    on_tool_event=on_tool_event,
                    pipe=pipe,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.tool_call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                if tool_call.tool == "finish" and result.get("status") == "success":
                    return {"content": pipe.content()}

        raise ApiError(502, "subagent_max_steps_reached", f"{agent_id} 未在步数上限内完成。")

    async def _execute_subagent_tool(
        self,
        *,
        project_id: str,
        agent_id: str,
        tool_call: MainAgentToolCall,
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: ToolEventSink | None,
        pipe: SubagentPipe,
    ) -> dict[str, Any]:
        if not tool_call.tool_call_id:
            raise ApiError(502, "subagent_invalid_tool_call", f"{agent_id} tool_call 缺少 id。")

        scope_label = f"subagent:{agent_id}"
        if session_id:
            self.store.append_session_event(
                project_id,
                session_id,
                event_type="tool_call",
                scope=scope_label,
                round_id=round_id,
                message_id=message_id,
                call_id=tool_call.tool_call_id,
                parent_call_id=parent_call_id,
                payload={"tool": tool_call.tool, "arguments": tool_call.arguments},
            )
        if on_tool_event is not None:
            await on_tool_event(
                "tool_call_started",
                {
                    "call_id": tool_call.tool_call_id,
                    "parent_call_id": parent_call_id,
                    "scope": scope_label,
                    "tool": tool_call.tool,
                    "summary": f"{agent_id} 调用 {tool_call.tool}",
                },
            )

        if tool_call.arguments_error:
            result = invalid_tool_arguments_json_result(tool_call.arguments_error)
        elif tool_call.tool == "document_read":
            result = self.document_read(project_id, tool_call.arguments, scope="subagent")
        elif tool_call.tool == "exec_command":
            result = self.exec_command(project_id, tool_call.arguments, scope="subagent")
        elif tool_call.tool == "write_pipe":
            result = pipe.write(tool_call.arguments)
        elif tool_call.tool == "finish":
            result = pipe.finish(tool_call.arguments)
        else:
            result = tool_failed("permission_denied", f"子 agent 不允许调用 {tool_call.tool}。")

        if session_id:
            self.store.append_session_event(
                project_id,
                session_id,
                event_type="tool_result",
                scope=scope_label,
                round_id=round_id,
                message_id=message_id,
                call_id=tool_call.tool_call_id,
                parent_call_id=parent_call_id,
                payload={"tool": tool_call.tool, **result},
            )
        if on_tool_event is not None:
            await on_tool_event(
                "tool_call_finished",
                {
                    "call_id": tool_call.tool_call_id,
                    "parent_call_id": parent_call_id,
                    "scope": scope_label,
                    "tool": tool_call.tool,
                    "summary": subagent_tool_summary(agent_id, tool_call.tool, result),
                    "result": result,
                },
            )
        return result

    async def _prepare_subagent_run_messages(
        self,
        *,
        project_id: str,
        agent_id: str,
        goal: str,
        messages: list[dict[str, Any]],
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
    ) -> list[dict[str, Any]]:
        usage = usage_for_messages(messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens or len(messages) < 2:
            return messages

        source_estimated_tokens = estimate_messages_tokens(messages)
        logger.info(
            "context compression triggered scope=subagent agent_id=%s project_id=%s session_id=%s round_id=%s message_id=%s parent_call_id=%s used_tokens=%s threshold_tokens=%s message_count=%s",
            agent_id,
            project_id,
            session_id,
            round_id,
            message_id,
            parent_call_id,
            usage.used_tokens,
            usage.threshold_tokens,
            len(messages),
        )
        logger.info(
            "context compression started scope=subagent agent_id=%s project_id=%s session_id=%s round_id=%s message_id=%s parent_call_id=%s source_messages=%s estimated_tokens_before=%s source_estimated_tokens=%s model=%s",
            agent_id,
            project_id,
            session_id,
            round_id,
            message_id,
            parent_call_id,
            len(messages),
            usage.used_tokens,
            source_estimated_tokens,
            self.settings.openai_model,
        )
        payload = build_compression_payload(
            current_user_message=goal,
            compressible_messages=messages,
        )
        try:
            result = await self.llm_client.generate_json(
                system_prompt=context_compressor_system_prompt(),
                user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
                temperature=0.1,
                timeout=self.settings.context_compression_timeout,
            )
            compressed_messages, compression_warnings = prepare_compressed_messages_with_warnings(
                result.get("compressed_messages"),
                source_messages=messages,
            )
            restored_messages = restore_compressed_messages_from_messages(
                compressed_messages,
                source_messages=messages,
            )
            task_message = render_barrier_message({"kind": "agent_task", "task": goal})
            next_messages = [*restored_messages, task_message]
            warnings = _combined_compression_warnings(result.get("warnings"), compression_warnings)
            estimated_tokens_after = estimate_messages_tokens(next_messages)
            if session_id:
                self.store.append_session_event(
                    project_id,
                    session_id,
                    event_type="context_summary",
                    scope=f"subagent:{agent_id}",
                    round_id=round_id,
                    message_id=message_id or "",
                    parent_call_id=parent_call_id,
                    payload={
                        "agent_scope": f"subagent:{agent_id}",
                        "covered_message_count": len(messages),
                        "compressed_messages": compressed_messages,
                        "estimated_tokens_before": usage.used_tokens,
                        "estimated_tokens_after": estimated_tokens_after,
                        "compression_model": self.settings.openai_model,
                        "warnings": warnings,
                    },
                )
            else:
                logger.info(
                    "context compression event skipped scope=subagent reason=no_session agent_id=%s project_id=%s round_id=%s message_id=%s parent_call_id=%s",
                    agent_id,
                    project_id,
                    round_id,
                    message_id,
                    parent_call_id,
                )
            logger.info(
                "context compression completed scope=subagent agent_id=%s project_id=%s session_id=%s round_id=%s message_id=%s parent_call_id=%s compressed_messages=%s estimated_tokens_before=%s estimated_tokens_after=%s warnings=%s",
                agent_id,
                project_id,
                session_id,
                round_id,
                message_id,
                parent_call_id,
                len(compressed_messages),
                usage.used_tokens,
                estimated_tokens_after,
                len(warnings),
            )
            return next_messages
        except Exception:
            logger.exception(
                "context compression failed scope=subagent agent_id=%s project_id=%s session_id=%s round_id=%s message_id=%s parent_call_id=%s used_tokens=%s threshold_tokens=%s message_count=%s",
                agent_id,
                project_id,
                session_id,
                round_id,
                message_id,
                parent_call_id,
                usage.used_tokens,
                usage.threshold_tokens,
                len(messages),
            )
            raise

    def _subagent_system_prompt(self, agent_id: str) -> str:
        declaration = get_subagent(agent_id)
        if agent_id == "section_writer":
            return build_section_writer_system_prompt(declaration)
        if agent_id == "material_analyst":
            return build_material_analyst_system_prompt(declaration)
        if agent_id == "solution_refiner":
            return build_solution_refiner_system_prompt(declaration)
        if agent_id == "consistency_reviewer":
            return build_consistency_reviewer_system_prompt(declaration)
        raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")


def _combined_compression_warnings(raw_warnings: Any, generated_warnings: list[dict[str, Any]]) -> list[Any]:
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    return [*warnings, *generated_warnings]
