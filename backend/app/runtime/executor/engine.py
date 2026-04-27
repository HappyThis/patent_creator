from __future__ import annotations

from typing import Any

from ...agents import get_subagent
from ...agents.runtime.openai_compat import OpenAICompatibleClient
from ...agents.workers import build_section_writer_context, run_section_writer
from ...core import ApiError
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
        scope: AgentScope = "main_agent",
    ) -> dict[str, Any]:
        if not can_use_tool(scope, "execute_subagent"):
            return tool_failed("permission_denied", "子 agent 不允许调用 execute_subagent。")

        declaration = get_subagent(str(arguments.get("agent_id")))
        call_type = arguments.get("call_type")
        if call_type not in declaration.allowed_types:
            return tool_failed("invalid_call_type", f"{declaration.id} 不支持 call_type：{call_type}")

        if declaration.id == "section_writer":
            target_section_id = str(arguments.get("target_section_id") or "technical_solution")
            context = build_section_writer_context(
                target_section_id=target_section_id,
                target_block_id=arguments.get("target_block_id"),
                goal=str(arguments.get("goal") or ""),
                user_message=str(arguments.get("user_message") or ""),
                outline=self.context_manager.build_outline_snapshot(project_id),
                section=self.context_manager.build_section_snapshot(project_id, target_section_id),
                recent_user_inputs=self.context_manager.recent_user_inputs(project_id, session_id),
            )
            result = await run_section_writer(declaration, self.llm_client, context)
            return tool_success(
                {
                    "agent_id": declaration.id,
                    "call_type": call_type,
                    "target_section_id": target_section_id,
                    "target_block_id": arguments.get("target_block_id"),
                    "result": result,
                }
            )

        return tool_success(
            {
                "agent_id": declaration.id,
                "call_type": call_type,
                "target_section_id": arguments.get("target_section_id"),
                "target_block_id": arguments.get("target_block_id"),
                "result": {
                    "status": "success",
                    "summary": f"{declaration.id} 当前仍使用占位实现。",
                    "proposal": {
                        "type": declaration.default_proposal_type,
                        "facts": [],
                        "candidate_terms": [],
                        "recommended_next_actions": [],
                    },
                    "questions": [],
                    "warnings": ["该子 agent 尚未接入真实模型能力。"],
                },
            }
        )
