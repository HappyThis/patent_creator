from __future__ import annotations

import json
from typing import Any

from ...storage.workspace_store import WorkspaceStore
from ...tools.output_storage import (
    TOOL_RESULT_PREVIEW_CHARS,
    TOOL_RESULT_TURN_BUDGET_CHARS,
    head_tail_preview,
    write_tool_output,
)


def apply_tool_result_turn_budget(
    store: WorkspaceStore,
    project_id: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    budgeted: list[dict[str, Any]] | None = None
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            index += 1
            continue
        block_indexes: list[int] = []
        index += 1
        while index < len(messages) and messages[index].get("role") == "tool":
            block_indexes.append(index)
            index += 1
        if not _tool_block_needs_budget(messages, block_indexes):
            continue
        if budgeted is None:
            budgeted = [dict(item) for item in messages]
        _budget_tool_block(store, project_id, budgeted, block_indexes)
    return budgeted if budgeted is not None else messages


def _tool_block_needs_budget(messages: list[dict[str, Any]], block_indexes: list[int]) -> bool:
    if _tool_block_chars(messages, block_indexes) <= TOOL_RESULT_TURN_BUDGET_CHARS:
        return False
    largest_index = max(block_indexes, key=lambda item: _content_len(messages[item]), default=None)
    if largest_index is None:
        return False
    return not _is_processed_tool_result(str(messages[largest_index].get("content") or ""))


def _budget_tool_block(
    store: WorkspaceStore,
    project_id: str,
    messages: list[dict[str, Any]],
    block_indexes: list[int],
) -> None:
    while _tool_block_chars(messages, block_indexes) > TOOL_RESULT_TURN_BUDGET_CHARS:
        largest_index = max(block_indexes, key=lambda item: _content_len(messages[item]), default=None)
        if largest_index is None:
            return
        content = str(messages[largest_index].get("content") or "")
        if _is_processed_tool_result(content):
            return
        path = write_tool_output(
            store,
            project_id,
            content,
            stem="tool_result",
            suffix=".json",
            dedupe_key=content,
        )
        messages[largest_index]["content"] = json.dumps(
            {
                "status": _original_status(content),
                "output": {
                    "tool_result_truncated": True,
                    "tool_result_path": path,
                    "tool_result_chars": len(content),
                    "preview": head_tail_preview(content, TOOL_RESULT_PREVIEW_CHARS),
                    "preview_policy": "head_tail",
                    "preview_hint": "工具结果已因本轮工具输出预算截断；如需完整内容，请调用 file_read 读取 tool_result_path。",
                },
            },
            ensure_ascii=False,
        )


def _tool_block_chars(messages: list[dict[str, Any]], block_indexes: list[int]) -> int:
    return sum(_content_len(messages[index]) for index in block_indexes)


def _content_len(message: dict[str, Any]) -> int:
    return len(str(message.get("content") or ""))


def _original_status(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return "success"
    if isinstance(parsed, dict) and isinstance(parsed.get("status"), str):
        return parsed["status"]
    return "success"


def _is_processed_tool_result(content: str) -> bool:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    return _contains_processed_marker(parsed)


def _contains_processed_marker(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_truncated") and item is True:
                return True
            if key in {"truncated", "tool_result_truncated"} and item is True:
                return True
            if key.endswith("_path") and isinstance(item, str) and item:
                return True
            if key in {"tool_result_path", "preview_policy"}:
                return True
            if _contains_processed_marker(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_processed_marker(item) for item in value)
    return False
