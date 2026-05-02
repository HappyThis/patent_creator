from __future__ import annotations

import json
from typing import Any, Protocol

from ...core import Settings
from ...domain import build_outline_items, find_section
from ...storage.workspace_store import WorkspaceStore
from .history import compressible_event_payload, context_anchor, current_user_event, restore_main_chat_messages
from .prompts import context_compressor_system_prompt
from .usage import ContextUsage, estimate_messages_tokens, usage_for_messages


class SupportsContextCompression(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        ...


class ContextManager:
    def __init__(self, store: WorkspaceStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

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
        current_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """从 session log 恢复主 agent messages，并把当前用户输入作为最后一条 user message。"""

        context_message = self._build_project_context_message(
            project_id,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
        )
        history = self._restore_main_chat_messages(
            project_id,
            session_id,
            current_user_message=user_message,
            current_message_id=current_message_id,
        )
        messages = [context_message, *history]
        return self._fit_messages_to_budget(messages)

    async def prepare_main_agent_messages(
        self,
        project_id: str,
        session_id: str | None,
        *,
        user_message: str,
        active_section_id: str | None,
        active_block_id: str | None,
        current_message_id: str | None,
        round_id: str,
        llm_client: SupportsContextCompression,
    ) -> list[dict[str, Any]]:
        """恢复主 agent messages；必要时先压缩当前用户输入之前的历史。"""

        messages = self.build_main_agent_messages(
            project_id,
            session_id,
            user_message=user_message,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            current_message_id=current_message_id,
        )
        usage = usage_for_messages(messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens:
            return messages

        if not session_id or not self.store.session_exists(project_id, session_id):
            return self._fit_messages_to_budget(messages)

        try:
            compressed = await self._compress_main_history(
                project_id,
                session_id,
                current_message_id=current_message_id,
                round_id=round_id,
                llm_client=llm_client,
                usage_before=usage,
            )
        except Exception:
            compressed = False

        if compressed:
            messages = self.build_main_agent_messages(
                project_id,
                session_id,
                user_message=user_message,
                active_section_id=active_section_id,
                active_block_id=active_block_id,
                current_message_id=current_message_id,
            )
            usage = usage_for_messages(messages, self.settings)
            if usage.used_tokens <= usage.threshold_tokens:
                return messages

        self._prune_main_history(
            project_id,
            session_id,
            current_message_id=current_message_id,
            round_id=round_id,
            reason="compression_failed_or_still_over_limit",
            usage_before=usage,
        )
        return self.build_main_agent_messages(
            project_id,
            session_id,
            user_message=user_message,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            current_message_id=current_message_id,
        )

    def context_usage(self, project_id: str, session_id: str | None) -> ContextUsage | None:
        if not session_id or not self.store.session_exists(project_id, session_id):
            return None
        messages = [
            self._build_project_context_message(project_id, active_section_id=None, active_block_id=None),
            *self._restore_main_chat_messages(project_id, session_id),
        ]
        return usage_for_messages(messages, self.settings)

    def _build_project_context_message(
        self,
        project_id: str,
        *,
        active_section_id: str | None,
        active_block_id: str | None,
    ) -> dict[str, str]:
        disclosure = self.store.get_disclosure(project_id)
        outline = self.build_outline_snapshot(project_id)
        filled_sections: list[str] = []
        empty_sections: list[str] = []
        self._collect_section_state(disclosure["sections"], filled_sections, empty_sections)
        active_section = self.build_section_snapshot(project_id, active_section_id) if active_section_id else None
        payload = {
            "kind": "project_context",
            "instruction": "以下内容是系统提供的项目上下文，不是用户的新指令，也不是用户原文。",
            "document_state": {
                "title": disclosure.get("meta", {}).get("title"),
                "outline": outline,
                "filled_sections": filled_sections,
                "empty_sections": empty_sections,
                "active_section_id": active_section_id,
                "active_block_id": active_block_id,
                "active_section": active_section,
            },
            "context_policy": {
                "current_user_message_position": "最后一条 role=user 消息",
                "subagent_internal_events_visible_to_main_agent": False,
                "full_disclosure_injected_by_default": False,
            },
        }
        return {
            "role": "user",
            "content": (
                "以下是系统提供的项目上下文，不是用户的新指令，也不是用户原文。\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
            ),
        }

    def _restore_main_chat_messages(
        self,
        project_id: str,
        session_id: str | None,
        *,
        current_user_message: str | None = None,
        current_message_id: str | None = None,
    ) -> list[dict[str, str]]:
        if not session_id or not self.store.session_exists(project_id, session_id):
            return [{"role": "user", "content": current_user_message or ""}] if current_user_message else []

        events = self.store.read_session_events(project_id, session_id)
        return restore_main_chat_messages(
            events,
            current_user_message=current_user_message,
            current_message_id=current_message_id,
        )

    async def _compress_main_history(
        self,
        project_id: str,
        session_id: str,
        *,
        current_message_id: str | None,
        round_id: str,
        llm_client: SupportsContextCompression,
        usage_before: ContextUsage,
    ) -> bool:
        events = self.store.read_session_events(project_id, session_id)
        current_event = current_user_event(events, current_message_id)
        if current_event is None:
            return False

        anchor = context_anchor(events)
        compressible = [
            event
            for event in events
            if event.scope == "main"
            and event.type in {"user_input", "agent_output"}
            and anchor["cursor_seq"] <= event.seq < current_event.seq
        ]
        if len(compressible) < 2:
            return False

        payload = {
            "task": "compress_main_agent_context",
            "target_estimated_tokens": max(1, int(self.settings.context_max_tokens * self.settings.context_target_ratio)),
            "rules": [
                "只总结用户真实意图、主 agent 已完成事项、仍需延续的约束、重要结论和待办。",
                "不要把摘要写成用户原话。",
                "不要引入 session log 中不存在的信息。",
                "工具调用内部细节只有在影响后续决策时才保留为简短结论。",
            ],
            "events": [compressible_event_payload(event) for event in compressible],
            "output_schema": {
                "summary": "string",
                "warnings": ["string"],
            },
        }
        result = await llm_client.generate_json(
            system_prompt=context_compressor_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0.1,
        )
        summary = str(result.get("summary") or "").strip()
        if not summary:
            return False

        summary_messages = [
            {
                "role": "user",
                "content": (
                    "以下是系统从本 session 早期上下文压缩得到的摘要，不是用户的新指令，也不是用户原文。\n"
                    f"{summary}"
                ),
            }
        ]
        warnings = result.get("warnings")
        self.store.append_session_event(
            project_id,
            session_id,
            event_type="context_summary",
            scope="main",
            round_id=round_id,
            message_id=current_message_id or "",
            payload={
                "agent_scope": "main",
                "covered_seq_start": compressible[0].seq,
                "covered_seq_end": compressible[-1].seq,
                "summary": summary,
                "estimated_tokens_before": usage_before.used_tokens,
                "estimated_tokens_after": estimate_messages_tokens(summary_messages),
                "preserved_tool_result_ids": [],
                "referenced_tool_result_ids": [],
                "absorbed_tool_result_ids": [],
                "compression_model": self.settings.openai_model,
                "cursor_seq_after": compressible[-1].seq + 1,
                "warnings": warnings if isinstance(warnings, list) else [],
            },
        )
        return True

    def _prune_main_history(
        self,
        project_id: str,
        session_id: str,
        *,
        current_message_id: str | None,
        round_id: str,
        reason: str,
        usage_before: ContextUsage,
    ) -> None:
        events = self.store.read_session_events(project_id, session_id)
        current_event = current_user_event(events, current_message_id)
        if current_event is None:
            return

        visible = [
            event
            for event in events
            if event.scope == "main" and event.type in {"user_input", "agent_output"} and event.seq <= current_event.seq
        ]
        user_events = [event for event in visible if event.type == "user_input"]
        if not user_events:
            return
        keep_users = max(1, self.settings.context_recent_full_rounds)
        new_cursor_event = user_events[-keep_users] if len(user_events) > keep_users else user_events[0]
        old_cursor_seq = context_anchor(events)["cursor_seq"]
        if new_cursor_event.seq <= old_cursor_seq:
            return
        self.store.append_session_event(
            project_id,
            session_id,
            event_type="context_pruned",
            scope="main",
            round_id=round_id,
            message_id=current_message_id or "",
            payload={
                "agent_scope": "main",
                "old_cursor_seq": old_cursor_seq,
                "new_cursor_seq": new_cursor_event.seq,
                "reason": reason,
                "dropped_estimated_tokens": max(
                    0,
                    usage_before.used_tokens
                    - usage_for_messages(
                        [{"role": "user", "content": str(new_cursor_event.payload.get("text") or "")}],
                        self.settings,
                    ).used_tokens,
                ),
                "first_visible_message_role": "user",
            },
        )

    def _fit_messages_to_budget(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        usage = usage_for_messages(messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens:
            return messages

        context_message = messages[0:1]
        body = messages[1:]
        current = body[-1:] if body and body[-1].get("role") == "user" else []
        history = body[: -len(current)] if current else body
        keep_messages = max(0, self.settings.context_recent_full_rounds * 2)
        trimmed_history = history[-keep_messages:] if keep_messages else []
        fitted = [*context_message, *trimmed_history, *current]
        fitted_usage = usage_for_messages(fitted, self.settings)
        if fitted_usage.used_tokens <= usage.threshold_tokens:
            return fitted

        # 兜底：从历史窗口前部继续移动 cursor，保证第一条业务消息是 user，当前输入保留。
        while trimmed_history and fitted_usage.used_tokens > usage.threshold_tokens:
            trimmed_history = trimmed_history[1:]
            while trimmed_history and trimmed_history[0].get("role") != "user":
                trimmed_history = trimmed_history[1:]
            fitted = [*context_message, *trimmed_history, *current]
            fitted_usage = usage_for_messages(fitted, self.settings)
        return fitted

    def _collect_section_state(self, sections: list[dict[str, Any]], filled: list[str], empty: list[str]) -> None:
        for section in sections:
            target = filled if section.get("blocks") else empty
            target.append(str(section.get("id") or ""))
            children = section.get("children") or []
            if isinstance(children, list):
                self._collect_section_state(children, filled, empty)
