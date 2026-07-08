from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ...storage.workspace_store import WorkspaceStore

logger = logging.getLogger("patent_creator.context.tool_output_content")

MAX_TOOL_OUTPUT_IMAGE_ATTACHMENTS_PER_REQUEST = 8
MAX_TOOL_OUTPUT_IMAGE_BYTES = 2_000_000
FIGURE_VISUAL_REVIEW_PROMPT = """下面是 figure_kit 刚渲染出的截图。请只检查图形质量，不要重新评价技术方案内容，也不要为了套用固定图型而重画。

请按交付级图片标准严格复盘：截图中不应保留任何肉眼可见、会降低专业度或理解效率的问题。不要用“基本还行”放过明显瑕疵。
如果 figure_kit 工具结果包含 geometry_report，请优先处理 issues 中 severity=error 的问题，尤其是 semantic_* 结构语义问题，并结合 warning 级问题判断是否需要修正；截图用于确认整体观感和语义清晰度。

硬失败条件：只要命中任一条，就必须读取 diagram.html 并调用 figure_kit.update 修正：
- 主关系不能在几秒内看出来，或画面没有清楚的阅读路径；
- 文字有重叠、裁切、过小、贴边、过密、换行别扭或像正文段落；
- 连接线穿过节点、文字或无关容器，或存在大范围绕线、交叉、回折、贴边；
- 箭头方向、起点、终点或关系含义不清楚；
- 业务节点没有参与任何关系，或同一分组内同类节点的连接关系不一致，导致节点像孤立摆设；
- 虚线、实线、回箭头、边界框和不同形状没有稳定语义；
- 出现无标签长虚线跨区、虚线过多，或虚线承担多个不同含义；
- 分组框没有明确表达系统、层级、责任或约束边界，或变成装饰性大框；
- 大外框包住整张主画面，边界样式与连接线语义冲突，或虚线边界和虚线路径混用；
- 分组标题远离它约束的内容、漂浮在大空白区域，或连接线穿过边界标题区；
- 外部对象和内部对象的边界关系混乱，连接线没有从边界边缘进入内部节点；
- 底部或角落出现独立线型示例、图例盒或说明卡，试图用图例弥补线条混乱；
- 存在跨越主画面的长斜线、长虚线或穿越多个区域的连接线；
- 布局拥挤、失衡、留白不足，或把结构、状态、异常、说明塞进同一张图导致理解困难；
- 图中说明文字承担了主要表达，布局、边界和连接关系本身不能说明问题。

修正时优先删减、重排、拆分关系、缩短连线、统一线条语义和减少文字；删除独立图例盒、长斜线和无意义大外框，跨层关系改为短折线、接口节点、局部回路或旁注。如果一张图无法同时表达多个关系，请保留最重要主关系，省略次要关系或拆成更清晰的表达。
只有截图已经达到交付级质量，且剩余问题只是非常轻微的审美偏好时，才不要继续修图，直接推进用户任务。不要为了同一张图反复微调；除非上一轮修正后仍存在上述硬失败问题。"""


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
