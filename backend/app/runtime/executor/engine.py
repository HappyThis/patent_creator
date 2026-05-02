from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from ...agents import get_subagent
from ...agents.prompts import (
    build_consistency_reviewer_system_prompt,
    build_consistency_reviewer_user_prompt,
    build_material_analyst_system_prompt,
    build_material_analyst_user_prompt,
    build_section_writer_system_prompt,
    build_section_writer_user_prompt,
    build_solution_refiner_system_prompt,
    build_solution_refiner_user_prompt,
)
from ...agents.runtime.openai_compat import OpenAICompatibleClient
from ...agents.workers import (
    MainAgentToolCall,
    build_consistency_reviewer_context,
    build_material_analyst_context,
    build_section_writer_context,
    build_solution_refiner_context,
)
from ...agents.workers.consistency_reviewer import build_consistency_reviewer_result
from ...agents.workers.material_analyst import build_material_analyst_result
from ...agents.workers.section_writer import build_section_writer_result
from ...agents.workers.solution_refiner import build_solution_refiner_result
from ...core import ApiError, Settings, generate_id
from ...domain.document_tools import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..context import ContextManager
from .registry import can_use_tool
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
                        "enum": ["get_meta", "get_outline", "get_section", "get_block", "search_blocks"],
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
            "description": "在项目工作区执行命令字符串，cwd 为当前 project 工作区。",
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
            "name": "submit_result",
            "description": "提交子 agent 的最终结构化结果。调用成功后子 agent 本轮结束。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "reply": {"type": "string"},
                    "rationale": {"type": "string"},
                    "proposal_type": {
                        "type": "string",
                        "enum": ["analysis_result", "document_edit_proposal", "review_report"],
                    },
                    "proposal": {"type": "object"},
                    "questions": {"type": "array", "items": {"type": "string"}},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "summary",
                    "reply",
                    "rationale",
                    "proposal_type",
                    "proposal",
                    "questions",
                    "warnings",
                ],
            },
        },
    },
]

_SUBAGENT_ALLOWED_PROPOSAL_TYPES: dict[str, set[str]] = {
    "section_writer": {"document_edit_proposal"},
    "material_analyst": {"analysis_result"},
    "solution_refiner": {"analysis_result", "document_edit_proposal"},
    "consistency_reviewer": {"review_report"},
}


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
    ) -> dict[str, Any]:
        if not can_use_tool(scope, "execute_subagent"):
            return tool_failed("permission_denied", "子 agent 不允许调用 execute_subagent。")

        declaration = get_subagent(str(arguments.get("agent_id")))
        call_type = arguments.get("call_type")
        if call_type not in declaration.allowed_types:
            return tool_failed("invalid_call_type", f"{declaration.id} 不支持 call_type：{call_type}")

        target_section_id = arguments.get("target_section_id")
        if declaration.id == "section_writer" and not target_section_id:
            return tool_failed("missing_target_section_id", "section_writer 必须提供 target_section_id。")

        context = await self._build_subagent_context(
            project_id=project_id,
            agent_id=declaration.id,
            call_type=str(call_type),
            arguments=arguments,
            session_id=session_id,
            round_id=round_id,
            message_id=message_id,
            parent_call_id=parent_call_id,
            on_tool_event=on_tool_event,
        )
        try:
            result = await self._run_subagent_loop(
                project_id=project_id,
                agent_id=declaration.id,
                call_type=str(call_type),
                context=context,
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
                "call_type": call_type,
                "target_section_id": target_section_id,
                "target_block_id": arguments.get("target_block_id"),
                "result": result,
            }
        )

    async def _build_subagent_context(
        self,
        *,
        project_id: str,
        agent_id: str,
        call_type: str,
        arguments: dict[str, Any],
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: ToolEventSink | None,
    ) -> dict[str, Any]:
        goal = str(arguments.get("goal") or "")
        user_message = str(arguments.get("user_message") or "")
        target_section_id = arguments.get("target_section_id")
        target_block_id = arguments.get("target_block_id")

        outline: list[dict[str, Any]] = []
        recent_inputs: list[str] = []
        section_snapshot: dict[str, Any] | None = None

        if call_type in {"rich_context_specialist", "forked_context"}:
            outline = self.context_manager.build_outline_snapshot(project_id)
            recent_inputs = self.context_manager.recent_user_inputs(project_id, session_id)
            if target_section_id:
                section_snapshot = await self._subagent_read_section(
                    project_id=project_id,
                    section_id=str(target_section_id),
                    agent_id=agent_id,
                    session_id=session_id,
                    round_id=round_id,
                    message_id=message_id,
                    parent_call_id=parent_call_id,
                    on_tool_event=on_tool_event,
                )

        if agent_id == "section_writer":
            context = build_section_writer_context(
                target_section_id=str(target_section_id),
                target_block_id=target_block_id,
                goal=goal,
                user_message=user_message,
                outline=outline,
                section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
        elif agent_id == "material_analyst":
            context = build_material_analyst_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
        elif agent_id == "solution_refiner":
            context = build_solution_refiner_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
        elif agent_id == "consistency_reviewer":
            context = build_consistency_reviewer_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                target_section_id=str(target_section_id) if target_section_id else None,
                recent_user_inputs=recent_inputs,
            )
        else:
            raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")

        context["call_type"] = call_type
        context["available_tools"] = ["document_read", "exec_command", "submit_result"]
        if call_type == "forked_context":
            context["caller_context"] = {
                "recent_session_events": self._recent_session_events(project_id, session_id),
            }
        return context

    async def _run_subagent_loop(
        self,
        *,
        project_id: str,
        agent_id: str,
        call_type: str,
        context: dict[str, Any],
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: ToolEventSink | None,
    ) -> dict[str, Any]:
        system_prompt, user_prompt = self._subagent_prompts(agent_id, context)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        max_steps = max(1, self.settings.subagent_max_steps)

        for _step in range(max_steps):
            action = await self.llm_client.generate_with_tools_stream(
                system_prompt=system_prompt,
                messages=messages,
                tools=SUBAGENT_TOOLS,
                on_text_delta=None,
                response_format_json=True,
            )
            assistant_message = action.get("assistant_message")
            if not isinstance(assistant_message, dict):
                raise ApiError(502, "subagent_invalid_action", f"{agent_id} 缺少 assistant_message。")
            messages.append(assistant_message)

            if action.get("type") == "respond":
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你不能直接回复文本或 JSON。请调用 submit_result 工具提交最终结果；"
                            "如果你还缺少信息，先调用 document_read 或 exec_command。"
                        ),
                    }
                )
                continue

            if action.get("type") != "tool_calls":
                raise ApiError(502, "subagent_invalid_action", f"{agent_id} 返回未知动作：{action.get('type')}")

            raw_calls = action.get("tool_calls")
            if not isinstance(raw_calls, list) or not raw_calls:
                raise ApiError(502, "subagent_invalid_action", f"{agent_id} tool_calls 为空。")
            completed_result: dict[str, Any] | None = None
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
                    context=context,
                    session_id=session_id,
                    round_id=round_id,
                    message_id=message_id,
                    parent_call_id=parent_call_id,
                    on_tool_event=on_tool_event,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.tool_call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                if tool_call.tool == "submit_result" and result.get("status") == "success":
                    output = result.get("output")
                    if isinstance(output, dict) and isinstance(output.get("result"), dict):
                        completed_result = output["result"]
            if completed_result is not None:
                return completed_result

        raise ApiError(502, "subagent_max_steps_reached", f"{agent_id} 未在步数上限内完成。")

    @staticmethod
    def _invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
        return {
            "status": "failed",
            "code": "invalid_tool_arguments_json",
            "message": message,
        }

    async def _execute_subagent_tool(
        self,
        *,
        project_id: str,
        agent_id: str,
        tool_call: MainAgentToolCall,
        context: dict[str, Any],
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: ToolEventSink | None,
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
            result = self._invalid_tool_arguments_json_result(tool_call.arguments_error)
        elif tool_call.tool == "document_read":
            result = self.document_read(project_id, tool_call.arguments, scope="subagent")
        elif tool_call.tool == "exec_command":
            result = self.exec_command(project_id, tool_call.arguments, scope="subagent")
        elif tool_call.tool == "submit_result":
            result = self._submit_subagent_result(agent_id, tool_call.arguments, context)
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
                    "summary": self._subagent_tool_summary(agent_id, tool_call.tool, result),
                    "result": result,
                },
            )
        return result

    async def _subagent_read_section(
        self,
        *,
        project_id: str,
        section_id: str,
        agent_id: str,
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: ToolEventSink | None,
    ) -> dict[str, Any] | None:
        """以子 agent 身份真实调用 document_read，并同步写入 session 事件。"""

        arguments = {"action": "get_section", "section_id": section_id, "include_children": True}
        result = await self._execute_subagent_tool(
            project_id=project_id,
            agent_id=agent_id,
            tool_call=MainAgentToolCall(
                tool="document_read",
                arguments=arguments,
                tool_call_id=generate_id("call"),
            ),
            context={},
            session_id=session_id,
            round_id=round_id,
            message_id=message_id,
            parent_call_id=parent_call_id,
            on_tool_event=on_tool_event,
        )

        if result.get("status") != "success":
            return None
        output = result.get("output") or {}
        section = output.get("section")
        return section if isinstance(section, dict) else None

    def _subagent_prompts(self, agent_id: str, context: dict[str, Any]) -> tuple[str, str]:
        declaration = get_subagent(agent_id)
        if agent_id == "section_writer":
            return build_section_writer_system_prompt(declaration), build_section_writer_user_prompt(context)
        if agent_id == "material_analyst":
            return build_material_analyst_system_prompt(declaration), build_material_analyst_user_prompt(context)
        if agent_id == "solution_refiner":
            return build_solution_refiner_system_prompt(declaration), build_solution_refiner_user_prompt(context)
        if agent_id == "consistency_reviewer":
            return build_consistency_reviewer_system_prompt(declaration), build_consistency_reviewer_user_prompt(context)
        raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")

    def _build_subagent_result(self, agent_id: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        self._validate_submit_result_envelope(agent_id, payload)
        if agent_id == "section_writer":
            task = context["task"]
            return build_section_writer_result(payload, task["target_section_id"], task.get("target_block_id"))
        if agent_id == "material_analyst":
            return build_material_analyst_result(payload)
        if agent_id == "solution_refiner":
            return build_solution_refiner_result(payload)
        if agent_id == "consistency_reviewer":
            return build_consistency_reviewer_result(payload)
        raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")

    def _submit_subagent_result(self, agent_id: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._build_subagent_result(agent_id, payload, context)
        except ApiError as exc:
            return tool_failed(exc.code, exc.message)
        return tool_success({"result": result})

    @staticmethod
    def _validate_submit_result_envelope(agent_id: str, payload: dict[str, Any]) -> None:
        string_fields = ("summary", "reply", "rationale")
        for field in string_fields:
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise ApiError(502, "subagent_invalid_submit_result", f"submit_result.{field} 必须是非空字符串。")

        proposal_type = payload.get("proposal_type")
        allowed_types = _SUBAGENT_ALLOWED_PROPOSAL_TYPES.get(agent_id)
        if not allowed_types:
            raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")
        if proposal_type not in {"analysis_result", "document_edit_proposal", "review_report"}:
            raise ApiError(
                502,
                "subagent_invalid_submit_result",
                "submit_result.proposal_type 必须是 analysis_result、document_edit_proposal 或 review_report。",
            )
        if proposal_type not in allowed_types:
            allowed = "、".join(sorted(allowed_types))
            raise ApiError(
                502,
                "subagent_invalid_submit_result",
                f"{agent_id} 只能提交 proposal_type：{allowed}。",
            )
        if not isinstance(payload.get("proposal"), dict):
            raise ApiError(502, "subagent_invalid_submit_result", "submit_result.proposal 必须是对象。")
        for field in ("questions", "warnings"):
            value = payload.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ApiError(502, "subagent_invalid_submit_result", f"submit_result.{field} 必须是字符串数组。")

    @staticmethod
    def _subagent_tool_summary(agent_id: str, tool: str, result: dict[str, Any]) -> str:
        if tool == "submit_result":
            if result.get("status") == "success":
                return f"{agent_id} 提交结果"
            return f"{agent_id} 提交结果失败"
        if result.get("status") == "failed":
            return f"{agent_id} 执行 {tool} 失败"
        return f"{agent_id} 完成 {tool}"

    def _recent_session_events(self, project_id: str, session_id: str | None, limit: int = 8) -> list[dict[str, Any]]:
        if not session_id or not self.store.session_exists(project_id, session_id):
            return []
        events = self.store.read_session_events(project_id, session_id)
        return [event.model_dump() for event in events[-limit:]]
