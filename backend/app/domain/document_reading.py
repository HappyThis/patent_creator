from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from .disclosure import find_section
from .document_tool_results import ToolResult, tool_failed, tool_success

DEFAULT_PREVIEW_CHARS = 40


def disclosure_outline(disclosure: dict[str, Any], *, limit: int, offset: int) -> ToolResult:
    items = build_outline_items(disclosure["sections"])
    return tool_success({**_page(items, limit=limit, offset=offset, key="items")})


def disclosure_search(
    disclosure: dict[str, Any],
    *,
    query: str,
    regex: bool,
    limit: int,
    offset: int,
) -> ToolResult:
    matcher_result = _build_matcher(query, regex=regex)
    if isinstance(matcher_result, dict):
        return matcher_result
    matches = search_blocks(disclosure["sections"], matcher_result)
    return tool_success({"query": query, "regex": regex, **_page(matches, limit=limit, offset=offset, key="matches")})


def disclosure_read_section(
    disclosure: dict[str, Any],
    *,
    section_id: str,
    limit: int,
    offset: int,
    block_ids: list[str] | None = None,
) -> ToolResult:
    path = find_section_path(disclosure["sections"], section_id)
    if path is None:
        return tool_failed("section_not_found", f"section_id 不存在：{section_id}")
    section = path[-1]
    section_locator = section_locator_for(disclosure["sections"], path)
    readable_blocks = [section["title"], *section.get("blocks", [])]
    block_entries = [read_block_entry(block, section_path=[item["id"] for item in path], index=index) for index, block in enumerate(readable_blocks)]

    if block_ids:
        block_id_set = set(block_ids)
        known_ids = {entry["locator"]["block_id"] for entry in block_entries}
        missing = [block_id for block_id in block_ids if block_id not in known_ids]
        if missing:
            return tool_failed(
                "block_not_in_section",
                f"block_ids 必须属于 section_id 的直接 blocks：{', '.join(missing)}",
            )
        selected = [entry for entry in block_entries if entry["locator"]["block_id"] in block_id_set]
        page_payload = {
            "blocks": selected,
            "returned": len(selected),
            "total": len(selected),
            "offset": None,
            "next_offset": None,
            "truncated": False,
        }
    else:
        page_payload = _page(block_entries, limit=limit, offset=offset, key="blocks")

    child_sections = []
    for index, child in enumerate(section.get("sections", [])):
        child_path = [*path, child]
        child_sections.append(
            {
                "locator": section_locator_for(disclosure["sections"], child_path, index_override=index),
                "title": {
                    "locator": block_locator(child["title"], [item["id"] for item in child_path], 0),
                    "preview": preview_text(block_text(child["title"])),
                },
            }
        )

    return tool_success(
        {
            "section": {
                "locator": section_locator,
                "title": read_block_entry(section["title"], section_path=[item["id"] for item in path], index=0),
                "blocks": page_payload.pop("blocks"),
                "sections": child_sections,
            },
            **page_payload,
        }
    )


def build_outline_items(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def visit(section_list: list[dict[str, Any]], path: list[dict[str, Any]]) -> None:
        for index, section in enumerate(section_list):
            section_path = [*path, section]
            section_ids = [item["id"] for item in section_path]
            items.append(
                {
                    "kind": "section",
                    "locator": section_locator_from_parts(section["id"], section_ids, index),
                    "title": {
                        "locator": block_locator(section["title"], section_ids, 0),
                        "preview": preview_text(block_text(section["title"])),
                    },
                }
            )
            for block_index, block in enumerate(section.get("blocks", []), start=1):
                items.append(
                    {
                        "kind": "block",
                        "locator": block_locator(block, section_ids, block_index),
                        "preview": preview_text(block_text(block)),
                    }
                )
            visit(section.get("sections", []), section_path)

    visit(sections, [])
    return items


def search_blocks(sections: list[dict[str, Any]], matcher: Callable[[str], bool]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    def visit(section_list: list[dict[str, Any]], path: list[dict[str, Any]]) -> None:
        for section in section_list:
            section_path = [*path, section]
            section_ids = [item["id"] for item in section_path]
            all_blocks = [section["title"], *section.get("blocks", [])]
            for index, block in enumerate(all_blocks):
                text = block_text(block)
                if matcher(text):
                    matches.append(
                        {
                            "locator": block_locator(block, section_ids, index),
                            "preview": preview_text(text),
                        }
                    )
            visit(section.get("sections", []), section_path)

    visit(sections, [])
    return matches


def find_section_path(sections: list[dict[str, Any]], section_id: str) -> list[dict[str, Any]] | None:
    for section in sections:
        if section["id"] == section_id:
            return [section]
        child_path = find_section_path(section.get("sections", []), section_id)
        if child_path:
            return [section, *child_path]
    return None


def section_locator_for(
    root_sections: list[dict[str, Any]],
    section_path: list[dict[str, Any]],
    *,
    index_override: int | None = None,
) -> dict[str, Any]:
    section = section_path[-1]
    if index_override is not None:
        index = index_override
    elif len(section_path) == 1:
        index = next(index for index, item in enumerate(root_sections) if item["id"] == section["id"])
    else:
        parent = section_path[-2]
        index = next(index for index, item in enumerate(parent.get("sections", [])) if item["id"] == section["id"])
    return section_locator_from_parts(section["id"], [item["id"] for item in section_path], index)


def section_locator_from_parts(section_id: str, section_path: list[str], index: int) -> dict[str, Any]:
    return {
        "kind": "section",
        "section_id": section_id,
        "section_path": section_path,
        "index": index,
    }


def block_locator(block: dict[str, Any], section_path: list[str], index: int) -> dict[str, Any]:
    return {
        "kind": "block",
        "section_id": section_path[-1],
        "section_path": section_path,
        "block_id": block["id"],
        "block_type": block["type"],
        "index": index,
    }


def read_block_entry(block: dict[str, Any], *, section_path: list[str], index: int) -> dict[str, Any]:
    payload = copy.deepcopy(block)
    payload["locator"] = block_locator(block, section_path, index)
    return payload


def preview_text(text: str, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    if len(text) <= preview_chars * 2:
        return text
    omitted = len(text) - preview_chars * 2
    return f"{text[:preview_chars]}…（省略 {omitted} 字）…{text[-preview_chars:]}"


def block_text(block: dict[str, Any]) -> str:
    if block["type"] in {"title", "paragraph"}:
        return block["text"]
    if block["type"] == "list":
        return "\n".join(block["items"])
    if block["type"] == "image":
        return "\n".join(value for value in [block.get("alt"), block.get("caption"), block.get("src")] if value)
    return "\n".join([" ".join(block["columns"]), *[" ".join(row) for row in block["rows"]]])


def _build_matcher(query: str, *, regex: bool) -> Callable[[str], bool] | ToolResult:
    if not query:
        return tool_failed("invalid_operation", "query 字段缺失。")
    if regex:
        try:
            compiled = re.compile(query, flags=re.IGNORECASE)
        except re.error as exc:
            return tool_failed("invalid_operation", f"regex 无效：{exc}")
        return lambda text: compiled.search(text) is not None
    folded_query = query.casefold()
    return lambda text: folded_query in text.casefold()


def _page(items: list[dict[str, Any]], *, limit: int, offset: int, key: str) -> dict[str, Any]:
    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    return {
        key: page,
        "returned": len(page),
        "total": total,
        "offset": offset,
        "next_offset": next_offset,
        "truncated": next_offset is not None,
    }
