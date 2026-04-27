from __future__ import annotations

from typing import Any

from ...agents import get_subagent
from ...agents.runtime.openai_compat import OpenAICompatibleClient
from ...agents.workers import (
    build_consistency_reviewer_context,
    build_material_analyst_context,
    build_section_writer_context,
    build_solution_refiner_context,
    run_consistency_reviewer,
    run_material_analyst,
    run_section_writer,
    run_solution_refiner,
)
from ...core import generate_id
from ...domain.document_tools import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..context import ContextManager
from .registry import can_use_tool
from .tools.document import document_edit, document_read
from .tools.shell import exec_command
from .types import AgentScope


class ExecutorEngine:
    def __init__(
        self,
        store: WorkspaceStore,
        context_manager: ContextManager,
        llm_client: OpenAICompatibleClient,
    ) -> None:
        self.store = store
        self.context_manager = context_manager
        self.llm_client = llm_client

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
    ) -> dict[str, Any]:
        if not can_use_tool(scope, "execute_subagent"):
            return tool_failed("permission_denied", "子 agent 不允许调用 execute_subagent。")

        declaration = get_subagent(str(arguments.get("agent_id")))
        call_type = arguments.get("call_type")
        if call_type not in declaration.allowed_types:
            return tool_failed("invalid_call_type", f"{declaration.id} 不支持 call_type：{call_type}")

        goal = str(arguments.get("goal") or "")
        user_message = str(arguments.get("user_message") or "")
        target_section_id = arguments.get("target_section_id")
        target_block_id = arguments.get("target_block_id")
        outline = self.context_manager.build_outline_snapshot(project_id)
        recent_inputs = self.context_manager.recent_user_inputs(project_id, session_id)

        section_snapshot: dict[str, Any] | None = None
        if target_section_id:
            section_snapshot = self._subagent_read_section(
                project_id=project_id,
                section_id=str(target_section_id),
                agent_id=declaration.id,
                session_id=session_id,
                round_id=round_id,
                message_id=message_id,
                parent_call_id=parent_call_id,
            )

        if declaration.id == "section_writer":
            if not target_section_id:
                return tool_failed("missing_target_section_id", "section_writer 必须提供 target_section_id。")
            effective_target = str(target_section_id)
            if section_snapshot is None:
                section_snapshot = self._subagent_read_section(
                    project_id=project_id,
                    section_id=effective_target,
                    agent_id=declaration.id,
                    session_id=session_id,
                    round_id=round_id,
                    message_id=message_id,
                    parent_call_id=parent_call_id,
                )
            context = build_section_writer_context(
                target_section_id=effective_target,
                target_block_id=target_block_id,
                goal=goal,
                user_message=user_message,
                outline=outline,
                section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
            result = await run_section_writer(declaration, self.llm_client, context)
            return tool_success(
                {
                    "agent_id": declaration.id,
                    "call_type": call_type,
                    "target_section_id": effective_target,
                    "target_block_id": target_block_id,
                    "result": result,
                }
            )

        if declaration.id == "material_analyst":
            context = build_material_analyst_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
            result = await run_material_analyst(declaration, self.llm_client, context)
            return tool_success(
                {
                    "agent_id": declaration.id,
                    "call_type": call_type,
                    "target_section_id": target_section_id,
                    "target_block_id": target_block_id,
                    "result": result,
                }
            )

        if declaration.id == "solution_refiner":
            context = build_solution_refiner_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
            result = await run_solution_refiner(declaration, self.llm_client, context)
            return tool_success(
                {
                    "agent_id": declaration.id,
                    "call_type": call_type,
                    "target_section_id": target_section_id,
                    "target_block_id": target_block_id,
                    "result": result,
                }
            )

        if declaration.id == "consistency_reviewer":
            context = build_consistency_reviewer_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                target_section_id=str(target_section_id) if target_section_id else None,
                recent_user_inputs=recent_inputs,
            )
            result = await run_consistency_reviewer(declaration, self.llm_client, context)
            return tool_success(
                {
                    "agent_id": declaration.id,
                    "call_type": call_type,
                    "target_section_id": target_section_id,
                    "target_block_id": target_block_id,
                    "result": result,
                }
            )

        return tool_failed("unsupported_agent", f"未实现的子 agent：{declaration.id}")

    def _subagent_read_section(
        self,
        *,
        project_id: str,
        section_id: str,
        agent_id: str,
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
    ) -> dict[str, Any] | None:
        """以子 agent 身份真实调用 document_read，并同步写入 session 事件。"""

        arguments = {"action": "get_section", "section_id": section_id, "include_children": True}
        result = self.document_read(project_id, arguments, scope="subagent")

        if session_id:
            call_id = generate_id("call")
            scope_label = f"subagent:{agent_id}"
            self.store.append_session_event(
                project_id,
                session_id,
                event_type="tool_call",
                scope=scope_label,
                round_id=round_id,
                message_id=message_id,
                call_id=call_id,
                parent_call_id=parent_call_id,
                payload={"tool": "document_read", "arguments": arguments},
            )
            self.store.append_session_event(
                project_id,
                session_id,
                event_type="tool_result",
                scope=scope_label,
                round_id=round_id,
                message_id=message_id,
                call_id=call_id,
                parent_call_id=parent_call_id,
                payload={"tool": "document_read", **result},
            )

        if result.get("status") != "success":
            return None
        output = result.get("output") or {}
        section = output.get("section")
        return section if isinstance(section, dict) else None
