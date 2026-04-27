from __future__ import annotations

from typing import Any

from ....domain.document_tools import apply_document_edit, read_document, tool_failed
from ....storage.workspace_store import WorkspaceStore
from ..registry import can_use_tool
from ..types import AgentScope


def document_read(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    scope: AgentScope,
) -> dict[str, Any]:
    if not can_use_tool(scope, "document_read"):
        return tool_failed("permission_denied", "当前调用方不允许读取文档。")
    return read_document(store.get_disclosure(project_id), arguments)


def document_edit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    scope: AgentScope,
) -> dict[str, Any]:
    if not can_use_tool(scope, "document_edit"):
        return tool_failed("permission_denied", "子 agent 不允许调用 document_edit。")
    disclosure = store.get_disclosure(project_id)
    result = apply_document_edit(disclosure, arguments)
    if result["status"] == "success":
        store.save_disclosure(project_id, disclosure)
    return result
