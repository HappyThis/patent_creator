from __future__ import annotations

import re
from typing import Any

from ..core import now_iso
from .document_tool_results import tool_failed

FIGURE_ID_PATTERN = re.compile(r"^fig_\d{6}$")
FIGURE_REF_PATTERN = re.compile(r"^figure:(?P<figure_id>fig_\d{6})$")
FIGURE_LINK_PATTERN = re.compile(r"\[(?P<label>[^\]]+)\]\(figure:(?P<figure_id>fig_\d{6})\)")
FIGURE_WIDTH = 1500
FIGURE_HEIGHT = 900
MAX_HTML_CHARS = 120_000
DIAGRAM_ROOT_PATTERN = re.compile(r"\bid\s*=\s*['\"]diagram['\"]", flags=re.IGNORECASE)
BLOCKED_HTML_PATTERN = re.compile(r"<\s*(script|iframe|object|embed|link)\b|javascript:|\bon[a-z]+\s*=", flags=re.IGNORECASE)
BLOCKED_EXTERNAL_RESOURCE_PATTERN = re.compile(
    r"(\bsrc\s*=|\bhref\s*=\s*['\"]\s*(?!#)|\bxlink:href\s*=\s*['\"]\s*(?!#)|url\()",
    flags=re.IGNORECASE,
)


def figure_ref(figure_id: str) -> str:
    return f"figure:{figure_id}"


def figure_label(index: int) -> str:
    return f"图{index}"


def figure_caption(figure: dict[str, Any]) -> str:
    label = str(figure.get("label") or "")
    title = str(figure.get("title") or "")
    return f"{label} {title}".strip()


def figure_summary(figure: dict[str, Any]) -> dict[str, Any]:
    figure_id = str(figure["figure_id"])
    label = str(figure.get("label") or "")
    return {
        "figure_id": figure_id,
        "ref": figure_ref(figure_id),
        "label": label,
        "title": figure.get("title") or "",
        "markdown_ref": f"[{label}]({figure_ref(figure_id)})",
        "caption": figure_caption(figure),
        "asset_path": figure.get("asset_path") or "",
        "source": figure.get("source") or {},
        "render": figure.get("render") or {},
    }


def build_figure_record(
    *,
    figure_id: str,
    index: int,
    title: str,
    source_path: str,
    render_path: str,
    asset_path: str,
) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "figure_id": figure_id,
        "label": figure_label(index),
        "title": title.strip(),
        "asset_path": asset_path,
        "source": {
            "type": "html",
            "path": source_path,
            "width": FIGURE_WIDTH,
            "height": FIGURE_HEIGHT,
        },
        "render": {
            "type": "png",
            "path": render_path,
            "width": FIGURE_WIDTH,
            "height": FIGURE_HEIGHT,
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def update_figure_record(figure: dict[str, Any], *, title: str | None) -> dict[str, Any]:
    next_figure = dict(figure)
    if title is not None:
        next_figure["title"] = title.strip()
    next_figure["updated_at"] = now_iso()
    return next_figure


def parse_figure_ref(ref: str) -> str | None:
    match = FIGURE_REF_PATTERN.fullmatch(str(ref or "").strip())
    return match.group("figure_id") if match else None


def validate_html_source(html: str) -> dict[str, Any]:
    source = html.strip()
    if not source:
        return tool_failed("figure_html_required", "HTML 源码不能为空。")
    if len(source) > MAX_HTML_CHARS:
        return tool_failed("figure_html_too_large", f"HTML 源码不能超过 {MAX_HTML_CHARS} 个字符。复杂图请拆成多张。")
    if not re.search(r"<!doctype\s+html|<html\b", source, flags=re.IGNORECASE):
        return tool_failed("figure_html_document_required", "figure_kit 需要完整 diagram.html，请包含 <!doctype html> 或 <html>。")
    if not DIAGRAM_ROOT_PATTERN.search(source):
        return tool_failed("figure_html_root_required", 'HTML 中必须包含固定画布根节点 id="diagram"。')
    blocked = BLOCKED_HTML_PATTERN.search(source) or BLOCKED_EXTERNAL_RESOURCE_PATTERN.search(source)
    if blocked:
        return tool_failed(
            "figure_html_unsafe",
            "HTML 附图不能包含脚本、资源引用、iframe/object/embed 或事件处理器；请使用纯 HTML/CSS 绘制。",
        )
    return {"status": "success", "output": {"html": source}}
