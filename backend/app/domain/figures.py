from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from .document_tool_results import tool_failed

FIGURE_ID_PATTERN = re.compile(r"^fig_\d{6}$")
FIGURE_REF_PATTERN = re.compile(r"^figure:(?P<figure_id>fig_\d{6})$")
FIGURE_LINK_PATTERN = re.compile(r"\[(?P<label>[^\]]+)\]\(figure:(?P<figure_id>fig_\d{6})\)")
FIGURE_WIDTH = 1500
FIGURE_HEIGHT = 900

DRAWIO_XML_MAX_CHARS = 500_000


def figure_ref(figure_id: str) -> str:
    return f"figure:{figure_id}"


def figure_label(index: int) -> str:
    return f"图{index}"


def figure_caption(figure: dict[str, Any]) -> str:
    label = str(figure.get("label") or "")
    title = str(figure.get("title") or "")
    return f"{label} {title}".strip()


def drawio_updated_at(figure: dict[str, Any]) -> str:
    source = figure.get("source")
    if isinstance(source, dict):
        return str(source.get("updated_at") or "")
    return ""


def new_drawio_updated_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


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
        "drawio_updated_at": drawio_updated_at(figure),
    }


def figure_attachment(figure: dict[str, Any]) -> dict[str, str]:
    return {
        "type": "render_image",
        "ref": figure_ref(str(figure["figure_id"])),
        "purpose": "visual_review",
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
    timestamp = new_drawio_updated_at()
    return {
        "figure_id": figure_id,
        "label": figure_label(index),
        "title": title.strip(),
        "asset_path": asset_path,
        "source": {
            "type": "drawio",
            "path": source_path,
            "updated_at": timestamp,
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


def update_figure_record(figure: dict[str, Any], *, title: str | None, drawio_timestamp: str) -> dict[str, Any]:
    next_figure = dict(figure)
    if title is not None:
        next_figure["title"] = title.strip()
    source = dict(next_figure.get("source") or {})
    source["type"] = "drawio"
    source["updated_at"] = drawio_timestamp
    next_figure["source"] = source
    next_figure["updated_at"] = drawio_timestamp
    return next_figure


def parse_figure_ref(ref: str) -> str | None:
    match = FIGURE_REF_PATTERN.fullmatch(str(ref or "").strip())
    return match.group("figure_id") if match else None


def validate_drawio_xml(drawio_xml: Any) -> dict[str, Any]:
    if not isinstance(drawio_xml, str):
        return tool_failed("drawio_xml_validation_failed", "drawio_xml 必须是字符串。")
    text = drawio_xml.strip()
    if not text:
        return tool_failed("drawio_xml_validation_failed", "drawio_xml 不能为空。")
    if len(text) > DRAWIO_XML_MAX_CHARS:
        return tool_failed("drawio_xml_validation_failed", f"drawio_xml 不能超过 {DRAWIO_XML_MAX_CHARS} 个字符。")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return tool_failed("drawio_xml_validation_failed", f"drawio_xml 不是合法 XML：{exc}")

    root_tag = _local_name(root.tag)
    if root_tag not in {"mxfile", "mxGraphModel"}:
        return tool_failed("drawio_xml_validation_failed", "drawio_xml 根节点必须是 mxfile 或 mxGraphModel。")
    if root_tag == "mxfile":
        diagrams = [child for child in root if _local_name(child.tag) == "diagram"]
        if not diagrams:
            return tool_failed("drawio_xml_validation_failed", "mxfile 必须包含至少一个 diagram。")
    if not _contains_mx_graph_model(root):
        return tool_failed("drawio_xml_validation_failed", "drawio_xml 必须包含 mxGraphModel。")
    return {
        "status": "success",
        "output": {
            "drawio_xml": text + "\n",
        },
    }


def _contains_mx_graph_model(root: ET.Element) -> bool:
    if _local_name(root.tag) == "mxGraphModel":
        return True
    return any(_local_name(element.tag) == "mxGraphModel" for element in root.iter())


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
