from __future__ import annotations

from typing import Any

from ..schemas import OutlineItem

STANDARD_SECTIONS: list[dict[str, Any]] = [
    {"id": "title", "title": "发明名称"},
    {"id": "technical_field", "title": "技术领域"},
    {"id": "background_technology", "title": "背景技术"},
    {"id": "existing_solution", "title": "现有技术方案"},
    {"id": "existing_solution_defects", "title": "现有技术缺陷"},
    {"id": "technical_problem", "title": "要解决的技术问题"},
    {"id": "technical_solution", "title": "技术方案"},
    {"id": "key_innovations", "title": "关键创新点"},
    {"id": "embodiments", "title": "具体实施方式"},
    {"id": "technical_effects", "title": "技术效果"},
    {"id": "drawings", "title": "附图说明"},
    {"id": "claim_suggestions", "title": "权利要求建议"},
]


def build_initial_disclosure(title: str) -> dict[str, Any]:
    return {
        "meta": {
            "document_type": "patent_disclosure",
            "schema_version": "v1",
            "title": title,
            "id_counters": {"block": 0},
        },
        "sections": [
            {"id": item["id"], "title": item["title"], "blocks": [], "children": []}
            for item in STANDARD_SECTIONS
        ],
    }


def build_outline_items(sections: list[dict[str, Any]], level: int = 2) -> list[OutlineItem]:
    items: list[OutlineItem] = []
    for section in sections:
        children = build_outline_items(section.get("children", []), level + 1)
        items.append(
            OutlineItem(
                id=section["id"],
                title=section["title"],
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
        "title": disclosure["meta"]["title"],
        "meta": {
            "document_type": disclosure["meta"]["document_type"],
            "schema_version": disclosure["meta"]["schema_version"],
        },
        "outline": outline,
        "children": [render_section(section, 2) for section in disclosure["sections"]],
    }


def render_section(section: dict[str, Any], level: int) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    for block in section.get("blocks", []):
        children.append(render_block(section["id"], block))
    for child in section.get("children", []):
        children.append(render_section(child, level + 1))
    return {
        "type": "section",
        "id": section["id"],
        "title": section["title"],
        "level": level,
        "anchor": section["id"],
        "children": children,
    }


def render_block(section_id: str, block: dict[str, Any]) -> dict[str, Any]:
    payload = {"type": block["type"], "id": block["id"], "section_id": section_id}
    if block["type"] == "paragraph":
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


def find_section(sections: list[dict[str, Any]], section_id: str) -> dict[str, Any] | None:
    for section in sections:
        if section["id"] == section_id:
            return section
        found = find_section(section.get("children", []), section_id)
        if found:
            return found
    return None


def find_block(sections: list[dict[str, Any]], block_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for section in sections:
        for block in section.get("blocks", []):
            if block["id"] == block_id:
                return section, block
        found = find_block(section.get("children", []), block_id)
        if found:
            return found
    return None


def next_block_id(disclosure: dict[str, Any]) -> str:
    counter = int(disclosure["meta"]["id_counters"]["block"]) + 1
    disclosure["meta"]["id_counters"]["block"] = counter
    return f"blk_{counter:06d}"


def disclosure_to_markdown(disclosure: dict[str, Any]) -> str:
    lines = [f"# {disclosure['meta']['title']}", ""]
    for section in disclosure["sections"]:
        lines.extend(section_to_markdown(section, 2))
    return "\n".join(lines).strip() + "\n"


def section_to_markdown(section: dict[str, Any], level: int) -> list[str]:
    lines = [f"{'#' * level} {section['title']}", ""]
    for block in section.get("blocks", []):
        lines.extend(block_to_markdown(block))
        lines.append("")
    for child in section.get("children", []):
        lines.extend(section_to_markdown(child, level + 1))
    return lines


def block_to_markdown(block: dict[str, Any]) -> list[str]:
    if block["type"] == "paragraph":
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
