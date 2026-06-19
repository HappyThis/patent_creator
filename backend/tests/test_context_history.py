from __future__ import annotations

import json

from app.runtime.context.barrier import COMPRESSED_CONTEXT_MESSAGE
from app.runtime.context.compression import COMPRESSED_MEMORY_PREFIX
from app.runtime.context.history import (
    main_context_events_after_latest_summary,
    project_main_event_segments,
    restore_main_chat_messages,
)
from app.schemas import SessionEvent


VALID_MARKDOWN = """## 当前任务

- 用户要求优化上下文压缩，不加入专利文档状态快照。

## 执行进度

- 压缩模型输出会被剥离 analysis，只保存 summary。

## 已完成事项

- 已确定程序负责包装压缩 message 和边界。

## 关键事实与证据

- 暂无。

## 待办与下一步

- 不做工具结果轻量化/投影。

## 风险与约束

- 不做格式检测，只剥离 analysis 并 trim。"""


def test_restore_main_chat_messages_injects_compressed_markdown_memory() -> None:
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="user_input",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"text": "先读取目录。"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="tool_call",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read_outline",
            payload={"tool": "disclosure_outline", "arguments": {}},
        ),
        SessionEvent(
            id="evt_3",
            ts="2026-05-09T00:00:02Z",
            type="tool_result",
            seq=3,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read_outline",
            payload={"tool": "disclosure_outline", "status": "success", "output": {"sections": ["技术方案"]}},
        ),
        SessionEvent(
            id="evt_4",
            ts="2026-05-09T00:00:03Z",
            type="context_summary",
            seq=4,
            scope="main",
            round_id="round_2",
            message_id="msg_2",
            payload={
                "cursor_seq_after": 4,
                "compressed_markdown": VALID_MARKDOWN,
            },
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="继续完善", current_message_id="msg_2")

    assert messages[0] == {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN}"}
    assert messages[1] == {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}
    assert not any(message.get("role") == "tool" for message in messages)
    assert messages[-1] == {"role": "user", "content": "继续完善"}


def test_restore_main_chat_messages_does_not_duplicate_current_user_after_tool_result() -> None:
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_read",
                "type": "function",
                "function": {
                    "name": "disclosure_outline",
                    "arguments": json.dumps({}, ensure_ascii=False),
                },
            }
        ],
    }
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="user_input",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"text": "读取目录。"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="agent_message",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"message": assistant_message},
        ),
        SessionEvent(
            id="evt_3",
            ts="2026-05-09T00:00:02Z",
            type="tool_result",
            seq=3,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read",
            payload={"tool": "disclosure_outline", "status": "success"},
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="读取目录。", current_message_id="msg_1")

    assert [message.get("role") for message in messages] == ["user", "assistant", "tool"]
    assert sum(1 for message in messages if message == {"role": "user", "content": "读取目录。"}) == 1


def test_restore_main_chat_messages_consumes_iterable_after_latest_summary() -> None:
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="user_input",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"text": "旧输入，不应恢复。"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="context_summary",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"cursor_seq_after": 2, "compressed_markdown": VALID_MARKDOWN},
        ),
        SessionEvent(
            id="evt_3",
            ts="2026-05-09T00:00:02Z",
            type="user_input",
            seq=3,
            scope="main",
            round_id="round_2",
            message_id="msg_2",
            payload={"text": "新输入。"},
        ),
    ]

    messages = restore_main_chat_messages((event for event in events))

    assert messages[0]["content"] == f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN}"
    assert {"role": "user", "content": "旧输入，不应恢复。"} not in messages
    assert messages[-1] == {"role": "user", "content": "新输入。"}


def test_restore_main_chat_messages_uses_saved_agent_message_reasoning() -> None:
    assistant_message = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "先读取目录。",
        "tool_calls": [
            {
                "id": "call_read_outline",
                "type": "function",
                "function": {
                    "name": "disclosure_outline",
                    "arguments": json.dumps({}, ensure_ascii=False),
                },
            }
        ],
    }
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="agent_message",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"message": assistant_message},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="tool_result",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read_outline",
            payload={"tool": "disclosure_outline", "status": "success"},
        ),
    ]

    messages = restore_main_chat_messages(events)

    assert messages[0]["reasoning_content"] == "先读取目录。"


def test_project_main_event_segments_keeps_assistant_tool_call_and_result_together() -> None:
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_read",
                "type": "function",
                "function": {
                    "name": "disclosure_outline",
                    "arguments": json.dumps({}, ensure_ascii=False),
                },
            }
        ],
    }
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="user_input",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"text": "读取目录。"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="agent_message",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"message": assistant_message},
        ),
        SessionEvent(
            id="evt_3",
            ts="2026-05-09T00:00:02Z",
            type="tool_call",
            seq=3,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read",
            payload={"tool": "disclosure_outline", "arguments": {}},
        ),
        SessionEvent(
            id="evt_4",
            ts="2026-05-09T00:00:03Z",
            type="tool_result",
            seq=4,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
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
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_read",
                "type": "function",
                "function": {
                    "name": "disclosure_outline",
                    "arguments": json.dumps({}, ensure_ascii=False),
                },
            }
        ],
    }
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="user_input",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"text": "读取目录。"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="agent_message",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"message": assistant_message},
        ),
    ]

    segments = project_main_event_segments(events)

    assert len(segments) == 1
    assert segments[0].end_seq == 1


def test_main_context_events_after_latest_summary_resets_candidates() -> None:
    events = [
        SessionEvent(
            id="evt_1",
            ts="2026-05-09T00:00:00Z",
            type="user_input",
            seq=1,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={"text": "old"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="context_summary",
            seq=2,
            scope="main",
            round_id="round_2",
            message_id="msg_2",
            payload={"cursor_seq_after": 2, "compressed_markdown": "first"},
        ),
        SessionEvent(
            id="evt_3",
            ts="2026-05-09T00:00:02Z",
            type="user_input",
            seq=3,
            scope="main",
            round_id="round_3",
            message_id="msg_3",
            payload={"text": "also old"},
        ),
        SessionEvent(
            id="evt_4",
            ts="2026-05-09T00:00:03Z",
            type="context_summary",
            seq=4,
            scope="main",
            round_id="round_4",
            message_id="msg_4",
            payload={"cursor_seq_after": 4, "compressed_markdown": "latest"},
        ),
        SessionEvent(
            id="evt_5",
            ts="2026-05-09T00:00:04Z",
            type="user_input",
            seq=5,
            scope="main",
            round_id="round_5",
            message_id="msg_5",
            payload={"text": "new"},
        ),
    ]

    marker, candidates = main_context_events_after_latest_summary(iter(events))

    assert marker == {"cursor_seq": 4, "compressed_markdown": "latest"}
    assert [event.seq for event in candidates] == [5]
