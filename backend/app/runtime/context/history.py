from __future__ import annotations

import json
from typing import Any

from ...schemas import SessionEvent
from .compression import restore_compressed_messages_from_events

MAIN_CONTEXT_EVENT_TYPES = {"user_input", "agent_message", "agent_output", "tool_call", "tool_result"}


def restore_main_chat_messages(
    events: list[SessionEvent],
    *,
    current_user_message: str | None = None,
    current_message_id: str | None = None,
) -> list[dict[str, Any]]:
    anchor = context_anchor(events)
    messages: list[dict[str, Any]] = []
    if anchor["compressed_messages"]:
        messages.extend(
            restore_compressed_messages_from_events(
                anchor["compressed_messages"],
                source_tool_blocks=_main_tool_blocks(events),
            )
        )

    visible = [
        event
        for event in events
        if event.scope == "main" and event.seq >= anchor["cursor_seq"] and event.type in MAIN_CONTEXT_EVENT_TYPES
    ]
    messages.extend(project_main_events(visible))

    if current_user_message is not None:
        if current_message_id and not any(
            event.message_id == current_message_id and event.type == "user_input" for event in events
        ):
            messages.append({"role": "user", "content": current_user_message})
        elif not messages or messages[-1] != {"role": "user", "content": current_user_message}:
            messages.append({"role": "user", "content": current_user_message})
    return messages


def context_anchor(events: list[SessionEvent]) -> dict[str, Any]:
    marker = next(
        (
            event
            for event in reversed(events)
            if event.scope == "main" and event.type in {"context_summary", "context_pruned"}
        ),
        None,
    )
    if marker is None:
        return {"cursor_seq": 1, "compressed_messages": []}
    if marker.type == "context_summary":
        cursor_seq = int(marker.payload.get("cursor_seq_after") or marker.payload.get("covered_seq_end") or 0) + (
            0 if marker.payload.get("cursor_seq_after") else 1
        )
        compressed_messages = marker.payload.get("compressed_messages")
        return {
            "cursor_seq": max(1, cursor_seq),
            "compressed_messages": compressed_messages if isinstance(compressed_messages, list) else [],
        }
    return {
        "cursor_seq": max(1, int(marker.payload.get("new_cursor_seq") or 1)),
        "compressed_messages": [],
    }


def current_user_event(events: list[SessionEvent], current_message_id: str | None) -> SessionEvent | None:
    if current_message_id:
        match = next(
            (
                event
                for event in events
                if event.scope == "main" and event.type == "user_input" and event.message_id == current_message_id
            ),
            None,
        )
        if match is not None:
            return match
    return next(
        (event for event in reversed(events) if event.scope == "main" and event.type == "user_input"),
        None,
    )


def project_main_events(events: list[SessionEvent]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(events):
        event = events[index]
        if event.type == "user_input":
            messages.append({"role": "user", "content": str(event.payload.get("text") or "")})
            index += 1
            continue

        if event.type == "agent_message":
            agent_messages, index = _consume_agent_message(events, index)
            messages.extend(agent_messages)
            continue

        if event.type == "agent_output":
            preamble = str(event.payload.get("text") or "")
            next_event = events[index + 1] if index + 1 < len(events) else None
            if next_event is not None and next_event.type in {"tool_call", "tool_result"}:
                tool_messages, index = _consume_tool_block(events, index + 1, assistant_content=preamble)
                messages.extend(tool_messages)
                continue
            messages.append({"role": "assistant", "content": preamble})
            index += 1
            continue

        if event.type == "tool_call":
            tool_messages, index = _consume_tool_block(events, index, assistant_content="")
            messages.extend(tool_messages)
            continue

        index += 1
    return messages


def _consume_agent_message(events: list[SessionEvent], start_index: int) -> tuple[list[dict[str, Any]], int]:
    event = events[start_index]
    message = _agent_message(event)
    if message is None:
        return [], start_index + 1

    messages = [message]
    index = start_index + 1
    while (
        index < len(events)
        and events[index].type == "agent_output"
        and events[index].round_id == event.round_id
        and events[index].message_id == event.message_id
    ):
        index += 1

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return messages, index

    call_ids = {str(call.get("id") or "") for call in raw_calls if isinstance(call, dict) and call.get("id")}
    while index < len(events) and events[index].type in {"tool_call", "tool_result"}:
        next_event = events[index]
        if next_event.round_id != event.round_id or next_event.message_id != event.message_id:
            break
        if next_event.type == "tool_result" and next_event.call_id and str(next_event.call_id) in call_ids:
            messages.append(_tool_result_message(next_event))
        index += 1
    return messages, index


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


def _consume_tool_block(
    events: list[SessionEvent],
    start_index: int,
    *,
    assistant_content: str,
) -> tuple[list[dict[str, Any]], int]:
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
        return [], index

    call_ids = {str(event.call_id) for event in calls if event.call_id}
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
    return messages, index


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
    payload.pop("tool", None)
    return {
        "role": "tool",
        "tool_call_id": str(event.call_id or ""),
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _main_tool_blocks(events: list[SessionEvent]) -> dict[str, dict[str, Any]]:
    assistant_messages: dict[str, dict[str, Any]] = {}
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, str] = {}
    for event in events:
        if event.scope == "main" and event.type == "agent_message":
            message = _agent_message(event)
            if message is not None:
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        assistant_messages[str(call["id"])] = message
        if event.scope == "main" and event.type == "tool_call" and event.call_id:
            calls[str(event.call_id)] = _assistant_tool_call(event)
        if event.scope == "main" and event.type == "tool_result" and event.call_id:
            results[str(event.call_id)] = _tool_result_message(event)["content"]
    return {
        call_id: {
            "tool_call": calls[call_id],
            "tool_result": results[call_id],
            **_assistant_metadata_for_call(assistant_messages.get(call_id)),
        }
        for call_id in calls.keys() & results.keys()
    }


def _assistant_metadata_for_call(message: dict[str, Any] | None) -> dict[str, Any]:
    if not message:
        return {}
    metadata: dict[str, Any] = {"assistant_content": str(message.get("content") or "")}
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        metadata["reasoning_content"] = reasoning_content
    return metadata
