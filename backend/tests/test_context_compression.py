from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.runtime.message_preparation import prepare_messages_for_model_request
from app.core.config import Settings
from app.core.errors import ApiError
from app.runtime.context import ContextManager
from app.runtime.context.barrier import COMPRESSED_CONTEXT_MESSAGE
from app.runtime.context.compression import (
    COMPRESSED_MEMORY_PREFIX,
    extract_compressed_summary,
    prepare_compressed_markdown_messages,
)
from app.runtime.context.tool_budget import apply_tool_result_turn_budget
from app.runtime.context.usage import estimate_messages_tokens, token_count_with_estimation, usage_for_messages
from app.storage.workspace_store import WorkspaceStore


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


def test_compression_threshold_uses_ratio_of_max_tokens(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, git_user_name="Test User", git_user_email="test@example.com")
    settings.context_max_tokens = 1000
    settings.context_compress_threshold_ratio = 0.8

    usage = usage_for_messages([{"role": "user", "content": "abcd"}], settings)

    assert usage.threshold_tokens == 800


def test_prepare_messages_for_request_strips_usage_metadata(tmp_path: Path) -> None:
    prepared = prepare_messages_for_model_request(
        [{"role": "assistant", "content": "上一轮", "usage": {"total_tokens": 100}}],
    )

    assert prepared == [{"role": "assistant", "content": "上一轮"}]


def test_prepare_messages_for_request_reuses_messages_when_no_metadata_is_stripped(tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "继续"}]

    prepared = prepare_messages_for_model_request(messages)

    assert prepared is messages


def test_context_manager_persists_raw_tool_result_when_turn_budget_exceeded(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project("一种图像检测方法")
    session_id = "sess_budget"
    round_id = "round_budget"
    message_id = "msg_budget"

    store.append_session_event(
        project.project_id,
        session_id,
        event_type="agent_message",
        scope="main",
        round_id=round_id,
        message_id=message_id,
        payload={
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_big",
                        "type": "function",
                        "function": {"name": "disclosure_outline", "arguments": "{}"},
                    }
                ],
            },
            "model": "test",
        },
    )
    store.append_session_event(
        project.project_id,
        session_id,
        event_type="tool_result",
        scope="main",
        round_id=round_id,
        message_id=message_id,
        call_id="call_big",
        payload={"tool": "disclosure_outline", "status": "success", "output": {"text": "x" * 180000}},
    )

    manager = ContextManager(
        store,
        Settings(data_dir=tmp_path / "data", git_user_name="Test User", git_user_email="test@example.com"),
    )
    messages = manager.build_main_agent_messages(
        project.project_id,
        session_id,
        user_message="继续",
    )

    tool_message = next(message for message in messages if message.get("role") == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["output"]["tool_result_truncated"] is True
    persisted_path = store.project_dir(project.project_id) / payload["output"]["tool_result_path"]
    assert persisted_path.exists()
    assert '"text": "xxx' in persisted_path.read_text(encoding="utf-8")

    messages_again = manager.build_main_agent_messages(
        project.project_id,
        session_id,
        user_message="继续",
    )
    payload_again = json.loads(next(message for message in messages_again if message.get("role") == "tool")["content"])
    assert payload_again["output"]["tool_result_path"] == payload["output"]["tool_result_path"]


def test_tool_result_turn_budget_reuses_messages_when_no_truncation_is_needed(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project("一种图像检测方法")
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        {
            "role": "tool",
            "tool_call_id": "call_small",
            "content": json.dumps({"status": "success", "output": {"text": "small"}}, ensure_ascii=False),
        },
    ]

    budgeted = apply_tool_result_turn_budget(store, project.project_id, messages)

    assert budgeted is messages


def test_tool_result_turn_budget_stops_when_largest_result_is_already_processed(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "data", "Test User", "test@example.com")
    project = store.create_project("一种图像检测方法")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_processed", "type": "function", "function": {"name": "exec_command", "arguments": "{}"}},
                {"id": "call_raw", "type": "function", "function": {"name": "disclosure_outline", "arguments": "{}"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_processed",
            "content": json.dumps(
                {
                    "status": "success",
                    "output": {"stdout": "x" * 170000, "stdout_truncated": True, "stdout_path": "runtime/out.txt"},
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "tool",
            "tool_call_id": "call_raw",
            "content": json.dumps({"status": "success", "output": {"text": "y" * 20000}}, ensure_ascii=False),
        },
    ]

    budgeted = apply_tool_result_turn_budget(store, project.project_id, messages)

    raw_payload = json.loads(budgeted[2]["content"])
    assert "tool_result_truncated" not in raw_payload["output"]

