from __future__ import annotations

from typing import Any

from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ...tools import get_tool_declaration


class ExecutorEngine:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    async def execute_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            declaration = get_tool_declaration(tool_name)
        except KeyError:
            return tool_failed("unsupported_tool", f"不支持的工具：{tool_name}")
        return declaration.function(self.store, project_id, arguments)
