from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ...domain.figures import MODEL_REVIEW_IMAGE_MAX_BYTES, drawio_updated_at, parse_figure_ref
from ...storage.workspace_store import WorkspaceStore

logger = logging.getLogger("patent_creator.context.tool_output_content")

MAX_TOOL_OUTPUT_IMAGE_ATTACHMENTS_PER_REQUEST = 8
FIGURE_VISUAL_REVIEW_PROMPT = """下面是 figure_kit 刚渲染出的截图。请确认它是否已经具备正确、清楚且基本美观的可用质量。不要重新判断技术方案本身是否成立或先进，也不要为了套用固定图型而重画；但必须检查截图是否忠实表达生成前已经确定的主体以及与本图目的相关的条件、方向、分支、反馈或终止关系，并在存在对应正文时保持一致。

仅在存在影响理解或使用的客观问题时继续修改，例如：
- 文字重叠、裁切、明显过小，或关键文字无法辨认；
- 关键节点被遗漏、错误断开，或主关系和阅读方向无法判断；
- 箭头方向、起点、终点明显错误，导致关系含义发生变化；
- 连线穿过关键文字或节点，导致连接对象无法辨认；
- 关键内容超出画布、截图显示不全，或布局严重拥挤；
- 形状、线型或边界在同一张图中表达互相冲突的含义。

轻微间距差异、细小不对称和纯审美偏好不构成继续修改的理由，可留给用户在 Draw.io 中人工调整。不要以自动达到视觉完美为目标，也不要反复进行没有明确收益的微调。

需要局部修正时，先 read 当前 drawio_xml，再调用 figure_kit.edit，并让每个 old_text 在当前 XML 中唯一匹配；需要整体重排或大范围修改时，先 read 后调用 figure_kit.write。两者都必须携带读取时的 drawio_updated_at 和当前 rules_version。修改后仍会返回新截图供你判断。"""


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
            if _has_visual_review_attachment(message):
                pending_reviews.append(
                    _figure_review_unavailable_message(
                        message,
                        "本轮待复盘图片超过附件数量上限，当前图片没有附加；不要声称已经查看该图。",
                    )
                )
                changed = True
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
    figure_id = _figure_id_from_output(output)
    if not isinstance(figure_id, str) or not figure_id:
        return None
    expected_updated_at = _figure_attachment_updated_at(output, figure_id)
    if expected_updated_at:
        current = store.get_figure(project_id, figure_id)
        if current is None or drawio_updated_at(current) != expected_updated_at:
            return _figure_review_unavailable_message(
                message,
                f"图片 {figure_id} 已在本次工具结果之后发生变化，未附加旧版本截图；不要声称已经查看该版本。",
            )
    figure_image = _figure_input_image(store, project_id, figure_id)
    if figure_image is None:
        return _figure_review_unavailable_message(
            message,
            f"图片 {figure_id} 的视觉复盘附件读取失败；不要声称已经查看该图。",
        )
    figure = output.get("figure")
    title = str(figure.get("title") or "").strip() if isinstance(figure, dict) else ""
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


def _figure_id_from_output(output: dict[str, Any]) -> str | None:
    for attachment in output.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        if attachment.get("type") != "render_image" or attachment.get("purpose") != "visual_review":
            continue
        figure_id = parse_figure_ref(str(attachment.get("ref") or ""))
        if figure_id:
            return figure_id
    return None


def _figure_attachment_updated_at(output: dict[str, Any], figure_id: str) -> str:
    for attachment in output.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        if parse_figure_ref(str(attachment.get("ref") or "")) != figure_id:
            continue
        return str(attachment.get("drawio_updated_at") or "")
    return ""


def _has_visual_review_attachment(message: dict[str, Any]) -> bool:
    parsed = _parse_tool_content(message.get("content"))
    output = parsed.get("output") if isinstance(parsed, dict) else None
    return isinstance(output, dict) and _figure_id_from_output(output) is not None


def _figure_review_unavailable_message(message: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
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
    if size <= 0 or size > MODEL_REVIEW_IMAGE_MAX_BYTES:
        logger.info(
            "figure render image skipped project_id=%s figure_id=%s size=%s max_size=%s",
            project_id,
            figure_id,
            size,
            MODEL_REVIEW_IMAGE_MAX_BYTES,
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
