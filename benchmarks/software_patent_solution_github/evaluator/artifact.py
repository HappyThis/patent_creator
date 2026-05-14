from __future__ import annotations

from pathlib import Path
from typing import Any


def extract_technical_solution(disclosure: dict[str, Any]) -> str:
    section = _find_section_by_type(disclosure.get("sections", []), "technical_solution")
    if not section:
        return ""
    lines = _section_to_markdown(section, 2)
    return "\n".join(lines).strip() + "\n"


def has_effective_solution(markdown: str, *, min_chars: int = 200) -> bool:
    content = _strip_heading(markdown).strip()
    if len(content) < min_chars:
        return False
    signal_terms = ("模块", "步骤", "流程", "机制", "数据", "状态", "接口", "规则", "处理")
    return any(term in content for term in signal_terms)


def write_artifact(path: Path, markdown: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")


def _strip_heading(markdown: str) -> str:
    lines = [line for line in markdown.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines)


def _find_section_by_type(sections: list[dict[str, Any]], section_type: str) -> dict[str, Any] | None:
    for section in sections:
        if section.get("type") == section_type:
            return section
        found = _find_section_by_type(section.get("children", []), section_type)
        if found:
            return found
    return None


def _section_to_markdown(section: dict[str, Any], level: int) -> list[str]:
    lines = [f"{'#' * level} {section.get('title', '')}", ""]
    for block in section.get("blocks", []):
        lines.extend(_block_to_markdown(block))
        lines.append("")
    for child in section.get("children", []):
        lines.extend(_section_to_markdown(child, level + 1))
    return lines


def _block_to_markdown(block: dict[str, Any]) -> list[str]:
    block_type = block.get("type")
    if block_type == "paragraph":
        return [str(block.get("text") or "")]
    if block_type == "list":
        ordered = bool(block.get("ordered"))
        lines = []
        for index, item in enumerate(block.get("items", [])):
            prefix = f"{index + 1}. " if ordered else "- "
            lines.append(f"{prefix}{item}")
        return lines
    if block_type == "table":
        columns = [str(item) for item in block.get("columns", [])]
        rows = block.get("rows", [])
        header = "| " + " | ".join(columns) + " |"
        divider = "| " + " | ".join(["---"] * len(columns)) + " |"
        body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
        return [header, divider, *body]
    if block_type == "image":
        alt = str(block.get("alt") or "image")
        src = str(block.get("src") or "")
        caption = str(block.get("caption") or "")
        return [f"![{alt}]({src})", caption] if caption else [f"![{alt}]({src})"]
    return []
