from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..core import now_iso


@dataclass(slots=True)
class RoundState:
    session_id: str
    message_id: str
    round_id: str


DEFAULT_CHANGED_PAYLOAD: dict[str, Any] = {
    "changed": False,
    "changed_section_ids": [],
    "changed_block_ids": [],
    "primary_section_id": None,
    "primary_block_id": None,
    "change_scope": None,
    "active_section_id": None,
    "active_block_id": None,
}


def assistant_message_text(assistant_message: dict[str, Any] | None) -> str:
    if not isinstance(assistant_message, dict):
        return ""
    content = assistant_message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts).strip()


def format_sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_commit_message(changed_payload: dict[str, Any]) -> str:
    sections = changed_payload.get("changed_section_ids", [])[:10]
    blocks = changed_payload.get("changed_block_ids", [])[:10]
    lines = ["update disclosure", "", f"Time: {now_iso()}", "", "Changed sections:"]
    lines.extend(f"- {section_id}" for section_id in sections)
    lines.append("")
    lines.append("Changed blocks:")
    lines.extend(f"- {block_id}" for block_id in blocks)
    return "\n".join(lines)
