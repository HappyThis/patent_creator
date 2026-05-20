from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from .disclosure import SECTION_TYPES, find_block, next_block_id, next_section_id
from .document_schema import BLOCK_TYPES
from .document_tool_results import ToolResult, tool_failed, tool_success
from .document_tree import collect_block_ids, dedupe, get_required_section, section_depth
from .document_validation import validate_disclosure

MAX_DOCUMENT_WRITE_TEXT_CHARS = 1500
_WRITE_CONTENT_KEYS = {"text", "title", "items", "caption", "alt", "columns", "rows"}


DocumentMutator = Callable[[dict[str, Any]], ToolResult]


def replace_section_blocks(disclosure: dict[str, Any], section_id: str, blocks_payload: Any) -> ToolResult:
    return _apply_document_write(
        disclosure,
        {"blocks": blocks_payload},
        lambda draft: _replace_section_blocks(draft, section_id, blocks_payload),
    )


def append_block(disclosure: dict[str, Any], section_id: str, block_payload: Any) -> ToolResult:
    return _apply_document_write(
        disclosure,
        {"block": block_payload},
        lambda draft: _append_block(draft, section_id, block_payload),
    )


def replace_block(disclosure: dict[str, Any], block_id: str, block_payload: Any) -> ToolResult:
    return _apply_document_write(
        disclosure,
        {"block": block_payload},
        lambda draft: _replace_block(draft, block_id, block_payload),
    )


def append_child_section(
    disclosure: dict[str, Any],
    parent_section_id: str,
    title: str,
    blocks_payload: Any,
) -> ToolResult:
    return _apply_document_write(
        disclosure,
        {"title": title, "blocks": blocks_payload},
        lambda draft: _append_child_section(draft, parent_section_id, title, blocks_payload),
    )


def clear_section_blocks(disclosure: dict[str, Any], section_id: str) -> ToolResult:
    return _apply_document_write(
        disclosure,
        {},
        lambda draft: _replace_section_blocks(draft, section_id, []),
    )


def _apply_document_write(
    disclosure: dict[str, Any],
    write_payload: dict[str, Any],
    mutator: DocumentMutator,
) -> ToolResult:
    size_result = _validate_write_size(write_payload)
    if size_result["status"] == "failed":
        return size_result

    draft = copy.deepcopy(disclosure)
    validation_error = validate_disclosure(draft)
    if validation_error:
        return validation_error

    result = mutator(draft)
    if result["status"] == "failed":
        return result

    validation_error = validate_disclosure(draft)
    if validation_error:
        return validation_error

    disclosure.clear()
    disclosure.update(draft)
    output = result["output"]
    return tool_success(
        {
            "changed_section_ids": dedupe(output["changed_section_ids"]),
            "changed_block_ids": dedupe(output["changed_block_ids"]),
            "primary_section_id": output["primary_section_id"],
            "primary_block_id": output["primary_block_id"],
            "change_scope": output["change_scope"],
        }
    )


def _validate_write_size(payload: dict[str, Any]) -> ToolResult:
    total_chars = _count_write_text_chars(payload)
    if total_chars > MAX_DOCUMENT_WRITE_TEXT_CHARS:
        return tool_failed(
            "edit_too_large",
            (
                f"单次文档正文写入不能超过 {MAX_DOCUMENT_WRITE_TEXT_CHARS} 字；"
                f"当前约 {total_chars} 字。请拆成多次小步写入：先写根章节总述，"
                "再逐个追加子章节或段落，每次只写一个短章节或少量段落。"
            ),
        )
    return tool_success({"text_chars": total_chars})


def _count_write_text_chars(value: Any, key: str | None = None) -> int:
    if isinstance(value, str):
        return len(value) if key in _WRITE_CONTENT_KEYS else 0
    if isinstance(value, list):
        if key in _WRITE_CONTENT_KEYS:
            return sum(_count_all_strings(item) for item in value)
        return sum(_count_write_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_write_text_chars(item, item_key) for item_key, item in value.items())
    return 0


def _count_all_strings(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_count_all_strings(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_all_strings(item) for item in value.values())
    return 0


def _replace_section_blocks(disclosure: dict[str, Any], section_id: str, blocks_payload: Any) -> ToolResult:
    section = get_required_section(disclosure, section_id)
    if isinstance(section, dict) and "status" in section:
        return section
    blocks_result = _prepare_blocks(disclosure, blocks_payload)
    if blocks_result["status"] == "failed":
        return blocks_result
    blocks = blocks_result["output"]["blocks"]
    section["blocks"] = blocks
    block_ids = [block["id"] for block in blocks]
    return _write_output(
        [section["id"]],
        block_ids,
        section["id"],
        block_ids[0] if block_ids else None,
        "section_blocks_replaced",
    )


def _append_block(disclosure: dict[str, Any], section_id: str, block_payload: Any) -> ToolResult:
    section = get_required_section(disclosure, section_id)
    if isinstance(section, dict) and "status" in section:
        return section
    block_result = _prepare_block(disclosure, block_payload)
    if block_result["status"] == "failed":
        return block_result
    block = block_result["output"]["block"]
    section.setdefault("blocks", []).append(block)
    return _write_output([section["id"]], [block["id"]], section["id"], block["id"], "block_appended")


def _replace_block(disclosure: dict[str, Any], block_id: str, block_payload: Any) -> ToolResult:
    if not isinstance(block_id, str):
        return tool_failed("invalid_operation", "replace_block 需要 block_id。")
    found = find_block(disclosure["sections"], block_id)
    if not found:
        return tool_failed("block_not_found", f"block_id 不存在：{block_id}")
    section, old_block = found
    block_result = _prepare_block(disclosure, block_payload, existing_block_id=block_id)
    if block_result["status"] == "failed":
        return block_result
    block = block_result["output"]["block"]
    section["blocks"] = [block if item is old_block else item for item in section["blocks"]]
    return _write_output([section["id"]], [block_id], section["id"], block_id, "block_replaced")


def _append_child_section(
    disclosure: dict[str, Any],
    parent_section_id: str,
    title: str,
    blocks_payload: Any,
) -> ToolResult:
    parent = get_required_section(disclosure, parent_section_id)
    if isinstance(parent, dict) and "status" in parent:
        return parent
    if section_depth(disclosure["sections"], parent["id"]) >= 2:
        return tool_failed("schema_validation_failed", "v2 不允许超过两级章节。")
    section_result = _prepare_section(
        disclosure,
        {
            "type": "custom",
            "title": title,
            "blocks": blocks_payload,
            "children": [],
        },
        depth=2,
    )
    if section_result["status"] == "failed":
        return section_result
    section = section_result["output"]["section"]
    parent.setdefault("children", []).append(section)
    block_ids = collect_block_ids([section])
    return _write_output(
        [parent["id"], section["id"]],
        block_ids,
        section["id"],
        block_ids[0] if block_ids else None,
        "child_section_appended",
    )


def _write_output(
    section_ids: list[str],
    block_ids: list[str],
    primary_section_id: str | None,
    primary_block_id: str | None,
    change_scope: str,
) -> ToolResult:
    return tool_success(
        {
            "changed_section_ids": section_ids,
            "changed_block_ids": block_ids,
            "primary_section_id": primary_section_id,
            "primary_block_id": primary_block_id,
            "change_scope": change_scope,
        }
    )


def _prepare_section(
    disclosure: dict[str, Any],
    payload: Any,
    existing_section_id: str | None = None,
    depth: int = 1,
) -> ToolResult:
    if not isinstance(payload, dict):
        return tool_failed("schema_validation_failed", "section 必须是对象。")
    if "id" in payload:
        return tool_failed("schema_validation_failed", "section 不允许携带 id；section_id 由系统生成或保留。")
    section_type = payload.get("type")
    if section_type not in SECTION_TYPES:
        return tool_failed("schema_validation_failed", f"不支持的 section.type：{section_type}")
    if depth > 1 and section_type != "custom":
        return tool_failed("schema_validation_failed", "子章节 section.type 必须为 custom。")
    title = payload.get("title")
    if not isinstance(title, str) or not title:
        return tool_failed("schema_validation_failed", "section 缺少 title。")
    section_id = existing_section_id or next_section_id(disclosure)
    blocks_result = _prepare_blocks(disclosure, payload.get("blocks", []))
    if blocks_result["status"] == "failed":
        return blocks_result
    children = payload.get("children", [])
    if not isinstance(children, list):
        return tool_failed("schema_validation_failed", "section.children 必须是数组。")
    prepared_children = []
    for child in children:
        child_result = _prepare_section(
            disclosure,
            child,
            depth=depth + 1,
        )
        if child_result["status"] == "failed":
            return child_result
        prepared_children.append(child_result["output"]["section"])
    return tool_success(
        {
            "section": {
                "id": section_id,
                "type": section_type,
                "title": title,
                "blocks": blocks_result["output"]["blocks"],
                "children": prepared_children,
            }
        }
    )


def _prepare_blocks(disclosure: dict[str, Any], payload: Any) -> ToolResult:
    if not isinstance(payload, list):
        return tool_failed("schema_validation_failed", "blocks 必须是数组。")
    blocks = []
    for item in payload:
        result = _prepare_block(disclosure, item)
        if result["status"] == "failed":
            return result
        blocks.append(result["output"]["block"])
    return tool_success({"blocks": blocks})


def _prepare_block(disclosure: dict[str, Any], payload: Any, existing_block_id: str | None = None) -> ToolResult:
    if not isinstance(payload, dict):
        return tool_failed("schema_validation_failed", "block 必须是对象。")
    block_type = payload.get("type")
    if block_type not in BLOCK_TYPES:
        return tool_failed("schema_validation_failed", f"不支持的 block.type：{block_type}")
    block = {"id": existing_block_id or next_block_id(disclosure), "type": block_type}
    if block_type == "paragraph":
        if not isinstance(payload.get("text"), str):
            return tool_failed("schema_validation_failed", "paragraph block 缺少 text 字段。")
        block["text"] = payload["text"]
    elif block_type == "list":
        if not isinstance(payload.get("ordered"), bool) or not isinstance(payload.get("items"), list):
            return tool_failed("schema_validation_failed", "list block 缺少 ordered 或 items 字段。")
        block["ordered"] = payload["ordered"]
        block["items"] = payload["items"]
    elif block_type == "image":
        if not isinstance(payload.get("src"), str):
            return tool_failed("schema_validation_failed", "image block 缺少 src 字段。")
        block["src"] = payload["src"]
        if payload.get("caption") is not None:
            block["caption"] = payload["caption"]
        if payload.get("alt") is not None:
            block["alt"] = payload["alt"]
    else:
        if not isinstance(payload.get("columns"), list) or not isinstance(payload.get("rows"), list):
            return tool_failed("schema_validation_failed", "table block 缺少 columns 或 rows 字段。")
        block["columns"] = payload["columns"]
        block["rows"] = payload["rows"]
    return tool_success({"block": block})
