from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from math import hypot
from typing import Any

from .figure_scene import (
    VALID_VISUAL_ROLES,
    FigureEdge,
    FigureNode,
    FigureScene,
    Rect,
    Segment,
    build_figure_scene,
    finite_number,
)

FIGURE_VISUAL_SAFE_MARGIN = 60.0
MIN_READABLE_FIGURE_FONT_SIZE = 11.0
MAX_PREFERRED_EXPLICIT_EDGE_WAYPOINTS = 3
MAX_FIGURE_FONT_SIZE_LEVELS = 4
EDGE_OVERLAP_TOLERANCE = 1.0
MIN_SEMANTIC_EDGE_OVERLAP_LENGTH = 12.0
MAX_PAIR_WARNINGS = 12

def inspect_figure_visuals(graph_model: ET.Element) -> list[dict[str, Any]]:
    scene = build_figure_scene(graph_model)
    warnings: list[dict[str, Any]] = []
    warnings.extend(_unknown_role_warnings(scene))
    warnings.extend(_layout_warnings(scene))
    warnings.extend(_route_warnings(scene))
    warnings.extend(_style_warnings(scene))
    warnings.extend(_role_consistency_warnings(scene))
    warnings.extend(_density_and_balance_warnings(scene))
    return warnings


def _unknown_role_warnings(scene: FigureScene) -> list[dict[str, Any]]:
    unknown = [node for node in scene.nodes.values() if node.explicit_role and node.explicit_role not in VALID_VISUAL_ROLES]
    if not unknown:
        return []
    ids = [node.id for node in unknown]
    roles = ", ".join(f"{node.id}={node.explicit_role}" for node in unknown[:12])
    return [
        _issue(
            "drawio_visual_role_unknown",
            f"以下 visualRole 无法识别：{roles}。支持 panel、primary、normal、decision、state、data、note；当前已按形状推断默认样式。",
            related_cell_ids=ids[:12],
        )
    ]


def _layout_warnings(scene: FigureScene) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    margin_nodes = [
        node.id
        for node in scene.nodes.values()
        if node.parent_id == "1"
        and node.bounds is not None
        and node.role != "note"
        and min(
            node.bounds.x,
            node.bounds.y,
            scene.width - node.bounds.right,
            scene.height - node.bounds.bottom,
        )
        < FIGURE_VISUAL_SAFE_MARGIN
    ]
    if margin_nodes:
        warnings.append(
            _issue(
                "drawio_visual_safe_margin",
                (
                    f"顶层主体 {', '.join(margin_nodes[:12])} 距页面边界不足建议的 "
                    f"{FIGURE_VISUAL_SAFE_MARGIN:g}px 安全边距。贴边会让画面显得拥挤并增加标签扩页风险；"
                    "请在不破坏构图的前提下向画布内收。"
                ),
                related_cell_ids=margin_nodes[:12],
            )
        )

    comparable = [node for node in scene.nodes.values() if node.bounds is not None and node.role != "note"]
    overlap_count = 0
    for index, left in enumerate(comparable):
        assert left.bounds is not None
        for right in comparable[index + 1 :]:
            assert right.bounds is not None
            if left.parent_id != right.parent_id:
                continue
            if left.bounds.contains(right.bounds) or right.bounds.contains(left.bounds):
                continue
            overlap = left.bounds.overlap_size(right.bounds)
            if overlap is None or min(overlap) <= 2.0:
                continue
            warnings.append(
                _issue(
                    "drawio_vertex_overlap",
                    (
                        f"同层节点 {left.id} 与 {right.id} 存在约 {overlap[0]:g}x{overlap[1]:g}px 重叠。"
                        "除非明确表达叠放关系，否则应调整位置或尺寸并恢复稳定留白。"
                    ),
                    cell_id=left.id,
                    related_cell_ids=[right.id],
                )
            )
            overlap_count += 1
            if overlap_count >= MAX_PAIR_WARNINGS:
                return warnings
    return warnings


def _route_warnings(scene: FigureScene) -> list[dict[str, Any]]:
    semantic_edges = [
        edge
        for edge in scene.edges.values()
        if edge.segments and not (edge.style.get("edgeRole") == "auxiliary" and edge.style.get("endArrow") == "none")
    ]
    warnings: list[dict[str, Any]] = []

    for edge in semantic_edges:
        overlap = _first_segment_overlap(edge.segments, edge.segments, skip_same_or_adjacent=True)
        if overlap is not None:
            warnings.append(_overlap_warning(edge.id, edge.id, overlap))
    for index, left in enumerate(semantic_edges):
        for right in semantic_edges[index + 1 :]:
            overlap = _first_segment_overlap(left.segments, right.segments)
            if overlap is not None:
                warnings.append(_overlap_warning(left.id, right.id, overlap))
            crossing = _first_crossing(left.segments, right.segments)
            if crossing is not None:
                warnings.append(
                    _issue(
                        "drawio_semantic_edge_crossing",
                        (
                            f"语义连线 {left.id} 与 {right.id} 在约 ({crossing[0]:g}, {crossing[1]:g}) 发生无节点交叉。"
                            "交叉会削弱阅读方向并使关系归属含糊；请调整分区或走线通道，确需汇聚时增加明确的汇聚节点。"
                        ),
                        cell_id=left.id,
                        related_cell_ids=[right.id],
                    )
                )
            if len(warnings) >= MAX_PAIR_WARNINGS:
                break
        if len(warnings) >= MAX_PAIR_WARNINGS:
            break

    non_containers = [node for node in scene.nodes.values() if node.bounds is not None and not node.is_container]
    for edge in semantic_edges:
        crossed: FigureNode | None = None
        for node in non_containers:
            if node.id in {edge.source_id, edge.target_id}:
                continue
            if scene.is_ancestor(node.id, edge.source_id) or scene.is_ancestor(node.id, edge.target_id):
                continue
            assert node.bounds is not None
            if any(_segment_crosses_rect(segment, node.bounds) for segment in edge.segments):
                crossed = node
                break
        if crossed is not None:
            warnings.append(
                _issue(
                    "drawio_edge_crosses_vertex",
                    f"语义连线 {edge.id} 穿过无关节点 {crossed.id} 的内部。这会造成节点与关系归属不清；请重新分配走线通道，或调整节点位置。",
                    cell_id=edge.id,
                    related_cell_ids=[crossed.id],
                )
            )
    return warnings


def _style_warnings(scene: FigureScene) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    all_items: list[FigureNode | FigureEdge] = [*scene.nodes.values(), *scene.edges.values()]
    font_families: set[str] = set()
    font_sizes: set[str] = set()
    stroke_widths: set[str] = set()
    non_monochrome: list[str] = []
    decorative: list[str] = []
    unreadable: list[str] = []
    dense: list[str] = []
    text_count = 0

    for item in all_items:
        style = item.style
        if item.label:
            text_count += 1
            size = finite_number(style.get("fontSize"))
            if size is not None and size < MIN_READABLE_FIGURE_FONT_SIZE:
                unreadable.append(item.id)
        for key, target in (("fontFamily", font_families), ("fontSize", font_sizes), ("strokeWidth", stroke_widths)):
            value = str(style.get(key) or "").strip()
            if value:
                target.add(value)
        if any(
            (color := str(style.get(key) or "").strip()) and not _is_monochrome(color)
            for key in ("fillColor", "strokeColor", "fontColor", "gradientColor")
        ):
            non_monochrome.append(item.id)
        if style.get("shadow") == "1" or style.get("gradientColor") not in {None, "", "none"}:
            decorative.append(item.id)

    for node in scene.nodes.values():
        if not node.label:
            continue
        compact = re.sub(r"\s+", "", node.label)
        line_count = max(1, len(re.findall(r"\n|<br\s*/?>|&#xa;", node.raw_value, flags=re.I)) + 1)
        char_limit, line_limit = {
            "panel": (26, 2),
            "primary": (48, 4),
            "note": (90, 6),
        }.get(node.role, (38, 3))
        if len(compact) > char_limit or line_count > line_limit:
            dense.append(node.id)

    for edge in scene.edges.values():
        if edge.style.get("curved") == "1" or edge.style.get("curve") == "1":
            warnings.append(_issue("drawio_edge_curved", f"连线 {edge.id} 使用曲线；黑白工程示意图通常使用直线或正交折线更清楚。", cell_id=edge.id))
        if len(edge.waypoints) > MAX_PREFERRED_EXPLICIT_EDGE_WAYPOINTS:
            warnings.append(
                _issue(
                    "drawio_edge_too_many_bends",
                    f"连线 {edge.id} 包含 {len(edge.waypoints)} 个显式拐点，超过推荐的 {MAX_PREFERRED_EXPLICIT_EDGE_WAYPOINTS} 个。请优先减少弯折、缩短绕行或重新分配走线通道。",
                    cell_id=edge.id,
                )
            )
        compact = re.sub(r"\s+", "", edge.label)
        if len(compact) > 18:
            warnings.append(
                _issue(
                    "drawio_edge_label_too_dense",
                    f"连线 {edge.id} 的标签包含 {len(compact)} 个字符，容易遮挡走线或破坏节奏；请缩成条件、动作或数据名，解释放回正文。",
                    cell_id=edge.id,
                )
            )

    if len(font_families) > 1:
        warnings.append(_issue("drawio_font_family_inconsistent", f"图中显式使用了多种字体：{', '.join(sorted(font_families))}。建议统一无衬线字体。"))
    if len(font_sizes) > MAX_FIGURE_FONT_SIZE_LEVELS:
        warnings.append(
            _issue(
                "drawio_font_size_excessive",
                f"图中显式使用了 {len(font_sizes)} 种字号：{', '.join(sorted(font_sizes))}，超过推荐的 {MAX_FIGURE_FONT_SIZE_LEVELS} 级。建议只保留标题、分区、节点和边标签等有限层级。",
            )
        )
    elif text_count >= 6 and len(font_sizes) == 1:
        warnings.append(
            _issue(
                "drawio_visual_hierarchy_missing",
                f"图中 {text_count} 个带文字单元全部使用同一字号 {next(iter(font_sizes))}。建议至少区分标题或分区标题与普通节点，建立清楚但有限的视觉层级。",
            )
        )
    if len(stroke_widths) > 2:
        warnings.append(_issue("drawio_stroke_width_inconsistent", f"图中显式使用了 {len(stroke_widths)} 种线宽：{', '.join(sorted(stroke_widths))}。建议统一同类容器和连线。"))
    if non_monochrome:
        unique = list(dict.fromkeys(non_monochrome))
        warnings.append(_issue("drawio_non_monochrome_style", f"以下单元使用了非黑白灰颜色：{', '.join(unique)}。请确认颜色确有技术表达作用。", related_cell_ids=unique))
    if decorative:
        unique = list(dict.fromkeys(decorative))
        warnings.append(_issue("drawio_decorative_effect", f"以下单元使用了阴影或渐变：{', '.join(unique)}。严肃工程图通常不需要这些效果。", related_cell_ids=unique))
    if unreadable:
        unique = list(dict.fromkeys(unreadable))
        warnings.append(_issue("drawio_font_size_too_small", f"以下单元字号小于 {MIN_READABLE_FIGURE_FONT_SIZE:g}px：{', '.join(unique)}。在交底书常用显示尺寸下可能难以阅读。", related_cell_ids=unique))
    if dense:
        unique = list(dict.fromkeys(dense))
        warnings.append(_issue("drawio_node_text_too_dense", f"以下节点的文字相对其视觉角色过长或层数过多：{', '.join(unique)}。节点优先保留职责名称和关键动作，详细解释移至正文或独立注释。", related_cell_ids=unique))
    return warnings


def _role_consistency_warnings(scene: FigureScene) -> list[dict[str, Any]]:
    groups: dict[str, list[FigureNode]] = defaultdict(list)
    for node in scene.nodes.values():
        # Inferred ``normal`` is the catch-all for legacy XML and may contain
        # intentionally different shapes. Consistency is only meaningful when
        # the author explicitly opted nodes into the same visual role.
        if node.explicit_role in {"normal", "decision", "state", "data"}:
            groups[node.role].append(node)
    warnings: list[dict[str, Any]] = []
    keys = ("shape", "rounded", "arcSize", "fillColor", "strokeColor", "strokeWidth", "fontSize")
    for role, nodes in groups.items():
        if len(nodes) < 3:
            continue
        signatures: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for node in nodes:
            signatures[tuple(str(node.style.get(key) or "") for key in keys)].append(node.id)
        if len(signatures) <= 1:
            continue
        ids = [node.id for node in nodes]
        warnings.append(
            _issue(
                "drawio_visual_role_inconsistent",
                f"同为 visualRole={role} 的节点 {', '.join(ids[:12])} 使用了 {len(signatures)} 套形状或样式。若无明确语义差异，请统一尺寸之外的视觉属性。",
                related_cell_ids=ids[:12],
            )
        )
    return warnings


def _density_and_balance_warnings(scene: FigureScene) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for panel in (node for node in scene.nodes.values() if node.role == "panel" and node.bounds is not None):
        children = [
            node
            for node in scene.nodes_inside(panel.id)
            if node.role not in {"note", "panel"} and node.bounds is not None
        ]
        used_area = sum(node.bounds.area for node in children if node.bounds is not None)
        if len(children) >= 8 and panel.bounds and used_area / max(panel.bounds.area, 1.0) > 0.55:
            warnings.append(
                _issue(
                    "drawio_panel_too_dense",
                    f"分区 {panel.id} 内含 {len(children)} 个主体节点，节点面积约占分区的 {used_area / panel.bounds.area:.0%}。建议拆分层级或扩大留白和走线通道。",
                    cell_id=panel.id,
                    related_cell_ids=[node.id for node in children[:12]],
                )
            )

    top_level = [
        node
        for node in scene.nodes.values()
        if node.parent_id == "1" and node.bounds is not None and node.role not in {"note", "panel"}
    ]
    if len(top_level) >= 4:
        left = min(node.bounds.x for node in top_level if node.bounds)
        right = max(node.bounds.right for node in top_level if node.bounds)
        top = min(node.bounds.y for node in top_level if node.bounds)
        bottom = max(node.bounds.bottom for node in top_level if node.bounds)
        center = ((left + right) / 2.0, (top + bottom) / 2.0)
        if abs(center[0] - scene.width / 2.0) > scene.width * 0.25 or abs(center[1] - scene.height / 2.0) > scene.height * 0.25:
            warnings.append(
                _issue(
                    "drawio_canvas_imbalance",
                    f"顶层主体的包围中心约为 ({center[0]:g}, {center[1]:g})，明显偏离画布视觉中心。请确认偏置是否服务于阅读方向，否则平衡两侧留白。",
                    related_cell_ids=[node.id for node in top_level[:12]],
                )
            )
    return warnings


def _first_crossing(left: list[Segment], right: list[Segment]) -> tuple[float, float] | None:
    for left_segment in left:
        for right_segment in right:
            crossing = _proper_crossing(left_segment, right_segment)
            if crossing is not None:
                return crossing
    return None


def _proper_crossing(left: Segment, right: Segment) -> tuple[float, float] | None:
    if left.axis is None or right.axis is None or left.axis == right.axis:
        return None
    horizontal = left if left.axis == "horizontal" else right
    vertical = right if left.axis == "horizontal" else left
    x, y = vertical.start[0], horizontal.start[1]
    if not (
        min(horizontal.start[0], horizontal.end[0]) - EDGE_OVERLAP_TOLERANCE <= x <= max(horizontal.start[0], horizontal.end[0]) + EDGE_OVERLAP_TOLERANCE
        and min(vertical.start[1], vertical.end[1]) - EDGE_OVERLAP_TOLERANCE <= y <= max(vertical.start[1], vertical.end[1]) + EDGE_OVERLAP_TOLERANCE
    ):
        return None
    if any(hypot(x - point[0], y - point[1]) <= EDGE_OVERLAP_TOLERANCE for point in (left.start, left.end, right.start, right.end)):
        return None
    return (x, y)


def _first_segment_overlap(
    left: list[Segment],
    right: list[Segment],
    *,
    skip_same_or_adjacent: bool = False,
) -> tuple[str, tuple[float, float], tuple[float, float], float] | None:
    for left_index, left_segment in enumerate(left):
        if left_segment.axis is None:
            continue
        for right_index, right_segment in enumerate(right):
            if skip_same_or_adjacent and abs(left_index - right_index) <= 1:
                continue
            if right_segment.axis != left_segment.axis:
                continue
            if left_segment.axis == "horizontal":
                if abs(left_segment.start[1] - right_segment.start[1]) > EDGE_OVERLAP_TOLERANCE:
                    continue
                overlap = _interval_overlap(left_segment.start[0], left_segment.end[0], right_segment.start[0], right_segment.end[0])
                if overlap:
                    return ("水平", (overlap[0], left_segment.start[1]), (overlap[1], left_segment.start[1]), overlap[1] - overlap[0])
            else:
                if abs(left_segment.start[0] - right_segment.start[0]) > EDGE_OVERLAP_TOLERANCE:
                    continue
                overlap = _interval_overlap(left_segment.start[1], left_segment.end[1], right_segment.start[1], right_segment.end[1])
                if overlap:
                    return ("垂直", (left_segment.start[0], overlap[0]), (left_segment.start[0], overlap[1]), overlap[1] - overlap[0])
    return None


def _interval_overlap(a1: float, a2: float, b1: float, b2: float) -> tuple[float, float] | None:
    start = max(min(a1, a2), min(b1, b2))
    end = min(max(a1, a2), max(b1, b2))
    return (start, end) if end - start >= MIN_SEMANTIC_EDGE_OVERLAP_LENGTH else None


def _overlap_warning(
    left_id: str,
    right_id: str,
    overlap: tuple[str, tuple[float, float], tuple[float, float], float],
) -> dict[str, Any]:
    orientation, start, end, length = overlap
    subject = f"语义连线 {left_id} 自身" if left_id == right_id else f"语义连线 {left_id} 与 {right_id}"
    return _issue(
        "drawio_semantic_edge_overlap",
        f"{subject}存在约 {length:g}px 的{orientation}共线重叠，重叠范围约为 ({start[0]:g}, {start[1]:g}) 至 ({end[0]:g}, {end[1]:g})。这会让分支看起来从半空开始，或使箭头和起止点含义不清。请为不同分支使用独立锚点和走线通道；确需共享路径时，增加明确的汇聚/分叉节点后分别连接。",
        cell_id=left_id,
        related_cell_ids=[] if left_id == right_id else [right_id],
    )


def _segment_crosses_rect(segment: Segment, bounds: Rect) -> bool:
    if segment.axis is None or bounds.width <= 4 or bounds.height <= 4:
        return False
    inset = 2.0
    if segment.axis == "horizontal":
        y = segment.start[1]
        if not bounds.y + inset < y < bounds.bottom - inset:
            return False
        return min(max(segment.start[0], segment.end[0]), bounds.right - inset) - max(min(segment.start[0], segment.end[0]), bounds.x + inset) > EDGE_OVERLAP_TOLERANCE
    x = segment.start[0]
    if not bounds.x + inset < x < bounds.right - inset:
        return False
    return min(max(segment.start[1], segment.end[1]), bounds.bottom - inset) - max(min(segment.start[1], segment.end[1]), bounds.y + inset) > EDGE_OVERLAP_TOLERANCE


def _is_monochrome(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"none", "default", "transparent", "white", "black"}:
        return True
    match = re.fullmatch(r"#([0-9a-f]{6})", normalized)
    if match is None:
        return False
    red, green, blue = (int(match.group(1)[index : index + 2], 16) for index in (0, 2, 4))
    return max(red, green, blue) - min(red, green, blue) <= 8


def _issue(
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
