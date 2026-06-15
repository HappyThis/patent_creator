from __future__ import annotations

import inspect

import pytest

from app.agents.prompts.main_agent import build_main_agent_system_prompt
from app.agents.workers.main_agent import MAIN_AGENT_TOOLS
from app.tools import DOCUMENT_WRITE_TOOL_NAMES, MAIN_AGENT_TOOL_NAMES, render_tool_manual


def test_main_agent_registers_disclosure_read_tools() -> None:
    tool_names = {tool["function"]["name"] for tool in MAIN_AGENT_TOOLS}

    assert {"disclosure_outline", "disclosure_search", "disclosure_read_section"} <= tool_names
    assert "document_read" not in tool_names

    search = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "disclosure_search")
    search_properties = search["function"]["parameters"]["properties"]
    assert set(search_properties) == {"query", "regex", "limit", "offset"}
    assert "case_sensitive" not in search_properties


def test_exec_command_metadata_comes_from_tool_docstring() -> None:
    tool = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "exec_command")

    description = tool["function"]["description"]
    assert description == "在项目工作区内执行命令字符串，cwd 为当前 project 工作区。"
    assert tool["function"]["parameters"]["properties"]["command"]["description"] == "要执行的命令字符串，按当前项目工作区作为 cwd 执行。"

    prompt = build_main_agent_system_prompt()
    assert "命令超时时返回 command_timeout" in prompt
    assert '执行诊断命令：{"command":"git status --short","timeout":30}' in prompt


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


def test_tool_schemas_inline_local_definitions_for_provider_compatibility() -> None:
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
    tool_manual = render_tool_manual(MAIN_AGENT_TOOL_NAMES)

    assert "自动生成工具声明" in prompt
    assert tool_manual in prompt
    for tool_name in DOCUMENT_WRITE_TOOL_NAMES:
        assert tool_name in tool_manual
    assert "document_edit" not in MAIN_AGENT_TOOL_NAMES
    assert "document_edit" not in tool_manual
    assert DOCUMENT_WRITE_TOOL_NAMES == ("disclosure_edit",)
    assert "operations" not in tool_manual
    assert "单次新增/替换文本总量不得超过 1500 字" in tool_manual
    assert "没有整章重写" in tool_manual
    assert "insert_section 只创建子章节标题" in tool_manual


def test_removed_subagent_tools_are_not_registered() -> None:
    tool_names = [tool["function"]["name"] for tool in MAIN_AGENT_TOOLS]

    assert "execute_subagent" not in tool_names
    assert "write_pipe" not in tool_names
    assert "finish" not in tool_names
    with pytest.raises(KeyError):
        render_tool_manual(("execute_subagent",))


def test_agent_prompt_sources_do_not_hardcode_removed_subagent_protocol() -> None:
    source = inspect.getsource(build_main_agent_system_prompt)

    for phrase in ("execute_subagent", "write_pipe", "finish({})", "子 agent"):
        assert phrase not in source
