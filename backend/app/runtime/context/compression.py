from __future__ import annotations

import copy
from typing import Any

from ...core import ApiError
from .barrier import render_barrier_message

CompressionWarning = dict[str, Any]


def build_compression_payload(
    *,
    current_user_message: str,
    compressible_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "task": "compress_agent_context_messages",
        "current_user_message": {
            "content": current_user_message,
            "usage": "仅用于判断压缩重点，不得写入 compressed_messages。",
        },
        "rules": [
            "把 compressible_messages 重写成更短的自然对话 transcript。",
            "压缩输出只允许 role=user + content、role=assistant + content、role=assistant + preserved_tool_call_ids。",
            "不要逐轮复盘历史对话；合并重复意图、重复确认、相同结论和已经完成的执行细节。",
            "大幅减少冗余文本，但不要极端压缩；优先保证后续任务连续性和关键信息保真。",
            "覆盖已经出现且对后续有价值的项目/文档状态、用户稳定偏好、已确认技术方案、重要修改、事实核查和纠偏、未决问题、必要工具证据。",
            "role=user 必须像用户自己在说话；role=assistant 必须像 assistant 自己在说话。",
            "role=user 只承载用户的目标、要求、偏好、纠偏和确认；不要把 assistant 已完成的工作写成 user 的话。",
            "role=assistant + content 承载 assistant 已完成的工作、判断、总结和后续注意事项。",
            "必须输出完整合法 JSON；信息较多时用高密度要点合并，避免输出到一半被截断。",
            "不要把压缩结果伪装成当前用户的新指令。",
            "不要引入 compressible_messages 中不存在的信息。",
            "事实核查、版本号、来源、文档读取、代码检查类工具结果如后续可能追溯，优先保留 preserved_tool_call_ids。",
            "如果保留历史工具调用，只输出 preserved_tool_call_ids；不要输出 tool_calls、工具参数、role=tool 或工具结果。",
            "不要输出上下文说明消息；程序会追加 compressed_context barrier。",
        ],
        "compressible_messages": compressible_messages,
        "output_schema": {
            "compressed_messages": [
                {
                    "role": "user|assistant",
                    "content": "自然语言内容；role=user 必填；role=assistant 与 preserved_tool_call_ids 二选一",
                    "preserved_tool_call_ids": ["role=assistant 保留工具调用时使用"],
                }
            ],
            "warnings": ["string"],
        },
    }


def prepare_compressed_messages_for_storage(
    raw_messages: Any,
    *,
    source_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compressed, _warnings = prepare_compressed_messages_with_warnings(
        raw_messages,
        source_messages=source_messages,
    )
    return compressed


def prepare_compressed_messages_with_warnings(
    raw_messages: Any,
    *,
    source_messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[CompressionWarning]]:
    warnings: list[CompressionWarning] = []
    compressed = validate_compressed_messages(raw_messages, source_messages=source_messages, warnings=warnings)
    compressed.append(render_barrier_message({"kind": "compressed_context"}))
    return compressed, warnings


def validate_compressed_messages(
    raw_messages: Any,
    *,
    source_messages: list[dict[str, Any]],
    warnings: list[CompressionWarning] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ApiError(502, "context_compression_invalid_output", "compressed_messages 必须是非空数组。")

    source_tool_blocks = _source_tool_blocks_from_messages(source_messages)
    messages: list[dict[str, Any]] = []

    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            raise ApiError(502, "context_compression_invalid_output", f"compressed_messages[{index}] 必须是对象。")
        role = raw.get("role")
        if role not in {"user", "assistant"}:
            raise ApiError(502, "context_compression_invalid_output", f"compressed_messages[{index}].role 不合法。")
        if "tool_calls" in raw or "tool_call_id" in raw:
            raise ApiError(502, "context_compression_invalid_output", f"compressed_messages[{index}] 不能输出工具调用结构。")

        if role == "user":
            if "preserved_tool_call_ids" in raw:
                raise ApiError(502, "context_compression_invalid_output", f"compressed_messages[{index}] user 不能保留工具调用。")
            content = _required_content(raw, index)
            messages.append({"role": "user", "content": content})
            continue

        has_content = "content" in raw
        has_preserved = "preserved_tool_call_ids" in raw
        if has_content == has_preserved:
            raise ApiError(502, "context_compression_invalid_output", f"compressed_messages[{index}] assistant 必须在 content 和 preserved_tool_call_ids 中二选一。")
        if has_content:
            messages.append({"role": "assistant", "content": _required_content(raw, index)})
            continue

        requested_ids = _preserved_tool_call_ids(raw.get("preserved_tool_call_ids"), index)
        preserved_ids = [call_id for call_id in requested_ids if call_id in source_tool_blocks]
        dropped_ids = [call_id for call_id in requested_ids if call_id not in source_tool_blocks]
        if dropped_ids and warnings is not None:
            warnings.append(
                {
                    "code": "dropped_unavailable_tool_calls",
                    "message": "压缩输出引用了不存在或未闭合的工具调用，已丢弃。",
                    "message_index": index,
                    "tool_call_ids": dropped_ids,
                }
            )
        if preserved_ids:
            messages.append({"role": "assistant", "preserved_tool_call_ids": preserved_ids})
    return messages


def restore_compressed_messages_from_messages(
    compressed_messages: list[dict[str, Any]],
    *,
    source_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _restore_compressed_messages(
        compressed_messages,
        source_tool_blocks=_source_tool_blocks_from_messages(source_messages),
    )


def restore_compressed_messages_from_events(
    compressed_messages: list[dict[str, Any]],
    *,
    source_tool_blocks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return _restore_compressed_messages(compressed_messages, source_tool_blocks=source_tool_blocks)


def _restore_compressed_messages(
    compressed_messages: list[dict[str, Any]],
    *,
    source_tool_blocks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, message in enumerate(compressed_messages):
        role = message.get("role")
        if role not in {"user", "assistant"} or "tool_calls" in message or "tool_call_id" in message:
            raise ApiError(
                500,
                "context_compression_invalid_stored_message",
                f"compressed_messages[{index}] 不是合法的压缩 transcript 消息。",
            )
        if role == "assistant" and "preserved_tool_call_ids" in message:
            if "content" in message:
                raise ApiError(
                    500,
                    "context_compression_invalid_stored_message",
                    f"compressed_messages[{index}] 不是合法的压缩 transcript 消息。",
                )
            call_ids = _preserved_tool_call_ids(message.get("preserved_tool_call_ids"), -1)
            tool_calls: list[dict[str, Any]] = []
            tool_messages: list[dict[str, Any]] = []
            for call_id in call_ids:
                block = source_tool_blocks.get(call_id)
                if block is None:
                    raise ApiError(500, "context_compression_missing_tool_result", f"找不到压缩工具调用：{call_id}")
                tool_calls.append(copy.deepcopy(block["tool_call"]))
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": str(block["tool_result"]),
                    }
                )
            messages.append(_restored_assistant_tool_message(tool_calls, [source_tool_blocks[call_id] for call_id in call_ids]))
            messages.extend(tool_messages)
            continue
        if "preserved_tool_call_ids" in message:
            raise ApiError(
                500,
                "context_compression_invalid_stored_message",
                f"compressed_messages[{index}] 不是合法的压缩 transcript 消息。",
            )
        _required_content(message, index)
        messages.append(copy.deepcopy(message))
    return messages


def _source_tool_blocks_from_messages(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            index += 1
            continue

        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            index += 1
            continue
        calls = [call for call in raw_calls if isinstance(call, dict) and call.get("id")]
        if len(calls) != len(raw_calls):
            index += 1
            continue

        following = messages[index + 1 : index + 1 + len(calls)]
        if len(following) != len(calls) or any(item.get("role") != "tool" for item in following):
            index += 1
            continue
        results = {str(item.get("tool_call_id") or ""): str(item.get("content") or "") for item in following}
        expected_ids = [str(call["id"]) for call in calls]
        if set(results) != set(expected_ids):
            index += 1
            continue
        for call in calls:
            call_id = str(call["id"])
            blocks[call_id] = {
                "tool_call": copy.deepcopy(call),
                "tool_result": results[call_id],
                **_assistant_metadata_for_tool_block(message),
            }
        index += 1 + len(calls)
    return blocks


def _restored_assistant_tool_message(
    tool_calls: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": _common_string(blocks, "assistant_content") or "",
        "tool_calls": tool_calls,
    }
    reasoning_content = _common_string(blocks, "reasoning_content")
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return message


def _assistant_metadata_for_tool_block(message: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"assistant_content": str(message.get("content") or "")}
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        metadata["reasoning_content"] = reasoning_content
    return metadata


def _common_string(blocks: list[dict[str, Any]], key: str) -> str | None:
    values = [block.get(key) for block in blocks]
    if not values or not all(isinstance(value, str) for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values) else None


def _required_content(raw: dict[str, Any], index: int) -> str:
    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError(502, "context_compression_invalid_output", f"compressed_messages[{index}].content 必须是非空字符串。")
    return content


def _preserved_tool_call_ids(value: Any, index: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ApiError(
            502,
            "context_compression_invalid_output",
            f"compressed_messages[{index}].preserved_tool_call_ids 必须是非空数组。",
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ApiError(
                502,
                "context_compression_invalid_output",
                f"compressed_messages[{index}].preserved_tool_call_ids 包含非法项。",
            )
        call_id = item.strip()
        if call_id in seen:
            raise ApiError(
                502,
                "context_compression_invalid_output",
                f"compressed_messages[{index}].preserved_tool_call_ids 包含重复项。",
            )
        seen.add(call_id)
        result.append(call_id)
    return result
