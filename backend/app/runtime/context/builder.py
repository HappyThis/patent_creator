from __future__ import annotations

import json
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

    def build_main_agent_messages(
        self,
        project_id: str,
        session_id: str | None,
        *,
        user_message: str,
        active_section_id: str | None,
        active_block_id: str | None,
    ) -> list[dict[str, Any]]:
        """给主 agent loop 准备首条 user message。

        遵循文档原则：默认只注入目录与最小必要信息，不注入完整 disclosure。
        """
        outline = self.build_outline_snapshot(project_id)
        recent = self.recent_user_inputs(project_id, session_id)
        payload: dict[str, Any] = {
            "user_message": user_message,
            "active_section_id": active_section_id,
            "active_block_id": active_block_id,
            "outline": outline,
            "recent_user_inputs": recent,
        }
        return [
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
