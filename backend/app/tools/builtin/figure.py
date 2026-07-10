from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.disclosure import section_title_text
from ...domain.figures import (
    FIGURE_LINK_PATTERN,
    MODEL_REVIEW_IMAGE_MAX_BYTES,
    figure_attachment,
    figure_ref,
    figure_summary,
    parse_figure_ref,
)
from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool

APPENDIX_TITLE = "附录"
FIGURE_RULES_VERSION = "figure-kit-drawio-v1"
FIGURE_RULES = (
    "create 只用于用户明确要求新增一张图；重试、重新生成、替换或修改现有图时，先 list/read 定位，再用 update。",
    "正文只使用工具返回的 markdown_ref 引用图；figure block 只在附录展示图本体。",
    "update 前必须 read，并携带读取到的 drawio_updated_at；提交完整新版 XML，不提交 patch。",
    "create/update 只接受完整、未压缩的 draw.io XML；根节点使用 mxfile，且只包含一个 diagram 和一个 mxGraphModel。",
    "页面固定为 1500x900；所有节点和连线拐点必须位于页面内，系统不自动排版、避障或改线。",
    "图只用于显著降低理解成本，优先表达模块关系、数据流、控制关系、处理阶段、状态变化、边界、输入输出或反馈闭环。",
    "默认采用简约黑白技术示意图：白底、黑色或深灰线条、少量浅灰填充、细边框和简洁箭头。",
    "先确定主链路、核心分组或关键对照，再通过对齐、留白和稳定间距组织；连线应短、少交叉、少回折且不穿过节点。",
    "形状必须有稳定语义：处理模块用矩形、判断用菱形、数据对象用字段框、系统边界用分组框、约束用轻量旁注。",
    "不要套固定图型；图的组织方式必须服务于当前技术关系。",
    "每条连接线必须有明确起点、终点、方向和含义；实线、虚线、反馈线等差异必须具有稳定语义，必要时使用短标签。",
    "虚线应少用且只表达一种含义；禁止无标签长虚线跨区、长斜线、穿越多个区域或穿过节点文字的线。",
    "文本使用短语，辅助说明不超过 1-2 行；禁止小字号密集文字、长句、段落和说明书式正文。",
    "图不是正文摘要；优先画结构、边界、流向、状态和约束，长说明留在正文。",
    "边界框只表达具体、局部、可命名的系统、层级、责任或约束边界；禁止用大外框包住整张主画面。",
    "边界样式不能与连接线语义冲突；分组标题应贴近其内容，线条不得穿过边界标题区。",
    "禁止独立线型示例、图例盒或说明卡；优先在线旁直接标注关系。",
    "避免彩色卡片、渐变、阴影、重圆角、装饰图标、背景纹理、页面式标题栏和营销插画。",
    "默认不添加 101/102/201 等专利附图编号，除非用户明确要求正式附图标记。",
    "单图只承载一个主关系和最多两类辅助关系，线条视觉语义最多三类；建议保留 6-12 个关键元素。",
    "结构、状态、异常恢复或控制路径同时过多时，应删减次要关系或拆图，不要靠图例和说明文字补救混乱。",
    "create/update 成功后检查随结果返回的截图；若布局、文字、线条、箭头、形状语义或可读性有明显问题，read 后 update。",
    "check 用于提交前或批量编辑后检查正文引用、图号文本和附录 figure block 是否一致。",
)


class FigureKitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["rules", "create", "read", "update", "delete", "list", "check"] = Field(
        description="操作类型。首次绘图先调用 rules；create/update 必须携带 rules 返回的 rules_version。"
    )
    ref: str | None = Field(default=None, description="read/update/delete 使用，格式为 figure:fig_000001。")
    title: str | None = Field(default=None, max_length=120, description="create/update 使用，附图标题，例如 系统结构示意图。")
    drawio_xml: str | None = Field(
        default=None,
        description=(
            "create/update 使用，完整 draw.io XML，建议使用 <mxfile><diagram><mxGraphModel>...</mxGraphModel></diagram></mxfile>。"
            "页面必须为 1500x900，节点和拐点不能超出页面。"
        ),
    )
    expected_drawio_updated_at: str | None = Field(default=None, description="update 使用，必须填写最近一次 read 返回的 drawio_updated_at。")
    rules_version: str | None = Field(default=None, description="create/update 使用，填写最近一次 rules 返回的 rules_version。")


@agent_tool(args_model=FigureKitArguments)
def figure_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """创建和维护用于解释技术方案的可编辑 draw.io 结构示意图；首次绘图先调用 rules 获取详细规则。

    Returns:
        rules 返回 rules_version 和完整绘图规则；list/read 返回附图引用或 draw.io XML；create/update 返回精简元数据，并在图片可用时返回 render_image attachment；check 返回引用一致性问题。

    Rules:
        - create/update 前必须先调用 rules，并携带其 rules_version；详细绘图规则只在 rules 结果中返回。
        - update 前还必须 read，并携带最新 drawio_updated_at。
        - 正文使用工具返回的 markdown_ref；figure block 只用于附录展示图本体。

    Examples:
        - 获取规则: {"action":"rules"}
        - 列出附图: {"action":"list"}
        - 读取附图 draw.io XML: {"action":"read","ref":"figure:fig_000001"}
        - 检查一致性: {"action":"check"}
    """
    parsed = _validate_figure_arguments(arguments)
    if parsed["status"] == "failed":
        return parsed
    arguments = parsed["output"]["arguments"]
    action = str(arguments.get("action") or "")
    if action == "rules":
        return {
            "status": "success",
            "output": {
                "rules_version": FIGURE_RULES_VERSION,
                "required_for": ["create", "update"],
                "rules": list(FIGURE_RULES),
            },
        }
    if action == "list":
        return {"status": "success", "output": {"figures": store.figure_summaries(project_id)}}
    if action == "check":
        return {"status": "success", "output": check_figures(store, project_id)}

    if action == "create":
        rules_result = _require_current_rules(arguments)
        if rules_result is not None:
            return rules_result
        title = str(arguments.get("title") or "").strip()
        drawio_xml = arguments.get("drawio_xml")
        if not title:
            return tool_failed("figure_title_required", "figure_kit.create 需要非空 title。")
        if drawio_xml is None:
            return tool_failed("drawio_xml_required", "figure_kit.create 需要非空 drawio_xml。")
        result = store.create_figure(project_id, title=title, drawio_xml=drawio_xml)
        if result.get("status") == "failed":
            return result
        figure = result["output"]["figure"]
        return _figure_change_success(store, project_id, figure)

    figure_id = _figure_id_from_arguments(arguments)
    if figure_id is None:
        return tool_failed("figure_ref_required", "该操作需要 ref，格式为 figure:fig_000001。")

    if action == "read":
        snapshot = store.get_figure_with_drawio(project_id, figure_id)
        if snapshot is None:
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        figure, drawio_xml = snapshot
        payload = figure_summary(figure)
        payload["drawio_xml"] = drawio_xml
        return {"status": "success", "output": {"figure": payload}}

    if action == "update":
        rules_result = _require_current_rules(arguments)
        if rules_result is not None:
            return rules_result
        drawio_xml = arguments.get("drawio_xml")
        if drawio_xml is None:
            return tool_failed("drawio_xml_required", "figure_kit.update 需要非空 drawio_xml。")
        title_value = arguments.get("title")
        update_title = str(title_value).strip() if title_value is not None else None
        result = store.update_figure(
            project_id,
            figure_id,
            title=update_title,
            drawio_xml=drawio_xml,
            expected_drawio_updated_at=arguments.get("expected_drawio_updated_at"),
        )
        if result.get("status") == "failed":
            return result
        return _figure_change_success(store, project_id, result["output"]["figure"])

    if action == "delete":
        usages = _figure_usages(store.get_disclosure(project_id), figure_id)
        if usages:
            return tool_failed("figure_in_use", f"figure 仍被交底书引用或展示：{figure_id}")
        if not store.delete_figure(project_id, figure_id):
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        return {"status": "success", "output": {"deleted": True, "figure_id": figure_id, "ref": figure_ref(figure_id)}}

    return tool_failed("invalid_action", f"不支持的 figure_kit action：{action}")


def _validate_figure_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = FigureKitArguments.model_validate(arguments)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
        message = str(first.get("msg") or "参数不符合工具 schema。")
        return tool_failed(
            "invalid_tool_arguments",
            f"工具参数不符合 schema：{location}: {message}",
            retry_hint="请严格按照当前工具的 parameters schema 重新调用。",
        )
    return {"status": "success", "output": {"arguments": parsed.model_dump(exclude_none=True)}}


def _require_current_rules(arguments: dict[str, Any]) -> dict[str, Any] | None:
    if arguments.get("rules_version") == FIGURE_RULES_VERSION:
        return None
    return tool_failed(
        "figure_rules_required",
        "create/update 前必须先调用 figure_kit.rules，并把返回的 rules_version 原样传入。",
        current_rules_version=FIGURE_RULES_VERSION,
    )


def check_figures(store: WorkspaceStore, project_id: str) -> dict[str, Any]:
    disclosure = store.get_disclosure(project_id)
    figures = store.list_figures(project_id)
    figures_by_id = {str(figure.get("figure_id")): figure for figure in figures}
    appendix = _appendix_section(disclosure)
    appendix_id = appendix.get("id") if appendix else None
    appendix_figure_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    referenced_ids: set[str] = set()

    def visit(section: dict[str, Any]) -> None:
        section_id = section["id"]
        is_appendix = section_id == appendix_id
        for block in section.get("blocks", []):
            block_type = block.get("type")
            if block_type == "figure":
                figure_id = str(block.get("figure_id") or "")
                if not is_appendix:
                    errors.append(_issue("figure_block_outside_appendix", section_id, block.get("id"), figure_id, "figure block 只能存在于附录章节。"))
                if figure_id not in figures_by_id:
                    errors.append(_issue("figure_not_found", section_id, block.get("id"), figure_id, f"figure 不存在：{figure_id}"))
                if is_appendix:
                    appendix_figure_ids.append(figure_id)
                continue
            for text in _block_text_values(block):
                for match in FIGURE_LINK_PATTERN.finditer(text):
                    figure_id = match.group("figure_id")
                    referenced_ids.add(figure_id)
                    figure = figures_by_id.get(figure_id)
                    if figure is None:
                        errors.append(_issue("figure_not_found", section_id, block.get("id"), figure_id, f"figure 不存在：{figure_id}"))
                        continue
                    expected = str(figure.get("label") or "")
                    actual = match.group("label")
                    if actual != expected:
                        errors.append(_issue("figure_label_mismatch", section_id, block.get("id"), figure_id, f"引用文本应为 {expected}，实际为 {actual}。"))
                for label_match in re.finditer(r"图\d+", text):
                    before = text[max(0, label_match.start() - 1) : label_match.start()]
                    if before == "[":
                        continue
                    warnings.append(_issue("figure_plain_text_reference", section_id, block.get("id"), "", f"发现未绑定的图号文本：{label_match.group(0)}。"))
        for child in section.get("sections", []):
            visit(child)

    for section in disclosure.get("sections", []):
        visit(section)

    for figure_id in referenced_ids:
        if figure_id in figures_by_id and figure_id not in appendix_figure_ids:
            warnings.append(_issue("figure_not_displayed_in_appendix", appendix_id or "", None, figure_id, f"正文引用的 {figure_id} 尚未在附录中展示。"))

    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "figures": [figure_summary(figure) for figure in figures],
    }


def _figure_id_from_arguments(arguments: dict[str, Any]) -> str | None:
    ref = arguments.get("ref")
    if not isinstance(ref, str):
        return None
    return parse_figure_ref(ref)


def _figure_change_success(
    store: WorkspaceStore,
    project_id: str,
    figure: dict[str, Any],
) -> dict[str, Any]:
    payload = figure_summary(figure)
    render_file = store.figure_render_file(project_id, str(figure.get("figure_id") or ""))
    try:
        render_size = render_file.stat().st_size
    except OSError:
        render_size = 0
    attachment_available = 0 < render_size <= MODEL_REVIEW_IMAGE_MAX_BYTES
    if attachment_available:
        message = (
            "已生成当前图片，并随本次工具结果附加截图供你查看。请复盘布局、文字、线条、箭头、"
            "形状语义和整体可读性；如存在明显问题，请 read 后 update。"
        )
        attachments = [figure_attachment(figure)]
    else:
        reason = "render.png 缺失或为空" if render_size <= 0 else f"render.png 超过 {MODEL_REVIEW_IMAGE_MAX_BYTES} 字节"
        message = f"图片已生成，但视觉复盘附件未附加：{reason}。不要声称已经看过截图。"
        attachments = []
    return {
        "status": "success",
        "output": {
            "figure": payload,
            "message": message,
            "attachments": attachments,
        },
    }


def _appendix_section(disclosure: dict[str, Any]) -> dict[str, Any] | None:
    for section in disclosure.get("sections", []):
        if section_title_text(section) == APPENDIX_TITLE:
            return section
    return None


def _figure_usages(disclosure: dict[str, Any], figure_id: str) -> list[dict[str, Any]]:
    usages: list[dict[str, Any]] = []

    def visit(section: dict[str, Any]) -> None:
        for block in section.get("blocks", []):
            if block.get("type") == "figure" and block.get("figure_id") == figure_id:
                usages.append({"section_id": section["id"], "block_id": block.get("id"), "kind": "figure_block"})
            for text in _block_text_values(block):
                for match in FIGURE_LINK_PATTERN.finditer(text):
                    if match.group("figure_id") == figure_id:
                        usages.append({"section_id": section["id"], "block_id": block.get("id"), "kind": "figure_ref"})
        for child in section.get("sections", []):
            visit(child)

    for section in disclosure.get("sections", []):
        visit(section)
    return usages


def _block_text_values(block: dict[str, Any]) -> list[str]:
    block_type = block.get("type")
    if block_type in {"title", "paragraph"}:
        return [str(block.get("text") or "")]
    if block_type == "list":
        return [str(item) for item in block.get("items") or []]
    if block_type == "table":
        values: list[str] = [str(item) for item in block.get("columns") or []]
        for row in block.get("rows") or []:
            values.extend(str(cell) for cell in row)
        return values
    return []


def _issue(code: str, section_id: str, block_id: Any, figure_id: str, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "section_id": section_id,
        "block_id": block_id,
        "figure_id": figure_id,
        "message": message,
    }
