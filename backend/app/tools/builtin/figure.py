from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.disclosure import section_title_text
from ...domain.figures import (
    DRAWIO_XML_MAX_CHARS,
    FIGURE_LINK_PATTERN,
    MODEL_REVIEW_IMAGE_MAX_BYTES,
    drawio_updated_at,
    figure_attachment,
    figure_ref,
    figure_summary,
    parse_figure_ref,
)
from ...domain.document_tool_results import tool_failed
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool

APPENDIX_TITLE = "附录"
MAX_FIGURE_UPDATES = 20
MAX_FIGURE_ATTEMPTS_PER_ROUND = 8


class FigureUpdateOperation(BaseModel):
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

    action: Literal["write", "update", "read", "list", "check", "delete"] = Field(
        description=(
            "write 新建或整体替换；update 局部精确替换；read 读取 XML 和已有截图；"
            "list 列出附图；check 检查引用与资源；delete 删除未被使用的附图。"
        )
    )
    ref: str | None = Field(default=None, description="write/update/read/delete 使用，格式为 figure:fig_000001；write 新建时省略。")
    title: str | None = Field(default=None, max_length=120, description="write/update 可选；write 新建时必填。")
    reason: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "write/update 必填。write 说明图的目的、图型或分区、主阅读方向、关键关系和期望结果；"
            "update 说明具体问题、影响、修改方式和预期效果。"
            "不设最低字数，但不能只写‘优化布局’等泛化原因。"
        ),
    )
    drawio_xml: str | None = Field(
        default=None,
        description=(
            "write 使用。只接受工具说明示例所示的 mxfile > 单个 diagram > 未压缩 mxGraphModel 完整 XML；"
            "安全的缺失属性会自动补齐，结构、画布、节点和连线错误不会自动猜测或修复。"
        ),
    )
    edits: list[FigureUpdateOperation] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_FIGURE_UPDATES,
        description="update 使用；按顺序执行精确替换。每个 old_text 必须在当前 XML 中恰好出现一次，任一失败则全部不落盘。",
    )


@agent_tool(args_model=FigureKitArguments)
def figure_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    *,
    context: Any | None = None,
) -> dict[str, Any]:
    """创建和维护用于解释技术方案的可编辑 draw.io 工程技术示意图；工具不自动排版或套用模板。

    write只接受以下唯一结构，示例含视觉语法：
    <mxfile host="app.diagrams.net">
      <diagram id="page-1" name="Page-1">
        <mxGraphModel grid="1" gridSize="10" guides="1" connect="1" arrows="1" page="1" pageScale="1" pageWidth="1500" pageHeight="900" math="0" shadow="0">
          <root>
            <mxCell id="0"/><mxCell id="1" parent="0"/>
            <mxCell id="title" value="技术处理与状态更新示意图" style="text;visualRole=note;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontFamily=Helvetica;fontSize=18;fontStyle=1;fontColor=#111111;" vertex="1" parent="1"><mxGeometry x="70" y="45" width="760" height="36" as="geometry"/></mxCell>
            <mxCell id="panel" value="核心处理链" style="visualRole=panel;rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor=#f7f7f7;strokeColor=#777777;strokeWidth=1;fontFamily=Helvetica;fontSize=15;fontStyle=1;fontColor=#111111;align=left;verticalAlign=top;spacingTop=12;spacingLeft=14;" vertex="1" parent="1"><mxGeometry x="70" y="115" width="1120" height="360" as="geometry"/></mxCell>
            <mxCell id="input" value="采集请求" style="visualRole=normal;rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#222222;strokeWidth=1.4;fontFamily=Helvetica;fontSize=14;fontColor=#111111;align=center;verticalAlign=middle;spacing=10;" vertex="1" parent="panel"><mxGeometry x="60" y="135" width="190" height="76" as="geometry"/></mxCell>
            <mxCell id="core" value="核心处理&#xa;校验并更新状态" style="visualRole=primary;rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#e9e9e9;strokeColor=#222222;strokeWidth=1.4;fontFamily=Helvetica;fontSize=14;fontStyle=1;fontColor=#111111;align=center;verticalAlign=middle;spacing=10;" vertex="1" parent="panel"><mxGeometry x="450" y="123" width="220" height="100" as="geometry"/></mxCell>
            <mxCell id="output" value="输出结果" style="visualRole=normal;rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#222222;strokeWidth=1.4;fontFamily=Helvetica;fontSize=14;fontColor=#111111;align=center;verticalAlign=middle;spacing=10;" vertex="1" parent="panel"><mxGeometry x="860" y="135" width="190" height="76" as="geometry"/></mxCell>
            <mxCell id="e1" value="受控输入" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#222222;strokeWidth=1.4;endArrow=block;endFill=1;fontFamily=Helvetica;fontSize=12;fontColor=#333333;labelBackgroundColor=#ffffff;exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="panel" source="input" target="core"><mxGeometry relative="1" as="geometry"/></mxCell>
            <mxCell id="e2" value="处理结果" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#222222;strokeWidth=1.4;endArrow=block;endFill=1;fontFamily=Helvetica;fontSize=12;fontColor=#333333;labelBackgroundColor=#ffffff;exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="panel" source="core" target="output"><mxGeometry relative="1" as="geometry"/></mxCell>
          </root>
        </mxGraphModel>
      </diagram>
    </mxfile>

    Returns:
        write/update 返回引用、进度、补齐项、警告和截图；失败返回全部硬错误并保留稳定图。read 返回 XML 和已有截图。

    Rules:
        - 新建用 write 且省略 ref；整体替换用 write 且提供 ref；局部修正用 update。修改现有图前须已通过 read 或本轮成功的 write/update 取得当前版本，版本信息由系统管理。
        - write/update 提供具体 reason。write 提交完整 XML；update 的 old_text 从当前 XML 原样复制且唯一出现。
        - write 前先决定核心命题、分区、阅读方向和关系语法，不要把正文逐框翻译成通用流程图。按技术内容选择层级、主链、决策、状态、时间轴或内部数据结构；多个机制可分区组合。
        - visualRole 像 CSS 语义类，可选 panel、primary、normal、decision、state、data、note；它提供默认样式，显式 style 始终优先。默认视觉系统：1500x900 画布四周通常保留 60px 安全边距；标题 18px、分区标题 15–16px、节点 13–14px、边标签 11–12px，统一 Helvetica；普通节点白色，分组 #f7f7f7，唯一重点 #e9e9e9；容器边框约 1px，节点和主线约 1.4px，padding 约 10px。
        - 美观先看构图：每区一个主阅读方向；同类节点尺寸、对齐、间距和圆角一致；用位置、留白、灰度和线宽区分主次，不靠阴影、渐变或装饰。避免挤满或偏在一角；节点文字通常不超过三行。
        - 先放边界、分组、层级、主干和共享走线，再放节点。普通连线优先直线或不超过两个转折的正交线，标签放在清晰直线段并用白底；不得穿越无关节点或文字，尽量不交叉、贴边或长绕行。多对多关系使用总线、汇聚点、中间层或集合，避免 N×M 线网。可增量生长，但不是固定工作流。
        - 硬错误包括非法结构、重复/缺失 ID、无效 parent、画布越界、悬空箭头、无效 source/target、离开真实形状边界的锚点、其他非法锚点和小于 4px 的显式线段；会阻断渲染并一次返回全部错误。
        - 缺失且安全的字体、线宽、灰度、padding 和正交走线默认值会自动补齐并列明；非正交进出、4–12px 短线、共线或交叉、穿越无关节点、带标签外绕线靠近页面边界、曲线、过多拐点及不一致样式只作为 warnings，仍返回截图供你判断。带箭头的 edge 即使标记 edgeRole=auxiliary 也必须连接节点；只有显式 edgeRole=auxiliary;endArrow=none; 的无箭头装饰线可以悬空。
        - 每张图在一次用户请求中最多尝试 8 次 write/update，预检和渲染失败也计数；read/check/list 不计数。失败不覆盖最近成功版本，连续失败或次数将尽时优先使用稳定图继续完成正文。
        - write/update 成功后返回新截图；read 复用已有截图，不重新渲染。
        - check 只检查正文引用、附录展示和附图资源是否一致；check 通过不代表附图完整覆盖最终正文的技术关系。
        - 正文使用工具返回的 markdown_ref；figure block 只用于附录展示图本体。
    """
    parsed = _validate_figure_arguments(arguments)
    if parsed["status"] == "failed":
        return parsed
    arguments = parsed["output"]["arguments"]
    action = str(arguments.get("action") or "")
    if action == "list":
        return {
            "status": "success",
            "output": {"figures": [_figure_reference_summary(figure) for figure in store.list_figures(project_id)]},
        }
    if action == "check":
        return {"status": "success", "output": check_figures(store, project_id)}

    if action == "write":
        if arguments.get("edits") is not None:
            return tool_failed("invalid_tool_arguments", "figure_kit.write 不接受 edits；请提交完整 drawio_xml。")
        reason_result = _require_reason(arguments, action="write")
        if reason_result is not None:
            return reason_result
        drawio_xml = arguments.get("drawio_xml")
        if drawio_xml is None:
            return tool_failed("drawio_xml_required", "figure_kit.write 需要非空 drawio_xml。")

        raw_ref = arguments.get("ref")
        stable_available = False
        if raw_ref is None:
            title = str(arguments.get("title") or "").strip()
            if not title:
                return tool_failed("figure_title_required", "figure_kit.write 新建附图时需要非空 title。")
            figure_id = store.next_figure_id(project_id)
            limit_result = _require_figure_attempt_available(context, figure_id, stable_available=False)
            if limit_result is not None:
                return limit_result
            _start_figure_attempt(context, figure_id)
            result = store.create_figure(project_id, title=title, drawio_xml=drawio_xml)
        else:
            figure_id = _figure_id_from_arguments(arguments)
            if figure_id is None:
                return tool_failed("figure_ref_required", "ref 格式必须为 figure:fig_000001。")
            stable_available = store.get_figure(project_id, figure_id) is not None
            limit_result = _require_figure_attempt_available(
                context,
                figure_id,
                stable_available=stable_available,
            )
            if limit_result is not None:
                return limit_result
            _start_figure_attempt(context, figure_id)
            version_result = _expected_drawio_version(figure_id, context)
            if version_result.get("status") == "failed":
                return _figure_change_failed(
                    version_result,
                    context=context,
                    figure_id=figure_id,
                    stable_available=stable_available,
                )
            title_value = arguments.get("title")
            write_title = str(title_value).strip() if title_value is not None else None
            result = store.write_figure(
                project_id,
                figure_id,
                title=write_title,
                drawio_xml=drawio_xml,
                expected_drawio_updated_at=version_result["output"]["drawio_updated_at"],
            )
        if result.get("status") == "failed":
            return _figure_change_failed(
                result,
                context=context,
                figure_id=figure_id,
                stable_available=stable_available,
            )
        figure = result["output"]["figure"]
        actual_figure_id = str(figure.get("figure_id") or figure_id)
        if actual_figure_id != figure_id:
            _move_figure_review_state(context, figure_id, actual_figure_id)
            figure_id = actual_figure_id
        _remember_figure_version(context, figure)
        return _figure_change_success(
            store,
            project_id,
            figure,
            context=context,
            validation=result["output"].get("validation"),
        )

    figure_id = _figure_id_from_arguments(arguments)
    if figure_id is None:
        return tool_failed("figure_ref_required", "该操作需要 ref，格式为 figure:fig_000001。")

    if action == "read":
        snapshot = store.get_figure_with_drawio(project_id, figure_id)
        if snapshot is None:
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        figure, drawio_xml = snapshot
        _remember_figure_version(context, figure)
        payload = _figure_reference_summary(figure)
        payload["drawio_xml"] = drawio_xml
        attachments = _existing_figure_attachments(store, project_id, figure)
        if attachments:
            message = (
                "已返回当前完整 XML 和已有截图；本次 read 未重新渲染，也不占渲染次数。"
                "若正文在上次看图后发生实质变化，请以最终正文重新核对技术关系覆盖，再决定是否 update。"
            )
        else:
            message = (
                "已返回当前完整 XML，但现有截图缺失、为空或超过模型附件大小限制，未附加视觉复盘图片；"
                "本次 read 未重新渲染，也不占渲染次数。不要声称已经查看截图。"
            )
        output: dict[str, Any] = {
            "figure": payload,
            "message": message,
            "attachments": attachments,
            "review": _figure_review_progress(context, figure_id, stable_available=True),
        }
        return {"status": "success", "output": output}

    if action == "update":
        if arguments.get("drawio_xml") is not None:
            return tool_failed("invalid_tool_arguments", "figure_kit.update 不接受 drawio_xml；请使用 edits 提交唯一精确替换。")
        reason_result = _require_reason(arguments, action="update")
        if reason_result is not None:
            return reason_result
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            return tool_failed("figure_updates_required", "figure_kit.update 至少需要一个替换项。")
        stable_available = store.get_figure(project_id, figure_id) is not None
        limit_result = _require_figure_attempt_available(
            context,
            figure_id,
            stable_available=stable_available,
        )
        if limit_result is not None:
            return limit_result
        _start_figure_attempt(context, figure_id)
        version_result = _expected_drawio_version(figure_id, context)
        if version_result.get("status") == "failed":
            return _figure_change_failed(
                version_result,
                context=context,
                figure_id=figure_id,
                stable_available=stable_available,
            )
        title_value = arguments.get("title")
        update_title = str(title_value).strip() if title_value is not None else None
        result = store.update_figure(
            project_id,
            figure_id,
            title=update_title,
            edits=edits,
            expected_drawio_updated_at=version_result["output"]["drawio_updated_at"],
        )
        if result.get("status") == "failed":
            return _figure_change_failed(
                result,
                context=context,
                figure_id=figure_id,
                stable_available=stable_available,
            )
        figure = result["output"]["figure"]
        _remember_figure_version(context, figure)
        return _figure_change_success(
            store,
            project_id,
            figure,
            context=context,
            validation=result["output"].get("validation"),
        )

    if action == "delete":
        usages = _figure_usages(store.get_disclosure(project_id), figure_id)
        if usages:
            return tool_failed("figure_in_use", f"figure 仍被交底书引用或展示：{figure_id}")
        if not store.delete_figure(project_id, figure_id):
            return tool_failed("figure_not_found", f"figure 不存在：{figure_id}")
        _forget_figure_context(context, figure_id)
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


def _require_reason(arguments: dict[str, Any], *, action: str) -> dict[str, Any] | None:
    if str(arguments.get("reason") or "").strip():
        return None
    return tool_failed("figure_reason_required", f"figure_kit.{action} 需要非空 reason。")


def _expected_drawio_version(
    figure_id: str,
    context: Any | None,
) -> dict[str, Any]:
    versions = getattr(context, "figure_drawio_versions", None) if context is not None else None
    expected = versions.get(figure_id) if isinstance(versions, dict) else None
    if not isinstance(expected, str) or not expected:
        return tool_failed(
            "drawio_read_required",
            "尚未取得当前附图版本；请先调用 figure_kit.read。本轮成功的 write/update 会自动记录后续修改所需版本。",
        )
    return {"status": "success", "output": {"drawio_updated_at": expected}}


def _remember_figure_version(context: Any | None, figure: dict[str, Any]) -> None:
    versions = getattr(context, "figure_drawio_versions", None) if context is not None else None
    if not isinstance(versions, dict):
        return
    figure_id = str(figure.get("figure_id") or "")
    updated_at = drawio_updated_at(figure)
    if figure_id and updated_at:
        versions[figure_id] = updated_at


def _forget_figure_context(context: Any | None, figure_id: str) -> None:
    versions = getattr(context, "figure_drawio_versions", None) if context is not None else None
    if isinstance(versions, dict):
        versions.pop(figure_id, None)
    states = getattr(context, "figure_review_states", None) if context is not None else None
    if isinstance(states, dict):
        states.pop(figure_id, None)


def _figure_review_state(context: Any | None, figure_id: str) -> dict[str, int] | None:
    states = getattr(context, "figure_review_states", None) if context is not None else None
    if not isinstance(states, dict):
        return None
    state = states.setdefault(
        figure_id,
        {"attempts": 0, "successful_renders": 0, "consecutive_failures": 0},
    )
    return state


def _figure_review_progress(
    context: Any | None,
    figure_id: str,
    *,
    stable_available: bool,
) -> dict[str, int | bool]:
    state = _figure_review_state(context, figure_id) or {}
    attempts = int(state.get("attempts", 0))
    return {
        "attempt": attempts,
        "limit": MAX_FIGURE_ATTEMPTS_PER_ROUND,
        "remaining": max(0, MAX_FIGURE_ATTEMPTS_PER_ROUND - attempts),
        "successful_renders": int(state.get("successful_renders", 0)),
        "consecutive_failures": int(state.get("consecutive_failures", 0)),
        "stable_version_available": stable_available,
    }


def _require_figure_attempt_available(
    context: Any | None,
    figure_id: str,
    *,
    stable_available: bool,
) -> dict[str, Any] | None:
    review = _figure_review_progress(context, figure_id, stable_available=stable_available)
    if int(review["attempt"]) < MAX_FIGURE_ATTEMPTS_PER_ROUND:
        return None
    return tool_failed(
        "figure_attempt_limit_reached",
        f"本次用户请求中 {figure_ref(figure_id)} 已用完 {MAX_FIGURE_ATTEMPTS_PER_ROUND} 次 write/update 尝试，不能继续修改。",
        review=review,
        stable_version_preserved=stable_available,
        recommendation=(
            "最近成功版本仍可使用，请停止修改并继续完成正文；如仍需调整，请在下一次用户请求中继续。"
            if stable_available
            else "当前没有成功版本；请先继续完成正文，并在下一次用户请求中重新绘图。"
        ),
    )


def _start_figure_attempt(context: Any | None, figure_id: str) -> None:
    state = _figure_review_state(context, figure_id)
    if state is not None:
        state["attempts"] = int(state.get("attempts", 0)) + 1


def _record_successful_render(context: Any | None, figure_id: str) -> dict[str, int | bool]:
    state = _figure_review_state(context, figure_id)
    if state is None:
        return {
            "attempt": 1,
            "limit": MAX_FIGURE_ATTEMPTS_PER_ROUND,
            "remaining": MAX_FIGURE_ATTEMPTS_PER_ROUND - 1,
            "successful_renders": 1,
            "consecutive_failures": 0,
            "stable_version_available": True,
        }
    state["successful_renders"] = int(state.get("successful_renders", 0)) + 1
    state["consecutive_failures"] = 0
    return _figure_review_progress(context, figure_id, stable_available=True)


def _record_failed_attempt(
    context: Any | None,
    figure_id: str,
    *,
    stable_available: bool,
) -> dict[str, int | bool]:
    state = _figure_review_state(context, figure_id)
    if state is None:
        return {
            "attempt": 1,
            "limit": MAX_FIGURE_ATTEMPTS_PER_ROUND,
            "remaining": MAX_FIGURE_ATTEMPTS_PER_ROUND - 1,
            "successful_renders": 0,
            "consecutive_failures": 1,
            "stable_version_available": stable_available,
        }
    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    return _figure_review_progress(context, figure_id, stable_available=stable_available)


def _move_figure_review_state(context: Any | None, old_figure_id: str, new_figure_id: str) -> None:
    states = getattr(context, "figure_review_states", None) if context is not None else None
    if isinstance(states, dict) and old_figure_id in states:
        states[new_figure_id] = states.pop(old_figure_id)


def _figure_change_failed(
    result: dict[str, Any],
    *,
    context: Any | None,
    figure_id: str,
    stable_available: bool,
) -> dict[str, Any]:
    output = dict(result.get("output") or {})
    validation = output.get("validation")
    if isinstance(validation, dict):
        output.setdefault("warnings", list(validation.get("warnings") or []))
        output.setdefault("normalized", bool(validation.get("normalized")))
        output.setdefault("normalized_fields", list(validation.get("normalized_fields") or []))
    review = _record_failed_attempt(context, figure_id, stable_available=stable_available)
    output["review"] = review
    output["stable_version_preserved"] = stable_available
    should_stop = int(review["consecutive_failures"]) >= 2 or int(review["remaining"]) <= 1
    if stable_available:
        output["recommendation"] = (
            "最近成功版本仍可使用。已连续失败或次数将尽，除非存在关键语义错误，否则停止修改并继续完成正文。"
            if should_stop
            else "本次修改未生效，最近成功版本未被覆盖。请根据一次返回的全部 errors 合并修正。"
        )
    else:
        output["recommendation"] = (
            "当前仍无成功版本。请一次修复全部 errors；若次数将尽，停止重试并继续完成正文。"
        )
    return {**result, "output": output}


def _figure_reference_summary(figure: dict[str, Any]) -> dict[str, Any]:
    summary = figure_summary(figure)
    return {key: summary[key] for key in ("figure_id", "ref", "label", "title", "markdown_ref")}


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

    for figure_id in figures_by_id:
        source_file = store.figure_drawio_file(project_id, figure_id)
        render_file = store.figure_render_file(project_id, figure_id)
        try:
            source_size = source_file.stat().st_size
        except OSError:
            source_size = 0
        try:
            render_size = render_file.stat().st_size
        except OSError:
            render_size = 0
        if source_size <= 0:
            errors.append(_issue("figure_source_missing", "", None, figure_id, f"draw.io 源文件缺失或为空：{figure_id}"))
        if render_size <= 0:
            errors.append(_issue("figure_render_missing", "", None, figure_id, f"渲染图片缺失或为空：{figure_id}"))

    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "scope_notice": (
            "本检查只验证正文引用、附录展示和附图资源的一致性，不判断附图是否完整、正确地覆盖最终正文的技术关系；"
            "通过后仍需根据最终正文进行语义复核。"
        ),
    }


def _existing_figure_attachments(
    store: WorkspaceStore,
    project_id: str,
    figure: dict[str, Any],
) -> list[dict[str, str]]:
    figure_id = str(figure.get("figure_id") or "")
    render_file = store.figure_render_file(project_id, figure_id)
    try:
        render_size = render_file.stat().st_size
    except OSError:
        return []
    if not 0 < render_size <= MODEL_REVIEW_IMAGE_MAX_BYTES:
        return []
    return [figure_attachment(figure)]


def _figure_id_from_arguments(arguments: dict[str, Any]) -> str | None:
    ref = arguments.get("ref")
    if not isinstance(ref, str):
        return None
    return parse_figure_ref(ref)


def _figure_change_success(
    store: WorkspaceStore,
    project_id: str,
    figure: dict[str, Any],
    *,
    context: Any | None,
    validation: Any,
) -> dict[str, Any]:
    payload = _figure_reference_summary(figure)
    figure_id = str(figure.get("figure_id") or "")
    review = _record_successful_render(context, figure_id)
    validation_payload = validation if isinstance(validation, dict) else {}
    warnings = list(validation_payload.get("warnings") or [])
    normalized_fields = list(validation_payload.get("normalized_fields") or [])
    render_file = store.figure_render_file(project_id, figure_id)
    try:
        render_size = render_file.stat().st_size
    except OSError:
        render_size = 0
    attachment_available = 0 < render_size <= MODEL_REVIEW_IMAGE_MAX_BYTES
    if attachment_available:
        message = (
            f"图片已生成并附加当前截图。硬检查已通过，另有 {len(warnings)} 条非阻断质量警告；"
            "自动检查不覆盖技术语义和最终视觉质量，请结合 warnings 和截图判断是否需要修改。"
        )
        attachments = [figure_attachment(figure)]
    else:
        reason = "render.png 缺失或为空" if render_size <= 0 else f"render.png 超过 {MODEL_REVIEW_IMAGE_MAX_BYTES} 字节"
        message = f"图片已生成，但视觉复盘附件未附加：{reason}。不要声称已经看过截图。"
        attachments = []
    output: dict[str, Any] = {
        "figure": payload,
        "message": message,
        "attachments": attachments,
        "warnings": warnings,
        "normalization": {
            "applied": bool(validation_payload.get("normalized")),
            "fields": normalized_fields,
        },
        "review": review,
    }
    return {"status": "success", "output": output}


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
