from __future__ import annotations

import re
from typing import Any

from ..core import now_iso
from ..schemas import OutlineItem

STANDARD_SECTION_TITLES: list[str] = [
    "发明名称",
    "技术领域",
    "背景技术",
    "现有技术方案",
    "现有技术缺陷",
    "要解决的技术问题",
    "技术方案",
    "关键创新点",
    "具体实施方式",
    "技术效果",
    "附图说明",
    "权利要求建议",
]

SECTION_ID_PATTERN = re.compile(r"^sec_\d{6}$")
BLOCK_ID_PATTERN = re.compile(r"^blk_\d{6}$")
TITLE_BLOCK_TYPE = "title"


def build_initial_disclosure(title: str) -> dict[str, Any]:
    created_at = now_iso()
    sections: list[dict[str, Any]] = []
    block_counter = 0
    for section_index, section_title in enumerate(STANDARD_SECTION_TITLES, start=1):
        block_counter += 1
        title_block = {
            "id": _numbered_id("blk", block_counter),
            "type": TITLE_BLOCK_TYPE,
            "text": section_title,
        }
        blocks: list[dict[str, Any]] = []
        if section_index == 1 and title:
            block_counter += 1
            blocks.append(
                {
                    "id": _numbered_id("blk", block_counter),
                    "type": "paragraph",
                    "text": title,
                }
            )
        sections.append(
            {
                "id": _numbered_id("sec", section_index),
                "title": title_block,
                "blocks": blocks,
                "sections": [],
            }
        )
    return {
        "meta": {
            "document_type": "patent_disclosure",
            "schema_version": "v3",
            "created_at": created_at,
            "updated_at": created_at,
        },
        "sections": sections,
    }


def build_outline_items(sections: list[dict[str, Any]], level: int = 2) -> list[OutlineItem]:
    items: list[OutlineItem] = []
    for section in sections:
        children = build_outline_items(section.get("sections", []), level + 1)
        items.append(
            OutlineItem(
                id=section["id"],
                title=section_title_text(section),
                level=level,
                anchor=section["id"],
                children=children,
            )
        )
    return items


def build_render_ast(disclosure: dict[str, Any]) -> dict[str, Any]:
    outline = [item.model_dump() for item in build_outline_items(disclosure["sections"])]
    return {
        "type": "document",
        "title": document_title(disclosure),
        "meta": {
            "document_type": disclosure["meta"]["document_type"],
            "schema_version": disclosure["meta"]["schema_version"],
        },
        "outline": outline,
        "children": [render_section(section, 2) for section in disclosure["sections"]],
    }


def document_title(disclosure: dict[str, Any]) -> str:
    first_section = disclosure.get("sections", [None])[0]
    if isinstance(first_section, dict):
        first_block = first_section.get("blocks", [None])
        if isinstance(first_block, list) and first_block:
            block = first_block[0]
            if isinstance(block, dict) and block.get("type") == "paragraph":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        title_text = section_title_text(first_section)
        if title_text:
            return title_text
    return "未命名专利交底书"


def render_section(section: dict[str, Any], level: int) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    for block in section.get("blocks", []):
        children.append(render_block(section["id"], block))
    for child in section.get("sections", []):
        children.append(render_section(child, level + 1))
    return {
        "type": "section",
        "id": section["id"],
        "title": section_title_text(section),
        "level": level,
        "anchor": section["id"],
        "children": children,
    }


def render_block(section_id: str, block: dict[str, Any]) -> dict[str, Any]:
    payload = {"type": block["type"], "id": block["id"], "section_id": section_id}
    if block["type"] in {"title", "paragraph"}:
        payload["text"] = block["text"]
        return payload
    if block["type"] == "list":
        payload["ordered"] = block["ordered"]
        payload["items"] = block["items"]
        return payload
    if block["type"] == "image":
        payload["src"] = block["src"]
        payload["caption"] = block.get("caption")
        payload["alt"] = block.get("alt")
        return payload
    payload["columns"] = block["columns"]
    payload["rows"] = block["rows"]
    return payload


def section_title_text(section: dict[str, Any]) -> str:
    title = section.get("title")
    if isinstance(title, dict):
        text = title.get("text")
        if isinstance(text, str):
            return text
    return ""


def find_section(sections: list[dict[str, Any]], section_id: str) -> dict[str, Any] | None:
    for section in sections:
        if section["id"] == section_id:
            return section
        found = find_section(section.get("sections", []), section_id)
        if found:
            return found
    return None


def find_section_parent(
    sections: list[dict[str, Any]],
    section_id: str,
    parent: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    for section in sections:
        if section["id"] == section_id:
            return parent, section
        found_parent, found = find_section_parent(section.get("sections", []), section_id, section)
        if found:
            return found_parent, found
    return None, None


def find_block(sections: list[dict[str, Any]], block_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for section in sections:
        title = section.get("title")
        if isinstance(title, dict) and title.get("id") == block_id:
            return section, title
        for block in section.get("blocks", []):
            if block["id"] == block_id:
                return section, block
        found = find_block(section.get("sections", []), block_id)
        if found:
            return found
    return None


def next_section_id(disclosure: dict[str, Any]) -> str:
    return _next_numbered_id("sec", collect_section_ids(disclosure["sections"]))


def next_block_id(disclosure: dict[str, Any]) -> str:
    return _next_numbered_id("blk", collect_block_ids(disclosure["sections"]))


def collect_section_ids(sections: list[dict[str, Any]]) -> list[str]:
    section_ids: list[str] = []
    for section in sections:
        section_ids.append(section["id"])
        section_ids.extend(collect_section_ids(section.get("sections", [])))
    return section_ids


def collect_block_ids(sections: list[dict[str, Any]]) -> list[str]:
    block_ids: list[str] = []
    for section in sections:
        title = section.get("title")
        if isinstance(title, dict):
            block_ids.append(title["id"])
        block_ids.extend(block["id"] for block in section.get("blocks", []))
        block_ids.extend(collect_block_ids(section.get("sections", [])))
    return block_ids


def disclosure_to_markdown(disclosure: dict[str, Any]) -> str:
    lines = [f"# {document_title(disclosure)}", ""]
    for section in disclosure["sections"]:
        lines.extend(section_to_markdown(section, 2))
    return "\n".join(lines).strip() + "\n"


def section_to_markdown(section: dict[str, Any], level: int) -> list[str]:
    lines = [f"{'#' * level} {section_title_text(section)}", ""]
    for block in section.get("blocks", []):
        lines.extend(block_to_markdown(block))
        lines.append("")
    for child in section.get("sections", []):
        lines.extend(section_to_markdown(child, level + 1))
    return lines


def block_to_markdown(block: dict[str, Any]) -> list[str]:
    if block["type"] in {"title", "paragraph"}:
        return [block["text"]]
    if block["type"] == "list":
        prefix = lambda index: f"{index + 1}. " if block["ordered"] else "- "
        return [f"{prefix(index)}{item}" for index, item in enumerate(block["items"])]
    if block["type"] == "image":
        alt = block.get("alt", "image")
        caption = block.get("caption") or ""
        return [f"![{alt}]({block['src']})", caption] if caption else [f"![{alt}]({block['src']})"]
    header = "| " + " | ".join(block["columns"]) + " |"
    divider = "| " + " | ".join(["---"] * len(block["columns"])) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in block["rows"]]
    return [header, divider, *rows]


def _numbered_id(prefix: str, value: int) -> str:
    return f"{prefix}_{value:06d}"


def _next_numbered_id(prefix: str, ids: list[str]) -> str:
    max_value = 0
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})$")
    for item in ids:
        match = pattern.match(item)
        if match:
            max_value = max(max_value, int(match.group(1)))
    return _numbered_id(prefix, max_value + 1)
