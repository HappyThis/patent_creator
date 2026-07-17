from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ...domain.figures import MODEL_REVIEW_IMAGE_MAX_BYTES, drawio_updated_at, parse_figure_ref
from ...storage.workspace_store import WorkspaceStore

logger = logging.getLogger("patent_creator.context.tool_output_content")

MAX_TOOL_OUTPUT_IMAGE_ATTACHMENTS_PER_REQUEST = 8
FIGURE_VISUAL_REVIEW_PROMPT = """下面是 figure_kit 返回的当前截图；read 复用已有截图，不占 write/update 尝试次数。请把它当作准备交给专利代理人员的正式技术附图复盘，而不是只确认 XML 能否渲染。不要重新评价技术方案是否先进，也不要追求像素级完美或固定图型。一张图在本次用户请求内最多尝试 8 次 write/update，预检或渲染失败也计数。

依次检查：
- 缩略图构图：先暂时不读小字，判断视觉中心、主阅读方向、语义分区和层级能否一眼识别；主体是否居中且疏密均衡，是否一侧拥挤、另一侧大面积无意义空白，或被无必要大外框削弱；
- 对齐与节奏：同类节点的尺寸、基线、间距、圆角和内部留白是否一致；分区标题是否稳定，主次对象是否通过位置、留白和有限灰度自然区分；
- 连线质量：首尾是否接触正确节点，箭头方向是否正确，主干和分支是否清楚；是否穿过无关节点或文字、交叉、贴边、长距离绕行，或存在悬空、微小折返、共线覆盖、错误共享路径和汇聚错位；多对多关系是否应收束为总线、汇聚点、中间层或集合；
- 文字与层级：标题、分区标题、节点、边标签是否形成有限且清楚的字号层级；是否重叠、裁切、过密、过小，节点是否塞入正文式长句，边标签是否遮线；
- 视觉系统：同一 visualRole 的节点是否保持统一无衬线字体、黑白灰度、边框和主线线宽、箭头、标签白底及形状；黑白是否仍有层级，而不是退化成默认细线与相同矩形；
- 图解逻辑与技术表达：图型或分区是否适合架构、过程、状态、时间窗口、队列/集合或映射关系；应呈现的对象、方向、条件、分支、反馈、汇聚和终止是否完整，并与当前正文一致。

不要因为“已经能看懂”就忽略明显的构图、线条或排版问题；首次成功渲染若仍不像可直接使用的正式工程图，可做一次合并的视觉整理。只修改能明确提升构图、可读性或严肃工程气质的问题，轻微不对称和纯个人偏好留给用户，不做无变化编辑。若正文在最后一次看图后实质改变，应按最终正文重新复核。"""


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

    prepared_reviews = _prepare_figure_reviews(
        store,
        project_id,
        messages,
        round_id=round_id,
        max_image_attachments=max_image_attachments,
    )
    if not prepared_reviews:
        return messages

    hydrated: list[dict[str, Any]] = []
    pending_reviews: list[dict[str, Any]] = []
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
        figure_review = prepared_reviews.get(index)
        if figure_review is not None:
            pending_reviews.append(figure_review)
        if not _next_message_is_tool(messages, index):
            _flush_pending_reviews(hydrated, pending_reviews)
    _flush_pending_reviews(hydrated, pending_reviews)
    return hydrated


def _prepare_figure_reviews(
    store: WorkspaceStore,
    project_id: str,
    messages: list[dict[str, Any]],
    *,
    round_id: str,
    max_image_attachments: int,
) -> dict[int, dict[str, Any]]:
    """Keep the newest distinct, usable screenshots while preserving message order."""

    prepared: dict[int, dict[str, Any]] = {}
    selected_images = 0
    seen_revisions: set[tuple[str, str]] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "tool" or message.get("round_id") != round_id:
            continue
        output = _figure_output_from_tool_message(message)
        if output is None:
            continue
        figure_id = _figure_id_from_output(output)
        if not figure_id:
            continue
        revision = _figure_attachment_updated_at(output, figure_id)
        revision_key = (figure_id, revision)
        if revision_key in seen_revisions:
            continue
        seen_revisions.add(revision_key)

        review = _figure_review_message_from_tool_message(store, project_id, message)
        if review is None:
            continue
        has_image = any(part.get("type") == "input_image" for part in review.get("content", []))
        if not has_image:
            prepared[index] = review
            continue
        if selected_images >= max(0, max_image_attachments):
            prepared[index] = _figure_review_unavailable_message(
                message,
                "当前截图早于最近图片窗口，未附加给模型；最近的有效截图会优先保留。",
            )
            continue
        prepared[index] = review
        selected_images += 1
    return prepared


def _figure_review_message_from_tool_message(
    store: WorkspaceStore,
    project_id: str,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    if message.get("tool_name") != "figure_kit":
        return None
    output = _figure_output_from_tool_message(message)
    if output is None:
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
    review = output.get("review")
    if isinstance(review, dict):
        attempt = review.get("attempt")
        limit = review.get("limit")
        successful_renders = review.get("successful_renders")
        remaining = review.get("remaining")
        if all(isinstance(value, int) for value in (attempt, limit, successful_renders, remaining)):
            review_text += (
                f"\n\n当前进度：已尝试 {attempt}/{limit} 次，成功渲染 {successful_renders} 次，"
                f"剩余 {remaining} 次。"
            )
    warnings = output.get("warnings")
    if isinstance(warnings, list) and warnings:
        warning_lines = [str(item.get("message") or "").strip() for item in warnings if isinstance(item, dict)]
        warning_lines = [line for line in warning_lines if line]
        if warning_lines:
            review_text += "\n\n自动检查的非阻断 warnings：\n- " + "\n- ".join(warning_lines)
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
    output = _figure_output_from_tool_message(message)
    return isinstance(output, dict) and _figure_id_from_output(output) is not None


def _figure_output_from_tool_message(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("tool_name") != "figure_kit":
        return None
    parsed = _parse_tool_content(message.get("content"))
    if not isinstance(parsed, dict) or parsed.get("status") != "success":
        return None
    output = parsed.get("output")
    return output if isinstance(output, dict) else None


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
