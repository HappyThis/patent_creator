from __future__ import annotations

from typing import Any

from ...schemas import SessionEvent


def restore_main_chat_messages(
    events: list[SessionEvent],
    *,
    current_user_message: str | None = None,
    current_message_id: str | None = None,
) -> list[dict[str, str]]:
    anchor = context_anchor(events)
    round_order: list[str] = []
    rounds: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.scope != "main":
            continue
        if event.seq < anchor["cursor_seq"]:
            continue
        if event.type not in {"user_input", "agent_output"}:
            continue
        if event.round_id not in rounds:
            rounds[event.round_id] = {"user": None, "assistant": None}
            round_order.append(event.round_id)
        if event.type == "user_input":
            rounds[event.round_id]["user"] = event
        elif event.type == "agent_output":
            rounds[event.round_id]["assistant"] = event

    messages: list[dict[str, str]] = []
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
    for round_id in round_order:
        item = rounds[round_id]
        user_event = item.get("user")
        assistant_event = item.get("assistant")
        if isinstance(user_event, SessionEvent):
            messages.append({"role": "user", "content": str(user_event.payload.get("text") or "")})
        if isinstance(assistant_event, SessionEvent):
            messages.append({"role": "assistant", "content": str(assistant_event.payload.get("text") or "")})

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
        return {"cursor_seq": 1, "summary": None}
    if marker.type == "context_summary":
        cursor_seq = int(marker.payload.get("cursor_seq_after") or marker.payload.get("covered_seq_end") or 0) + (
            0 if marker.payload.get("cursor_seq_after") else 1
        )
        return {"cursor_seq": max(1, cursor_seq), "summary": str(marker.payload.get("summary") or "")}
    return {"cursor_seq": max(1, int(marker.payload.get("new_cursor_seq") or 1)), "summary": None}


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
    return {
        "seq": event.seq,
        "role": "user" if event.type == "user_input" else "assistant",
        "round_id": event.round_id,
        "content": str(event.payload.get("text") or ""),
    }
