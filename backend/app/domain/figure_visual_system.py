from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter

from .figure_scene import build_figure_scene

BASE_TEXT_STYLE = {
    "html": "1",
    "fontFamily": "Helvetica",
    "fontColor": "#111111",
}

VISUAL_ROLE_STYLE_PROFILES: dict[str, dict[str, str]] = {
    "panel": {
        "rounded": "1",
        "arcSize": "6",
        "whiteSpace": "wrap",
        "fillColor": "#f7f7f7",
        "strokeColor": "#777777",
        "strokeWidth": "1",
        "fontSize": "15",
        "fontStyle": "1",
        "align": "left",
        "verticalAlign": "top",
        "spacingTop": "12",
        "spacingLeft": "14",
    },
    "primary": {
        "rounded": "1",
        "arcSize": "8",
        "whiteSpace": "wrap",
        "fillColor": "#e9e9e9",
        "strokeColor": "#222222",
        "strokeWidth": "1.4",
        "fontSize": "14",
        "fontStyle": "1",
        "align": "center",
        "verticalAlign": "middle",
        "spacing": "10",
    },
    "normal": {
        "rounded": "1",
        "arcSize": "8",
        "whiteSpace": "wrap",
        "fillColor": "#ffffff",
        "strokeColor": "#222222",
        "strokeWidth": "1.4",
        "fontSize": "14",
        "align": "center",
        "verticalAlign": "middle",
        "spacing": "10",
    },
    "decision": {
        "shape": "rhombus",
        "whiteSpace": "wrap",
        "fillColor": "#ffffff",
        "strokeColor": "#222222",
        "strokeWidth": "1.4",
        "fontSize": "14",
        "align": "center",
        "verticalAlign": "middle",
        "spacing": "8",
    },
    "state": {
        "rounded": "1",
        "arcSize": "24",
        "whiteSpace": "wrap",
        "fillColor": "#ffffff",
        "strokeColor": "#222222",
        "strokeWidth": "1.4",
        "fontSize": "14",
        "align": "center",
        "verticalAlign": "middle",
        "spacing": "10",
    },
    "data": {
        "shape": "cylinder3",
        "boundedLbl": "1",
        "backgroundOutline": "1",
        "size": "15",
        "whiteSpace": "wrap",
        "fillColor": "#ffffff",
        "strokeColor": "#222222",
        "strokeWidth": "1.4",
        "fontSize": "14",
        "align": "center",
        "verticalAlign": "middle",
        "spacing": "10",
    },
    "note": {
        "strokeColor": "none",
        "fillColor": "none",
        "fontSize": "14",
        "align": "left",
        "verticalAlign": "middle",
        "spacing": "0",
    },
}

EDGE_DEFAULT_STYLE = {
    "edgeStyle": "orthogonalEdgeStyle",
    "rounded": "0",
    "orthogonalLoop": "1",
    "jettySize": "auto",
    "html": "1",
    "strokeColor": "#222222",
    "strokeWidth": "1.4",
    "endArrow": "block",
    "endFill": "1",
}


def apply_visual_defaults(graph_model: ET.Element) -> list[str]:
    """Apply the shared visual system while preserving every explicit style property."""

    scene = build_figure_scene(graph_model)
    applied: Counter[tuple[str, str]] = Counter()
    for node in scene.nodes.values():
        defaults = dict(VISUAL_ROLE_STYLE_PROFILES[node.role])
        if node.label:
            defaults = {**BASE_TEXT_STYLE, **defaults}
        _append_missing_styles(node.element, node.raw_style, node.style, defaults, applied)

    for edge in scene.edges.values():
        defaults = dict(EDGE_DEFAULT_STYLE)
        if edge.label:
            defaults.update(
                {
                    "fontFamily": "Helvetica",
                    "fontSize": "12",
                    "fontColor": "#333333",
                    "labelBackgroundColor": "#ffffff",
                }
            )
        _append_missing_styles(edge.element, edge.raw_style, edge.style, defaults, applied)

    return [
        f"mxCell.style.{key}={value} ({count} cells)"
        for (key, value), count in sorted(applied.items())
    ]


def _append_missing_styles(
    element: ET.Element,
    raw_style: str,
    style: dict[str, str],
    defaults: dict[str, str],
    applied: Counter[tuple[str, str]],
) -> None:
    additions: list[str] = []
    for key, value in defaults.items():
        if key in style:
            continue
        additions.append(f"{key}={value}")
        applied[(key, value)] += 1
    if not additions:
        return
    prefix = raw_style.rstrip(";")
    element.set("style", ";".join([item for item in (prefix, *additions) if item]) + ";")
