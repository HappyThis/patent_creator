from __future__ import annotations

from .types import AgentScope

TOOL_PERMISSIONS: dict[AgentScope, frozenset[str]] = {
    "main_agent": frozenset({"document_read", "document_edit", "execute_subagent", "exec_command"}),
    "subagent": frozenset({"document_read", "exec_command", "write_pipe", "finish"}),
}


def can_use_tool(scope: AgentScope, tool_name: str) -> bool:
    return tool_name in TOOL_PERMISSIONS.get(scope, frozenset())
