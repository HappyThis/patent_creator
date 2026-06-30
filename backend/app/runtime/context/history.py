from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from ...schemas import SessionEvent
from .compression import prepare_compressed_markdown_messages

MAIN_CONTEXT_EVENT_TYPES = {
    "user_input",
    "agent_message",
    "agent_output",
    "tool_call",
    "tool_result",
    "technical_solution_enhancement_feedback",
}

INTERRUPTED_OUTPUT_CONTEXT_NOTE = "【系统注记：上一条 assistant 输出因模型流式连接中断，内容可能不完整。】"


@dataclass(frozen=True, slots=True)
class MessageSegment:
    """A complete OpenAI message segment.

    The messages inside a segment can be moved, compressed, or retained as a unit
    without splitting assistant tool calls from their tool results.
    """

    start_seq: int
    end_seq: int
    messages: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ContextEventScan:
    marker: dict[str, Any]
    events: list[SessionEvent]
    current_message_exists: bool


def restore_main_chat_messages(
    events: Iterable[SessionEvent],
    *,
    current_user_message: str | None = None,
    current_message_id: str | None = None,
) -> list[dict[str, Any]]:
    scan = _scan_main_context_events_after_latest_summary(events, current_message_id=current_message_id)

    messages: list[dict[str, Any]] = []
    if scan.marker["compressed_markdown"]:
        messages.extend(prepare_compressed_markdown_messages(scan.marker["compressed_markdown"]))

    messages.extend(project_main_events(scan.events))

    if current_user_message is not None:
        if scan.current_message_exists:
            return messages
        if current_message_id:
            messages.append({"role": "user", "content": current_user_message})
        elif not messages or messages[-1] != {"role": "user", "content": current_user_message}:
            messages.append({"role": "user", "content": current_user_message})
    return messages


def main_context_events_after_latest_summary(events: Iterable[SessionEvent]) -> tuple[dict[str, Any], list[SessionEvent]]:
    scan = _scan_main_context_events_after_latest_summary(events)
    return scan.marker, scan.events


def _scan_main_context_events_after_latest_summary(
    events: Iterable[SessionEvent],
    *,
    current_message_id: str | None = None,
) -> ContextEventScan:
    all_events = list(events)
    marker: dict[str, Any] = {"cursor_seq": 1, "compressed_markdown": ""}
    summary_events = [
        event
        for event in all_events
        if event.scope == "main" and event.type == "context_summary"
    ]
    latest_summary_event: SessionEvent | None = None
    if summary_events:
        latest_summary_event = max(summary_events, key=lambda event: event.seq)
        marker = _context_summary_marker_from_event(latest_summary_event)

    candidate_events = [
        event
        for event in all_events
        if event.scope == "main"
        and event.seq >= marker["cursor_seq"]
        and event.type in MAIN_CONTEXT_EVENT_TYPES
    ]
    current_message_exists = any(
        current_message_id
        and event.message_id == current_message_id
        and event.type == "user_input"
        for event in candidate_events
    ) or bool(
        current_message_id
        and latest_summary_event is not None
        and latest_summary_event.message_id == current_message_id
    )
    return ContextEventScan(marker=marker, events=candidate_events, current_message_exists=current_message_exists)


def _context_summary_marker_from_event(marker: SessionEvent) -> dict[str, Any]:
    cursor_seq = int(marker.payload.get("cursor_seq_after") or marker.payload.get("covered_seq_end") or 0) + (
        0 if marker.payload.get("cursor_seq_after") else 1
    )
    compressed_markdown = marker.payload.get("compressed_markdown")
    return {
        "cursor_seq": max(1, cursor_seq),
        "compressed_markdown": compressed_markdown if isinstance(compressed_markdown, str) else "",
    }


def project_main_events(events: list[SessionEvent]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for segment in project_main_event_segments(events):
        messages.extend(segment.messages)
    return messages


def project_main_event_segments(events: list[SessionEvent]) -> list[MessageSegment]:
    segments: list[MessageSegment] = []
    index = 0
    while index < len(events):
        event = events[index]
        if event.type in {"user_input", "technical_solution_enhancement_feedback"}:
            segments.append(
                MessageSegment(
                    start_seq=event.seq,
                    end_seq=event.seq,
                    messages=[{"role": "user", "content": str(event.payload.get("text") or "")}],
                )
            )
            index += 1
            continue

        if event.type == "agent_message":
            segment, index = _consume_agent_message_segment(events, index)
            if segment is None:
                break
            segments.append(segment)
            continue

        if event.type == "agent_output":
            if _is_failed_agent_output(event):
                index += 1
                continue
            preamble = str(event.payload.get("text") or "")
            if _is_interrupted_agent_output(event):
                preamble = _append_interrupted_context_note(preamble)
            next_event = events[index + 1] if index + 1 < len(events) else None
            if next_event is not None and next_event.type in {"tool_call", "tool_result"}:
                segment, index = _consume_tool_block_segment(events, index + 1, assistant_content=preamble)
                if segment is None:
                    break
                segments.append(
                    MessageSegment(
                        start_seq=event.seq,
                        end_seq=segment.end_seq,
                        messages=segment.messages,
                    )
                )
                continue
            segments.append(
                MessageSegment(
                    start_seq=event.seq,
                    end_seq=event.seq,
                    messages=[{"role": "assistant", "content": preamble}],
                )
            )
            index += 1
            continue

        if event.type == "tool_call":
            segment, index = _consume_tool_block_segment(events, index, assistant_content="")
            if segment is None:
                break
            segments.append(segment)
            continue

        index += 1
    return segments


def _consume_agent_message_segment(
    events: list[SessionEvent],
    start_index: int,
) -> tuple[MessageSegment | None, int]:
    event = events[start_index]
    message = _agent_message(event)
    if message is None:
        return None, start_index + 1

    messages = [message]
    index = start_index + 1
    end_seq = event.seq
    output_interrupted = False
    while (
        index < len(events)
        and events[index].type == "agent_output"
        and events[index].round_id == event.round_id
        and events[index].message_id == event.message_id
    ):
        output_interrupted = output_interrupted or _is_interrupted_agent_output(events[index])
        end_seq = events[index].seq
        index += 1

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        if output_interrupted:
            messages[0] = _assistant_message_with_interrupted_note(message)
        return MessageSegment(event.seq, end_seq, messages), index

    call_ids = {str(call.get("id") or "") for call in raw_calls if isinstance(call, dict) and call.get("id")}
    result_call_ids: set[str] = set()
    while index < len(events) and events[index].type in {"tool_call", "tool_result"}:
        next_event = events[index]
        if next_event.round_id != event.round_id or next_event.message_id != event.message_id:
            break
        if next_event.type == "tool_result" and next_event.call_id and str(next_event.call_id) in call_ids:
            result_call_ids.add(str(next_event.call_id))
            messages.append(_tool_result_message(next_event))
        end_seq = next_event.seq
        index += 1
    if call_ids and result_call_ids != call_ids:
        return None, start_index
    return MessageSegment(event.seq, end_seq, messages), index


def _agent_message(event: SessionEvent) -> dict[str, Any] | None:
    raw_message = event.payload.get("message")
    if not isinstance(raw_message, dict):
        return None
    role = raw_message.get("role")
    if role != "assistant":
        return None
    message = dict(raw_message)
    content = message.get("content")
    if content is None:
        message["content"] = ""
    elif not isinstance(content, str):
        message["content"] = str(content)
    return message


def _is_interrupted_agent_output(event: SessionEvent) -> bool:
    return event.type == "agent_output" and event.payload.get("status") == "interrupted"


def _is_failed_agent_output(event: SessionEvent) -> bool:
    return event.type == "agent_output" and event.payload.get("status") == "failed"


def _assistant_message_with_interrupted_note(message: dict[str, Any]) -> dict[str, Any]:
    next_message = dict(message)
    next_message["content"] = _append_interrupted_context_note(str(next_message.get("content") or ""))
    return next_message


def _append_interrupted_context_note(content: str) -> str:
    if INTERRUPTED_OUTPUT_CONTEXT_NOTE in content:
        return content
    if not content:
        return INTERRUPTED_OUTPUT_CONTEXT_NOTE
    return f"{content}\n\n{INTERRUPTED_OUTPUT_CONTEXT_NOTE}"


def _consume_tool_block_segment(
    events: list[SessionEvent],
    start_index: int,
    *,
    assistant_content: str,
) -> tuple[MessageSegment | None, int]:
    block: list[SessionEvent] = []
    index = start_index
    while index < len(events) and events[index].type in {"tool_call", "tool_result"}:
        block.append(events[index])
        index += 1

    result_call_ids = {str(event.call_id) for event in block if event.type == "tool_result" and event.call_id}
    calls = [
        event
        for event in block
        if event.type == "tool_call" and event.call_id and str(event.call_id) in result_call_ids
    ]
    if not calls:
        return None, start_index

    call_ids = {str(event.call_id) for event in calls if event.call_id}
    if call_ids != result_call_ids:
        return None, start_index
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": [_assistant_tool_call(event) for event in calls],
        }
    ]
    for event in block:
        if event.type == "tool_result" and event.call_id and str(event.call_id) in call_ids:
            messages.append(_tool_result_message(event))
    return MessageSegment(block[0].seq, block[-1].seq, messages), index


def _assistant_tool_call(event: SessionEvent) -> dict[str, Any]:
    arguments = event.payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return {
        "id": str(event.call_id or event.id),
        "type": "function",
        "function": {
            "name": str(event.payload.get("tool") or ""),
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _tool_result_message(event: SessionEvent) -> dict[str, Any]:
    payload = dict(event.payload)
    tool_name = str(payload.pop("tool", "") or "")
    return {
        "role": "tool",
        "tool_call_id": str(event.call_id or ""),
        "content": json.dumps(payload, ensure_ascii=False),
        "tool_name": tool_name,
        "round_id": event.round_id,
        "message_id": event.message_id,
    }
