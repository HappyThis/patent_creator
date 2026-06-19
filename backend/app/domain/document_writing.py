from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from ..core import now_iso
from .disclosure import next_block_id, next_section_id, section_title_text
from .document_schema import BLOCK_TYPES
from .document_tool_results import ToolResult, tool_failed, tool_success
from .document_tree import dedupe, get_required_section
from .document_validation import validate_disclosure

MAX_DOCUMENT_WRITE_TEXT_CHARS = 1500
_WRITE_CONTENT_KEYS = {"text", "title", "items", "caption", "alt", "columns", "rows", "latex", "figure_id"}

DocumentMutator = Callable[[dict[str, Any]], ToolResult]


def edit_disclosure(disclosure: dict[str, Any], arguments: dict[str, Any]) -> ToolResult:
    return _apply_document_write(disclosure, arguments, lambda draft: _edit_disclosure(draft, arguments))


def _edit_disclosure(disclosure: dict[str, Any], arguments: dict[str, Any]) -> ToolResult:
    operation = arguments.get("operation")
    section_id = arguments.get("section_id")
    section = get_required_section(disclosure, section_id)
    if isinstance(section, dict) and "status" in section:
        return section

    if operation == "replace_block":
        return _replace_block(disclosure, section, arguments.get("block_id"), arguments.get("block"))
    if operation == "delete_block":
        return _delete_block(section, arguments.get("block_id"))
    if operation == "insert_block":
        return _insert_block(disclosure, section, arguments.get("position") or {}, arguments.get("block"))
    if operation == "insert_section":
        return _insert_section(disclosure, section, arguments.get("position") or {}, arguments.get("section") or {})
    if operation == "delete_section":
        return _delete_section(section, arguments.get("target_section_id"))
    return tool_failed("invalid_operation", f"不支持的 disclosure_edit operation：{operation}")


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

    draft["meta"]["updated_at"] = now_iso()
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
            "updated_at": draft["meta"]["updated_at"],
            "notice": "交底书已更新。",
        }
    )


def _replace_block(
    disclosure: dict[str, Any],
    section: dict[str, Any],
    block_id: Any,
    block_payload: Any,
) -> ToolResult:
    if not isinstance(block_id, str):
        return tool_failed("invalid_operation", "replace_block 需要 block_id。")
    if section["title"]["id"] == block_id:
        block_result = _prepare_block(disclosure, block_payload, existing_block_id=block_id, expected_type="title")
        if block_result["status"] == "failed":
            return block_result
        section["title"] = block_result["output"]["block"]
        return _write_output([section["id"]], [block_id], section["id"], block_id, "block_replaced")

    for index, old_block in enumerate(section.get("blocks", [])):
        if old_block["id"] == block_id:
            block_result = _prepare_block(disclosure, block_payload, existing_block_id=block_id, forbid_title=True)
            if block_result["status"] == "failed":
                return block_result
            if block_result["output"]["block"].get("type") == "figure" and section_title_text(section) != "附录":
                return tool_failed("figure_block_outside_appendix", "figure block 只能写入附录章节。")
            section["blocks"][index] = block_result["output"]["block"]
            return _write_output([section["id"]], [block_id], section["id"], block_id, "block_replaced")
    return tool_failed("block_not_found", f"block_id 不是该 section 的直接 block：{block_id}")


def _delete_block(section: dict[str, Any], block_id: Any) -> ToolResult:
    if not isinstance(block_id, str):
        return tool_failed("invalid_operation", "delete_block 需要 block_id。")
    if section["title"]["id"] == block_id:
        return tool_failed("invalid_operation", "title block 不允许删除，只能 replace_block。")
    original_len = len(section.get("blocks", []))
    section["blocks"] = [block for block in section.get("blocks", []) if block["id"] != block_id]
    if len(section["blocks"]) == original_len:
        return tool_failed("block_not_found", f"block_id 不是该 section 的直接 block：{block_id}")
    return _write_output([section["id"]], [block_id], section["id"], None, "block_deleted")


def _insert_block(
    disclosure: dict[str, Any],
    section: dict[str, Any],
    position: dict[str, Any],
    block_payload: Any,
) -> ToolResult:
    block_result = _prepare_block(disclosure, block_payload, forbid_title=True)
    if block_result["status"] == "failed":
        return block_result
    insert_at = _block_insert_index(section, position)
    if isinstance(insert_at, dict):
        return insert_at
    block = block_result["output"]["block"]
    if block.get("type") == "figure" and section_title_text(section) != "附录":
        return tool_failed("figure_block_outside_appendix", "figure block 只能写入附录章节。")
    section.setdefault("blocks", []).insert(insert_at, block)
    return _write_output([section["id"]], [block["id"]], section["id"], block["id"], "block_inserted")


def _insert_section(
    disclosure: dict[str, Any],
    parent: dict[str, Any],
    position: dict[str, Any],
    section_payload: dict[str, Any],
) -> ToolResult:
    insert_at = _section_insert_index(parent, position)
    if isinstance(insert_at, dict):
        return insert_at
    title = section_payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return tool_failed("schema_validation_failed", "insert_section 需要非空 section.title。")
    if set(section_payload.keys()) != {"title"}:
        return tool_failed("schema_validation_failed", "insert_section 的 section 只能包含 title。")
    section_id = next_section_id(disclosure)
    title_block_id = next_block_id(disclosure)
    section = {
        "id": section_id,
        "title": {"id": title_block_id, "type": "title", "text": title},
        "blocks": [],
        "sections": [],
    }
    parent.setdefault("sections", []).insert(insert_at, section)
    return _write_output([parent["id"], section_id], [title_block_id], section_id, title_block_id, "section_inserted")


def _delete_section(parent: dict[str, Any], target_section_id: Any) -> ToolResult:
    if not isinstance(target_section_id, str):
        return tool_failed("invalid_operation", "delete_section 需要 target_section_id。")
    original_len = len(parent.get("sections", []))
    deleted = next((section for section in parent.get("sections", []) if section["id"] == target_section_id), None)
    parent["sections"] = [section for section in parent.get("sections", []) if section["id"] != target_section_id]
    if len(parent["sections"]) == original_len or deleted is None:
        return tool_failed("section_not_found", f"target_section_id 不是该 section 的直接子 section：{target_section_id}")
    deleted_block_ids = _collect_block_ids([deleted])
    return _write_output([parent["id"], target_section_id], deleted_block_ids, parent["id"], None, "section_deleted")


def _block_insert_index(section: dict[str, Any], position: dict[str, Any]) -> int | ToolResult:
    mode = position.get("mode", "end")
    blocks = section.get("blocks", [])
    if mode == "start":
        return 0
    if mode == "end":
        return len(blocks)
    if mode == "index":
        index = position.get("index")
        if not isinstance(index, int):
            return tool_failed("invalid_operation", "position.index 必须是整数。")
        if index <= 0 or index > len(blocks) + 1:
            return tool_failed("invalid_operation", "block index 必须位于 1 到当前 blocks 末尾后一位之间。")
        return index - 1
    anchor_id = position.get("block_id")
    if not isinstance(anchor_id, str):
        return tool_failed("invalid_operation", "before/after position 需要 block_id。")
    if anchor_id == section["title"]["id"]:
        if mode == "before":
            return tool_failed("invalid_operation", "不能在 title block 前插入 block。")
        if mode == "after":
            return 0
    for index, block in enumerate(blocks):
        if block["id"] == anchor_id:
            if mode == "before":
                return index
            if mode == "after":
                return index + 1
            return tool_failed("invalid_operation", "position.mode 必须是 start、end、index、before 或 after。")
    return tool_failed("block_not_found", f"position.block_id 不是该 section 的直接 block：{anchor_id}")


def _section_insert_index(parent: dict[str, Any], position: dict[str, Any]) -> int | ToolResult:
    mode = position.get("mode", "end")
    sections = parent.get("sections", [])
    if mode == "start":
        return 0
    if mode == "end":
        return len(sections)
    if mode == "index":
        index = position.get("index")
        if not isinstance(index, int):
            return tool_failed("invalid_operation", "position.index 必须是整数。")
        if index < 0 or index > len(sections):
            return tool_failed("invalid_operation", "section index 必须位于 0 到当前子 section 末尾后一位之间。")
        return index
    anchor_id = position.get("section_id")
    if not isinstance(anchor_id, str):
        return tool_failed("invalid_operation", "before/after position 需要 section_id。")
    for index, section in enumerate(sections):
        if section["id"] == anchor_id:
            if mode == "before":
                return index
            if mode == "after":
                return index + 1
            return tool_failed("invalid_operation", "position.mode 必须是 start、end、index、before 或 after。")
    return tool_failed("section_not_found", f"position.section_id 不是该 section 的直接子 section：{anchor_id}")


def _validate_write_size(payload: dict[str, Any]) -> ToolResult:
    total_chars = _count_write_text_chars(payload)
    if total_chars > MAX_DOCUMENT_WRITE_TEXT_CHARS:
        return tool_failed(
            "edit_too_large",
            (
                f"单次交底书正文写入不能超过 {MAX_DOCUMENT_WRITE_TEXT_CHARS} 字；"
                f"当前约 {total_chars} 字。请拆成多次 disclosure_edit 调用："
                "先插入或定位 section，再逐个插入/替换 block。"
            ),
            retry_hint="请拆成多次 disclosure_edit 调用；一次只替换一个 block，或分多次插入段落。",
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


def _prepare_block(
    disclosure: dict[str, Any],
    payload: Any,
    existing_block_id: str | None = None,
    *,
    expected_type: str | None = None,
    forbid_title: bool = False,
) -> ToolResult:
    if not isinstance(payload, dict):
        return tool_failed("schema_validation_failed", "block 必须是对象。")
    block_type = payload.get("type")
    if block_type not in BLOCK_TYPES:
        return tool_failed("schema_validation_failed", f"不支持的 block.type：{block_type}")
    if expected_type is not None and block_type != expected_type:
        return tool_failed("schema_validation_failed", f"block.type 必须为 {expected_type}。")
    if forbid_title and block_type == "title":
        return tool_failed("schema_validation_failed", "普通正文 block 不能使用 title 类型。")

    block = {"id": existing_block_id or next_block_id(disclosure), "type": block_type}
    if block_type in {"title", "paragraph"}:
        if not isinstance(payload.get("text"), str):
            return tool_failed("schema_validation_failed", f"{block_type} block 缺少 text 字段。")
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
    elif block_type == "formula":
        if not isinstance(payload.get("latex"), str):
            return tool_failed("schema_validation_failed", "formula block 缺少 latex 字段。")
        block["latex"] = payload["latex"]
    elif block_type == "figure":
        if not isinstance(payload.get("figure_id"), str):
            return tool_failed("schema_validation_failed", "figure block 缺少 figure_id 字段。")
        block["figure_id"] = payload["figure_id"]
    else:
        if not isinstance(payload.get("columns"), list) or not isinstance(payload.get("rows"), list):
            return tool_failed("schema_validation_failed", "table block 缺少 columns 或 rows 字段。")
        block["columns"] = payload["columns"]
        block["rows"] = payload["rows"]
    return tool_success({"block": block})


def _collect_block_ids(sections: list[dict[str, Any]]) -> list[str]:
    block_ids: list[str] = []
    for section in sections:
        block_ids.append(section["title"]["id"])
        block_ids.extend(block["id"] for block in section.get("blocks", []))
        block_ids.extend(_collect_block_ids(section.get("sections", [])))
    return block_ids


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
