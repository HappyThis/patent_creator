from __future__ import annotations

import copy
import json
from typing import Any

from .disclosure import (
    BLOCK_ID_PATTERN,
    SECTION_ID_PATTERN,
    SECTION_TYPES,
    build_outline_items,
    find_block,
    find_section,
    next_block_id,
    next_section_id,
)

ToolResult = dict[str, Any]

BLOCK_TYPES = {"paragraph", "list", "image", "table"}
CHANGE_SCOPE_BY_OP = {
    "update_meta": "meta_updated",
    "replace_section_blocks": "section_blocks_replaced",
    "append_block": "block_appended",
    "replace_block": "block_replaced",
    "append_child_section": "child_section_appended",
    "replace_section": "section_replaced",
}


def tool_success(output: dict[str, Any]) -> ToolResult:
    return {"status": "success", "output": output}


def tool_failed(code: str, message: str, **extra: Any) -> ToolResult:
    return {"status": "failed", "output": {"code": code, "message": message, **extra}}


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


def get_required_section(disclosure: dict[str, Any], section_id: Any) -> dict[str, Any] | ToolResult:
    if not isinstance(section_id, str):
        return tool_failed("invalid_operation", "operation 需要 section_id。")
    section = find_section(disclosure["sections"], section_id)
    if not section:
        return tool_failed("section_not_found", f"section_id 不存在：{section_id}")
    return section


def replace_section_in_tree(sections: list[dict[str, Any]], section_id: str, replacement: dict[str, Any]) -> bool:
    for index, section in enumerate(sections):
        if section["id"] == section_id:
            sections[index] = replacement
            return True
        if replace_section_in_tree(section["children"], section_id, replacement):
            return True
    return False


def section_depth(sections: list[dict[str, Any]], section_id: str, depth: int = 1) -> int:
    for section in sections:
        if section["id"] == section_id:
            return depth
        child_depth = section_depth(section["children"], section_id, depth + 1)
        if child_depth:
            return child_depth
    return 0


def collect_block_ids(sections: list[dict[str, Any]]) -> list[str]:
    block_ids: list[str] = []
    for section in sections:
        block_ids.extend(block["id"] for block in section.get("blocks", []))
        block_ids.extend(collect_block_ids(section.get("children", [])))
    return block_ids


def collect_section_ids(sections: list[dict[str, Any]]) -> list[str]:
    section_ids: list[str] = []
    for section in sections:
        section_ids.append(section["id"])
        section_ids.extend(collect_section_ids(section.get("children", [])))
    return section_ids


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


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
