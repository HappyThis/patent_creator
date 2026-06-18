from __future__ import annotations

from typing import Any

from .disclosure import BLOCK_ID_PATTERN, SECTION_ID_PATTERN
from .document_schema import BLOCK_TYPES
from .document_tool_results import ToolResult, tool_failed


def validate_disclosure(disclosure: dict[str, Any]) -> ToolResult | None:
    if set(disclosure.keys()) != {"meta", "sections"}:
        return tool_failed("schema_validation_failed", "disclosure 顶层只能包含 meta 和 sections。")
    meta = disclosure.get("meta", {})
    if meta.get("document_type") != "patent_disclosure":
        return tool_failed("schema_validation_failed", "disclosure.document_type 必须为 patent_disclosure。")
    if meta.get("schema_version") != "v3.3":
        return tool_failed("schema_validation_failed", "disclosure.schema_version 必须为 v3.3。")
    for key in ("created_at", "updated_at"):
        if not isinstance(meta.get(key), str) or not meta.get(key):
            return tool_failed("schema_validation_failed", f"meta.{key} 必须是非空字符串。")
    if not isinstance(disclosure.get("sections"), list):
        return tool_failed("schema_validation_failed", "disclosure.sections 必须是数组。")

    section_ids: set[str] = set()
    block_ids: set[str] = set()

    def validate_sections(sections: list[dict[str, Any]]) -> ToolResult | None:
        for section in sections:
            if not isinstance(section, dict):
                return tool_failed("schema_validation_failed", "section 必须是对象。")
            if set(section.keys()) != {"id", "title", "blocks", "sections"}:
                return tool_failed("schema_validation_failed", "section 只能包含 id、title、blocks、sections。")
            section_id = section.get("id")
            if not isinstance(section_id, str) or not SECTION_ID_PATTERN.match(section_id):
                return tool_failed("schema_validation_failed", f"section.id 格式错误：{section_id}")
            if section_id in section_ids:
                return tool_failed("duplicate_section_id", f"section_id 重复：{section_id}")
            section_ids.add(section_id)

            title_error = validate_block(section["title"], block_ids, expected_type="title")
            if title_error:
                return title_error

            if not isinstance(section["blocks"], list):
                return tool_failed("schema_validation_failed", "section.blocks 必须是数组。")
            for block in section["blocks"]:
                block_error = validate_block(block, block_ids)
                if block_error:
                    return block_error

            if not isinstance(section["sections"], list):
                return tool_failed("schema_validation_failed", "section.sections 必须是数组。")
            child_error = validate_sections(section["sections"])
            if child_error:
                return child_error
        return None

    return validate_sections(disclosure["sections"])


def validate_block(
    block: dict[str, Any],
    seen_block_ids: set[str],
    *,
    expected_type: str | None = None,
) -> ToolResult | None:
    if not isinstance(block, dict):
        return tool_failed("schema_validation_failed", "block 必须是对象。")
    block_id = block.get("id")
    if not isinstance(block_id, str) or not BLOCK_ID_PATTERN.match(block_id):
        return tool_failed("schema_validation_failed", "block 缺少合法 id 字段。")
    if block_id in seen_block_ids:
        return tool_failed("duplicate_block_id", f"block_id 重复：{block_id}")
    seen_block_ids.add(block_id)
    block_type = block.get("type")
    if block_type not in BLOCK_TYPES:
        return tool_failed("schema_validation_failed", f"不支持的 block.type：{block_type}")
    if expected_type is not None and block_type != expected_type:
        return tool_failed("schema_validation_failed", f"block.type 必须为 {expected_type}。")
    if block_type in {"title", "paragraph"} and not isinstance(block.get("text"), str):
        return tool_failed("schema_validation_failed", f"{block_type} block 缺少 text 字段。")
    if block_type == "list" and (
        not isinstance(block.get("ordered"), bool) or not isinstance(block.get("items"), list)
    ):
        return tool_failed("schema_validation_failed", "list block 缺少 ordered 或 items 字段。")
    if block_type == "image" and not isinstance(block.get("src"), str):
        return tool_failed("schema_validation_failed", "image block 缺少 src 字段。")
    if block_type == "formula" and not isinstance(block.get("latex"), str):
        return tool_failed("schema_validation_failed", "formula block 缺少 latex 字段。")
    if block_type == "figure" and not isinstance(block.get("figure_id"), str):
        return tool_failed("schema_validation_failed", "figure block 缺少 figure_id 字段。")
    if block_type == "table" and (
        not isinstance(block.get("columns"), list) or not isinstance(block.get("rows"), list)
    ):
        return tool_failed("schema_validation_failed", "table block 缺少 columns 或 rows 字段。")
    return None
