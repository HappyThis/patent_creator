from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ...core import Settings


@dataclass(slots=True)
class ContextUsage:
    max_tokens: int
    used_tokens: int
    used_ratio: float
    threshold_tokens: int
    reserved_output_tokens: int
    status: str

    def model_dump(self) -> dict[str, int | float | str]:
        return {
            "max_tokens": self.max_tokens,
            "used_tokens": self.used_tokens,
            "used_ratio": self.used_ratio,
            "threshold_tokens": self.threshold_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "status": self.status,
        }


def estimate_messages_tokens(messages: list[dict[str, Any]], *, char_coefficient: float = 0.5) -> int:
    if not messages:
        return 0
    chars = sum(_message_estimated_chars(message) for message in messages)
    if chars <= 0:
        return 1
    return max(1, math.ceil(chars * max(char_coefficient, 0.01)))


def usage_for_messages(messages: list[dict[str, Any]], settings: Settings) -> ContextUsage:
    used_tokens = token_count_with_estimation(
        messages,
        char_coefficient=settings.context_token_char_coefficient,
    )
    max_tokens = settings.context_max_tokens
    threshold_tokens = max(
        1,
        int(max_tokens * settings.context_compress_threshold_ratio),
    )
    used_ratio = used_tokens / max_tokens if max_tokens > 0 else 0
    status = "over_limit" if used_tokens > threshold_tokens else "ok"
    return ContextUsage(
        max_tokens=max_tokens,
        used_tokens=used_tokens,
        used_ratio=round(used_ratio, 4),
        threshold_tokens=threshold_tokens,
        reserved_output_tokens=settings.context_reserved_output_tokens,
        status=status,
    )


def token_count_with_estimation(messages: list[dict[str, Any]], *, char_coefficient: float = 0.5) -> int:
    latest_usage_index = -1
    latest_usage_tokens = 0
    for index, message in enumerate(messages):
        usage_tokens = _usage_total_tokens(message.get("usage"))
        if usage_tokens > 0:
            latest_usage_index = index
            latest_usage_tokens = usage_tokens

    if latest_usage_index < 0:
        return estimate_messages_tokens(messages, char_coefficient=char_coefficient)

    tail = messages[latest_usage_index + 1 :]
    return latest_usage_tokens + estimate_messages_tokens(tail, char_coefficient=char_coefficient)


def _usage_total_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "total"):
        value = usage.get(key)
        if isinstance(value, int) and value > 0:
            return value
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return max(0, prompt + completion)
    return 0


def _message_estimated_chars(message: dict[str, Any]) -> int:
    total = _value_chars(message.get("content"))
    total += _value_chars(message.get("reasoning_content"))
    total += _value_chars(message.get("tool_calls"))
    total += _value_chars(message.get("tool_call_id"))
    return total


def _value_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (int, float, bool)):
        return len(str(value))
    if isinstance(value, list):
        return sum(_value_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_value_chars(key) + _value_chars(item) for key, item in value.items())
    return len(str(value))
