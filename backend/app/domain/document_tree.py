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


def section_depth(sections: list[dict[str, Any]], section_id: str, depth: int = 1) -> int:
    for section in sections:
        if section["id"] == section_id:
            return depth
        child_depth = section_depth(section.get("sections", []), section_id, depth + 1)
        if child_depth:
            return child_depth
    return 0


def collect_block_ids(sections: list[dict[str, Any]]) -> list[str]:
    block_ids: list[str] = []
    for section in sections:
        title = section.get("title")
        if isinstance(title, dict):
            block_ids.append(title["id"])
        block_ids.extend(block["id"] for block in section.get("blocks", []))
        block_ids.extend(collect_block_ids(section.get("sections", [])))
    return block_ids


def collect_section_ids(sections: list[dict[str, Any]]) -> list[str]:
    section_ids: list[str] = []
    for section in sections:
        section_ids.append(section["id"])
        section_ids.extend(collect_section_ids(section.get("sections", [])))
    return section_ids


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
