from __future__ import annotations

import copy
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

from ...agents.runtime.model_profiles import prepare_messages_for_model_request
from ...core import Settings
from ...storage.workspace_store import WorkspaceStore
from .compression import (
    build_compression_payload,
    prepare_compressed_messages_with_warnings,
    restore_compressed_messages_from_messages,
)
from .history import MAIN_CONTEXT_EVENT_TYPES, context_anchor, current_user_event, project_main_events, restore_main_chat_messages
from .prompts import context_compressor_system_prompt
from .usage import ContextUsage, estimate_messages_tokens, usage_for_messages

logger = logging.getLogger("patent_creator.context")

ContextEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


class SupportsContextCompression(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        ...


class ContextManager:
    def __init__(self, store: WorkspaceStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

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

        messages = self._build_main_agent_messages_unfitted(
            project_id,
            session_id,
            user_message=user_message,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            current_message_id=current_message_id,
        )
        messages = prepare_messages_for_model_request(messages, self.settings)
        return self._fit_messages_to_budget(messages)

    def _build_main_agent_messages_unfitted(
        self,
        project_id: str,
        session_id: str | None,
        *,
        user_message: str,
        active_section_id: str | None,
        active_block_id: str | None,
        current_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        history = self._restore_main_chat_messages(
            project_id,
            session_id,
            current_user_message=user_message,
            current_message_id=current_message_id,
        )
        return history

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
        on_context_event: ContextEventSink | None = None,
    ) -> list[dict[str, Any]]:
        """恢复主 agent messages；必要时先压缩当前用户输入之前的历史。"""

        messages = self._build_main_agent_messages_unfitted(
            project_id,
            session_id,
            user_message=user_message,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            current_message_id=current_message_id,
        )
        messages = prepare_messages_for_model_request(messages, self.settings)
        usage = usage_for_messages(messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens:
            return messages

        if not session_id or not self.store.session_exists(project_id, session_id):
            logger.info(
                "context compression skipped scope=main reason=no_session project_id=%s session_id=%s used_tokens=%s threshold_tokens=%s",
                project_id,
                session_id,
                usage.used_tokens,
                usage.threshold_tokens,
            )
            return self._fit_messages_to_budget(messages)

        logger.info(
            "context compression triggered scope=main project_id=%s session_id=%s round_id=%s message_id=%s used_tokens=%s threshold_tokens=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            usage.used_tokens,
            usage.threshold_tokens,
        )
        if on_context_event is not None:
            await on_context_event(
                "context_compression_started",
                {
                    "scope": "main",
                    "used_tokens": usage.used_tokens,
                    "threshold_tokens": usage.threshold_tokens,
                    "summary": "上下文正在压缩",
                },
            )
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
            logger.exception(
                "context compression failed scope=main project_id=%s session_id=%s round_id=%s message_id=%s used_tokens=%s threshold_tokens=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                usage.used_tokens,
                usage.threshold_tokens,
            )
            if on_context_event is not None:
                await on_context_event(
                    "context_compression_failed",
                    {
                        "scope": "main",
                        "used_tokens": usage.used_tokens,
                        "threshold_tokens": usage.threshold_tokens,
                        "summary": "上下文压缩失败",
                    },
                )
            compressed = False

        if compressed:
            messages = self._build_main_agent_messages_unfitted(
                project_id,
                session_id,
                user_message=user_message,
                active_section_id=active_section_id,
                active_block_id=active_block_id,
                current_message_id=current_message_id,
            )
            messages = prepare_messages_for_model_request(messages, self.settings)
            usage = usage_for_messages(messages, self.settings)
            if usage.used_tokens <= usage.threshold_tokens:
                logger.info(
                    "context compression accepted scope=main project_id=%s session_id=%s round_id=%s message_id=%s used_tokens=%s threshold_tokens=%s",
                    project_id,
                    session_id,
                    round_id,
                    current_message_id,
                    usage.used_tokens,
                    usage.threshold_tokens,
                )
                if on_context_event is not None:
                    await on_context_event(
                        "context_compression_completed",
                        {
                            "scope": "main",
                            "used_tokens": usage.used_tokens,
                            "threshold_tokens": usage.threshold_tokens,
                            "summary": "上下文压缩已完成",
                            **compressed,
                        },
                    )
                return self._fit_messages_to_budget(messages)

            logger.warning(
                "context compression still over limit scope=main project_id=%s session_id=%s round_id=%s message_id=%s used_tokens=%s threshold_tokens=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                usage.used_tokens,
                usage.threshold_tokens,
            )

        logger.warning(
            "context prune fallback triggered scope=main project_id=%s session_id=%s round_id=%s message_id=%s reason=compression_failed_or_still_over_limit used_tokens=%s threshold_tokens=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            usage.used_tokens,
            usage.threshold_tokens,
        )
        if on_context_event is not None:
            await on_context_event(
                "context_compression_failed",
                {
                    "scope": "main",
                    "used_tokens": usage.used_tokens,
                    "threshold_tokens": usage.threshold_tokens,
                    "summary": "上下文压缩失败",
                },
            )
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
        messages = self._restore_main_chat_messages(project_id, session_id)
        messages = prepare_messages_for_model_request(messages, self.settings)
        return usage_for_messages(messages, self.settings)

    def _restore_main_chat_messages(
        self,
        project_id: str,
        session_id: str | None,
        *,
        current_user_message: str | None = None,
        current_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
    ) -> dict[str, Any] | None:
        events = self.store.read_session_events(project_id, session_id)
        current_event = current_user_event(events, current_message_id)
        if current_event is None:
            logger.info(
                "context compression skipped scope=main reason=current_event_missing project_id=%s session_id=%s round_id=%s message_id=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
            )
            return None

        anchor = context_anchor(events)
        compressible = [
            event
            for event in events
            if event.scope == "main"
            and event.type in MAIN_CONTEXT_EVENT_TYPES
            and anchor["cursor_seq"] <= event.seq < current_event.seq
        ]
        if len(compressible) < 2:
            logger.info(
                "context compression skipped scope=main reason=insufficient_events project_id=%s session_id=%s round_id=%s message_id=%s compressible_events=%s cursor_seq=%s current_seq=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                len(compressible),
                anchor["cursor_seq"],
                current_event.seq,
            )
            return None

        source_messages = project_main_events(compressible)
        if len(source_messages) < 2:
            logger.info(
                "context compression skipped scope=main reason=insufficient_messages project_id=%s session_id=%s round_id=%s message_id=%s compressible_events=%s source_messages=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                len(compressible),
                len(source_messages),
            )
            return None
        source_estimated_tokens = estimate_messages_tokens(source_messages)
        logger.info(
            "context compression started scope=main project_id=%s session_id=%s round_id=%s message_id=%s covered_seq_start=%s covered_seq_end=%s compressible_events=%s source_messages=%s estimated_tokens_before=%s source_estimated_tokens=%s model=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            compressible[0].seq,
            compressible[-1].seq,
            len(compressible),
            len(source_messages),
            usage_before.used_tokens,
            source_estimated_tokens,
            self.settings.openai_model,
        )
        payload = build_compression_payload(
            current_user_message=str(current_event.payload.get("text") or ""),
            compressible_messages=_strip_reasoning_content(source_messages),
        )
        result = await llm_client.generate_json(
            system_prompt=context_compressor_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0.1,
            timeout=self.settings.context_compression_timeout,
        )
        compressed_messages, compression_warnings = prepare_compressed_messages_with_warnings(
            result.get("compressed_messages"),
            source_messages=source_messages,
        )
        estimated_messages = restore_compressed_messages_from_messages(
            compressed_messages,
            source_messages=source_messages,
        )
        warnings = _combined_compression_warnings(result.get("warnings"), compression_warnings)
        estimated_tokens_after = estimate_messages_tokens(estimated_messages)
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
                "compressed_messages": compressed_messages,
                "estimated_tokens_before": usage_before.used_tokens,
                "estimated_tokens_after": estimated_tokens_after,
                "compression_model": self.settings.openai_model,
                "cursor_seq_after": compressible[-1].seq + 1,
                "warnings": warnings,
            },
        )
        logger.info(
            "context compression completed scope=main project_id=%s session_id=%s round_id=%s message_id=%s covered_seq_start=%s covered_seq_end=%s compressed_messages=%s estimated_tokens_before=%s estimated_tokens_after=%s warnings=%s cursor_seq_after=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            compressible[0].seq,
            compressible[-1].seq,
            len(compressed_messages),
            usage_before.used_tokens,
            estimated_tokens_after,
            len(warnings),
            compressible[-1].seq + 1,
        )
        return {
            "covered_seq_start": compressible[0].seq,
            "covered_seq_end": compressible[-1].seq,
            "compressed_message_count": len(compressed_messages),
            "estimated_tokens_before": usage_before.used_tokens,
            "estimated_tokens_after": estimated_tokens_after,
            "cursor_seq_after": compressible[-1].seq + 1,
        }

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
            logger.warning(
                "context prune skipped scope=main reason=current_event_missing project_id=%s session_id=%s round_id=%s message_id=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
            )
            return

        visible = [
            event
            for event in events
            if event.scope == "main" and event.type in {"user_input", "agent_message", "agent_output"} and event.seq <= current_event.seq
        ]
        user_events = [event for event in visible if event.type == "user_input"]
        if not user_events:
            logger.warning(
                "context prune skipped scope=main reason=no_user_events project_id=%s session_id=%s round_id=%s message_id=%s visible_events=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                len(visible),
            )
            return
        keep_users = max(1, self.settings.context_recent_full_rounds)
        new_cursor_event = user_events[-keep_users] if len(user_events) > keep_users else user_events[0]
        old_cursor_seq = context_anchor(events)["cursor_seq"]
        if new_cursor_event.seq <= old_cursor_seq:
            logger.warning(
                "context prune skipped scope=main reason=cursor_not_advanced project_id=%s session_id=%s round_id=%s message_id=%s old_cursor_seq=%s new_cursor_seq=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                old_cursor_seq,
                new_cursor_event.seq,
            )
            return
        dropped_estimated_tokens = max(
            0,
            usage_before.used_tokens
            - usage_for_messages(
                [{"role": "user", "content": str(new_cursor_event.payload.get("text") or "")}],
                self.settings,
            ).used_tokens,
        )
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
                "dropped_estimated_tokens": dropped_estimated_tokens,
                "first_visible_message_role": "user",
            },
        )
        logger.warning(
            "context pruned scope=main project_id=%s session_id=%s round_id=%s message_id=%s reason=%s old_cursor_seq=%s new_cursor_seq=%s dropped_estimated_tokens=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            reason,
            old_cursor_seq,
            new_cursor_event.seq,
            dropped_estimated_tokens,
        )

    def _fit_messages_to_budget(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        usage = usage_for_messages(messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens:
            return messages

        current = messages[-1:] if messages and messages[-1].get("role") == "user" else []
        history = messages[: -len(current)] if current else messages
        trimmed_history = _recent_history_from_user_boundary(history, max(1, self.settings.context_recent_full_rounds))
        fitted = [*trimmed_history, *current]
        fitted_usage = usage_for_messages(fitted, self.settings)
        if fitted_usage.used_tokens <= usage.threshold_tokens:
            return fitted

        # 兜底：从历史窗口前部继续移动 cursor，保证第一条业务消息是 user，当前输入保留。
        while trimmed_history and fitted_usage.used_tokens > usage.threshold_tokens:
            trimmed_history = trimmed_history[1:]
            while trimmed_history and trimmed_history[0].get("role") != "user":
                trimmed_history = trimmed_history[1:]
            fitted = [*trimmed_history, *current]
            fitted_usage = usage_for_messages(fitted, self.settings)
        return fitted


def _recent_history_from_user_boundary(history: list[dict[str, Any]], keep_user_messages: int) -> list[dict[str, Any]]:
    user_indexes = [index for index, message in enumerate(history) if message.get("role") == "user"]
    if not user_indexes:
        return []
    start = user_indexes[-keep_user_messages] if len(user_indexes) >= keep_user_messages else user_indexes[0]
    return history[start:]


def _strip_reasoning_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped = copy.deepcopy(messages)
    for message in stripped:
        if message.get("role") == "assistant":
            message.pop("reasoning_content", None)
    return stripped


def _combined_compression_warnings(raw_warnings: Any, generated_warnings: list[dict[str, Any]]) -> list[Any]:
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    return [*warnings, *generated_warnings]
