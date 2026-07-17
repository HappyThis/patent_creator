from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from math import hypot
from typing import Any

from .document_tool_results import tool_failed
from .figure_scene import (
    cell_geometry as _cell_geometry,
    edge_waypoints as _edge_waypoints,
    finite_number as _finite_number,
    local_name as _local_name,
    parse_drawio_style as _parse_drawio_style,
)
from .figure_visual_lint import inspect_figure_visuals
from .figure_visual_system import apply_visual_defaults

FIGURE_ID_PATTERN = re.compile(r"^fig_\d{6}$")
FIGURE_REF_PATTERN = re.compile(r"^figure:(?P<figure_id>fig_\d{6})$")
FIGURE_LINK_PATTERN = re.compile(r"\[(?P<label>[^\]]+)\]\(figure:(?P<figure_id>fig_\d{6})\)")
FIGURE_WIDTH = 1500
FIGURE_HEIGHT = 900

DRAWIO_XML_MAX_CHARS = 500_000
MODEL_REVIEW_IMAGE_MAX_BYTES = 2_000_000
CANVAS_TOLERANCE = 0.5
EDGE_ENDPOINT_TOLERANCE = 1.0
MIN_EXPLICIT_EDGE_SEGMENT_LENGTH = 12.0
MIN_VALID_EXPLICIT_EDGE_SEGMENT_LENGTH = 4.0
AUXILIARY_EDGE_ROLE = "auxiliary"
LABELED_EDGE_HORIZONTAL_PAGE_MARGIN = 120.0
LABELED_EDGE_VERTICAL_PAGE_MARGIN = 60.0

MXFILE_DEFAULT_ATTRIBUTES = {"host": "app.diagrams.net"}
DIAGRAM_DEFAULT_ATTRIBUTES = {"id": "page-1", "name": "Page-1"}
GRAPH_MODEL_DEFAULT_ATTRIBUTES = {
    "grid": "1",
    "gridSize": "10",
    "guides": "1",
    "connect": "1",
    "arrows": "1",
    "page": "1",
    "pageScale": "1",
    "pageWidth": str(FIGURE_WIDTH),
    "pageHeight": str(FIGURE_HEIGHT),
    "math": "0",
    "shadow": "0",
}


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


def write_figure_record(
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
        return _validation_failed([_validation_issue("drawio_xml_validation_failed", "drawio_xml 必须是字符串。")])
    text = drawio_xml.strip()
    if not text:
        return _validation_failed([_validation_issue("drawio_xml_validation_failed", "drawio_xml 不能为空。")])
    if len(text) > DRAWIO_XML_MAX_CHARS:
        return _validation_failed(
            [_validation_issue("drawio_xml_validation_failed", f"drawio_xml 不能超过 {DRAWIO_XML_MAX_CHARS} 个字符。")]
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return _validation_failed([_validation_issue("drawio_xml_validation_failed", f"drawio_xml 不是合法 XML：{exc}")])

    if _local_name(root.tag) != "mxfile":
        return _validation_failed(
            [
                _validation_issue(
                    "drawio_xml_format_invalid",
                    "drawio_xml 只支持 mxfile > 单个 diagram > 未压缩 mxGraphModel 的标准结构；不再接受裸 mxGraphModel。请按 figure_kit 工具说明中的完整示例提交。",
                )
            ]
        )
    graph_model_result = _single_graph_model(root)
    if graph_model_result["status"] == "failed":
        return graph_model_result
    diagram = graph_model_result["output"]["diagram"]
    graph_model = graph_model_result["output"]["graph_model"]

    normalized_fields = _fill_safe_drawio_defaults(root, diagram, graph_model)
    normalized_fields.extend(apply_visual_defaults(graph_model))
    errors = _validate_graph_structure(graph_model)
    errors.extend(_validate_canvas(graph_model))
    edge_errors, edge_warnings = _validate_semantic_edges(graph_model)
    errors.extend(edge_errors)
    warnings = edge_warnings + inspect_figure_visuals(graph_model)
    if errors:
        return _validation_failed(errors, warnings=warnings, normalized_fields=normalized_fields)

    ET.indent(root, space="  ")
    normalized_xml = ET.tostring(root, encoding="unicode", short_empty_elements=True).strip() + "\n"
    return {
        "status": "success",
        "output": {
            "drawio_xml": normalized_xml,
            "normalized": bool(normalized_fields),
            "normalized_fields": normalized_fields,
            "warnings": warnings,
        },
    }


def _single_graph_model(root: ET.Element) -> dict[str, Any]:
    diagrams = [child for child in root if _local_name(child.tag) == "diagram"]
    if len(diagrams) != 1:
        return _validation_failed(
            [_validation_issue("drawio_xml_format_invalid", "mxfile 必须且只能直接包含一个 diagram。")]
        )
    graph_models = [element for element in diagrams[0].iter() if _local_name(element.tag) == "mxGraphModel"]
    if len(graph_models) != 1:
        return _validation_failed(
            [
                _validation_issue(
                    "drawio_xml_format_invalid",
                    "diagram 必须包含且只能包含一个未压缩的 mxGraphModel。请按 figure_kit 示例提交完整 XML。",
                )
            ]
        )
    return {"status": "success", "output": {"diagram": diagrams[0], "graph_model": graph_models[0]}}


def _fill_safe_drawio_defaults(
    root: ET.Element,
    diagram: ET.Element,
    graph_model: ET.Element,
) -> list[str]:
    normalized_fields: list[str] = []
    for prefix, element, defaults in (
        ("mxfile", root, MXFILE_DEFAULT_ATTRIBUTES),
        ("diagram", diagram, DIAGRAM_DEFAULT_ATTRIBUTES),
        ("mxGraphModel", graph_model, GRAPH_MODEL_DEFAULT_ATTRIBUTES),
    ):
        for name, value in defaults.items():
            if element.get(name) is not None:
                continue
            element.set(name, value)
            normalized_fields.append(f"{prefix}.{name}={value}")
    return normalized_fields


def _validate_graph_structure(graph_model: ET.Element) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    graph_roots = [child for child in graph_model if _local_name(child.tag) == "root"]
    if len(graph_roots) != 1:
        return [
            _validation_issue(
                "drawio_graph_structure_invalid",
                "mxGraphModel 必须且只能直接包含一个 root。",
            )
        ]

    all_cells = [cell for cell in graph_roots[0].iter() if _local_name(cell.tag) == "mxCell"]
    ids: dict[str, ET.Element] = {}
    for cell in all_cells:
        cell_id = str(cell.get("id") or "")
        if not cell_id:
            errors.append(_validation_issue("drawio_cell_id_missing", "mxCell 缺少 id。"))
            continue
        if cell_id in ids:
            errors.append(_validation_issue("drawio_cell_id_duplicate", f"mxCell id={cell_id} 重复。", cell_id=cell_id))
            continue
        ids[cell_id] = cell

    if "0" not in ids:
        errors.append(_validation_issue("drawio_base_cell_missing", "缺少基础 mxCell id=0。", cell_id="0"))
    if "1" not in ids:
        errors.append(_validation_issue("drawio_base_cell_missing", "缺少基础 mxCell id=1 parent=0。", cell_id="1"))
    elif ids["1"].get("parent") != "0":
        errors.append(_validation_issue("drawio_base_cell_invalid", "基础 mxCell id=1 的 parent 必须为 0。", cell_id="1"))

    for cell_id, cell in ids.items():
        parent_id = str(cell.get("parent") or "")
        if cell_id == "0":
            continue
        if not parent_id:
            errors.append(_validation_issue("drawio_parent_missing", f"mxCell {cell_id} 缺少 parent。", cell_id=cell_id))
        elif parent_id not in ids:
            errors.append(
                _validation_issue(
                    "drawio_parent_reference_invalid",
                    f"mxCell {cell_id} 的 parent={parent_id} 不存在。",
                    cell_id=cell_id,
                )
            )

    for cell_id in ids:
        seen: set[str] = set()
        current_id = cell_id
        while current_id in ids:
            if current_id in seen:
                errors.append(
                    _validation_issue(
                        "drawio_parent_cycle",
                        f"mxCell {cell_id} 的 parent 链形成循环：{current_id}。",
                        cell_id=cell_id,
                    )
                )
                break
            seen.add(current_id)
            current_id = str(ids[current_id].get("parent") or "")
    return errors


def _validate_canvas(graph_model: ET.Element) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    page_width = _finite_number(graph_model.get("pageWidth"))
    page_height = _finite_number(graph_model.get("pageHeight"))
    if page_width != float(FIGURE_WIDTH) or page_height != float(FIGURE_HEIGHT):
        errors.append(
            _validation_issue(
                "drawio_canvas_invalid",
                f"mxGraphModel 页面必须为 {FIGURE_WIDTH}x{FIGURE_HEIGHT}，实际为 {graph_model.get('pageWidth') or 'missing'}x{graph_model.get('pageHeight') or 'missing'}。",
            )
        )
    if graph_model.get("page") != "1" or _finite_number(graph_model.get("pageScale")) != 1.0:
        errors.append(
            _validation_issue(
                "drawio_canvas_invalid",
                f"mxGraphModel 必须使用 page=1、pageScale=1，实际为 page={graph_model.get('page')}, pageScale={graph_model.get('pageScale')}。",
            )
        )

    cells = {
        str(cell.get("id")): cell
        for cell in graph_model.iter()
        if _local_name(cell.tag) == "mxCell" and cell.get("id")
    }
    offset_cache: dict[str, tuple[float, float] | None] = {}
    visiting: set[str] = set()

    def absolute_offset(cell_id: str) -> tuple[float, float] | None:
        if cell_id in offset_cache:
            return offset_cache[cell_id]
        if cell_id in visiting:
            return None
        visiting.add(cell_id)
        cell = cells.get(cell_id)
        if cell is None:
            result: tuple[float, float] | None = (0.0, 0.0)
        else:
            geometry = _cell_geometry(cell)
            x = _finite_number(geometry.get("x")) if geometry is not None else 0.0
            y = _finite_number(geometry.get("y")) if geometry is not None else 0.0
            x = x if x is not None else 0.0
            y = y if y is not None else 0.0
            parent_id = str(cell.get("parent") or "")
            parent_offset = absolute_offset(parent_id) if parent_id in cells else (0.0, 0.0)
            result = None if parent_offset is None else (parent_offset[0] + x, parent_offset[1] + y)
        visiting.discard(cell_id)
        offset_cache[cell_id] = result
        return result

    for cell_id, cell in cells.items():
        if cell.get("vertex") != "1":
            continue
        geometry = _cell_geometry(cell)
        if geometry is None:
            errors.append(_validation_issue("drawio_canvas_invalid", f"节点 {cell_id} 缺少 mxGeometry。", cell_id=cell_id))
            continue
        if geometry.get("relative") == "1":
            continue
        for axis in ("x", "y"):
            raw_value = geometry.get(axis)
            if raw_value is not None and _finite_number(raw_value) is None:
                errors.append(
                    _validation_issue(
                        "drawio_canvas_invalid",
                        f"节点 {cell_id} 的 {axis}={raw_value} 不是有效有限数值。",
                        cell_id=cell_id,
                    )
                )
        width = _finite_number(geometry.get("width"))
        height = _finite_number(geometry.get("height"))
        if width is None or height is None or width <= 0 or height <= 0:
            errors.append(
                _validation_issue(
                    "drawio_canvas_invalid",
                    f"节点 {cell_id} 缺少有效的正数 width/height。",
                    cell_id=cell_id,
                )
            )
            continue
        offset = absolute_offset(cell_id)
        if offset is None:
            continue
        x, y = offset
        if _outside_canvas(x, y) or _outside_canvas(x + width, y + height):
            errors.append(
                _validation_issue(
                    "drawio_canvas_overflow",
                    f"节点 {cell_id} 超出 {FIGURE_WIDTH}x{FIGURE_HEIGHT} 页面边界：x={x:g}, y={y:g}, width={width:g}, height={height:g}。",
                    cell_id=cell_id,
                )
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
    for point_index, point in enumerate(absolute_points, start=1):
        x = _finite_number(point.get("x"))
        y = _finite_number(point.get("y"))
        if x is None or y is None:
            errors.append(
                _validation_issue(
                    "drawio_edge_point_invalid",
                    f"第 {point_index} 个显式连线点缺少有效的有限 x/y。",
                )
            )
            continue
        if _outside_canvas(x, y):
            errors.append(
                _validation_issue(
                    "drawio_canvas_overflow",
                    f"连线拐点超出 {FIGURE_WIDTH}x{FIGURE_HEIGHT} 页面边界：x={x:g}, y={y:g}。",
                )
            )
    return errors


def _validate_semantic_edges(
    graph_model: ET.Element,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    cells = {
        str(cell.get("id")): cell
        for cell in graph_model.iter()
        if _local_name(cell.tag) == "mxCell" and cell.get("id")
    }
    bounds_cache: dict[str, tuple[float, float, float, float] | None] = {}
    semantic_signatures: dict[tuple[Any, ...], str] = {}

    for edge_id, edge in cells.items():
        if edge.get("edge") != "1":
            continue
        style = _parse_drawio_style(edge.get("style"))
        is_auxiliary = style.get("edgeRole") == AUXILIARY_EDGE_ROLE
        if is_auxiliary and _is_explicitly_arrowless_auxiliary(style):
            for endpoint_name in ("source", "target"):
                endpoint_id = str(edge.get(endpoint_name) or "")
                endpoint = cells.get(endpoint_id)
                if endpoint_id and (endpoint is None or endpoint.get("vertex") != "1"):
                    errors.append(
                        _validation_issue(
                            "drawio_auxiliary_edge_reference_invalid",
                            f"辅助线 {edge_id} 的 {endpoint_name}={endpoint_id} 不是有效节点。",
                            cell_id=edge_id,
                        )
                    )
            continue

        source_id = str(edge.get("source") or "")
        target_id = str(edge.get("target") or "")
        if not source_id or not target_id:
            missing = "source 和 target" if not source_id and not target_id else ("source" if not source_id else "target")
            role_note = (
                "带箭头的辅助线同样必须连接真实节点；只有显式 endArrow=none 的无箭头辅助线可以悬空。"
                if is_auxiliary
                else "若它确实只是装饰线，请同时设置 edgeRole=auxiliary;endArrow=none;。"
            )
            errors.append(
                _validation_issue(
                    "drawio_semantic_edge_dangling",
                    f"连线 {edge_id} 缺少 {missing}，不允许悬空。{role_note}",
                    cell_id=edge_id,
                )
            )
            continue

        source = cells.get(source_id)
        target = cells.get(target_id)
        if source is None or source.get("vertex") != "1":
            errors.append(
                _validation_issue(
                    "drawio_semantic_edge_reference_invalid",
                    f"语义连线 {edge_id} 的 source={source_id} 不是有效节点。",
                    cell_id=edge_id,
                )
            )
        if target is None or target.get("vertex") != "1":
            errors.append(
                _validation_issue(
                    "drawio_semantic_edge_reference_invalid",
                    f"语义连线 {edge_id} 的 target={target_id} 不是有效节点。",
                    cell_id=edge_id,
                )
            )
        if source is None or source.get("vertex") != "1" or target is None or target.get("vertex") != "1":
            continue

        geometry = _cell_geometry(edge)
        waypoints = _edge_waypoints(geometry)
        page_margin_warning = _labeled_edge_page_margin_warning(edge_id, edge, waypoints)
        if page_margin_warning is not None:
            warnings.append(page_margin_warning)
        source_result = _validate_edge_endpoint_route(
            edge_id=edge_id,
            endpoint_name="起点",
            cell_id=source_id,
            cell=source,
            cells=cells,
            style=style,
            axis_prefix="exit",
            waypoint=waypoints[0] if waypoints else None,
            bounds_cache=bounds_cache,
        )
        if source_result is not None:
            (errors if source_result["code"] == "drawio_semantic_edge_anchor_invalid" else warnings).append(source_result)
        target_result = _validate_edge_endpoint_route(
            edge_id=edge_id,
            endpoint_name="终点",
            cell_id=target_id,
            cell=target,
            cells=cells,
            style=style,
            axis_prefix="entry",
            waypoint=waypoints[-1] if waypoints else None,
            bounds_cache=bounds_cache,
        )
        if target_result is not None:
            (errors if target_result["code"] == "drawio_semantic_edge_anchor_invalid" else warnings).append(target_result)
        segment_errors, segment_warnings = _validate_explicit_edge_segments(
            edge_id=edge_id,
            source_id=source_id,
            source=source,
            target_id=target_id,
            target=target,
            cells=cells,
            style=style,
            waypoints=waypoints,
            bounds_cache=bounds_cache,
        )
        errors.extend(segment_errors)
        warnings.extend(segment_warnings)
        duplicate_signature = (
            source_id,
            target_id,
            str(edge.get("value") or ""),
            tuple(waypoints),
            style.get("exitX"),
            style.get("exitY"),
            style.get("entryX"),
            style.get("entryY"),
            style.get("edgeStyle"),
            style.get("orthogonalLoop"),
            style.get("jettySize"),
        )
        previous_edge = semantic_signatures.get(duplicate_signature)
        if previous_edge is None:
            semantic_signatures[duplicate_signature] = edge_id
        else:
            errors.append(
                _validation_issue(
                    "drawio_semantic_edge_duplicate",
                    f"语义连线 {edge_id} 与 {previous_edge} 的起止节点、标签和显式走线完全重复，请删除重复连线。",
                    cell_id=edge_id,
                    related_cell_ids=[previous_edge],
                )
            )

    return errors, warnings


def _validate_edge_endpoint_route(
    *,
    edge_id: str,
    endpoint_name: str,
    cell_id: str,
    cell: ET.Element,
    cells: dict[str, ET.Element],
    style: dict[str, str],
    axis_prefix: str,
    waypoint: tuple[float, float] | None,
    bounds_cache: dict[str, tuple[float, float, float, float] | None],
) -> dict[str, Any] | None:
    x_value = style.get(f"{axis_prefix}X")
    y_value = style.get(f"{axis_prefix}Y")
    if x_value is None and y_value is None:
        return None
    if x_value is None or y_value is None:
        return _validation_issue(
            "drawio_semantic_edge_anchor_invalid",
            f"语义连线 {edge_id} 的{endpoint_name}锚点必须同时提供 {axis_prefix}X 和 {axis_prefix}Y。",
            cell_id=edge_id,
        )
    anchor_x = _finite_number(x_value)
    anchor_y = _finite_number(y_value)
    if anchor_x is None or anchor_y is None or not (0.0 <= anchor_x <= 1.0 and 0.0 <= anchor_y <= 1.0):
        return _validation_issue(
            "drawio_semantic_edge_anchor_invalid",
            f"语义连线 {edge_id} 的{endpoint_name}锚点必须位于节点归一化范围 0..1 内，实际为 {axis_prefix}X={x_value}, {axis_prefix}Y={y_value}。",
            cell_id=edge_id,
        )
    shape = str(_parse_drawio_style(cell.get("style")).get("shape") or "").strip().lower()
    if shape == "rhombus" and abs(abs(anchor_x - 0.5) + abs(anchor_y - 0.5) - 0.5) > 0.02:
        return _validation_issue(
            "drawio_semantic_edge_anchor_invalid",
            (
                f"语义连线 {edge_id} 的{endpoint_name}锚点 {axis_prefix}X={anchor_x:g}, {axis_prefix}Y={anchor_y:g} "
                f"不在菱形节点 {cell_id} 的实际边界上，会渲染成从包围盒半空起止的悬空短线。"
                "请把锚点移到菱形边界（归一化坐标满足 |X-0.5|+|Y-0.5|=0.5），"
                "或从菱形合法锚点连接到明确的分叉/汇聚节点后再分支。"
            ),
            cell_id=edge_id,
        )
    if waypoint is None:
        return None
    if not _supports_hard_endpoint_geometry(cell, axis_prefix=axis_prefix, style=style):
        return None

    geometry = _cell_geometry(cell)
    if geometry is not None and geometry.get("relative") == "1":
        return None
    bounds = _absolute_vertex_bounds(cell_id, cells, bounds_cache, set())
    if bounds is None:
        return None
    x, y, width, height = bounds
    absolute_anchor = (x + width * anchor_x, y + height * anchor_y)
    center = (x + width / 2.0, y + height / 2.0)
    if _waypoint_leaves_anchor_orthogonally(absolute_anchor, center, waypoint):
        return None
    return _validation_issue(
        "drawio_semantic_edge_route_invalid",
        (
            f"语义连线 {edge_id} 的{endpoint_name}没有从节点 {cell_id} 沿边界法向离开或进入："
            f"锚点约为 ({absolute_anchor[0]:g}, {absolute_anchor[1]:g})，相邻拐点为 ({waypoint[0]:g}, {waypoint[1]:g})。"
            "请先从锚点水平或垂直延伸一段再转弯，或删除高风险的显式锚点/拐点交给 draw.io 自动连接。"
        ),
        cell_id=edge_id,
    )


def _validate_explicit_edge_segments(
    *,
    edge_id: str,
    source_id: str,
    source: ET.Element,
    target_id: str,
    target: ET.Element,
    cells: dict[str, ET.Element],
    style: dict[str, str],
    waypoints: list[tuple[float, float]],
    bounds_cache: dict[str, tuple[float, float, float, float] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not waypoints:
        return errors, warnings
    named_segments = _known_explicit_edge_segments(
        source_id=source_id,
        source=source,
        target_id=target_id,
        target=target,
        cells=cells,
        style=style,
        waypoints=waypoints,
        bounds_cache=bounds_cache,
        include_names=True,
    )
    for left_name, left, right_name, right in named_segments:
        length = hypot(right[0] - left[0], right[1] - left[1])
        if length >= MIN_EXPLICIT_EDGE_SEGMENT_LENGTH:
            continue
        issue = _validation_issue(
            "drawio_semantic_edge_segment_invalid"
            if length < MIN_VALID_EXPLICIT_EDGE_SEGMENT_LENGTH
            else "drawio_semantic_edge_segment_too_short",
            (
                f"语义连线 {edge_id} 的显式走线在{left_name}与{right_name}之间只有约 {length:g}px。"
                + (
                    f"小于 {MIN_VALID_EXPLICIT_EDGE_SEGMENT_LENGTH:g}px，会形成接近零长度的小折断，必须合并相邻拐点。"
                    if length < MIN_VALID_EXPLICIT_EDGE_SEGMENT_LENGTH
                    else f"处于 {MIN_VALID_EXPLICIT_EDGE_SEGMENT_LENGTH:g}–{MIN_EXPLICIT_EDGE_SEGMENT_LENGTH:g}px 的短线警告范围，建议拉开走线通道或删除不必要的显式拐点。"
                )
            ),
            cell_id=edge_id,
        )
        (errors if length < MIN_VALID_EXPLICIT_EDGE_SEGMENT_LENGTH else warnings).append(issue)
    return errors, warnings


def _labeled_edge_page_margin_warning(
    edge_id: str,
    edge: ET.Element,
    waypoints: list[tuple[float, float]],
) -> dict[str, Any] | None:
    label = re.sub(r"<[^>]+>", " ", str(edge.get("value") or "")).strip()
    if not label or not waypoints:
        return None
    for x, y in waypoints:
        distances = (
            ("左", x, LABELED_EDGE_HORIZONTAL_PAGE_MARGIN),
            ("右", FIGURE_WIDTH - x, LABELED_EDGE_HORIZONTAL_PAGE_MARGIN),
            ("上", y, LABELED_EDGE_VERTICAL_PAGE_MARGIN),
            ("下", FIGURE_HEIGHT - y, LABELED_EDGE_VERTICAL_PAGE_MARGIN),
        )
        risky = [(side, distance, margin) for side, distance, margin in distances if distance < margin]
        if not risky:
            continue
        side, distance, margin = min(risky, key=lambda item: item[1] / item[2])
        return _validation_issue(
            "drawio_edge_label_page_margin",
            (
                f"带标签语义连线 {edge_id} 的显式拐点 ({x:g}, {y:g}) 距{side}侧页面边界仅约 {distance:g}px，"
                f"小于建议安全边距 {margin:g}px。边标签实际渲染后可能跨入相邻页面，"
                f"使 1500x900 导出被等比缩成 1500x450 等异常尺寸。"
                "请把外绕通道和标签向画布内移动，或为标签安排明确且不越界的位置。"
            ),
            cell_id=edge_id,
        )
    return None


def _known_explicit_edge_segments(
    *,
    source_id: str,
    source: ET.Element,
    target_id: str,
    target: ET.Element,
    cells: dict[str, ET.Element],
    style: dict[str, str],
    waypoints: list[tuple[float, float]],
    bounds_cache: dict[str, tuple[float, float, float, float] | None],
    include_names: bool = False,
) -> list[Any]:
    if not waypoints:
        return []
    named_points: list[tuple[str, tuple[float, float]]] = []
    source_anchor = _safe_absolute_style_anchor(source_id, source, cells, style, "exit", bounds_cache)
    if source_anchor is not None:
        named_points.append(("起点锚点", source_anchor))
    named_points.extend((f"拐点 {index}", point) for index, point in enumerate(waypoints, start=1))
    target_anchor = _safe_absolute_style_anchor(target_id, target, cells, style, "entry", bounds_cache)
    if target_anchor is not None:
        named_points.append(("终点锚点", target_anchor))
    if include_names:
        return [
            (left_name, left, right_name, right)
            for (left_name, left), (right_name, right) in zip(named_points, named_points[1:])
        ]
    return [(left, right) for (_, left), (_, right) in zip(named_points, named_points[1:])]


def _supports_hard_endpoint_geometry(
    cell: ET.Element,
    *,
    axis_prefix: str,
    style: dict[str, str],
) -> bool:
    cell_style = _parse_drawio_style(cell.get("style"))
    shape = str(cell_style.get("shape") or "").strip().lower()
    if shape not in {"", "rectangle"}:
        return False
    if style.get(f"{axis_prefix}Perimeter") == "0":
        return False
    return True


def _safe_absolute_style_anchor(
    cell_id: str,
    cell: ET.Element,
    cells: dict[str, ET.Element],
    style: dict[str, str],
    axis_prefix: str,
    bounds_cache: dict[str, tuple[float, float, float, float] | None],
) -> tuple[float, float] | None:
    if not _supports_hard_endpoint_geometry(cell, axis_prefix=axis_prefix, style=style):
        return None
    return _absolute_style_anchor(cell_id, cell, cells, style, axis_prefix, bounds_cache)


def _absolute_style_anchor(
    cell_id: str,
    cell: ET.Element,
    cells: dict[str, ET.Element],
    style: dict[str, str],
    axis_prefix: str,
    bounds_cache: dict[str, tuple[float, float, float, float] | None],
) -> tuple[float, float] | None:
    anchor_x = _finite_number(style.get(f"{axis_prefix}X"))
    anchor_y = _finite_number(style.get(f"{axis_prefix}Y"))
    if anchor_x is None or anchor_y is None:
        return None
    geometry = _cell_geometry(cell)
    if geometry is not None and geometry.get("relative") == "1":
        return None
    bounds = _absolute_vertex_bounds(cell_id, cells, bounds_cache, set())
    if bounds is None:
        return None
    x, y, width, height = bounds
    return (x + width * anchor_x, y + height * anchor_y)


def _absolute_vertex_bounds(
    cell_id: str,
    cells: dict[str, ET.Element],
    cache: dict[str, tuple[float, float, float, float] | None],
    visiting: set[str],
) -> tuple[float, float, float, float] | None:
    if cell_id in cache:
        return cache[cell_id]
    if cell_id in visiting:
        return None
    visiting.add(cell_id)
    cell = cells.get(cell_id)
    geometry = _cell_geometry(cell) if cell is not None else None
    if geometry is None:
        result = None
    else:
        x = _finite_number(geometry.get("x")) or 0.0
        y = _finite_number(geometry.get("y")) or 0.0
        width = _finite_number(geometry.get("width"))
        height = _finite_number(geometry.get("height"))
        if width is None or height is None:
            result = None
        else:
            parent_id = str(cell.get("parent") or "")
            parent = cells.get(parent_id)
            if parent is not None and parent.get("vertex") == "1":
                parent_bounds = _absolute_vertex_bounds(parent_id, cells, cache, visiting)
                if parent_bounds is not None:
                    x += parent_bounds[0]
                    y += parent_bounds[1]
            result = (x, y, width, height)
    visiting.remove(cell_id)
    cache[cell_id] = result
    return result


def _waypoint_leaves_anchor_orthogonally(
    anchor: tuple[float, float],
    center: tuple[float, float],
    waypoint: tuple[float, float],
) -> bool:
    anchor_x, anchor_y = anchor
    center_x, center_y = center
    point_x, point_y = waypoint
    vertical = abs(point_x - anchor_x) <= EDGE_ENDPOINT_TOLERANCE
    if vertical:
        if anchor_y < center_y - EDGE_ENDPOINT_TOLERANCE:
            vertical = point_y <= anchor_y + EDGE_ENDPOINT_TOLERANCE
        elif anchor_y > center_y + EDGE_ENDPOINT_TOLERANCE:
            vertical = point_y >= anchor_y - EDGE_ENDPOINT_TOLERANCE
    horizontal = abs(point_y - anchor_y) <= EDGE_ENDPOINT_TOLERANCE
    if horizontal:
        if anchor_x < center_x - EDGE_ENDPOINT_TOLERANCE:
            horizontal = point_x <= anchor_x + EDGE_ENDPOINT_TOLERANCE
        elif anchor_x > center_x + EDGE_ENDPOINT_TOLERANCE:
            horizontal = point_x >= anchor_x - EDGE_ENDPOINT_TOLERANCE
    return vertical or horizontal


def _is_explicitly_arrowless_auxiliary(style: dict[str, str]) -> bool:
    return style.get("edgeRole") == AUXILIARY_EDGE_ROLE and style.get("endArrow") == "none" and style.get(
        "startArrow", "none"
    ) == "none"


def _validation_issue(
    code: str,
    message: str,
    *,
    cell_id: str | None = None,
    related_cell_ids: list[str] | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message}
    if cell_id is not None:
        issue["cell_id"] = cell_id
    if related_cell_ids:
        issue["related_cell_ids"] = related_cell_ids
    return issue


def _validation_failed(
    errors: list[dict[str, Any]],
    *,
    warnings: list[dict[str, Any]] | None = None,
    normalized_fields: list[str] | None = None,
) -> dict[str, Any]:
    first = errors[0]
    error_count = len(errors)
    message = f"draw.io 渲染前检查发现 {error_count} 个必须修复的问题，已一次返回全部失败点。首项：{first['message']}"
    return tool_failed(
        str(first["code"]),
        message,
        errors=errors,
        warnings=warnings or [],
        normalized=bool(normalized_fields),
        normalized_fields=normalized_fields or [],
    )


def _outside_canvas(x: float, y: float) -> bool:
    return (
        x < -CANVAS_TOLERANCE
        or y < -CANVAS_TOLERANCE
        or x > FIGURE_WIDTH + CANVAS_TOLERANCE
        or y > FIGURE_HEIGHT + CANVAS_TOLERANCE
    )
