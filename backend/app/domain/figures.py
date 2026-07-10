from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from math import isfinite
from typing import Any

from .document_tool_results import tool_failed

FIGURE_ID_PATTERN = re.compile(r"^fig_\d{6}$")
FIGURE_REF_PATTERN = re.compile(r"^figure:(?P<figure_id>fig_\d{6})$")
FIGURE_LINK_PATTERN = re.compile(r"\[(?P<label>[^\]]+)\]\(figure:(?P<figure_id>fig_\d{6})\)")
FIGURE_WIDTH = 1500
FIGURE_HEIGHT = 900

DRAWIO_XML_MAX_CHARS = 500_000
MODEL_REVIEW_IMAGE_MAX_BYTES = 2_000_000
CANVAS_TOLERANCE = 0.5


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
        "drawio_updated_at": drawio_updated_at(figure),
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


def update_figure_record(
    figure: dict[str, Any],
    *,
    title: str | None,
    drawio_timestamp: str,
    source_path: str,
    render_path: str,
) -> dict[str, Any]:
    next_figure = dict(figure)
    if title is not None:
        next_figure["title"] = title.strip()
    source = dict(next_figure.get("source") or {})
    source["type"] = "drawio"
    source["path"] = source_path
    source["updated_at"] = drawio_timestamp
    next_figure["source"] = source
    render = dict(next_figure.get("render") or {})
    render.update(
        {
            "type": "png",
            "path": render_path,
            "width": FIGURE_WIDTH,
            "height": FIGURE_HEIGHT,
        }
    )
    next_figure["render"] = render
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
    graph_model_result = _single_graph_model(root)
    if graph_model_result["status"] == "failed":
        return graph_model_result
    graph_model = graph_model_result["output"]["graph_model"]
    canvas_result = _validate_canvas(graph_model)
    if canvas_result["status"] == "failed":
        return canvas_result
    return {
        "status": "success",
        "output": {
            "drawio_xml": text + "\n",
        },
    }


def _single_graph_model(root: ET.Element) -> dict[str, Any]:
    if _local_name(root.tag) == "mxGraphModel":
        return {"status": "success", "output": {"graph_model": root}}

    diagrams = [child for child in root if _local_name(child.tag) == "diagram"]
    if len(diagrams) != 1:
        return tool_failed("drawio_xml_validation_failed", "mxfile 必须且只能包含一个 diagram。")
    graph_models = [element for element in diagrams[0].iter() if _local_name(element.tag) == "mxGraphModel"]
    if len(graph_models) != 1:
        return tool_failed(
            "drawio_xml_validation_failed",
            "diagram 必须包含一个未压缩的 mxGraphModel。请导出完整、未压缩的 draw.io XML。",
        )
    return {"status": "success", "output": {"graph_model": graph_models[0]}}


def _validate_canvas(graph_model: ET.Element) -> dict[str, Any]:
    page_width = _finite_number(graph_model.get("pageWidth"))
    page_height = _finite_number(graph_model.get("pageHeight"))
    if page_width != float(FIGURE_WIDTH) or page_height != float(FIGURE_HEIGHT):
        return tool_failed(
            "drawio_canvas_invalid",
            f"mxGraphModel 页面必须为 {FIGURE_WIDTH}x{FIGURE_HEIGHT}，实际为 {graph_model.get('pageWidth') or 'missing'}x{graph_model.get('pageHeight') or 'missing'}。",
        )

    cells = {
        str(cell.get("id")): cell
        for cell in graph_model.iter()
        if _local_name(cell.tag) == "mxCell" and cell.get("id")
    }
    offset_cache: dict[str, tuple[float, float]] = {}
    visiting: set[str] = set()

    def absolute_offset(cell_id: str) -> tuple[float, float]:
        if cell_id in offset_cache:
            return offset_cache[cell_id]
        if cell_id in visiting:
            raise ValueError(f"parent 循环：{cell_id}")
        visiting.add(cell_id)
        cell = cells.get(cell_id)
        if cell is None:
            result = (0.0, 0.0)
        else:
            geometry = _cell_geometry(cell)
            x = _finite_number(geometry.get("x")) if geometry is not None else 0.0
            y = _finite_number(geometry.get("y")) if geometry is not None else 0.0
            x = x if x is not None else 0.0
            y = y if y is not None else 0.0
            parent_id = str(cell.get("parent") or "")
            parent_x, parent_y = absolute_offset(parent_id) if parent_id in cells else (0.0, 0.0)
            result = (parent_x + x, parent_y + y)
        visiting.remove(cell_id)
        offset_cache[cell_id] = result
        return result

    for cell_id, cell in cells.items():
        if cell.get("vertex") != "1":
            continue
        geometry = _cell_geometry(cell)
        if geometry is None or geometry.get("relative") == "1":
            continue
        width = _finite_number(geometry.get("width"))
        height = _finite_number(geometry.get("height"))
        if width is None or height is None or width <= 0 or height <= 0:
            return tool_failed("drawio_canvas_invalid", f"节点 {cell_id} 缺少有效的 width/height。")
        try:
            x, y = absolute_offset(cell_id)
        except ValueError as exc:
            return tool_failed("drawio_canvas_invalid", str(exc))
        if _outside_canvas(x, y) or _outside_canvas(x + width, y + height):
            return tool_failed(
                "drawio_canvas_overflow",
                f"节点 {cell_id} 超出 {FIGURE_WIDTH}x{FIGURE_HEIGHT} 页面边界：x={x:g}, y={y:g}, width={width:g}, height={height:g}。",
            )

    absolute_points: list[ET.Element] = []
    for cell in cells.values():
        if cell.get("edge") != "1":
            continue
        geometry = _cell_geometry(cell)
        if geometry is None:
            continue
        absolute_points.extend(
            point
            for point in geometry
            if _local_name(point.tag) == "mxPoint" and point.get("as") in {"sourcePoint", "targetPoint"}
        )
        for points_array in geometry:
            if _local_name(points_array.tag) == "Array" and points_array.get("as") == "points":
                absolute_points.extend(
                    point for point in points_array if _local_name(point.tag) == "mxPoint"
                )
    for point in absolute_points:
        x = _finite_number(point.get("x"))
        y = _finite_number(point.get("y"))
        if x is None or y is None:
            continue
        if _outside_canvas(x, y):
            return tool_failed(
                "drawio_canvas_overflow",
                f"连线拐点超出 {FIGURE_WIDTH}x{FIGURE_HEIGHT} 页面边界：x={x:g}, y={y:g}。",
            )
    return {"status": "success", "output": {}}


def _cell_geometry(cell: ET.Element) -> ET.Element | None:
    return next((child for child in cell if _local_name(child.tag) == "mxGeometry"), None)


def _finite_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if isfinite(number) else None


def _outside_canvas(x: float, y: float) -> bool:
    return (
        x < -CANVAS_TOLERANCE
        or y < -CANVAS_TOLERANCE
        or x > FIGURE_WIDTH + CANVAS_TOLERANCE
        or y > FIGURE_HEIGHT + CANVAS_TOLERANCE
    )


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
