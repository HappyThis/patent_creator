from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.agents.prompts.main_agent import build_main_agent_system_prompt
from app.agents.workers.main_agent import MAIN_AGENT_TOOLS
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
    assert "figure block 只用于在“附录”章节展示图本体" in description
    assert "固定 1500x900 画布" in description
    assert "黑白风格" in description
    assert "架构图应体现分层、系统边界、模块职责和依赖方向" in description
    assert 'id="diagram"' in properties["html"]["description"]
    assert set(properties) == {"action", "ref", "title", "html"}


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
    assert "disclosure_edit 小步落盘" in prompt
    assert "document_edit" not in MAIN_AGENT_TOOL_NAMES
    assert DOCUMENT_WRITE_TOOL_NAMES == ("disclosure_edit",)
    assert "operations" not in edit_description
    assert "单次新增/替换文本总量不得超过 1500 字。" in edit_description
    assert "没有整章重写；重写章节必须拆成删除、插入 section、逐个 insert/replace block。" in edit_description
    assert "创新内核" not in edit_description
    assert "innovation_kernel_required" not in edit_description
    assert "innovation_kernel_read_required" not in edit_description
    assert "只能写最终态正文" in edit_description
    assert "section 负责结构，block 承接内容" in edit_description
    assert "不要跨 section 操作" in edit_description
    assert "两个以上独立机制、流程阶段、模块、实施例、规则组或异常分支" in edit_description
    assert "insert_section 只创建子章节标题" in edit_description
    assert "$...$ 行内 LaTeX" in edit_description
    assert "不要裸写 D_i、Active_i" in edit_description
    assert "独立公式使用 type=formula 的块级 LaTeX" in edit_description
    assert "[式(1)](formula:<formula_block_id>)" in edit_description
    assert "$...$ 行内 LaTeX" in block_schema["properties"]["text"]["description"]
    assert "[式(1)](formula:blk_000001)" in block_schema["properties"]["type"]["description"]
    assert "自动编号为式(1)、式(2)" in block_schema["properties"]["latex"]["description"]
    assert "单元格支持 $...$ 行内 LaTeX" in block_schema["properties"]["rows"]["description"]


def test_tool_descriptions_carry_lookup_workflow_guidance_without_kernel_tool() -> None:
    outline_description = _tool("disclosure_outline")["function"]["description"]
    search_description = _tool("disclosure_search")["function"]["description"]
    read_description = _tool("disclosure_read_section")["function"]["description"]
    prompt = build_main_agent_system_prompt()
    tool_names = {tool["function"]["name"] for tool in MAIN_AGENT_TOOLS}

    assert "当需要了解交底书结构、寻找可编辑位置或判断章节层级时，先用本工具定位。" in outline_description
    assert "当不知道概念、术语或目标文本在哪个章节时，先用本工具定位。" in search_description
    assert "当写作、评价或修改依赖当前正文时，应先精读相关 section 或目标 block。" in read_description
    assert "writing_guide_markdown" in read_description
    assert "空章节写作要领，不是交底书正文" in read_description
    assert "innovation_kernel_kit" not in tool_names

    assert "先用 disclosure_outline 或 disclosure_search 定位" in prompt
    assert "再用 disclosure_read_section 精读目标 section 或 block" in prompt
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
