from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.agents.prompts.main_agent import build_main_agent_system_prompt
from app.agents.workers.main_agent import MAIN_AGENT_TOOLS
from app.domain.figures import validate_drawio_xml
from app.tools import DOCUMENT_WRITE_TOOL_NAMES, MAIN_AGENT_TOOL_NAMES, get_tool_declaration


def _tool(name: str) -> dict[str, Any]:
    return next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == name)


def test_main_agent_registers_disclosure_read_tools() -> None:
    tool_names = {tool["function"]["name"] for tool in MAIN_AGENT_TOOLS}

    assert {"disclosure_outline", "disclosure_search", "disclosure_read_section"} <= tool_names
    assert "document_read" not in tool_names

    search = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "disclosure_search")
    search_properties = search["function"]["parameters"]["properties"]
    assert set(search_properties) == {"query", "regex", "limit", "offset"}
    assert "case_sensitive" not in search_properties


def test_file_scan_budgets_are_internal_not_agent_parameters() -> None:
    for name in ("file_glob", "file_search"):
        properties = _tool(name)["function"]["parameters"]["properties"]
        assert "max_scanned_paths" not in properties
        assert "max_elapsed_ms" not in properties
        description = _tool(name)["function"]["description"]
        assert "stop_reason" in description
        assert "缩小" in description


def test_exec_command_metadata_comes_from_tool_docstring() -> None:
    tool = _tool("exec_command")

    description = tool["function"]["description"]
    assert "用途：在项目工作区内执行命令字符串，cwd 为当前 project 工作区。" in description
    assert "返回：返回 exit_code、stdout 和 stderr" in description
    assert "规则：" in description
    assert "命令超时时返回 command_timeout。" in description
    assert tool["function"]["parameters"]["properties"]["command"]["description"] == "要执行的命令字符串，按当前项目工作区作为 cwd 执行。"

    prompt = build_main_agent_system_prompt()
    assert "命令超时时返回 command_timeout" not in prompt
    assert '执行诊断命令：{"command":"git status --short","timeout":30}' not in prompt


def test_disclosure_edit_protocol_is_single_shape() -> None:
    tool_names = [tool["function"]["name"] for tool in MAIN_AGENT_TOOLS]
    assert "document_edit" not in tool_names
    assert "document_append_child_section" not in tool_names
    assert "disclosure_edit" in tool_names

    edit = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "disclosure_edit")
    properties = edit["function"]["parameters"]["properties"]

    assert set(properties) == {"section_id", "operation", "block_id", "target_section_id", "position", "block", "section"}
    assert edit["function"]["parameters"]["required"] == ["section_id", "operation"]
    assert "operations" not in properties
    assert "op" not in properties
    block_schema = next(item for item in properties["block"]["anyOf"] if isinstance(item, dict) and item.get("type") == "object")
    block_properties = block_schema["properties"]
    assert "latex" in block_properties
    assert "figure_id" in block_properties


def test_main_agent_registers_figure_kit() -> None:
    tool_names = {tool["function"]["name"] for tool in MAIN_AGENT_TOOLS}

    assert "figure_kit" in tool_names
    figure = _tool("figure_kit")
    description = figure["function"]["description"]
    properties = figure["function"]["parameters"]["properties"]
    assert "markdown_ref" in description
    assert "figure block 只用于附录展示图本体" in description
    assert "版本信息由系统管理" in description
    assert "新建用 write 且省略 ref" in description
    assert "局部修正用 update" in description
    assert "已通过 read 或本轮成功的 write/update 取得当前版本" in description
    assert "最多尝试 8 次 write/update" in description
    assert "预检和渲染失败也计数" in description
    assert "工具不自动排版或套用模板" in description
    assert "唯一结构" in description
    assert '<mxfile host="app.diagrams.net">' in description
    assert 'pageWidth="1500" pageHeight="900"' in description
    assert "edgeRole=auxiliary" in description
    assert "带箭头的 edge 即使标记 edgeRole=auxiliary 也必须连接节点" in description
    assert "小于 4px 的显式线段" in description
    assert "离开真实形状边界的锚点" in description
    assert "4–12px 短线" in description
    assert "一次返回全部错误" in description
    assert "read 复用已有截图，不重新渲染" in description
    assert "失败不覆盖最近成功版本" in description
    assert "check 通过不代表附图完整覆盖最终正文的技术关系" in description
    assert "不要把正文逐框翻译成通用流程图" in description
    assert "多个机制可分区组合" in description
    assert "多对多关系使用总线、汇聚点、中间层或集合" in description
    assert "带标签外绕线靠近页面边界" in description
    assert "安全的字体、线宽、灰度、padding 和正交走线默认值会自动补齐" in description
    assert "reason" in properties
    assert "expected_drawio_updated_at" not in properties
    assert "rules_version" not in properties
    assert "edits" in properties
    assert "完整 XML" in description
    assert "不要用大外框包住整张主画面" not in description
    assert "虚线应少用" not in description
    assert "fontFamily=Helvetica" in description
    assert "strokeWidth=1.4" in description
    assert "labelBackgroundColor=#ffffff" in description
    assert "visualRole 像 CSS 语义类" in description
    assert "panel、primary、normal、decision、state、data、note" in description
    assert "显式 style 始终优先" in description
    assert "visualRole=primary" in description
    assert "1500x900 画布四周通常保留 60px 安全边距" in description
    assert "标题 18px、分区标题 15–16px、节点 13–14px、边标签 11–12px" in description
    assert "普通连线优先直线或不超过两个转折的正交线" in description
    assert "不得穿越无关节点或文字" in description
    assert len(description) < 4_500
    assert "mxfile > 单个 diagram > 未压缩 mxGraphModel" in properties["drawio_xml"]["description"]
    assert "安全的缺失属性会自动补齐" in properties["drawio_xml"]["description"]
    reason_schema = properties["reason"]
    reason_description = reason_schema["description"]
    assert "不设最低字数" in reason_description
    assert "图的目的、图型或分区、主阅读方向、关键关系和期望结果" in reason_description
    assert "具体问题、影响、修改方式和预期效果" in reason_description
    assert "不能只写‘优化布局’等泛化原因" in reason_description
    assert all("minLength" not in variant for variant in reason_schema["anyOf"])
    assert properties["action"]["enum"] == ["write", "update", "read", "list", "check", "delete"]
    assert "create" not in properties["action"]["enum"]
    assert "edit" not in properties["action"]["enum"]
    assert "rules" not in properties["action"]["enum"]
    assert "恰好出现一次" in properties["edits"]["description"]
    assert "read 读取 XML 和已有截图" in properties["action"]["description"]
    assert "delete 删除未被使用的附图" in properties["action"]["description"]
    assert "write/update 可选；write 新建时必填" in properties["title"]["description"]
    assert set(properties) == {"action", "ref", "title", "reason", "drawio_xml", "edits"}


def test_figure_tool_visual_example_is_valid_canonical_drawio_xml() -> None:
    description = _tool("figure_kit")["function"]["description"]
    start = description.index("<mxfile")
    end = description.index("</mxfile>", start) + len("</mxfile>")

    result = validate_drawio_xml(description[start:end])

    assert result["status"] == "success"
    normalized_xml = result["output"]["drawio_xml"]
    assert 'fontFamily=Helvetica' in normalized_xml
    assert 'strokeWidth=1.4' in normalized_xml
    assert 'labelBackgroundColor=#ffffff' in normalized_xml
    assert 'visualRole=primary' in normalized_xml
    assert "drawio_font_size_excessive" not in {item["code"] for item in result["output"]["warnings"]}


def test_tool_schemas_inline_local_definitions_for_responses_api() -> None:
    def walk(value: object) -> list[object]:
        if isinstance(value, dict):
            return [value, *(item for child in value.values() for item in walk(child))]
        if isinstance(value, list):
            return [item for child in value for item in walk(child)]
        return []

    for tool in MAIN_AGENT_TOOLS:
        schema = tool["function"]["parameters"]
        for node in walk(schema):
            assert not (isinstance(node, dict) and "$defs" in node)
            assert not (isinstance(node, dict) and "$ref" in node)


def test_main_agent_prompt_requires_small_document_edits() -> None:
    prompt = build_main_agent_system_prompt()
    edit = _tool("disclosure_edit")
    edit_description = edit["function"]["description"]
    properties = edit["function"]["parameters"]["properties"]
    block_schema = next(item for item in properties["block"]["anyOf"] if isinstance(item, dict) and item.get("type") == "object")

    assert "自动生成工具声明" not in prompt
    assert "disclosure_edit 小步写入" in prompt
    assert "document_edit" not in MAIN_AGENT_TOOL_NAMES
    assert DOCUMENT_WRITE_TOOL_NAMES == ("disclosure_edit",)
    assert "operations" not in edit_description
    assert "单次新增/替换文本总量不得超过 1500 字。" in edit_description
    assert "工具没有整章重写；大范围修改需拆成 section 和 block 操作。" in edit_description
    assert "创新内核" not in edit_description
    assert "innovation_kernel_required" not in edit_description
    assert "innovation_kernel_read_required" not in edit_description
    assert "只写最终态正文" in edit_description
    assert "section 负责结构，block 承接内容" in edit_description
    assert "不要跨 section 操作" in edit_description
    assert "insert_section 只创建子章节标题" in edit_description
    assert "$...$ 行内 LaTeX" in edit_description
    assert "独立公式使用 formula block" in edit_description
    assert "[式(1)](formula:<block_id>)" in edit_description
    assert "$...$ 行内 LaTeX" in block_schema["properties"]["text"]["description"]
    assert "[式(1)](formula:blk_000001)" in block_schema["properties"]["type"]["description"]
    assert "自动编号为式(1)、式(2)" in block_schema["properties"]["latex"]["description"]
    assert "单元格支持 $...$ 行内 LaTeX" in block_schema["properties"]["rows"]["description"]


def test_main_prompt_carries_lookup_workflow_while_tools_keep_local_contracts() -> None:
    outline_description = _tool("disclosure_outline")["function"]["description"]
    search_description = _tool("disclosure_search")["function"]["description"]
    read_description = _tool("disclosure_read_section")["function"]["description"]
    prompt = build_main_agent_system_prompt()
    tool_names = {tool["function"]["name"] for tool in MAIN_AGENT_TOOLS}

    assert "不要基于 preview 直接改写关键正文" in outline_description
    assert "不要基于搜索摘要直接改写关键正文" in search_description
    assert "writing_guide_markdown" in read_description
    assert "空章节写作要领，不是交底书正文" in read_description
    assert "innovation_kernel_kit" not in tool_names

    assert "disclosure_outline 或 disclosure_search 定位" in prompt
    assert "disclosure_read_section 精读目标 section 或 block" in prompt
    assert "innovation_kernel_kit" not in prompt


def test_removed_subagent_tools_are_not_registered() -> None:
    tool_names = [tool["function"]["name"] for tool in MAIN_AGENT_TOOLS]

    assert "execute_subagent" not in tool_names
    assert "write_pipe" not in tool_names
    assert "finish" not in tool_names
    with pytest.raises(KeyError):
        get_tool_declaration("execute_subagent")


def test_agent_prompt_sources_do_not_hardcode_removed_subagent_protocol() -> None:
    source = inspect.getsource(build_main_agent_system_prompt)

    for phrase in ("execute_subagent", "write_pipe", "finish({})", "子 agent"):
        assert phrase not in source
