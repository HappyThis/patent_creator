from __future__ import annotations

import json
import re
from typing import Any

from ...core import ApiError
from .barrier import render_barrier_message

COMPRESSED_MEMORY_PREFIX = "【系统压缩后的累计工作状态，不是当前用户的新请求】"


def build_compression_prompt(
    *,
    previous_compressed_markdown: str,
    messages_to_merge: list[dict[str, Any]],
) -> str:
    payload = {
        "previous_compressed_markdown": {
            "content": previous_compressed_markdown,
            "usage": "上一轮累计摘要。若为空，表示这是第一次压缩。必须与本次新增上下文合并成新的单一累计摘要。",
        },
        "messages_to_merge": messages_to_merge,
    }
    return (
        "请滚动压缩以下上下文，生成新的累计工作状态。旧摘要和本次新增消息必须合并成一个新的 summary，不要堆叠多个摘要。\n"
        "输出必须先包含 <analysis>...</analysis>，再包含 <summary>...</summary>。\n"
        "<summary> 内写后续 agent 可直接继续执行的 Markdown 状态，必须说明当前任务执行到哪一步。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def prepare_compressed_markdown_messages(markdown: str) -> list[dict[str, Any]]:
    normalized = str(markdown or "").strip()
    if not normalized:
        raise ApiError(502, "context_compression_empty_output", "上下文压缩结果为空。")
    return [
        {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{normalized}"},
        render_barrier_message({"kind": "compressed_context"}),
    ]


def extract_compressed_summary(raw_output: str) -> str:
    """剥离压缩模型的 scratchpad；剥离不到正文时保留非空原文。"""

    normalized = _strip_markdown_fence(raw_output).strip()
    if not normalized:
        raise ApiError(502, "context_compression_empty_output", "上下文压缩结果为空。")

    summary = _tag_body(normalized, "summary")
    if summary is not None:
        extracted = _strip_markdown_fence(summary).strip()
    else:
        extracted = _remove_tag_block(normalized, "analysis")
        extracted = _strip_markdown_fence(extracted).strip()

    if not extracted:
        return normalized
    return extracted


def _strip_markdown_fence(markdown: str) -> str:
    text = str(markdown or "").strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(?P<body>.*)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group("body").strip()
    return text


def _tag_body(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*(?P<body>.*?)\s*</{tag}>", text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    return match.group("body")


def _remove_tag_block(text: str, tag: str) -> str:
    return re.sub(rf"<{tag}>\s*.*?\s*</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
