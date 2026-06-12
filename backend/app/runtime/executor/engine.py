from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ...tools import get_tool_declaration


@dataclass(slots=True)
class ToolRuntimeContext:
    session_id: str | None = None
    round_id: str | None = None
    message_id: str | None = None
    parent_call_id: str | None = None
    caller_messages: list[dict[str, Any]] | None = None
    system_prompt: str | None = None
    tools: list[dict[str, Any]] | None = None
    llm_client: Any | None = None
    settings: Any | None = None


class ExecutorEngine:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    async def execute_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> dict[str, Any]:
        try:
            declaration = get_tool_declaration(tool_name)
        except KeyError:
            return tool_failed("unsupported_tool", f"不支持的工具：{tool_name}")
        if runtime_context is not None and "context" in inspect.signature(declaration.function).parameters:
            result = declaration.function(self.store, project_id, arguments, context=runtime_context)
        else:
            result = declaration.function(self.store, project_id, arguments)
        if inspect.isawaitable(result):
            return await result
        return result
