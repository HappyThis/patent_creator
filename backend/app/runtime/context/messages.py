from __future__ import annotations

import copy
from typing import Any


def closed_message_prefix(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the longest prefix whose assistant tool calls all have matching tool results."""

    closed: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")

        if role == "tool":
            break

        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if not tool_calls:
            closed.append(copy.deepcopy(message))
            index += 1
            continue

        if not isinstance(tool_calls, list):
            break
        expected_ids = [str(call.get("id") or "") for call in tool_calls if isinstance(call, dict) and call.get("id")]
        if len(expected_ids) != len(tool_calls):
            break

        block = [copy.deepcopy(message)]
        pending = set(expected_ids)
        cursor = index + 1
        while cursor < len(messages) and pending:
            candidate = messages[cursor]
            if candidate.get("role") != "tool":
                break
            call_id = str(candidate.get("tool_call_id") or "")
            if call_id not in pending:
                break
            pending.remove(call_id)
            block.append(copy.deepcopy(candidate))
            cursor += 1

        if pending:
            break

        closed.extend(block)
        index = cursor

    return closed
