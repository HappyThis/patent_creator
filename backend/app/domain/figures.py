from __future__ import annotations

import re
from typing import Any

from ..core import now_iso
from .document_tool_results import tool_failed

FIGURE_ID_PATTERN = re.compile(r"^fig_\d{6}$")
FIGURE_REF_PATTERN = re.compile(r"^figure:(?P<figure_id>fig_\d{6})$")
FIGURE_LINK_PATTERN = re.compile(r"\[(?P<label>[^\]]+)\]\(figure:(?P<figure_id>fig_\d{6})\)")
MAX_MERMAID_CHARS = 8000
NODE_PATTERN = re.compile(r"(?P<id>[A-Za-z][A-Za-z0-9_]*)\[(?P<label>[^\]]+)\]")


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
    }


def build_figure_record(
    *,
    figure_id: str,
    index: int,
    title: str,
    mermaid: str,
    asset_path: str,
) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "figure_id": figure_id,
        "label": figure_label(index),
        "title": title.strip(),
        "asset_path": asset_path,
        "source": {
            "type": "mermaid",
            "content": mermaid.strip(),
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def update_figure_record(figure: dict[str, Any], *, title: str | None, mermaid: str) -> dict[str, Any]:
    next_figure = dict(figure)
    if title is not None:
        next_figure["title"] = title.strip()
    next_figure["source"] = {"type": "mermaid", "content": mermaid.strip()}
    next_figure["updated_at"] = now_iso()
    return next_figure


def parse_figure_ref(ref: str) -> str | None:
    match = FIGURE_REF_PATTERN.fullmatch(str(ref or "").strip())
    return match.group("figure_id") if match else None


def validate_mermaid_source(mermaid: str) -> dict[str, Any]:
    source = mermaid.strip()
    if not source:
        return tool_failed("figure_mermaid_required", "Mermaid 源码不能为空。")
    if len(source) > MAX_MERMAID_CHARS:
        return tool_failed("figure_mermaid_too_large", f"Mermaid 源码不能超过 {MAX_MERMAID_CHARS} 个字符。复杂图请拆成多张。")
    return {"status": "success", "output": {"mermaid": source}}


def mermaid_layout_warnings(mermaid: str) -> list[dict[str, str]]:
    source = mermaid.strip()
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    header = lines[0] if lines else ""
    direction_match = re.fullmatch(r"(flowchart|graph)\s+(?P<direction>TD|TB|LR|RL)", header, flags=re.IGNORECASE)
    direction = direction_match.group("direction").upper() if direction_match else ""
    labels = [match.group("label").strip() for match in NODE_PATTERN.finditer(source)]
    unique_labels = list(dict.fromkeys(labels))
    warnings: list[dict[str, str]] = []

    if len(unique_labels) > 10:
        warnings.append(
            {
                "code": "figure_too_many_nodes",
                "message": f"当前 Mermaid 约 {len(unique_labels)} 个节点，交底书附图建议控制在 6-10 个节点，复杂图应拆分或抽象。",
            }
        )
    if direction in {"LR", "RL"} and len(unique_labels) > 6:
        warnings.append(
            {
                "code": "figure_lr_too_wide",
                "message": "flowchart LR/RL 仅建议用于 3-6 个模块的横向关系；长流程链路应改用 TD/TB 或拆图。",
            }
        )
    long_labels = [label for label in unique_labels if len(label) > 10]
    if long_labels:
        preview = "、".join(long_labels[:3])
        warnings.append(
            {
                "code": "figure_label_too_long",
                "message": f"节点文字偏长：{preview}。节点文字建议使用不超过 10 个汉字的短语。",
            }
        )
    if len(re.findall(r"-->", source)) > max(len(unique_labels) + 2, 12):
        warnings.append(
            {
                "code": "figure_too_many_edges",
                "message": "连线较多，可能导致图形拥挤；建议只保留主流程或拆成多张图。",
            }
        )
    return warnings
