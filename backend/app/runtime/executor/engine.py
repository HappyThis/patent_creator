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
from ...domain.document_tool_results import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ...tools import SUBAGENT_TOOLS, build_subagent_tools, get_tool_declaration
from ...tools.builtin.pipe import SubagentPipe, invalid_tool_arguments_json_result, subagent_tool_summary
from ...tools.types import AgentScope
from ..context import ContextManager
from ..context.barrier import render_barrier_message
from ..context.compression import (
    build_compression_prompt,
    fallback_compressed_markdown,
    prepare_compressed_markdown_messages,
    validate_compressed_markdown,
)
from ..context.messages import closed_message_prefix
from ..context.prompts import context_compressor_system_prompt
from ..context.usage import estimate_messages_tokens, usage_for_messages

ToolEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger("patent_creator.executor")

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

    async def execute_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        scope: AgentScope = "main_agent",
        pipe: SubagentPipe | None = None,
        session_id: str | None = None,
        round_id: str | None = None,
        message_id: str | None = None,
        parent_call_id: str | None = None,
        on_tool_event: ToolEventSink | None = None,
        caller_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            declaration = get_tool_declaration(tool_name)
        except KeyError:
            return tool_failed("unsupported_tool", f"不支持的工具：{tool_name}")
        if not declaration.can_use(scope):
            return tool_failed("permission_denied", f"{scope} 不允许调用 {tool_name}。")
        if tool_name == "execute_subagent":
            return await self._execute_subagent(
                project_id,
                arguments,
                session_id=session_id,
                round_id=round_id,
                message_id=message_id,
                parent_call_id=parent_call_id,
                on_tool_event=on_tool_event,
                caller_messages=caller_messages,
            )
        if tool_name == "write_pipe":
            if pipe is None:
                return tool_failed("missing_pipe", "write_pipe 只能在子 agent run 中调用。")
            return pipe.write(arguments)
        if tool_name == "finish":
            if pipe is None:
                return tool_failed("missing_pipe", "finish 只能在子 agent run 中调用。")
            return pipe.finish(arguments)
        return declaration.function(self.store, project_id, arguments)

    async def _execute_subagent(
        self,
        project_id: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
        round_id: str | None = None,
        message_id: str | None = None,
        parent_call_id: str | None = None,
        on_tool_event: ToolEventSink | None = None,
        caller_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
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
        declaration = get_subagent(agent_id)
        system_prompt = self._subagent_system_prompt(agent_id)
        tools = build_subagent_tools(declaration)
        messages: list[dict[str, Any]] = [dict(message) for message in initial_messages]
        pipe = SubagentPipe()
        step_index = 0

        while True:
            step_index += 1
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
                tools=tools,
                on_text_delta=None,
                trace_context={
                    "scope": "subagent",
                    "agent_id": agent_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "round_id": round_id,
                    "message_id": message_id,
                    "parent_call_id": parent_call_id,
                    "step_index": step_index,
                },
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
                if (
                    tool_call.tool == "write_pipe"
                    and result.get("status") == "success"
                    and isinstance(result.get("output"), dict)
                    and result["output"].get("auto_finished") is True
                ):
                    return {"content": pipe.content()}
                if tool_call.tool == "finish" and result.get("status") == "success":
                    return {"content": pipe.content()}

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
        else:
            result = await self.execute_tool(
                project_id,
                tool_call.tool,
                tool_call.arguments,
                scope="subagent",
                pipe=pipe,
            )

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
        prompt = build_compression_prompt(
            current_user_message=goal,
            compressible_messages=messages,
        )
        warnings: list[Any] = []
        try:
            raw_markdown = await self.llm_client.generate_text(
                system_prompt=context_compressor_system_prompt(),
                user_prompt=prompt,
                temperature=0.1,
                timeout=self.settings.context_compression_timeout,
                trace_context={
                    "scope": "context_compression",
                    "agent_scope": "subagent",
                    "agent_id": agent_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "round_id": round_id,
                    "message_id": message_id,
                    "parent_call_id": parent_call_id,
                },
            )
            compressed_markdown = validate_compressed_markdown(raw_markdown)
        except Exception as exc:
            logger.warning(
                "context compression markdown fallback scope=subagent agent_id=%s project_id=%s session_id=%s round_id=%s message_id=%s parent_call_id=%s error=%s",
                agent_id,
                project_id,
                session_id,
                round_id,
                message_id,
                parent_call_id,
                exc,
            )
            compressed_markdown = fallback_compressed_markdown(str(exc))
            warnings.append({"code": "compression_markdown_fallback", "message": str(exc)})
        compressed_memory_messages = prepare_compressed_markdown_messages(compressed_markdown)
        task_message = render_barrier_message({"kind": "agent_task", "task": goal})
        next_messages = [*compressed_memory_messages, task_message]
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
                    "compressed_markdown": compressed_markdown,
                    "estimated_tokens_before": usage.used_tokens,
                    "estimated_tokens_after": estimated_tokens_after,
                    "compression_model": self.settings.openai_model,
                    "compression_mode": "markdown_memory",
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
            "context compression completed scope=subagent agent_id=%s project_id=%s session_id=%s round_id=%s message_id=%s parent_call_id=%s compressed_chars=%s estimated_tokens_before=%s estimated_tokens_after=%s warnings=%s",
            agent_id,
            project_id,
            session_id,
            round_id,
            message_id,
            parent_call_id,
            len(compressed_markdown),
            usage.used_tokens,
            estimated_tokens_after,
            len(warnings),
        )
        return next_messages

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
