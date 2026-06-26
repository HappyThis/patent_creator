from __future__ import annotations

import json

from app.runtime.context.barrier import COMPRESSED_CONTEXT_MESSAGE
from app.runtime.context.compression import COMPRESSED_MEMORY_PREFIX
from app.runtime.context.history import (
    main_context_events_after_latest_summary,
    project_main_event_segments,
    restore_main_chat_messages,
)
from app.schemas import SessionEvent, SessionEventType


VALID_MARKDOWN = """## Current Task

- Continue drafting the patent disclosure.

## Progress

- Context has been compacted into rolling memory.

## Next Steps

- Keep using retained facts from the compacted history.
"""


def event(
    *,
    event_id: str,
    seq: int,
    event_type: SessionEventType,
    payload: dict,
    round_id: str = "round_1",
    message_id: str = "msg_1",
    call_id: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        id=event_id,
        ts="2026-05-09T00:00:00Z",
        type=event_type,
        seq=seq,
        scope="main",
        round_id=round_id,
        message_id=message_id,
        call_id=call_id,
        payload=payload,
    )


def assistant_tool_message(call_id: str = "call_read") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "disclosure_outline",
                    "arguments": json.dumps({}, ensure_ascii=False),
                },
            }
        ],
    }


def test_restore_main_chat_messages_injects_compressed_markdown_memory() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "read outline"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="tool_call",
            call_id="call_read_outline",
            payload={"tool": "disclosure_outline", "arguments": {}},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="tool_result",
            call_id="call_read_outline",
            payload={"tool": "disclosure_outline", "status": "success", "output": {"sections": ["field"]}},
        ),
        event(
            event_id="evt_4",
            seq=4,
            event_type="context_summary",
            round_id="round_2",
            message_id="msg_2",
            payload={"cursor_seq_after": 4, "compressed_markdown": VALID_MARKDOWN},
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="continue", current_message_id="msg_2")

    assert messages[0] == {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN.strip()}"}
    assert messages[1] == {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}
    assert not any(message.get("role") == "tool" for message in messages)
    assert {"role": "user", "content": "continue"} not in messages
    assert messages[-1] == {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}


def test_restore_main_chat_messages_does_not_duplicate_current_user_after_tool_result() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "read outline"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="agent_message",
            payload={"message": assistant_tool_message()},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="tool_result",
            call_id="call_read",
            payload={"tool": "disclosure_outline", "status": "success"},
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="read outline", current_message_id="msg_1")

    assert [message.get("role") for message in messages] == ["user", "assistant", "tool"]
    assert sum(1 for message in messages if message == {"role": "user", "content": "read outline"}) == 1


def test_restore_main_chat_messages_consumes_iterable_after_latest_summary() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "old input"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="context_summary",
            payload={"cursor_seq_after": 2, "compressed_markdown": VALID_MARKDOWN},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="user_input",
            round_id="round_2",
            message_id="msg_2",
            payload={"text": "new input"},
        ),
    ]

    messages = restore_main_chat_messages((item for item in events))

    assert messages[0]["content"] == f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN.strip()}"
    assert {"role": "user", "content": "old input"} not in messages
    assert messages[-1] == {"role": "user", "content": "new input"}


def test_project_main_event_segments_keeps_assistant_tool_call_and_result_together() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "read outline"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="agent_message",
            payload={"message": assistant_tool_message("call_read")},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="tool_call",
            call_id="call_read",
            payload={"tool": "disclosure_outline", "arguments": {}},
        ),
        event(
            event_id="evt_4",
            seq=4,
            event_type="tool_result",
            call_id="call_read",
            payload={"tool": "disclosure_outline", "status": "success"},
        ),
    ]

    segments = project_main_event_segments(events)

    assert len(segments) == 2
    assert segments[1].start_seq == 2
    assert segments[1].end_seq == 4
    assert [message["role"] for message in segments[1].messages] == ["assistant", "tool"]


def test_project_main_event_segments_stops_before_incomplete_tool_call() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "read outline"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="agent_message",
            payload={"message": assistant_tool_message("call_read")},
        ),
    ]

    segments = project_main_event_segments(events)

    assert len(segments) == 1
    assert segments[0].end_seq == 1


def test_restore_main_chat_messages_ignores_llm_audit_events() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "research OpenClaw"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="llm_audit",
            payload={
                "category": "web_search",
                "source": "openai_responses",
                "item": {"type": "web_search_call", "status": "completed"},
            },
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="agent_message",
            payload={"message": {"role": "assistant", "content": "OpenClaw is ..."}},
        ),
    ]

    messages = restore_main_chat_messages(events)
    marker, candidates = main_context_events_after_latest_summary(events)

    assert messages == [
        {"role": "user", "content": "research OpenClaw"},
        {"role": "assistant", "content": "OpenClaw is ..."},
    ]
    assert marker == {"cursor_seq": 1, "compressed_markdown": ""}
    assert [item.type for item in candidates] == ["user_input", "agent_message"]


def test_restore_main_chat_messages_ignores_failed_agent_output() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "write"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="agent_output",
            payload={
                "text": "本轮未完成，请重试或补充信息。",
                "status": "failed",
                "message": "模型调用失败。",
            },
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="user_input",
            round_id="round_2",
            message_id="msg_2",
            payload={"text": "重试"},
        ),
    ]

    messages = restore_main_chat_messages(events)

    assert messages == [
        {"role": "user", "content": "write"},
        {"role": "user", "content": "重试"},
    ]


def test_restore_main_chat_messages_keeps_current_user_when_summary_appended_after_it() -> None:
    events = [
        event(
            event_id="evt_1",
            seq=1,
            event_type="user_input",
            payload={"text": "old input"},
            message_id="msg_old",
        ),
        event(
            event_id="evt_2",
            seq=2,
            event_type="user_input",
            round_id="round_2",
            message_id="msg_current",
            payload={"text": "current input"},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="context_summary",
            round_id="round_2",
            message_id="msg_current",
            payload={"cursor_seq_after": 2, "compressed_markdown": VALID_MARKDOWN},
        ),
    ]

    messages = restore_main_chat_messages(
        events,
        current_user_message="current input",
        current_message_id="msg_current",
    )

    assert messages[0]["content"] == f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN.strip()}"
    assert messages[1] == {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}
    assert {"role": "user", "content": "old input"} not in messages
    assert messages[-1] == {"role": "user", "content": "current input"}


def test_restore_main_chat_messages_does_not_reappend_current_user_if_summary_covers_it() -> None:
    events = [
        event(
            event_id="evt_1",
            seq=1,
            event_type="user_input",
            payload={"text": "old input"},
            message_id="msg_old",
        ),
        event(
            event_id="evt_2",
            seq=2,
            event_type="user_input",
            round_id="round_2",
            message_id="msg_current",
            payload={"text": "current input"},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="context_summary",
            round_id="round_2",
            message_id="msg_current",
            payload={"cursor_seq_after": 3, "compressed_markdown": VALID_MARKDOWN},
        ),
    ]

    messages = restore_main_chat_messages(
        events,
        current_user_message="current input",
        current_message_id="msg_current",
    )

    assert {"role": "user", "content": "old input"} not in messages
    assert {"role": "user", "content": "current input"} not in messages
    assert messages[-1] == {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}


def test_main_context_events_after_latest_summary_resets_candidates() -> None:
    events = [
        event(event_id="evt_1", seq=1, event_type="user_input", payload={"text": "old"}),
        event(
            event_id="evt_2",
            seq=2,
            event_type="context_summary",
            round_id="round_2",
            message_id="msg_2",
            payload={"cursor_seq_after": 2, "compressed_markdown": "first"},
        ),
        event(
            event_id="evt_3",
            seq=3,
            event_type="user_input",
            round_id="round_3",
            message_id="msg_3",
            payload={"text": "also old"},
        ),
        event(
            event_id="evt_4",
            seq=4,
            event_type="context_summary",
            round_id="round_4",
            message_id="msg_4",
            payload={"cursor_seq_after": 4, "compressed_markdown": "latest"},
        ),
        event(
            event_id="evt_5",
            seq=5,
            event_type="user_input",
            round_id="round_5",
            message_id="msg_5",
            payload={"text": "new"},
        ),
    ]

    marker, candidates = main_context_events_after_latest_summary(iter(events))

    assert marker == {"cursor_seq": 4, "compressed_markdown": "latest"}
    assert [item.seq for item in candidates] == [5]
