from __future__ import annotations

import json

import pytest

from app.core.errors import ApiError
from app.runtime.context.barrier import COMPRESSED_CONTEXT_MESSAGE
from app.runtime.context.compression import (
    COMPRESSED_MEMORY_PREFIX,
    fallback_compressed_markdown,
    prepare_compressed_markdown_messages,
    validate_compressed_markdown,
)
from app.runtime.context.history import restore_main_chat_messages
from app.schemas import SessionEvent


VALID_MARKDOWN = """## 已确认事实

- 用户要求使用 Markdown 轻结构压缩上下文。

## 当前进展

- 已确定程序负责包装压缩 message 和边界。

## 后续注意

- 不要再要求模型输出 JSON。"""


def test_compressed_markdown_wraps_memory_and_barrier() -> None:
    messages = prepare_compressed_markdown_messages(VALID_MARKDOWN)

    assert messages == [
        {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN}"},
        {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE},
    ]


def test_compressed_markdown_strips_markdown_fence() -> None:
    wrapped = f"```markdown\n{VALID_MARKDOWN}\n```"

    assert validate_compressed_markdown(wrapped) == VALID_MARKDOWN


@pytest.mark.parametrize(
    "markdown",
    [
        "",
        "## 已确认事实\n\n- 只有一个标题。",
        "## 已确认事实\n\n- A\n\n## 当前进展\n\n\n## 后续注意\n\n- C",
        json.dumps({"compressed_" + "messages": [{"role": "user", "content": "旧格式"}]}),
    ],
)
def test_compressed_markdown_rejects_missing_required_sections(markdown: str) -> None:
    with pytest.raises(ApiError):
        validate_compressed_markdown(markdown)


def test_fallback_compressed_markdown_is_valid() -> None:
    markdown = fallback_compressed_markdown("模型输出缺少必要标题。")

    assert validate_compressed_markdown(markdown) == markdown
    assert "模型输出缺少必要标题" in markdown


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
                "compressed_markdown": VALID_MARKDOWN,
            },
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="继续完善", current_message_id="msg_2")

    assert messages[0] == {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN}"}
    assert messages[1] == {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}
    assert not any(message.get("role") == "tool" for message in messages)
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
            payload={"tool": "document_read", "status": "success"},
        ),
    ]

    messages = restore_main_chat_messages(events)

    assert messages[0]["reasoning_content"] == "先读取目录。"
