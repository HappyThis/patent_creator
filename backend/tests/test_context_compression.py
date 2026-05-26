from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.runtime.model_profiles import prepare_messages_for_model_request
from app.core.config import Settings
from app.core.errors import ApiError
from app.runtime.context.barrier import COMPRESSED_CONTEXT_MESSAGE
from app.runtime.context.compression import (
    COMPRESSED_MEMORY_PREFIX,
    extract_compressed_summary,
    prepare_compressed_markdown_messages,
)
from app.runtime.context.history import project_main_event_segments, restore_main_chat_messages
from app.runtime.context.usage import estimate_messages_tokens, token_count_with_estimation
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


def test_compressed_markdown_wraps_memory_and_barrier() -> None:
    messages = prepare_compressed_markdown_messages(VALID_MARKDOWN)

    assert messages == [
        {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{VALID_MARKDOWN}"},
        {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE},
    ]


def test_extract_compressed_summary_uses_summary_tag() -> None:
    raw = f"<analysis>这里是不会保存的分析。</analysis>\n<summary>\n{VALID_MARKDOWN}\n</summary>"

    assert extract_compressed_summary(raw) == VALID_MARKDOWN


def test_extract_compressed_summary_removes_analysis_without_summary_tag() -> None:
    raw = f"<analysis>这里是不会保存的分析。</analysis>\n{VALID_MARKDOWN}"

    assert extract_compressed_summary(raw) == VALID_MARKDOWN


def test_extract_compressed_summary_strips_markdown_fence() -> None:
    wrapped = f"```markdown\n<summary>\n{VALID_MARKDOWN}\n</summary>\n```"

    assert extract_compressed_summary(wrapped) == VALID_MARKDOWN


def test_extract_compressed_summary_rejects_empty_response() -> None:
    with pytest.raises(ApiError):
        extract_compressed_summary("")


@pytest.mark.parametrize(
    "raw_output",
    [
        "<analysis>只有分析，没有 summary。</analysis>",
        "<summary>   </summary>",
    ],
)
def test_extract_compressed_summary_preserves_non_empty_output_when_stripped_empty(raw_output: str) -> None:
    assert extract_compressed_summary(raw_output) == raw_output


def test_usage_estimate_prefers_latest_usage_plus_tail() -> None:
    messages = [
        {"role": "user", "content": "旧消息不会重新按长度估算"},
        {"role": "assistant", "content": "已处理", "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}},
        {"role": "user", "content": "abcd"},
    ]

    assert estimate_messages_tokens([{"role": "user", "content": "abcd"}], char_coefficient=0.5) == 2
    assert token_count_with_estimation(messages, char_coefficient=0.5) == 102


def test_prepare_messages_for_request_strips_usage_metadata(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, git_user_name="Test User", git_user_email="test@example.com")

    prepared = prepare_messages_for_model_request(
        [{"role": "assistant", "content": "上一轮", "reasoning_content": "不回放", "usage": {"total_tokens": 100}}],
        settings,
    )

    assert prepared == [{"role": "assistant", "content": "上一轮"}]


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


def test_restore_main_chat_messages_does_not_duplicate_current_user_after_tool_result() -> None:
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_read",
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
            payload={"tool": "document_read", "status": "success"},
        ),
    ]

    messages = restore_main_chat_messages(events, current_user_message="读取目录。", current_message_id="msg_1")

    assert [message.get("role") for message in messages] == ["user", "assistant", "tool"]
    assert sum(1 for message in messages if message == {"role": "user", "content": "读取目录。"}) == 1


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


def test_project_main_event_segments_keeps_assistant_tool_call_and_result_together() -> None:
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_read",
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
            call_id="call_read",
            payload={"tool": "document_read", "status": "success"},
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
