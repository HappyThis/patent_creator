from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.disclosure import section_title_text
from ...domain.figures import (
    DRAWIO_XML_MAX_CHARS,
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
FIGURE_RULES_VERSION = "figure-kit-drawio-v2"
MAX_FIGURE_EDITS = 20
FIGURE_RULES = (
    "create 只用于用户明确要求新增一张图；修改现有图时先 list/read 定位，局部修改用 edit，整体重构或大范围调整用 write。",
    "正文只使用工具返回的 markdown_ref 引用图；figure block 只在附录展示图本体。",
    "write/edit 前必须 read 并携带读取到的 drawio_updated_at；write 提交完整新版 XML，edit 提交从当前 XML 复制的唯一 old_text 与对应 new_text。",
    "create/write 只接受完整、未压缩、单 diagram 的 draw.io XML；页面固定为 1500x900，所有节点和连线拐点必须位于页面内；edit 完成后同样执行完整校验。",
    "系统不自动排版、避障、改线或美化；Agent 和人类都直接修改同一份 draw.io XML。",
    "图只用于显著降低理解成本，优先表达模块关系、数据流、控制关系、处理阶段、状态变化、边界、输入输出或反馈闭环。",
    "默认采用简约黑白技术示意图；除非用户另有要求，避免渐变、重阴影、装饰图标、背景纹理和营销式视觉元素。",
    "形状、连线、边界和必要图例可按技术关系自由选择，但同一张图中的视觉语义必须稳定，不强制固定图型或形状映射。",
    "先建立清楚的主关系或阅读路径，再通过对齐、留白和稳定间距组织；连线应尽量短、少交叉，并避免穿过节点或文字。",
    "文本使用短语，复杂度按表达需要决定；当一张图已难以理解时删减次要关系或拆图，不要用长段说明弥补结构混乱。",
    "默认不添加 101/102/201 等专利附图编号，除非用户明确要求正式附图标记。",
    "create/write/edit 成功后检查随结果返回的截图；只修复影响理解或使用的客观问题，局部问题用 edit，整体问题用 write，轻微审美偏好留给人工调整。",
)


class FigureEditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_text: str = Field(
        min_length=1,
        max_length=DRAWIO_XML_MAX_CHARS,
        description="必须从最近一次 read 返回的 drawio_xml 中原样复制，并在当前 XML 中唯一出现。",
    )
    new_text: str = Field(
        max_length=DRAWIO_XML_MAX_CHARS,
        description="用于替换 old_text 的新 XML 片段；允许为空字符串以删除目标片段。",
    )


class FigureKitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["rules", "create", "read", "write", "edit", "delete", "list", "check"] = Field(
        description="操作类型。首次绘图先调用 rules；create/write/edit 必须携带 rules 返回的 rules_version。"
    )
    ref: str | None = Field(default=None, description="read/write/edit/delete 使用，格式为 figure:fig_000001。")
    title: str | None = Field(default=None, max_length=120, description="create/write/edit 使用，附图标题，例如 系统结构示意图。")
    drawio_xml: str | None = Field(
        default=None,
        description=(
            "create/write 使用，完整 draw.io XML，建议使用 <mxfile><diagram><mxGraphModel>...</mxGraphModel></diagram></mxfile>。"
            "页面必须为 1500x900，节点和拐点不能超出页面。"
        ),
    )
    edits: list[FigureEditOperation] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_FIGURE_EDITS,
        description="edit 使用；按顺序执行的精确替换。每个 old_text 必须在当前 XML 中恰好出现一次，任一失败则全部不落盘。",
    )
    expected_drawio_updated_at: str | None = Field(default=None, description="write/edit 使用，必须填写最近一次 read 返回的 drawio_updated_at。")
    rules_version: str | None = Field(default=None, description="create/write/edit 使用，填写最近一次 rules 返回的 rules_version。")


@agent_tool(args_model=FigureKitArguments)
def figure_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """创建和维护用于解释技术方案的可编辑 draw.io 结构示意图；首次绘图先调用 rules 获取详细规则。

    Returns:
        rules 返回 rules_version 和完整绘图规则；list/read 返回附图引用或 draw.io XML；create/write/edit 返回精简元数据，并在图片可用时返回 render_image attachment；check 返回引用一致性问题。

    Rules:
        - create/write/edit 前必须先调用 rules，并携带其 rules_version；详细绘图规则只在 rules 结果中返回。
        - write/edit 前还必须 read，并携带最新 drawio_updated_at；edit 的每个 old_text 必须唯一匹配。
        - 正文使用工具返回的 markdown_ref；figure block 只用于附录展示图本体。

    Examples:
        - 获取规则: {"action":"rules"}
        - 列出附图: {"action":"list"}
        - 读取附图 draw.io XML: {"action":"read","ref":"figure:fig_000001"}
        - 局部替换: {"action":"edit","ref":"figure:fig_000001","expected_drawio_updated_at":"...","rules_version":"figure-kit-drawio-v2","edits":[{"old_text":"旧名称","new_text":"新名称"}]}
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
                "required_for": ["create", "write", "edit"],
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
        if arguments.get("edits") is not None:
            return tool_failed("invalid_tool_arguments", "figure_kit.create 不接受 edits；新建图请提交完整 drawio_xml。")
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

    if action == "write":
        rules_result = _require_current_rules(arguments)
        if rules_result is not None:
            return rules_result
        if arguments.get("edits") is not None:
            return tool_failed("invalid_tool_arguments", "figure_kit.write 不接受 edits；请提交完整 drawio_xml。")
        drawio_xml = arguments.get("drawio_xml")
        if drawio_xml is None:
            return tool_failed("drawio_xml_required", "figure_kit.write 需要非空 drawio_xml。")
        title_value = arguments.get("title")
        write_title = str(title_value).strip() if title_value is not None else None
        result = store.write_figure(
            project_id,
            figure_id,
            title=write_title,
            drawio_xml=drawio_xml,
            expected_drawio_updated_at=arguments.get("expected_drawio_updated_at"),
        )
        if result.get("status") == "failed":
            return result
        return _figure_change_success(store, project_id, result["output"]["figure"])

    if action == "edit":
        rules_result = _require_current_rules(arguments)
        if rules_result is not None:
            return rules_result
        if arguments.get("drawio_xml") is not None:
            return tool_failed("invalid_tool_arguments", "figure_kit.edit 不接受 drawio_xml；请使用 edits 提交唯一精确替换。")
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            return tool_failed("figure_edits_required", "figure_kit.edit 至少需要一个替换项。")
        title_value = arguments.get("title")
        edit_title = str(title_value).strip() if title_value is not None else None
        result = store.edit_figure(
            project_id,
            figure_id,
            title=edit_title,
            edits=edits,
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
        "create/write/edit 前必须先调用 figure_kit.rules，并把返回的 rules_version 原样传入。",
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
            "形状语义和整体可读性；只修复影响理解或使用的客观问题，局部修改用 edit，整体修改用 write。"
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
