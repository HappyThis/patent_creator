from __future__ import annotations

from typing import Any

from ...domain import build_outline_items, find_section
from ...storage.workspace_store import WorkspaceStore


class ContextManager:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    def build_outline_snapshot(self, project_id: str) -> list[dict[str, Any]]:
        disclosure = self.store.get_disclosure(project_id)
        return [item.model_dump() for item in build_outline_items(disclosure["sections"])]

    def build_section_snapshot(self, project_id: str, section_id: str) -> dict[str, Any] | None:
        disclosure = self.store.get_disclosure(project_id)
        return find_section(disclosure["sections"], section_id)

    def recent_user_inputs(self, project_id: str, session_id: str | None, limit: int = 3) -> list[str]:
        if not session_id or not self.store.session_exists(project_id, session_id):
            return []
        events = self.store.read_session_events(project_id, session_id)
        messages = [event.payload.get("text", "") for event in events if event.type == "user_input"]
        return [message for message in messages[-limit:] if message]
