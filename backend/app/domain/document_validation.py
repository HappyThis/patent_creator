from __future__ import annotations

from typing import Any

from .disclosure import BLOCK_ID_PATTERN, SECTION_ID_PATTERN, SECTION_TYPES
from .document_schema import BLOCK_TYPES
from .document_tool_results import ToolResult, tool_failed


def validate_disclosure(disclosure: dict[str, Any]) -> ToolResult | None:
    if set(disclosure.keys()) != {"meta", "sections"}:
        return tool_failed("schema_validation_failed", "disclosure 顶层只能包含 meta 和 sections。")
    meta = disclosure.get("meta", {})
    if meta.get("schema_version") != "v2":
        return tool_failed("schema_validation_failed", "disclosure.schema_version 必须为 v2。")
    id_counters = meta.get("id_counters")
    if not isinstance(id_counters, dict) or "section" not in id_counters or "block" not in id_counters:
        return tool_failed("schema_validation_failed", "meta.id_counters 必须包含 section 和 block。")
    section_ids: set[str] = set()
    block_ids: set[str] = set()

    def validate_sections(sections: list[dict[str, Any]], depth: int) -> ToolResult | None:
        for section in sections:
            if depth > 2:
                return tool_failed("schema_validation_failed", "v2 不允许超过两级章节。")
            for key in ("id", "type", "title", "blocks", "children"):
                if key not in section:
                    return tool_failed("schema_validation_failed", f"section 缺少 {key} 字段。")
            if not isinstance(section["id"], str) or not SECTION_ID_PATTERN.match(section["id"]):
                return tool_failed("schema_validation_failed", f"section.id 格式错误：{section['id']}")
            if section["type"] not in SECTION_TYPES:
                return tool_failed("schema_validation_failed", f"不支持的 section.type：{section['type']}")
            if depth > 1 and section["type"] != "custom":
                return tool_failed("schema_validation_failed", "子章节 section.type 必须为 custom。")
            if not isinstance(section["title"], str) or not section["title"]:
                return tool_failed("schema_validation_failed", "section 缺少 title。")
            if section["id"] in section_ids:
                return tool_failed("duplicate_section_id", f"section_id 重复：{section['id']}")
            section_ids.add(section["id"])
            if not isinstance(section["blocks"], list):
                return tool_failed("schema_validation_failed", "section.blocks 必须是数组。")
            for block in section["blocks"]:
                block_error = validate_block(block, block_ids)
                if block_error:
                    return block_error
            if not isinstance(section["children"], list):
                return tool_failed("schema_validation_failed", "section.children 必须是数组。")
            child_error = validate_sections(section["children"], depth + 1)
            if child_error:
                return child_error
        return None

    return validate_sections(disclosure["sections"], 1)

def validate_block(block: dict[str, Any], seen_block_ids: set[str]) -> ToolResult | None:
    block_id = block.get("id")
    if not isinstance(block_id, str) or not BLOCK_ID_PATTERN.match(block_id):
        return tool_failed("schema_validation_failed", "block 缺少 id 字段。")
    if block_id in seen_block_ids:
        return tool_failed("duplicate_block_id", f"block_id 重复：{block_id}")
    seen_block_ids.add(block_id)
    block_type = block.get("type")
    if block_type not in BLOCK_TYPES:
        return tool_failed("schema_validation_failed", f"不支持的 block.type：{block_type}")
    if block_type == "paragraph" and not isinstance(block.get("text"), str):
        return tool_failed("schema_validation_failed", "paragraph block 缺少 text 字段。")
    if block_type == "list" and (
        not isinstance(block.get("ordered"), bool) or not isinstance(block.get("items"), list)
    ):
        return tool_failed("schema_validation_failed", "list block 缺少 ordered 或 items 字段。")
    if block_type == "image" and not isinstance(block.get("src"), str):
        return tool_failed("schema_validation_failed", "image block 缺少 src 字段。")
    if block_type == "table" and (
        not isinstance(block.get("columns"), list) or not isinstance(block.get("rows"), list)
    ):
        return tool_failed("schema_validation_failed", "table block 缺少 columns 或 rows 字段。")
    return None
