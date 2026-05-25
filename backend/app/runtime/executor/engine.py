from __future__ import annotations

from typing import Any, Awaitable, Callable

from ...agents.runtime.openai_compat import OpenAICompatibleClient
from ...core import Settings
from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ...tools import get_tool_declaration
from ...tools.types import AgentScope
from ..context import ContextManager

ToolEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


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
        return declaration.function(self.store, project_id, arguments)
