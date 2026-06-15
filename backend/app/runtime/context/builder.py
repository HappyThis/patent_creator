from __future__ import annotations

import copy
import logging
from typing import Any, Awaitable, Callable, Protocol

from ...agents.runtime.model_profiles import prepare_messages_for_model_request
from ...core import Settings
from ...storage.workspace_store import WorkspaceStore
from .barrier import COMPRESSED_CONTEXT_MESSAGE
from .compression import (
    COMPRESSED_MEMORY_PREFIX,
    extract_compressed_summary,
    prepare_compressed_markdown_messages,
)
from .history import (
    MAIN_CONTEXT_EVENT_TYPES,
    latest_context_summary_marker,
    project_main_event_segments,
    restore_main_chat_messages,
)
from .prompts import context_compression_user_prompt
from .tool_budget import apply_tool_result_turn_budget
from .usage import ContextUsage, estimate_messages_tokens, usage_for_messages

logger = logging.getLogger("patent_creator.context")

ContextEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


class SupportsContextCompression(Protocol):
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> str:
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

        messages = self._build_main_agent_messages_raw(
            project_id,
            session_id,
            user_message=user_message,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            current_message_id=current_message_id,
        )
        messages = apply_tool_result_turn_budget(self.store, project_id, messages)
        return prepare_messages_for_model_request(self._emergency_trim_messages(messages), self.settings)

    def _build_main_agent_messages_raw(
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
        system_prompt: str,
        llm_client: SupportsContextCompression,
        on_context_event: ContextEventSink | None = None,
    ) -> list[dict[str, Any]]:
        """恢复主 agent messages；每次模型调用前必要时滚动压缩当前窗口。"""

        raw_messages = self._build_main_agent_messages_raw(
            project_id,
            session_id,
            user_message=user_message,
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            current_message_id=current_message_id,
        )
        raw_messages = apply_tool_result_turn_budget(self.store, project_id, raw_messages)
        request_messages = prepare_messages_for_model_request(raw_messages, self.settings)
        usage = usage_for_messages(raw_messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens:
            return request_messages

        if not session_id or not self.store.session_exists(project_id, session_id):
            logger.info(
                "context compression skipped scope=main reason=no_session project_id=%s session_id=%s used_tokens=%s threshold_tokens=%s",
                project_id,
                session_id,
                usage.used_tokens,
                usage.threshold_tokens,
            )
            trimmed = self._emergency_trim_messages(raw_messages)
            return prepare_messages_for_model_request(trimmed, self.settings)

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
                    "summary": "上下文正在滚动压缩",
                },
            )
        try:
            compressed = await self._compress_main_history(
                project_id,
                session_id,
                current_message_id=current_message_id,
                round_id=round_id,
                system_prompt=system_prompt,
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
            compressed = None

        if compressed:
            raw_messages = self._build_main_agent_messages_raw(
                project_id,
                session_id,
                user_message=user_message,
                active_section_id=active_section_id,
                active_block_id=active_block_id,
                current_message_id=current_message_id,
            )
            raw_messages = apply_tool_result_turn_budget(self.store, project_id, raw_messages)
            usage = usage_for_messages(raw_messages, self.settings)
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
                return prepare_messages_for_model_request(raw_messages, self.settings)

            logger.warning(
                "context compression completed but emergency trim is still needed scope=main project_id=%s session_id=%s round_id=%s message_id=%s used_tokens=%s threshold_tokens=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                usage.used_tokens,
                usage.threshold_tokens,
            )
            trimmed = self._emergency_trim_messages(raw_messages)
            if on_context_event is not None:
                await on_context_event(
                    "context_emergency_trim_applied",
                    {
                        "scope": "main",
                        "used_tokens": usage.used_tokens,
                        "threshold_tokens": usage.threshold_tokens,
                        "summary": "上下文压缩后仍超限，已应用最终兜底裁剪",
                    },
                )
            return prepare_messages_for_model_request(trimmed, self.settings)

        logger.warning(
            "context emergency trim triggered scope=main project_id=%s session_id=%s round_id=%s message_id=%s reason=compression_unavailable used_tokens=%s threshold_tokens=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            usage.used_tokens,
            usage.threshold_tokens,
        )
        if on_context_event is not None:
            await on_context_event(
                "context_emergency_trim_applied",
                {
                    "scope": "main",
                    "used_tokens": usage.used_tokens,
                    "threshold_tokens": usage.threshold_tokens,
                    "summary": "上下文压缩不可用，已应用最终兜底裁剪",
                },
            )
        trimmed = self._emergency_trim_messages(raw_messages)
        return prepare_messages_for_model_request(trimmed, self.settings)

    def context_usage(self, project_id: str, session_id: str | None) -> ContextUsage | None:
        if not session_id or not self.store.session_exists(project_id, session_id):
            return None
        messages = self._restore_main_chat_messages(project_id, session_id)
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
        messages = restore_main_chat_messages(
            events,
            current_user_message=current_user_message,
            current_message_id=current_message_id,
        )
        return messages

    async def _compress_main_history(
        self,
        project_id: str,
        session_id: str,
        *,
        current_message_id: str | None,
        round_id: str,
        system_prompt: str,
        llm_client: SupportsContextCompression,
        usage_before: ContextUsage,
    ) -> dict[str, Any] | None:
        events = self.store.read_session_events(project_id, session_id)
        compression_marker = latest_context_summary_marker(events)
        candidate_events = [
            event
            for event in events
            if event.scope == "main"
            and event.type in MAIN_CONTEXT_EVENT_TYPES
            and event.seq >= compression_marker["cursor_seq"]
        ]
        segments = project_main_event_segments(candidate_events)
        if not segments:
            logger.info(
                "context compression skipped scope=main reason=no_complete_segment project_id=%s session_id=%s round_id=%s message_id=%s candidate_events=%s cursor_seq=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                len(candidate_events),
                compression_marker["cursor_seq"],
            )
            return None
        latest_candidate_seq = candidate_events[-1].seq
        if segments[-1].end_seq < latest_candidate_seq:
            logger.info(
                "context compression skipped scope=main reason=latest_event_not_complete_segment project_id=%s session_id=%s round_id=%s message_id=%s covered_seq_end=%s latest_candidate_seq=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                segments[-1].end_seq,
                latest_candidate_seq,
            )
            return None

        source_messages: list[dict[str, Any]] = []
        for segment in segments:
            source_messages.extend(segment.messages)
        source_messages = apply_tool_result_turn_budget(self.store, project_id, source_messages)
        if not source_messages:
            logger.info(
                "context compression skipped scope=main reason=insufficient_messages project_id=%s session_id=%s round_id=%s message_id=%s candidate_events=%s source_messages=%s",
                project_id,
                session_id,
                round_id,
                current_message_id,
                len(candidate_events),
                len(source_messages),
            )
            return None
        covered_seq_start = segments[0].start_seq
        covered_seq_end = segments[-1].end_seq
        source_estimated_tokens = estimate_messages_tokens(
            source_messages,
            char_coefficient=self.settings.context_token_char_coefficient,
        )
        logger.info(
            "context compression started scope=main project_id=%s session_id=%s round_id=%s message_id=%s covered_seq_start=%s covered_seq_end=%s candidate_events=%s complete_segments=%s source_messages=%s estimated_tokens_before=%s source_estimated_tokens=%s model=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            covered_seq_start,
            covered_seq_end,
            len(candidate_events),
            len(segments),
            len(source_messages),
            usage_before.used_tokens,
            source_estimated_tokens,
            self.settings.openai_model,
        )
        compression_messages: list[dict[str, Any]] = []
        previous_markdown = str(compression_marker.get("compressed_markdown") or "").strip()
        if previous_markdown:
            compression_messages.extend(prepare_compressed_markdown_messages(previous_markdown))
        compression_messages.extend(_strip_reasoning_content(source_messages))
        raw_markdown = await llm_client.generate_text(
            system_prompt=system_prompt,
            messages=compression_messages,
            user_prompt=context_compression_user_prompt(),
            temperature=0.1,
            timeout=self.settings.context_compression_timeout,
            trace_context={
                "scope": "context_compression",
                "agent_scope": "main",
                "project_id": project_id,
                "session_id": session_id,
                "round_id": round_id,
                "message_id": current_message_id,
                "covered_seq_start": covered_seq_start,
                "covered_seq_end": covered_seq_end,
            },
        )
        compressed_markdown = extract_compressed_summary(raw_markdown)
        compressed_memory_messages = prepare_compressed_markdown_messages(compressed_markdown)
        estimated_messages = compressed_memory_messages
        estimated_tokens_after = estimate_messages_tokens(
            estimated_messages,
            char_coefficient=self.settings.context_token_char_coefficient,
        )
        self.store.append_session_event(
            project_id,
            session_id,
            event_type="context_summary",
            scope="main",
            round_id=round_id,
            message_id=current_message_id or "",
            payload={
                "agent_scope": "main",
                "covered_seq_start": covered_seq_start,
                "covered_seq_end": covered_seq_end,
                "compressed_markdown": compressed_markdown,
                "estimated_tokens_before": usage_before.used_tokens,
                "estimated_tokens_after": estimated_tokens_after,
                "compression_model": self.settings.openai_model,
                "compression_mode": "rolling_markdown_memory",
                "cursor_seq_after": covered_seq_end + 1,
                "warnings": [],
            },
        )
        logger.info(
            "context compression completed scope=main project_id=%s session_id=%s round_id=%s message_id=%s covered_seq_start=%s covered_seq_end=%s compressed_chars=%s estimated_tokens_before=%s estimated_tokens_after=%s warnings=%s cursor_seq_after=%s",
            project_id,
            session_id,
            round_id,
            current_message_id,
            covered_seq_start,
            covered_seq_end,
            len(compressed_markdown),
            usage_before.used_tokens,
            estimated_tokens_after,
            0,
            covered_seq_end + 1,
        )
        return {
            "covered_seq_start": covered_seq_start,
            "covered_seq_end": covered_seq_end,
            "compressed_chars": len(compressed_markdown),
            "estimated_tokens_before": usage_before.used_tokens,
            "estimated_tokens_after": estimated_tokens_after,
            "cursor_seq_after": covered_seq_end + 1,
        }

    def _emergency_trim_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        usage = usage_for_messages(messages, self.settings)
        if usage.used_tokens <= usage.threshold_tokens:
            return messages

        protected_prefix, tail = _split_compressed_prefix(messages)
        tail_segments = _message_segments(tail)
        if not tail_segments:
            return protected_prefix or messages[-1:]

        target_tokens = max(1, usage.used_tokens // 2)
        prefix_tokens = estimate_messages_tokens(
            protected_prefix,
            char_coefficient=self.settings.context_token_char_coefficient,
        )
        tail_budget = max(1, target_tokens - prefix_tokens)
        selected_start = _emergency_trim_start_index(
            tail_segments,
            tail_budget,
            char_coefficient=self.settings.context_token_char_coefficient,
        )
        selected = tail_segments[selected_start:]

        trimmed_tail = [message for segment in selected for message in segment]
        trimmed = [*protected_prefix, *trimmed_tail]
        logger.warning(
            "context emergency trim applied messages_before=%s messages_after=%s used_tokens=%s threshold_tokens=%s target_tokens=%s selected_start=%s",
            len(messages),
            len(trimmed),
            usage.used_tokens,
            usage.threshold_tokens,
            target_tokens,
            selected_start,
        )
        return trimmed


def _strip_reasoning_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped = copy.deepcopy(messages)
    for message in stripped:
        message.pop("usage", None)
        if message.get("role") == "assistant":
            message.pop("reasoning_content", None)
    return stripped


def _split_compressed_prefix(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(messages) >= 2:
        first = str(messages[0].get("content") or "")
        second = str(messages[1].get("content") or "")
        if (
            messages[0].get("role") == "user"
            and messages[1].get("role") == "user"
            and first.startswith(COMPRESSED_MEMORY_PREFIX)
            and second == COMPRESSED_CONTEXT_MESSAGE
        ):
            return messages[:2], messages[2:]
    return [], messages


def _message_segments(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    segments: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        raw_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(raw_calls, list) and raw_calls:
            call_ids = [str(call.get("id") or "") for call in raw_calls if isinstance(call, dict) and call.get("id")]
            segment = [message]
            index += 1
            seen: set[str] = set()
            while index < len(messages) and messages[index].get("role") == "tool":
                tool_message = messages[index]
                tool_call_id = str(tool_message.get("tool_call_id") or "")
                if tool_call_id in call_ids:
                    seen.add(tool_call_id)
                    segment.append(tool_message)
                    index += 1
                    continue
                break
            segments.append(segment)
            continue
        segments.append([message])
        index += 1
    return segments


def _emergency_trim_start_index(
    segments: list[list[dict[str, Any]]],
    token_budget: int,
    *,
    char_coefficient: float,
) -> int:
    user_starts = [
        index
        for index, segment in enumerate(segments)
        if segment and segment[0].get("role") == "user"
    ]
    for index in user_starts:
        suffix = [message for segment in segments[index:] for message in segment]
        suffix_tokens = estimate_messages_tokens(suffix, char_coefficient=char_coefficient)
        if suffix_tokens <= token_budget:
            return index
    if user_starts:
        return user_starts[-1]

    selected_tokens = 0
    for index in range(len(segments) - 1, -1, -1):
        segment_tokens = estimate_messages_tokens(segments[index], char_coefficient=char_coefficient)
        if index < len(segments) - 1 and selected_tokens + segment_tokens > token_budget:
            return index + 1
        selected_tokens += segment_tokens
    return 0
