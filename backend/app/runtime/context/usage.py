from __future__ import annotations

import json
import math
from dataclasses import dataclass

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


def estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return max(1, math.ceil(len(text) / 2))


def usage_for_messages(messages: list[dict[str, str]], settings: Settings) -> ContextUsage:
    used_tokens = estimate_messages_tokens(messages)
    max_tokens = settings.context_max_tokens
    threshold_tokens = max(
        1,
        int((max_tokens - settings.context_reserved_output_tokens) * settings.context_compress_threshold_ratio),
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
