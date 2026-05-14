from __future__ import annotations

import json

import pytest

from app.core.errors import ApiError
from app.runtime.context.compression import (
    prepare_compressed_messages_with_warnings,
    prepare_compressed_messages_for_storage,
    restore_compressed_messages_from_messages,
)
from app.runtime.context.history import restore_main_chat_messages
from app.schemas import SessionEvent


def _source_messages_with_tool_call() -> list[dict[str, object]]:
    arguments = {"action": "get_outline"}
    return [
        {"role": "user", "content": "先读取目录。"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_read_outline",
                    "type": "function",
                    "function": {
                        "name": "document_read",
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read_outline",
            "content": json.dumps({"status": "success", "output": {"sections": ["技术方案"]}}, ensure_ascii=False),
        },
        {"role": "assistant", "content": "已读取目录。"},
    ]


def test_compressed_messages_store_preserved_tool_ids_and_restore_original_tool_block() -> None:
    source_messages = _source_messages_with_tool_call()
    compressed = prepare_compressed_messages_for_storage(
        [
            {"role": "user", "content": "我让你先读取目录。"},
            {"role": "assistant", "preserved_tool_call_ids": ["call_read_outline"]},
            {"role": "assistant", "content": "已读取目录，后续围绕技术方案继续。"},
        ],
        source_messages=source_messages,
    )

    assert compressed[1] == {"role": "assistant", "preserved_tool_call_ids": ["call_read_outline"]}
    assert "tool_calls" not in compressed[1]
    assert compressed[-1]["content"].startswith("【上下文说明】")

    restored = restore_compressed_messages_from_messages(compressed, source_messages=source_messages)
    restored_tool_call_message = restored[1]
    assert restored_tool_call_message["role"] == "assistant"
    assert restored_tool_call_message["content"] == ""
    assert restored_tool_call_message["tool_calls"][0]["id"] == "call_read_outline"
    assert restored_tool_call_message["tool_calls"][0]["function"]["name"] == "document_read"
    assert restored[2]["role"] == "tool"
    assert restored[2]["tool_call_id"] == "call_read_outline"
    assert json.loads(restored[2]["content"])["status"] == "success"


def test_compressed_messages_restore_preserved_tool_reasoning_content() -> None:
    source_messages = _source_messages_with_tool_call()
    source_messages[1]["reasoning_content"] = "需要读取目录再继续。"

    compressed = prepare_compressed_messages_for_storage(
        [
            {"role": "assistant", "preserved_tool_call_ids": ["call_read_outline"]},
            {"role": "assistant", "content": "已保留工具证据。"},
        ],
        source_messages=source_messages,
    )

    restored = restore_compressed_messages_from_messages(compressed, source_messages=source_messages)

    assert restored[0]["role"] == "assistant"
    assert restored[0]["reasoning_content"] == "需要读取目录再继续。"
    assert restored[0]["tool_calls"][0]["id"] == "call_read_outline"


def test_compressed_messages_drop_unavailable_preserved_tool_ids() -> None:
    compressed, warnings = prepare_compressed_messages_with_warnings(
        [
            {"role": "user", "content": "我让你读取目录。"},
            {"role": "assistant", "preserved_tool_call_ids": ["call_missing", "call_read_outline"]},
            {"role": "assistant", "preserved_tool_call_ids": ["call_missing_only"]},
            {"role": "assistant", "content": "已保留可恢复的工具证据。"},
        ],
        source_messages=_source_messages_with_tool_call(),
    )

    assert {"role": "assistant", "preserved_tool_call_ids": ["call_read_outline"]} in compressed
    assert {"role": "assistant", "preserved_tool_call_ids": ["call_missing_only"]} not in compressed
    assert warnings == [
        {
            "code": "dropped_unavailable_tool_calls",
            "message": "压缩输出引用了不存在或未闭合的工具调用，已丢弃。",
            "message_index": 1,
            "tool_call_ids": ["call_missing"],
        },
        {
            "code": "dropped_unavailable_tool_calls",
            "message": "压缩输出引用了不存在或未闭合的工具调用，已丢弃。",
            "message_index": 2,
            "tool_call_ids": ["call_missing_only"],
        },
    ]


@pytest.mark.parametrize(
    "raw_messages",
    [
        [{"role": "tool", "tool_call_id": "call_read_outline", "content": ""}],
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read_outline",
                        "type": "function",
                        "function": {"name": "document_read", "arguments": "{}"},
                    }
                ],
            }
        ],
        [{"role": "assistant", "content": "已读取目录。", "preserved_tool_call_ids": ["call_read_outline"]}],
        [{"role": "user", "content": "我让你读取目录。", "preserved_tool_call_ids": ["call_read_outline"]}],
    ],
)
def test_compressed_messages_reject_old_tool_shapes(raw_messages: list[dict[str, object]]) -> None:
    with pytest.raises(ApiError):
        prepare_compressed_messages_for_storage(raw_messages, source_messages=_source_messages_with_tool_call())


def test_restore_rejects_stored_old_tool_shapes() -> None:
    with pytest.raises(ApiError):
        restore_compressed_messages_from_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_read_outline",
                            "type": "function",
                            "function": {"name": "document_read", "arguments": "{}"},
                        }
                    ],
                }
            ],
            source_messages=_source_messages_with_tool_call(),
        )


def test_restore_main_chat_messages_expands_preserved_tool_ids_from_session_events() -> None:
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
            payload={"tool": "document_read", "arguments": {"action": "get_outline"}},
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
            payload={"tool": "document_read", "status": "success", "output": {"sections": ["技术方案"]}},
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
                "compressed_messages": [
                    {"role": "assistant", "preserved_tool_call_ids": ["call_read_outline"]},
                    {"role": "user", "content": "上述消息经过系统压缩，后续继续正常对话。"},
                ],
            },
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="继续完善", current_message_id="msg_2")

    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == ""
    assert messages[0]["tool_calls"][0]["id"] == "call_read_outline"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "document_read"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_read_outline"
    assert json.loads(messages[1]["content"])["status"] == "success"
    assert messages[-1] == {"role": "user", "content": "继续完善"}


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
                    "name": "document_read",
                    "arguments": json.dumps({"action": "get_outline"}, ensure_ascii=False),
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
            payload={"text": "先读取目录。"},
        ),
        SessionEvent(
            id="evt_2",
            ts="2026-05-09T00:00:01Z",
            type="agent_message",
            seq=2,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            payload={
                "message": assistant_message,
                "model": "deepseek-reasoner",
                "provider": "deepseek",
                "thinking": "enabled",
            },
        ),
        SessionEvent(
            id="evt_3",
            ts="2026-05-09T00:00:02Z",
            type="tool_call",
            seq=3,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read_outline",
            payload={"tool": "document_read", "arguments": {"action": "get_outline"}},
        ),
        SessionEvent(
            id="evt_4",
            ts="2026-05-09T00:00:03Z",
            type="tool_result",
            seq=4,
            scope="main",
            round_id="round_1",
            message_id="msg_1",
            call_id="call_read_outline",
            payload={"tool": "document_read", "status": "success", "output": {"sections": ["技术方案"]}},
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="继续完善", current_message_id="msg_2")

    assert messages[1]["role"] == "assistant"
    assert messages[1]["reasoning_content"] == "先读取目录。"
    assert messages[1]["tool_calls"][0]["id"] == "call_read_outline"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_read_outline"
