from __future__ import annotations

import copy
import json
from typing import Any

from .disclosure import SECTION_TYPES, find_block, next_block_id, next_section_id
from .document_schema import BLOCK_TYPES, CHANGE_SCOPE_BY_OP
from .document_tool_results import ToolResult, tool_failed, tool_success
from .document_tree import (
    collect_block_ids,
    collect_section_ids,
    dedupe,
    get_required_section,
    replace_section_in_tree,
    section_depth,
)
from .document_validation import validate_disclosure


def apply_document_edit(disclosure: dict[str, Any], arguments: dict[str, Any]) -> ToolResult:
    operations_result = normalize_operations(arguments.get("operations"))
    if operations_result["status"] == "failed":
        return operations_result
    operations = operations_result["output"]["operations"]

    draft = copy.deepcopy(disclosure)
    validation_error = validate_disclosure(draft)
    if validation_error:
        return validation_error

    changed_section_ids: list[str] = []
    changed_block_ids: list[str] = []
    primary_section_id: str | None = None
    primary_block_id: str | None = None
    change_scope: str | None = None

    for operation in operations:
        if not isinstance(operation, dict):
            return tool_failed("invalid_operation", "operation 必须是对象。")
        op = operation.get("op")
        if op not in CHANGE_SCOPE_BY_OP:
            return tool_failed("invalid_operation", f"不支持的 edit op：{op}")

        result = apply_operation(draft, operation)
        if result["status"] == "failed":
            return result

        output = result["output"]
        changed_section_ids.extend(output["changed_section_ids"])
        changed_block_ids.extend(output["changed_block_ids"])
        primary_section_id = primary_section_id or output["primary_section_id"]
        primary_block_id = primary_block_id or output["primary_block_id"]
        change_scope = change_scope or output["change_scope"]

    validation_error = validate_disclosure(draft)
    if validation_error:
        return validation_error

    disclosure.clear()
    disclosure.update(draft)
    deduped_sections = dedupe(changed_section_ids)
    deduped_blocks = dedupe(changed_block_ids)
    return tool_success(
        {
            "changed_section_ids": deduped_sections,
            "changed_block_ids": deduped_blocks,
            "operations_applied": len(operations),
            "primary_section_id": primary_section_id,
            "primary_block_id": primary_block_id,
            "change_scope": change_scope,
        }
    )

def normalize_operations(raw_operations: Any) -> ToolResult:
    operations = raw_operations
    if isinstance(raw_operations, str):
        try:
            operations = json.loads(raw_operations)
        except json.JSONDecodeError:
            return tool_failed(
                "invalid_operation",
                "document_edit.operations 必须是非空数组；当前收到的是字符串，且无法解析为 JSON 数组。",
            )

    if not isinstance(operations, list) or not operations:
        return tool_failed("invalid_operation", "document_edit.operations 必须是非空数组。")
    if not all(isinstance(operation, dict) for operation in operations):
        return tool_failed("invalid_operation", "document_edit.operations 中的每一项都必须是对象。")
    return tool_success({"operations": operations})

def apply_operation(disclosure: dict[str, Any], operation: dict[str, Any]) -> ToolResult:
    op = operation["op"]
    if op == "update_meta":
        fields = operation.get("fields")
        if not isinstance(fields, dict):
            return tool_failed("invalid_operation", "update_meta 需要 fields。")
        for key, value in fields.items():
            if key == "id_counters":
                return tool_failed("invalid_operation", "id_counters 由系统维护，不允许直接修改。")
            if key in {"title"}:
                disclosure["meta"][key] = value
        return edit_output([], [], None, None, "meta_updated")

    if op == "replace_section_blocks":
        section = get_required_section(disclosure, operation.get("section_id"))
        if isinstance(section, dict) and "status" in section:
            return section
        blocks_result = prepare_blocks(disclosure, operation.get("blocks"))
        if blocks_result["status"] == "failed":
            return blocks_result
        blocks = blocks_result["output"]["blocks"]
        section["blocks"] = blocks
        block_ids = [block["id"] for block in blocks]
        return edit_output(
            [section["id"]],
            block_ids,
            section["id"],
            block_ids[0] if block_ids else None,
            "section_blocks_replaced",
        )

    if op == "append_block":
        section = get_required_section(disclosure, operation.get("section_id"))
        if isinstance(section, dict) and "status" in section:
            return section
        block_result = prepare_block(disclosure, operation.get("block"))
        if block_result["status"] == "failed":
            return block_result
        block = block_result["output"]["block"]
        section.setdefault("blocks", []).append(block)
        return edit_output([section["id"]], [block["id"]], section["id"], block["id"], "block_appended")

    if op == "replace_block":
        block_id = operation.get("block_id")
        if not isinstance(block_id, str):
            return tool_failed("invalid_operation", "replace_block 需要 block_id。")
        found = find_block(disclosure["sections"], block_id)
        if not found:
            return tool_failed("block_not_found", f"block_id 不存在：{block_id}")
        section, old_block = found
        block_result = prepare_block(disclosure, operation.get("block"), existing_block_id=block_id)
        if block_result["status"] == "failed":
            return block_result
        block = block_result["output"]["block"]
        section["blocks"] = [block if item is old_block else item for item in section["blocks"]]
        return edit_output([section["id"]], [block_id], section["id"], block_id, "block_replaced")

    if op == "append_child_section":
        if not isinstance(operation.get("parent_section_id"), str) or not isinstance(operation.get("section"), dict):
            return tool_failed("invalid_operation", "append_child_section 需要 parent_section_id 和 section。")
        parent = get_required_section(disclosure, operation.get("parent_section_id"))
        if isinstance(parent, dict) and "status" in parent:
            return parent
        if section_depth(disclosure["sections"], parent["id"]) >= 2:
            return tool_failed("schema_validation_failed", "v2 不允许超过两级章节。")
        section_result = prepare_section(disclosure, operation.get("section"), depth=2)
        if section_result["status"] == "failed":
            return section_result
        section = section_result["output"]["section"]
        parent.setdefault("children", []).append(section)
        block_ids = collect_block_ids([section])
        return edit_output(
            [parent["id"], section["id"]],
            block_ids,
            section["id"],
            block_ids[0] if block_ids else None,
            "child_section_appended",
        )

    section_id = operation.get("section_id")
    current = get_required_section(disclosure, section_id)
    if isinstance(current, dict) and "status" in current:
        return current
    section_payload = operation.get("section")
    if not isinstance(section_payload, dict):
        return tool_failed("invalid_operation", "replace_section 需要 section。")
    if "id" in section_payload:
        return tool_failed("invalid_operation", "replace_section 的 section 不允许携带 id；section_id 由工具保留。")
    if section_payload.get("type") != current.get("type"):
        return tool_failed("invalid_operation", "replace_section 的 section.type 必须与原章节 type 一致。")
    section_result = prepare_section(
        disclosure,
        section_payload,
        existing_section_id=section_id,
        depth=section_depth(disclosure["sections"], section_id),
    )
    if section_result["status"] == "failed":
        return section_result
    replace_section_in_tree(disclosure["sections"], section_id, section_result["output"]["section"])
    block_ids = collect_block_ids([section_result["output"]["section"]])
    return edit_output([section_id], block_ids, section_id, block_ids[0] if block_ids else None, "section_replaced")

def edit_output(
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

def prepare_section(
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
    blocks_result = prepare_blocks(disclosure, payload.get("blocks", []))
    if blocks_result["status"] == "failed":
        return blocks_result
    children = payload.get("children", [])
    if not isinstance(children, list):
        return tool_failed("schema_validation_failed", "section.children 必须是数组。")
    prepared_children = []
    for child in children:
        child_result = prepare_section(
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

def prepare_blocks(disclosure: dict[str, Any], payload: Any) -> ToolResult:
    if not isinstance(payload, list):
        return tool_failed("schema_validation_failed", "blocks 必须是数组。")
    blocks = []
    for item in payload:
        result = prepare_block(disclosure, item)
        if result["status"] == "failed":
            return result
        blocks.append(result["output"]["block"])
    return tool_success({"blocks": blocks})

def prepare_block(disclosure: dict[str, Any], payload: Any, existing_block_id: str | None = None) -> ToolResult:
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
