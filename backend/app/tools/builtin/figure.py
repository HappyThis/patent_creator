from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.disclosure import section_title_text
from ...domain.figures import FIGURE_LINK_PATTERN, figure_attachment, figure_ref, figure_summary, parse_figure_ref
from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool

APPENDIX_TITLE = "附录"


class FigureKitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "read", "update", "delete", "list", "check"] = Field(
        description="操作类型。create 新建 draw.io 附图；read 读取当前 draw.io XML；update 覆盖 draw.io XML 并重新导出图片；delete 删除附图；list 列出可引用格式；check 检查正文引用和附录展示。"
    )
    ref: str | None = Field(default=None, description="read/update/delete 使用，格式为 figure:fig_000001。")
    title: str | None = Field(default=None, description="create/update 使用，附图标题，例如 系统结构示意图。")
    drawio_xml: str | None = Field(
        default=None,
        description=(
            "create/update 使用，完整 draw.io XML，建议使用 <mxfile><diagram><mxGraphModel>...</mxGraphModel></diagram></mxfile>。"
            "源文件会保存为 diagram.drawio，并通过官方 draw.io embed 导出 render.png。"
        ),
    )
    expected_drawio_updated_at: str | None = Field(default=None, description="update 使用，必须填写最近一次 read 返回的 drawio_updated_at。")


@agent_tool(args_model=FigureKitArguments)
def figure_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """创建和维护用于解释技术方案的可编辑黑白结构示意图，工具会保存 diagram.drawio 并同步导出 render.png。

    Returns:
        list 返回 figures，每项包含 ref、markdown_ref、caption、drawio_updated_at；read 返回 drawio_xml 和 drawio_updated_at；create/update 返回精简 figure 元数据、复盘说明和 render_image attachment，随后模型会收到 render.png 截图用于视觉复盘；check 返回 errors/warnings；失败返回 failed 和 code/message。

    Rules:
        - create 只用于用户明确要求新增一张图；用户说“重试/重新生成/替换/修改当前图/修改图1”时，必须 list 或 read 定位现有图，再用 update 覆盖原图，不能 create 新图。
        - 正文引用图时使用 list/create/read 返回的 markdown_ref，例如 [图1](figure:fig_000001)，不要手写或猜测图号。
        - figure block 只用于在“附录”章节展示图本体；非附录章节只能使用 Markdown 链接引用图。
        - 修改图前必须 read，基于返回的 drawio_xml 生成完整新版 draw.io XML，再 update；update 必须带 read 返回的 drawio_updated_at，否则会被拒绝。
        - create/update 必须提交完整 drawio_xml，不要提交片段、增量 patch、Mermaid 或 HTML。推荐根节点使用 mxfile，页面宽高使用 1500x900。
        - 你可以使用 draw.io/mxGraph 支持的节点、边、分组、泳道、数据库、文件夹、圆角、折线、标签等能力表达结构；不要提交旧的结构化 JSON。
        - 排版由你在 draw.io XML 的 mxGeometry、mxPoint、style 中明确控制；系统不会自动排版、避障或改线。
        - 只有当图能显著降低理解成本时才创建图：适合表达模块关系、数据流向、控制关系、处理阶段、状态变化、层级边界、输入输出和关键反馈闭环；不适合把长段文字换成图。
        - 默认生成帮助理解结构的简约黑白技术示意图，而不是产品页面、海报、仪表盘或正式专利标号图。
        - 视觉样式应是简约黑白 technical schematic：白底，黑色或深灰线条，少量浅灰填充，细边框，简洁箭头；边界框只在确实表达具体、局部、可命名的系统、层级、责任或约束边界时使用，不要用大外框包住整张主画面。
        - 排版优先表达主关系：先确定读者应先看的主链路、核心分组或关键对照，再用对齐、留白和稳定间距组织元素；连线应尽量短、少交叉、少回折、少穿越节点。
        - 形状必须有稳定语义：模块/处理步骤可用矩形，判断分支可用菱形，数据对象或记录可用表格/字段框，系统边界可用分组框，注释或约束可用轻量旁注；不要所有对象都无差别使用同一种框，也不要混用形状却没有语义规则。
        - 不要先套固定图型：先确定这张图要回答的核心问题、主视觉路径、辅助关系和省略内容；可以借用常见图的组织方式，但不要为了像某类图而牺牲技术关系本身。
        - 箭头和连接线必须表达明确关系：每条线应有清楚起点、终点、方向和含义；实线、虚线、回箭头、反馈线等视觉差异必须各自代表稳定语义，必要时加线旁短标签。虚线应少用，通常只用于可选、弱依赖、未确认、反馈或边界含义；不要用无标签长虚线跨多个分区，不要让线穿过节点、文字或无关容器。
        - 文本应短、可读、层级清楚：节点标题用短语，辅助说明不超过 1-2 行；避免小字号密集文字、长句、段落和说明书式正文。
        - 图不是正文摘要：优先画结构、边界、流向、状态和约束关系；长说明应放在正文，不要塞进节点或底部长条注释。
        - 边界样式不能与连接线语义冲突；如果虚线用于恢复、弱依赖、可选或控制路径，边界不要使用虚线，改用浅灰填充、细实线、局部标题或留白分区。
        - 分组标题必须贴近它约束的内容，不要在大空白区域放漂浮标题；外部对象应在边界外、内部对象应在边界内，连接线只能从边界边缘进入内部节点，不要穿过边界标题区。
        - 不要在底部或角落生成独立线型示例、图例盒或说明卡；不要用图例弥补线条混乱。线条含义优先用线旁短标签表达。
        - 不要使用跨越主画面的长斜线、长虚线或穿越多个区域的连接线；跨层关系应通过短折线、接口节点、局部回路、旁注或拆图表达。
        - 避免彩色卡片、渐变、阴影、圆角过重、装饰图标、背景纹理、页面式标题栏和营销插画；可以用线框、分区框、泳道、表格式字段表达结构。
        - 默认不要在节点角落添加 101/102/201 这类专利附图编号；只有用户明确要求“附图标记/编号/正式专利附图”时才添加。
        - 复杂度预算必须克制：单图只承载一个主关系和少量辅助关系，辅助关系最多 2 类，线条视觉语义最多 3 类；虚线只代表一种稳定含义且必须可从线旁标签或上下文看出。
        - 单张图建议 6-12 个关键元素，同时控制连接线数量、文字密度和语义层数；若同时存在结构关系、状态链、异常恢复或控制路径，优先拆图或省略次要关系；不要把完整调用链、日志流、工具调用明细、异常和解释全部塞进一张图，也不要通过增加线条、图例或说明文字来解释已经混乱的图。如果需要靠长标题、图例或说明卡才能解释关系，应优先删减、重排、局部化或拆图。
        - create/update 后会随工具结果附加截图供视觉复盘，请检查布局、文字、线条、箭头、形状语义和整体可读性；若明显影响理解或专业度，请 read 后 update。
        - check 用于提交前或批量编辑后检查 figure 引用、图号文本和 figure block 位置是否一致。

    Examples:
        - 列出附图: {"action":"list"}
        - 创建附图: {"action":"create","title":"系统结构示意图","drawio_xml":"<mxfile><diagram name='系统结构示意图'><mxGraphModel page='1' pageWidth='1500' pageHeight='900'><root><mxCell id='0'/><mxCell id='1' parent='0'/></root></mxGraphModel></diagram></mxfile>"}
        - 读取附图 draw.io XML: {"action":"read","ref":"figure:fig_000001"}
        - 检查一致性: {"action":"check"}
    """
    parsed = _validate_figure_arguments(arguments)
    if parsed["status"] == "failed":
        return parsed
    arguments = parsed["output"]["arguments"]
    action = str(arguments.get("action") or "")
    if action == "list":
        return {"status": "success", "output": {"figures": store.figure_summaries(project_id)}}
    if action == "check":
        return {"status": "success", "output": check_figures(store, project_id)}

    if action == "create":
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
        return _figure_change_success(figure)

    figure_id = _figure_id_from_arguments(arguments)
    if figure_id is None:
        return tool_failed("figure_ref_required", "该操作需要 ref，格式为 figure:fig_000001。")

    if action == "read":
        figure = store.get_figure(project_id, figure_id)
        if figure is None:
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        return {"status": "success", "output": {"figure": _full_figure_payload(store, project_id, figure)}}

    if action == "update":
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
        return _figure_change_success(result["output"]["figure"])

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


def _full_figure_payload(store: WorkspaceStore, project_id: str, figure: dict[str, Any]) -> dict[str, Any]:
    payload = figure_summary(figure)
    drawio_xml = store.read_figure_drawio_xml(project_id, str(figure.get("figure_id") or ""))
    if drawio_xml is not None:
        payload["drawio_xml"] = drawio_xml
    return payload


def _figure_change_success(figure: dict[str, Any]) -> dict[str, Any]:
    payload = figure_summary(figure)
    return {
        "status": "success",
        "output": {
            "figure": payload,
            "message": "已生成当前图片，并随本次工具结果附加截图供你查看。请基于图片附件复盘布局、文字、线条、箭头、形状语义和整体可读性；如存在明显影响理解或专业度的问题，请读取并修改该图，尽可能保持交付级图片质量。",
            "attachments": [figure_attachment(figure)],
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
