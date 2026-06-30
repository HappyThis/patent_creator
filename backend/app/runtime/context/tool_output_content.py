from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ...storage.workspace_store import WorkspaceStore

logger = logging.getLogger("patent_creator.context.tool_output_content")

MAX_TOOL_OUTPUT_IMAGE_ATTACHMENTS_PER_REQUEST = 8
MAX_TOOL_OUTPUT_IMAGE_BYTES = 2_000_000
FIGURE_VISUAL_REVIEW_PROMPT = """下面是 figure_kit 刚渲染出的截图。请只检查图形质量，不要重新评价技术方案内容。

重点检查：
- 文字是否重叠、裁切、过小或贴边；
- 连接线是否穿过节点、文字或无关容器；
- 箭头方向和起止点是否清楚；
- 虚线/实线是否有稳定语义；
- 布局是否明显拥挤，是否影响理解。

如果存在明显影响理解的问题，请读取 diagram.html 并调用 figure_kit.update 进行有针对性的修正。
如果没有明显问题，或只是轻微审美差异，请不要继续修图，直接推进用户任务。
不要为了同一张图反复微调；除非上一轮修正后仍存在严重可见错误。"""


def hydrate_tool_output_content(
    store: WorkspaceStore,
    project_id: str,
    messages: list[dict[str, Any]],
    *,
    round_id: str | None,
    max_image_attachments: int = MAX_TOOL_OUTPUT_IMAGE_ATTACHMENTS_PER_REQUEST,
) -> list[dict[str, Any]]:
    """Attach model-visible media to recent tool outputs without persisting base64.

    Responses API function_call_output.output is a JSON string in the local SDK,
    so image parts are added as transient user messages after the current tool
    result block instead of being embedded into the tool output item itself.
    """

    if not round_id:
        return messages

    hydrated: list[dict[str, Any]] = []
    pending_reviews: list[dict[str, Any]] = []
    changed = False
    attached_images = 0
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            _flush_pending_reviews(hydrated, pending_reviews)
            hydrated.append(message)
            continue
        hydrated.append(message)
        if message.get("round_id") != round_id:
            if not _next_message_is_tool(messages, index):
                _flush_pending_reviews(hydrated, pending_reviews)
            continue
        if attached_images >= max_image_attachments:
            if not _next_message_is_tool(messages, index):
                _flush_pending_reviews(hydrated, pending_reviews)
            continue
        figure_review = _figure_review_message_from_tool_message(store, project_id, message)
        if figure_review is None:
            if not _next_message_is_tool(messages, index):
                _flush_pending_reviews(hydrated, pending_reviews)
            continue
        pending_reviews.append(figure_review)
        attached_images += 1
        changed = True
        if not _next_message_is_tool(messages, index):
            _flush_pending_reviews(hydrated, pending_reviews)
    _flush_pending_reviews(hydrated, pending_reviews)
    return hydrated if changed else messages


def _figure_review_message_from_tool_message(
    store: WorkspaceStore,
    project_id: str,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    if message.get("tool_name") != "figure_kit":
        return None
    parsed = _parse_tool_content(message.get("content"))
    if not isinstance(parsed, dict) or parsed.get("status") != "success":
        return None
    output = parsed.get("output")
    if not isinstance(output, dict):
        return None
    figure = output.get("figure")
    if not isinstance(figure, dict):
        return None
    figure_id = figure.get("figure_id")
    if not isinstance(figure_id, str) or not figure_id:
        return None
    figure_image = _figure_input_image(store, project_id, figure_id)
    if figure_image is None:
        return None
    title = str(figure.get("title") or "").strip()
    review_text = FIGURE_VISUAL_REVIEW_PROMPT
    if title:
        review_text += f"\n\n当前图片：{figure_id}（{title}）"
    else:
        review_text += f"\n\n当前图片：{figure_id}"
    return {
        "role": "user",
        "content": [
            {"type": "input_text", "text": review_text},
            figure_image,
        ],
        "tool_output_attachment": True,
        "tool_name": "figure_kit",
        "round_id": message.get("round_id"),
        "message_id": message.get("message_id"),
    }


def _figure_input_image(
    store: WorkspaceStore,
    project_id: str,
    figure_id: str,
) -> dict[str, Any] | None:
    png_path = store.figure_render_file(project_id, figure_id)
    try:
        size = png_path.stat().st_size
    except OSError as exc:
        logger.info(
            "figure render image skipped project_id=%s figure_id=%s error=%s",
            project_id,
            figure_id,
            exc,
        )
        return None
    if size <= 0 or size > MAX_TOOL_OUTPUT_IMAGE_BYTES:
        logger.info(
            "figure render image skipped project_id=%s figure_id=%s size=%s max_size=%s",
            project_id,
            figure_id,
            size,
            MAX_TOOL_OUTPUT_IMAGE_BYTES,
        )
        return None
    try:
        encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")
    except OSError as exc:
        logger.info(
            "figure render image encode failed project_id=%s figure_id=%s error=%s",
            project_id,
            figure_id,
            exc,
        )
        return None
    return {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{encoded}",
        "detail": "high",
    }


def _parse_tool_content(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "input_text":
                text = item.get("text")
                if isinstance(text, str):
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return None
        return None
    return None


def _next_message_is_tool(messages: list[dict[str, Any]], index: int) -> bool:
    return index + 1 < len(messages) and messages[index + 1].get("role") == "tool"


def _flush_pending_reviews(target: list[dict[str, Any]], pending_reviews: list[dict[str, Any]]) -> None:
    if not pending_reviews:
        return
    target.extend(pending_reviews)
    pending_reviews.clear()
