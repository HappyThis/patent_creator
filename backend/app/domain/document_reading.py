from __future__ import annotations

import copy
from typing import Any

from .disclosure import build_outline_items, find_block, find_section
from .document_tool_results import ToolResult, tool_failed, tool_success


def read_document(disclosure: dict[str, Any], arguments: dict[str, Any]) -> ToolResult:
    action = arguments.get("action")
    if action == "get_meta":
        return tool_success({"meta": disclosure["meta"]})
    if action == "get_project_context":
        return tool_success(
            {
                "context": {
                    "kind": "project_context",
                    "document": {
                        "title": disclosure.get("meta", {}).get("title"),
                        "outline": project_context_outline(disclosure["sections"]),
                    },
                }
            }
        )
    if action == "get_outline":
        return tool_success({"sections": [item.model_dump() for item in build_outline_items(disclosure["sections"])]})
    if action == "get_section":
        section_id = arguments.get("section_id")
        if not isinstance(section_id, str):
            return tool_failed("invalid_action", "get_section 需要 section_id。")
        section = find_section(disclosure["sections"], section_id)
        if not section:
            return tool_failed("section_not_found", f"section_id 不存在：{section_id}")
        output_section = copy.deepcopy(section)
        if not arguments.get("include_children", False):
            output_section["children"] = []
        return tool_success({"section": output_section})
    if action == "get_block":
        block_id = arguments.get("block_id")
        if not isinstance(block_id, str):
            return tool_failed("invalid_action", "get_block 需要 block_id。")
        found = find_block(disclosure["sections"], block_id)
        if not found:
            return tool_failed("block_not_found", f"block_id 不存在：{block_id}")
        section, block = found
        return tool_success({"section_id": section["id"], "block": copy.deepcopy(block)})
    if action == "search_blocks":
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            return tool_failed("invalid_action", "search_blocks 需要非空 query。")
        section_id = arguments.get("section_id")
        if section_id is not None and find_section(disclosure["sections"], section_id) is None:
            return tool_failed("section_not_found", f"section_id 不存在：{section_id}")
        return tool_success({"matches": search_blocks(disclosure["sections"], query, section_id)})
    return tool_failed("invalid_action", f"不支持的 document_read action：{action}")

def project_context_outline(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    for section in sections:
        outline.append(
            {
                "id": str(section.get("id") or ""),
                "type": str(section.get("type") or ""),
                "title": str(section.get("title") or ""),
                "children": project_context_outline(section.get("children") or []),
            }
        )
    return outline

def search_blocks(sections: list[dict[str, Any]], query: str, section_id: str | None) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for section in sections:
        if section_id is None or section["id"] == section_id:
            for block in section.get("blocks", []):
                text = block_text(block)
                if query in text:
                    matches.append({"section_id": section["id"], "block_id": block["id"], "text": text})
        matches.extend(search_blocks(section.get("children", []), query, section_id))
    return matches

def block_text(block: dict[str, Any]) -> str:
    if block["type"] == "paragraph":
        return block["text"]
    if block["type"] == "list":
        return "\n".join(block["items"])
    if block["type"] == "image":
        return "\n".join(value for value in [block.get("alt"), block.get("caption")] if value)
    return "\n".join([" ".join(block["columns"]), *[" ".join(row) for row in block["rows"]]])
