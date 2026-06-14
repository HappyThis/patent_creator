from __future__ import annotations

from pathlib import Path
from typing import Any


def extract_technical_solution(disclosure: dict[str, Any]) -> str:
    section = find_technical_solution_section(disclosure.get("sections", []))
    if not section:
        return ""
    lines = _section_to_markdown(section, 2)
    return "\n".join(lines).strip() + "\n"


def find_technical_solution_section(sections: list[dict[str, Any]]) -> dict[str, Any] | None:
    return _find_section_by_title(sections, "技术方案")


def has_effective_solution(markdown: str, *, min_chars: int = 200) -> bool:
    content = _strip_heading(markdown).strip()
    if len(content) < min_chars:
        return False
    signal_terms = (
        "模块",
        "步骤",
        "流程",
        "机制",
        "数据",
        "状态",
        "接口",
        "规则",
        "处理",
        "结构",
        "特征",
        "协同",
        "技术效果",
        "参数",
        "材料",
        "控制",
    )
    return any(term in content for term in signal_terms)


def write_artifact(path: Path, markdown: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")


def _strip_heading(markdown: str) -> str:
    lines = [line for line in markdown.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines)


def _find_section_by_title(sections: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    for section in sections:
        if _section_title(section) == title:
            return section
        found = _find_section_by_title(section.get("sections", []), title)
        if found:
            return found
    return None


def _section_to_markdown(section: dict[str, Any], level: int) -> list[str]:
    lines = [f"{'#' * level} {_section_title(section)}", ""]
    for block in section.get("blocks", []):
        lines.extend(_block_to_markdown(block))
        lines.append("")
    for child in section.get("sections", []):
        lines.extend(_section_to_markdown(child, level + 1))
    return lines


def _section_title(section: dict[str, Any]) -> str:
    title = section.get("title")
    if isinstance(title, dict):
        return str(title.get("text") or "")
    return ""


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
