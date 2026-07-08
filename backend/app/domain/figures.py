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
BLOCKED_HTML_TAG_PATTERN = re.compile(r"<\s*(script|iframe|object|embed|link)\b", flags=re.IGNORECASE)
BLOCKED_JAVASCRIPT_URL_PATTERN = re.compile(r"javascript\s*:", flags=re.IGNORECASE)
BLOCKED_EVENT_HANDLER_PATTERN = re.compile(r"\bon[a-z]+\s*=", flags=re.IGNORECASE)
SRC_ATTRIBUTE_PATTERN = re.compile(r"\bsrc\s*=", flags=re.IGNORECASE)
HREF_ATTRIBUTE_PATTERN = re.compile(
    r"\b(?P<name>href|xlink:href)\s*=\s*(?P<quote>['\"])(?P<quoted_value>.*?)(?P=quote)|"
    r"\b(?P<unquoted_name>href|xlink:href)\s*=\s*(?P<unquoted_value>[^\s>]+)",
    flags=re.IGNORECASE | re.DOTALL,
)
URL_FUNCTION_PATTERN = re.compile(r"url\(\s*(?P<quote>['\"]?)(?P<value>.*?)(?P=quote)\s*\)", flags=re.IGNORECASE | re.DOTALL)
LOCAL_FRAGMENT_PATTERN = re.compile(r"^#[A-Za-z_][\w:.-]*$")


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
        "geometry": figure.get("geometry") or {},
    }


def build_figure_record(
    *,
    figure_id: str,
    index: int,
    title: str,
    source_path: str,
    render_path: str,
    geometry_path: str,
    geometry_report_path: str,
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
        "geometry": {
            "type": "json",
            "path": geometry_report_path,
            "raw_path": geometry_path,
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
    blocked = _blocked_html_usage(source)
    if blocked:
        return tool_failed(
            blocked["code"],
            blocked["message"],
        )
    return {"status": "success", "output": {"html": source}}


def _blocked_html_usage(source: str) -> dict[str, str] | None:
    if BLOCKED_HTML_TAG_PATTERN.search(source):
        return {
            "code": "figure_html_embed_blocked",
            "message": "HTML 附图不能包含 script、iframe、object、embed 或 link 标签；请使用自包含的纯 HTML/CSS/SVG 绘制。",
        }
    if BLOCKED_JAVASCRIPT_URL_PATTERN.search(source):
        return {
            "code": "figure_html_javascript_url_blocked",
            "message": "HTML 附图不能包含 javascript: URL；请使用无脚本的静态结构图。",
        }
    if BLOCKED_EVENT_HANDLER_PATTERN.search(source):
        return {
            "code": "figure_html_event_handler_blocked",
            "message": "HTML 附图不能包含 onclick/onload 等事件处理器；请使用无脚本的静态结构图。",
        }
    if SRC_ATTRIBUTE_PATTERN.search(source):
        return {
            "code": "figure_html_external_src_blocked",
            "message": "HTML 附图不能包含 src 资源引用；请不要使用外部图片、脚本、字体或媒体资源。",
        }
    for match in HREF_ATTRIBUTE_PATTERN.finditer(source):
        value = match.group("quoted_value") if match.group("quoted_value") is not None else match.group("unquoted_value")
        if not _is_local_fragment_reference(value):
            return {
                "code": "figure_html_external_href_blocked",
                "message": "HTML 附图的 href/xlink:href 只能引用当前文档内的 #id；请不要引用外部资源。",
            }
    for match in URL_FUNCTION_PATTERN.finditer(source):
        if not _is_local_fragment_reference(match.group("value")):
            return {
                "code": "figure_html_external_url_blocked",
                "message": "HTML 附图的 url(...) 只能引用当前文档内的 #id，例如 url(#arrow)；请不要引用外部图片、字体或样式资源。",
            }
    return None


def _is_local_fragment_reference(value: str | None) -> bool:
    return bool(LOCAL_FRAGMENT_PATTERN.fullmatch(str(value or "").strip()))
