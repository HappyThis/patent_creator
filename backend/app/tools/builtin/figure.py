from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.disclosure import section_title_text
from ...domain.figures import FIGURE_LINK_PATTERN, figure_ref, figure_summary, parse_figure_ref
from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool

APPENDIX_TITLE = "附录"


class FigureKitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "read", "update", "delete", "list", "check"] = Field(
        description="操作类型。create 新建 HTML 附图；read 读取 diagram.html；update 覆盖 HTML 并重新截图；delete 删除附图；list 列出可引用格式；check 检查正文引用和附录展示。"
    )
    ref: str | None = Field(default=None, description="read/update/delete 使用，格式为 figure:fig_000001。")
    title: str | None = Field(default=None, description="create/update 使用，附图标题，例如 系统结构示意图。")
    html: str | None = Field(
        default=None,
        description=(
            "create/update 使用，完整 diagram.html 源码。必须是纯 HTML/CSS，包含 id=\"diagram\" 的固定画布根节点；"
            "画布尺寸固定为 1500x900，不要引用外部资源、脚本、iframe 或事件处理器，保证图片可离线、可复现、无执行风险；"
            "允许 SVG 内部定义引用，例如 marker-end=\"url(#arrow)\"、clip-path=\"url(#clip)\" 或 href=\"#localId\"；"
            "图片应是简约黑白技术示意图（monochrome technical schematic / engineering block diagram），"
            "用网格化排版、分组边界、正交箭头和少量短标签帮助理解结构、流程、层级、模块边界和逻辑关系；"
            "形状、布局、连接线和文字都必须服务语义：形状区分对象类型，布局给出稳定阅读路径，"
            "每条箭头/连线有明确起点、终点、方向和含义，文字短且可读；默认不要添加专利附图编号。"
        ),
    )


@agent_tool(args_model=FigureKitArguments)
def figure_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """创建和维护用于解释技术方案的简约黑白结构示意图，工具会保存 diagram.html 并同步截图为 render.png。

    Returns:
        list 返回 figures，每项包含 ref、markdown_ref、caption；read/create/update 返回 figure 元数据和 html 源码；create/update 后模型会收到 render.png 截图用于视觉复盘；check 返回 errors/warnings；失败返回 failed 和 code/message。

    Rules:
        - create 只用于用户明确要求新增一张图；用户说“重试/重新生成/替换/修改当前图/修改图1”时，必须 list 或 read 定位现有图，再用 update 覆盖原图，不能 create 新图。
        - 正文引用图时使用 list/create/read 返回的 markdown_ref，例如 [图1](figure:fig_000001)，不要手写或猜测图号。
        - figure block 只用于在“附录”章节展示图本体；非附录章节只能使用 Markdown 链接引用图。
        - 修改图前先 read，基于返回的 html 生成完整新版 diagram.html，再 update。
        - create/update 必须提交完整 HTML 文档，包含 <!doctype html> 或 <html>，并包含 id="diagram" 的根节点。
        - HTML 附图固定 1500x900 画布；不要让内容依赖滚动、动画、外链字体、外部图片或脚本，保证离线可渲染、结果可复现、不会执行不可信代码。
        - create/update 后请查看随工具结果提供的截图，检查布局、文字、线条、箭头和形状语义；只有存在明显影响理解的问题时才用 update 修正，不要为了轻微审美差异反复微调。
        - 可以使用 SVG 内部 defs 引用来画箭头、裁剪和局部效果，例如 marker-end="url(#arrow)"、clip-path="url(#clip)"、href="#localId"；这些必须引用当前 HTML 内已经定义的 id，不能引用外部 URL。
        - 只有当图能显著降低理解成本时才创建图：适合表达模块关系、数据流向、处理阶段、层级边界、输入输出和关键反馈闭环；不适合把长段文字换成图。
        - 默认生成帮助理解结构的简约黑白技术示意图，而不是产品页面、海报、仪表盘或正式专利标号图。
        - 视觉样式应接近 engineering block diagram / technical schematic：白底，黑色或深灰线条，少量浅灰填充，细边框，直角/正交连接线，简洁箭头，清晰分组边界。
        - 排版优先使用网格和对齐：画布四周保留充足边距，模块尺寸尽量一致，模块之间保持稳定间距；连线尽量水平/垂直，减少交叉、回折和穿越节点。
        - 形状必须有稳定语义：模块/处理步骤可用矩形，判断分支可用菱形，数据对象或记录可用表格/字段框，系统边界可用分组框，注释或约束可用轻量旁注；不要所有对象都无差别使用同一种框，也不要混用形状却没有语义规则。
        - 布局必须先确定主阅读路径：优先左到右或上到下；分层、泳道、分组、留白和对齐应让读者先看出主链路，再看辅助关系；避免把架构、流程、异常和说明全部塞进同一张拥挤图。
        - 箭头和连接线必须表达明确关系：每条线应有清楚起点、终点、方向和含义；实线/虚线/回箭头/反馈线应各自代表稳定语义，必要时加短标签或图例；不要用无标签长虚线跨多个分区，不要让线穿过节点、文字或无关容器。
        - 文本应短、可读、层级清楚：节点标题用短语，辅助说明不超过 1-2 行；避免小字号密集文字、长句、段落和说明书式正文。
        - 图不是正文摘要：优先画结构、边界、流向、状态和约束关系；长说明应放在正文，不要塞进节点或底部长条注释。
        - 避免彩色卡片、渐变、阴影、圆角过重、装饰图标、背景纹理、页面式标题栏和营销插画；可以用线框、分区框、泳道、表格式字段表达结构。
        - 默认不要在节点角落添加 101/102/201 这类专利附图编号；只有用户明确要求“附图标记/编号/正式专利附图”时才添加。
        - 先判断图型再排版：流程图用于步骤、状态流转和判断分支；架构图用于模块协同、系统边界、层次关系和依赖方向；结构示意图用于部件、通道、数据流和约束关系。
        - 架构图应体现分层、系统边界、模块职责和依赖方向，不要画成流程/状态回环；流程图则应突出主路径和少量关键分支。
        - 单张图建议 6-12 个关键元素；复杂图拆成“高层结构图 + 关键子流程图”，不要把完整调用链、日志流、工具调用明细全部塞进一张图。
        - check 用于提交前或批量编辑后检查 figure 引用、图号文本和 figure block 位置是否一致。

    Examples:
        - 列出附图: {"action":"list"}
        - 创建附图: {"action":"create","title":"系统结构示意图","html":"<!doctype html><html><body><div id=\\"diagram\\">...</div></body></html>"}
        - 读取附图源码: {"action":"read","ref":"figure:fig_000001"}
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
        html = str(arguments.get("html") or "").strip()
        if not title:
            return tool_failed("figure_title_required", "figure_kit.create 需要非空 title。")
        if not html:
            return tool_failed("figure_html_required", "figure_kit.create 需要非空 html。")
        result = store.create_figure(project_id, title=title, html=html)
        if result.get("status") == "failed":
            return result
        figure = result["output"]["figure"]
        return {"status": "success", "output": {"figure": _full_figure_payload(store, project_id, figure), "warnings": result["output"].get("warnings", [])}}

    figure_id = _figure_id_from_arguments(arguments)
    if figure_id is None:
        return tool_failed("figure_ref_required", "该操作需要 ref，格式为 figure:fig_000001。")

    if action == "read":
        figure = store.get_figure(project_id, figure_id)
        if figure is None:
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        return {"status": "success", "output": {"figure": _full_figure_payload(store, project_id, figure)}}

    if action == "update":
        html = str(arguments.get("html") or "").strip()
        if not html:
            return tool_failed("figure_html_required", "figure_kit.update 需要非空 html。")
        title_value = arguments.get("title")
        update_title = str(title_value).strip() if title_value is not None else None
        result = store.update_figure(project_id, figure_id, title=update_title, html=html)
        if result.get("status") == "failed":
            return result
        return {
            "status": "success",
            "output": {"figure": _full_figure_payload(store, project_id, result["output"]["figure"]), "warnings": result["output"].get("warnings", [])},
        }

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
    payload = dict(figure)
    payload.update(figure_summary(figure))
    html = store.read_figure_html(project_id, str(figure.get("figure_id") or ""))
    if html is not None:
        payload["html"] = html
    return payload


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
