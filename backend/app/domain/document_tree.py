from __future__ import annotations

from typing import Any

from .disclosure import find_section
from .document_tool_results import ToolResult, tool_failed


def get_required_section(disclosure: dict[str, Any], section_id: Any) -> dict[str, Any] | ToolResult:
    if not isinstance(section_id, str):
        return tool_failed("invalid_operation", "operation 需要 section_id。")
    section = find_section(disclosure["sections"], section_id)
    if not section:
        return tool_failed("section_not_found", f"section_id 不存在：{section_id}")
    return section


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
