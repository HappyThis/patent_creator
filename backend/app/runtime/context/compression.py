from __future__ import annotations

import json
import re
from typing import Any

from ...core import ApiError
from .barrier import render_barrier_message

REQUIRED_MARKDOWN_HEADINGS = ("## 已确认事实", "## 当前进展", "## 后续注意")
OPTIONAL_MARKDOWN_HEADINGS = ("## 关键片段", "## 待确认问题")
COMPRESSED_MEMORY_PREFIX = "【系统压缩后的历史记忆，不是当前用户的新请求】"


def build_compression_prompt(
    *,
    current_user_message: str,
    compressible_messages: list[dict[str, Any]],
) -> str:
    payload = {
        "current_user_message": {
            "content": current_user_message,
            "usage": "仅用于判断压缩重点，不得写成当前用户的新请求。",
        },
        "compressible_messages": compressible_messages,
    }
    return (
        "请压缩以下上下文。当前用户消息只用于判断压缩重点，不要写成新指令。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def prepare_compressed_markdown_messages(markdown: str) -> list[dict[str, Any]]:
    normalized = validate_compressed_markdown(markdown)
    return [
        {"role": "user", "content": f"{COMPRESSED_MEMORY_PREFIX}\n\n{normalized}"},
        render_barrier_message({"kind": "compressed_context"}),
    ]


def validate_compressed_markdown(markdown: str) -> str:
    normalized = _strip_markdown_fence(markdown).strip()
    if not normalized:
        raise ApiError(502, "context_compression_empty_output", "上下文压缩结果为空。")

    missing = [heading for heading in REQUIRED_MARKDOWN_HEADINGS if heading not in normalized]
    if missing:
        raise ApiError(502, "context_compression_invalid_markdown", f"上下文压缩结果缺少必要标题：{', '.join(missing)}")

    for heading in REQUIRED_MARKDOWN_HEADINGS:
        body = _section_body(normalized, heading)
        if not body:
            raise ApiError(502, "context_compression_invalid_markdown", f"上下文压缩标题下内容为空：{heading}")

    return normalized


def fallback_compressed_markdown(reason: str) -> str:
    detail = reason.strip() or "压缩模型未生成合格 Markdown 记忆。"
    return (
        "## 已确认事实\n\n"
        f"- 历史上下文已触发压缩，但自动压缩结果未通过弱校验：{detail}\n\n"
        "## 当前进展\n\n"
        "- 系统将保留最近若干轮可见上下文继续执行。\n\n"
        "## 后续注意\n\n"
        "- 如继续执行时信息不足，应重新读取必要文档、代码或运行状态。"
    )


def _strip_markdown_fence(markdown: str) -> str:
    text = str(markdown or "").strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(?P<body>.*)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group("body").strip()
    return text


def _section_body(markdown: str, heading: str) -> str:
    start = markdown.find(heading)
    if start < 0:
        return ""
    body_start = start + len(heading)
    next_positions = [
        pos
        for candidate in (*REQUIRED_MARKDOWN_HEADINGS, *OPTIONAL_MARKDOWN_HEADINGS)
        if candidate != heading
        for pos in [markdown.find(candidate, body_start)]
        if pos >= 0
    ]
    body_end = min(next_positions) if next_positions else len(markdown)
    return markdown[body_start:body_end].strip()
