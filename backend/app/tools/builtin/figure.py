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
        description="操作类型。create 新建 Mermaid 附图；read 读取源码；update 覆盖源码；delete 删除附图；list 列出可引用格式；check 检查正文引用和附录展示。"
    )
    ref: str | None = Field(default=None, description="read/update/delete 使用，格式为 figure:fig_000001。")
    title: str | None = Field(default=None, description="create/update 使用，附图标题，例如 系统结构示意图。")
    mermaid: str | None = Field(
        default=None,
        description=(
            "create/update 使用，完整 Mermaid 源码。交底书附图优先 flowchart TD/TB；"
            "flowchart LR 仅用于 3-6 个模块的横向结构关系，不能用于长流程链路；"
            "架构图必须体现分层、系统边界、模块职责和依赖方向，不要画成流程/状态回环；"
            "单图建议 6-10 个节点，节点文字使用短语，复杂图拆成多张。"
        ),
    )


@agent_tool(args_model=FigureKitArguments)
def figure_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """管理项目级附图资产，附图源码使用 Mermaid，预览端使用 Mermaid 官方渲染器展示为 SVG。

    Returns:
        list 返回 figures，每项包含 ref、markdown_ref、caption；read/create/update 返回 figure 完整 Mermaid 源码；check 返回 errors/warnings；失败返回 failed 和 code/message。

    Rules:
        - create 只用于用户明确要求新增一张图；用户说“重试/重新生成/替换/修改当前图/修改图1”时，必须 list 或 read 定位现有图，再用 update 覆盖原图，不能 create 新图。
        - 正文引用图时使用 list/create/read 返回的 markdown_ref，例如 [图1](figure:fig_000001)，不要手写或猜测图号。
        - figure block 只用于在“附录”章节展示图本体；非附录章节只能使用 Markdown 链接引用图。
        - 修改图前先 read，基于返回的 source.content 生成完整新版 mermaid，再 update。
        - create/update 不做自研 SVG 布局，必须提交完整 Mermaid 源码，由成熟 Mermaid 渲染器负责展示。
        - Mermaid 生成策略必须面向交底书版心：默认使用 flowchart TD/TB；flowchart LR 仅用于 3-6 个模块的横向结构关系，禁止用于长流程链路。
        - 先判断图型再写 Mermaid：流程图用于步骤、状态流转和判断分支；架构图用于模块协同、系统边界、层次关系和依赖方向；时序图用于跨主体调用顺序。不要把执行流程伪装成架构图。
        - 架构图应优先使用 subgraph 表达层次或边界，例如“外部系统 / 编排层 / 执行层 / 存储与观测”；连线表达依赖或数据流，避免回环、交叉长线和无层次的中心辐射图。
        - 架构图节点应是稳定模块或外部依赖，不应使用“轮询工单、候选过滤、二次校验、退避重试”这类步骤节点；这些应画成流程图或状态图。
        - 单张图建议 6-10 个节点；超过 10 个节点时，应抽象为高层节点或拆成“高层结构图 + 关键子流程图”。
        - 节点文字使用短语，建议不超过 10 个汉字；不要把完整调用链、日志流、工具调用明细、回环状态全部塞进一张图。
        - check 用于提交前或批量编辑后检查 figure 引用、图号文本和 figure block 位置是否一致。

    Examples:
        - 列出附图: {"action":"list"}
        - 创建附图: {"action":"create","title":"系统结构示意图","mermaid":"flowchart TD\\nA[任务接收] --> B[策略解析]"}
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
        mermaid = str(arguments.get("mermaid") or "").strip()
        if not title:
            return tool_failed("figure_title_required", "figure_kit.create 需要非空 title。")
        if not mermaid:
            return tool_failed("figure_mermaid_required", "figure_kit.create 需要非空 mermaid。")
        result = store.create_figure(project_id, title=title, mermaid=mermaid)
        if result.get("status") == "failed":
            return result
        figure = result["output"]["figure"]
        return {"status": "success", "output": {"figure": _full_figure_payload(figure), "warnings": result["output"].get("warnings", [])}}

    figure_id = _figure_id_from_arguments(arguments)
    if figure_id is None:
        return tool_failed("figure_ref_required", "该操作需要 ref，格式为 figure:fig_000001。")

    if action == "read":
        figure = store.get_figure(project_id, figure_id)
        if figure is None:
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        return {"status": "success", "output": {"figure": _full_figure_payload(figure)}}

    if action == "update":
        mermaid = str(arguments.get("mermaid") or "").strip()
        if not mermaid:
            return tool_failed("figure_mermaid_required", "figure_kit.update 需要非空 mermaid。")
        title_value = arguments.get("title")
        update_title = str(title_value).strip() if title_value is not None else None
        result = store.update_figure(project_id, figure_id, title=update_title, mermaid=mermaid)
        if result.get("status") == "failed":
            return result
        return {
            "status": "success",
            "output": {"figure": _full_figure_payload(result["output"]["figure"]), "warnings": result["output"].get("warnings", [])},
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


def _full_figure_payload(figure: dict[str, Any]) -> dict[str, Any]:
    payload = dict(figure)
    payload.update(figure_summary(figure))
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
