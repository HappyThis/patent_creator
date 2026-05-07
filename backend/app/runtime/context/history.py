from __future__ import annotations

import json
from typing import Any

from ...schemas import SessionEvent

MAIN_CONTEXT_EVENT_TYPES = {"user_input", "agent_output", "tool_call", "tool_result"}


def restore_main_chat_messages(
    events: list[SessionEvent],
    *,
    current_user_message: str | None = None,
    current_message_id: str | None = None,
) -> list[dict[str, Any]]:
    anchor = context_anchor(events)
    messages: list[dict[str, Any]] = []
    if anchor["summary"]:
        messages.append(
            {
                "role": "user",
                "content": (
                    "以下是系统从本 session 早期上下文压缩得到的摘要，"
                    "不是用户的新指令，也不是用户原文。\n"
                    f"{anchor['summary']}"
                ),
            }
        )
    messages.extend(_restore_preserved_tool_messages(events, anchor["preserved_tool_result_ids"]))

    visible = [
        event
        for event in events
        if event.scope == "main" and event.seq >= anchor["cursor_seq"] and event.type in MAIN_CONTEXT_EVENT_TYPES
    ]
    messages.extend(_project_main_events(visible))

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
        return {"cursor_seq": 1, "summary": None, "preserved_tool_result_ids": []}
    if marker.type == "context_summary":
        cursor_seq = int(marker.payload.get("cursor_seq_after") or marker.payload.get("covered_seq_end") or 0) + (
            0 if marker.payload.get("cursor_seq_after") else 1
        )
        preserved = marker.payload.get("preserved_tool_result_ids")
        return {
            "cursor_seq": max(1, cursor_seq),
            "summary": str(marker.payload.get("summary") or ""),
            "preserved_tool_result_ids": [str(item) for item in preserved if item] if isinstance(preserved, list) else [],
        }
    return {
        "cursor_seq": max(1, int(marker.payload.get("new_cursor_seq") or 1)),
        "summary": None,
        "preserved_tool_result_ids": [],
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


def compressible_event_payload(event: SessionEvent) -> dict[str, Any]:
    if event.type in {"user_input", "agent_output"}:
        return {
            "seq": event.seq,
            "role": "user" if event.type == "user_input" else "assistant",
            "round_id": event.round_id,
            "content": str(event.payload.get("text") or ""),
        }
    if event.type == "tool_call":
        return {
            "seq": event.seq,
            "role": "assistant_tool_call_ref",
            "round_id": event.round_id,
            "call_id": event.call_id,
            "tool": str(event.payload.get("tool") or ""),
        }
    return {
        "seq": event.seq,
        "role": "tool_result_ref",
        "round_id": event.round_id,
        "call_id": event.call_id,
        "tool": str(event.payload.get("tool") or ""),
        "status": str(event.payload.get("status") or ""),
    }


def _project_main_events(events: list[SessionEvent]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(events):
        event = events[index]
        if event.type == "user_input":
            messages.append({"role": "user", "content": str(event.payload.get("text") or "")})
            index += 1
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


def _restore_preserved_tool_messages(events: list[SessionEvent], call_ids: list[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call_id in call_ids:
        if call_id in seen:
            continue
        seen.add(call_id)
        call_event = next(
            (
                event
                for event in events
                if event.scope == "main" and event.type == "tool_call" and event.call_id == call_id
            ),
            None,
        )
        result_event = next(
            (
                event
                for event in events
                if event.scope == "main" and event.type == "tool_result" and event.call_id == call_id
            ),
            None,
        )
        if call_event is None or result_event is None:
            continue
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_assistant_tool_call(call_event)],
            }
        )
        messages.append(_tool_result_message(result_event))
    return messages


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
