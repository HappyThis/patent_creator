from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import isfinite


VALID_VISUAL_ROLES = frozenset({"panel", "primary", "normal", "decision", "state", "data", "note"})


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def area(self) -> float:
        return self.width * self.height

    def contains(self, other: Rect, *, tolerance: float = 1.0) -> bool:
        return (
            other.x >= self.x - tolerance
            and other.y >= self.y - tolerance
            and other.right <= self.right + tolerance
            and other.bottom <= self.bottom + tolerance
        )

    def overlap_size(self, other: Rect) -> tuple[float, float] | None:
        width = min(self.right, other.right) - max(self.x, other.x)
        height = min(self.bottom, other.bottom) - max(self.y, other.y)
        if width <= 0 or height <= 0:
            return None
        return (width, height)


@dataclass(frozen=True, slots=True)
class Segment:
    start: tuple[float, float]
    end: tuple[float, float]

    @property
    def axis(self) -> str | None:
        if abs(self.start[1] - self.end[1]) <= 1.0:
            return "horizontal"
        if abs(self.start[0] - self.end[0]) <= 1.0:
            return "vertical"
        return None


@dataclass(slots=True)
class FigureNode:
    id: str
    parent_id: str
    label: str
    raw_value: str
    raw_style: str
    style: dict[str, str]
    bounds: Rect | None
    explicit_role: str | None
    inferred_role: str
    role: str
    is_text_only: bool
    is_container: bool
    element: ET.Element


@dataclass(slots=True)
class FigureEdge:
    id: str
    parent_id: str
    source_id: str
    target_id: str
    label: str
    raw_value: str
    raw_style: str
    style: dict[str, str]
    waypoints: list[tuple[float, float]]
    segments: list[Segment]
    element: ET.Element


@dataclass(slots=True)
class FigureScene:
    width: float
    height: float
    nodes: dict[str, FigureNode]
    edges: dict[str, FigureEdge]
    cells: dict[str, ET.Element]

    def is_ancestor(self, candidate_id: str, cell_id: str) -> bool:
        seen: set[str] = set()
        current = self.cells.get(cell_id)
        current_id = str(current.get("parent") or "") if current is not None else ""
        while current_id and current_id not in seen:
            if current_id == candidate_id:
                return True
            seen.add(current_id)
            parent = self.cells.get(current_id)
            current_id = str(parent.get("parent") or "") if parent is not None else ""
        return False

    def nodes_inside(self, container_id: str) -> list[FigureNode]:
        container = self.nodes.get(container_id)
        if container is None or container.bounds is None:
            return []
        return [
            node
            for node in self.nodes.values()
            if node.id != container_id
            and node.bounds is not None
            and (self.is_ancestor(container_id, node.id) or container.bounds.contains(node.bounds))
        ]


def build_figure_scene(graph_model: ET.Element) -> FigureScene:
    cells = {
        str(cell.get("id")): cell
        for cell in graph_model.iter()
        if local_name(cell.tag) == "mxCell" and cell.get("id")
    }
    bounds_cache: dict[str, Rect | None] = {}
    vertex_bounds = {
        cell_id: absolute_vertex_bounds(cell_id, cells, bounds_cache, set())
        for cell_id, cell in cells.items()
        if cell.get("vertex") == "1"
    }
    container_ids = _container_ids(cells, vertex_bounds)

    nodes: dict[str, FigureNode] = {}
    for cell_id, cell in cells.items():
        if cell.get("vertex") != "1":
            continue
        raw_style = str(cell.get("style") or "")
        style = parse_drawio_style(raw_style)
        is_text_only = _is_text_only(raw_style, style)
        is_container = cell_id in container_ids
        explicit_role = str(style.get("visualRole") or "").strip().lower() or None
        inferred_role = _infer_role(style, is_text_only=is_text_only, is_container=is_container)
        nodes[cell_id] = FigureNode(
            id=cell_id,
            parent_id=str(cell.get("parent") or ""),
            label=plain_label(str(cell.get("value") or "")),
            raw_value=str(cell.get("value") or ""),
            raw_style=raw_style,
            style=style,
            bounds=vertex_bounds.get(cell_id),
            explicit_role=explicit_role,
            inferred_role=inferred_role,
            role=explicit_role if explicit_role in VALID_VISUAL_ROLES else inferred_role,
            is_text_only=is_text_only,
            is_container=is_container,
            element=cell,
        )

    edges: dict[str, FigureEdge] = {}
    for cell_id, cell in cells.items():
        if cell.get("edge") != "1":
            continue
        raw_style = str(cell.get("style") or "")
        style = parse_drawio_style(raw_style)
        waypoints = edge_waypoints(cell_geometry(cell))
        source_id = str(cell.get("source") or "")
        target_id = str(cell.get("target") or "")
        points: list[tuple[float, float]] = []
        source_anchor = _explicit_anchor(source_id, nodes.get(source_id), style, "exit")
        target_anchor = _explicit_anchor(target_id, nodes.get(target_id), style, "entry")
        if source_anchor is not None:
            points.append(source_anchor)
        points.extend(waypoints)
        if target_anchor is not None:
            points.append(target_anchor)
        edges[cell_id] = FigureEdge(
            id=cell_id,
            parent_id=str(cell.get("parent") or ""),
            source_id=source_id,
            target_id=target_id,
            label=plain_label(str(cell.get("value") or "")),
            raw_value=str(cell.get("value") or ""),
            raw_style=raw_style,
            style=style,
            waypoints=waypoints,
            segments=[Segment(left, right) for left, right in zip(points, points[1:])],
            element=cell,
        )

    return FigureScene(
        width=finite_number(graph_model.get("pageWidth")) or 1500.0,
        height=finite_number(graph_model.get("pageHeight")) or 900.0,
        nodes=nodes,
        edges=edges,
        cells=cells,
    )


def absolute_vertex_bounds(
    cell_id: str,
    cells: dict[str, ET.Element],
    cache: dict[str, Rect | None],
    visiting: set[str],
) -> Rect | None:
    if cell_id in cache:
        return cache[cell_id]
    if cell_id in visiting:
        return None
    visiting.add(cell_id)
    cell = cells.get(cell_id)
    geometry = cell_geometry(cell) if cell is not None else None
    result: Rect | None = None
    if geometry is not None and geometry.get("relative") != "1":
        x = finite_number(geometry.get("x")) or 0.0
        y = finite_number(geometry.get("y")) or 0.0
        width = finite_number(geometry.get("width"))
        height = finite_number(geometry.get("height"))
        if width is not None and height is not None:
            parent_id = str(cell.get("parent") or "")
            parent = cells.get(parent_id)
            if parent is not None and parent.get("vertex") == "1":
                parent_bounds = absolute_vertex_bounds(parent_id, cells, cache, visiting)
                if parent_bounds is not None:
                    x += parent_bounds.x
                    y += parent_bounds.y
            result = Rect(x, y, width, height)
    visiting.remove(cell_id)
    cache[cell_id] = result
    return result


def parse_drawio_style(value: str | None) -> dict[str, str]:
    style: dict[str, str] = {}
    for item in str(value or "").split(";"):
        if "=" not in item:
            continue
        key, item_value = item.split("=", 1)
        if key:
            style[key] = item_value
    return style


def cell_geometry(cell: ET.Element) -> ET.Element | None:
    return next((child for child in cell if local_name(child.tag) == "mxGeometry"), None)


def edge_waypoints(geometry: ET.Element | None) -> list[tuple[float, float]]:
    if geometry is None:
        return []
    for child in geometry:
        if local_name(child.tag) != "Array" or child.get("as") != "points":
            continue
        points: list[tuple[float, float]] = []
        for point in child:
            if local_name(point.tag) != "mxPoint":
                continue
            x = finite_number(point.get("x"))
            y = finite_number(point.get("y"))
            if x is not None and y is not None:
                points.append((x, y))
        return points
    return []


def finite_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if isfinite(number) else None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[1] if "}" in tag else tag


def plain_label(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value.replace("&#xa;", "\n"))).strip()


def _container_ids(cells: dict[str, ET.Element], bounds: dict[str, Rect | None]) -> set[str]:
    vertex_ids = [cell_id for cell_id, cell in cells.items() if cell.get("vertex") == "1"]
    container_ids = {
        str(cells[cell_id].get("parent"))
        for cell_id in vertex_ids
        if cells[cell_id].get("parent") in cells and cells[str(cells[cell_id].get("parent"))].get("vertex") == "1"
    }
    for outer_id in vertex_ids:
        outer = bounds.get(outer_id)
        if outer is None or outer.area <= 0:
            continue
        outer_cell = cells[outer_id]
        for inner_id in vertex_ids:
            if inner_id == outer_id or cells[inner_id].get("parent") != outer_cell.get("parent"):
                continue
            inner = bounds.get(inner_id)
            if inner is None or inner.area <= 0:
                continue
            if outer.area >= inner.area * 1.5 and outer.contains(inner):
                container_ids.add(outer_id)
                break
    return container_ids


def _is_text_only(raw_style: str, style: dict[str, str]) -> bool:
    return raw_style == "text" or raw_style.startswith("text;") or (
        style.get("strokeColor") == "none" and style.get("fillColor") == "none"
    )


def _infer_role(style: dict[str, str], *, is_text_only: bool, is_container: bool) -> str:
    if is_container:
        return "panel"
    if is_text_only:
        return "note"
    shape = str(style.get("shape") or "").strip().lower()
    if shape == "rhombus":
        return "decision"
    if shape in {"cylinder", "cylinder3"}:
        return "data"
    return "normal"


def _explicit_anchor(
    cell_id: str,
    node: FigureNode | None,
    edge_style: dict[str, str],
    prefix: str,
) -> tuple[float, float] | None:
    if not cell_id or node is None or node.bounds is None:
        return None
    shape = str(node.style.get("shape") or "").strip().lower()
    if shape not in {"", "rectangle"} or edge_style.get(f"{prefix}Perimeter") == "0":
        return None
    x = finite_number(edge_style.get(f"{prefix}X"))
    y = finite_number(edge_style.get(f"{prefix}Y"))
    if x is None or y is None:
        return None
    return (node.bounds.x + node.bounds.width * x, node.bounds.y + node.bounds.height * y)
