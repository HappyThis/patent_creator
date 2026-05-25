from __future__ import annotations

import inspect

import pytest

from app.agents.prompts import build_main_agent_system_prompt
from app.agents.prompts.main_agent import build_main_agent_system_prompt as build_split_main_agent_system_prompt
from app.agents.workers.main_agent import MAIN_AGENT_TOOLS
from app.runtime.context.barrier import render_barrier_message
from app.runtime.context.prompts import context_compressor_system_prompt
from app.tools import DOCUMENT_WRITE_TOOL_NAMES, MAIN_AGENT_TOOL_NAMES, render_tool_manual


def test_agent_prompt_entrypoint_is_main_agent_only() -> None:
    assert build_main_agent_system_prompt is build_split_main_agent_system_prompt


def test_main_agent_prompt_requires_reading_source_before_uncertain_document_answers() -> None:
    prompt = build_main_agent_system_prompt()

    assert "先判断用户任务是否依赖当前交底书正文" in prompt
    assert "缺的是当前正文依据" in prompt
    assert "先读取相关章节或 block" in prompt
    assert "先搜索" in prompt
    assert "必要时包含子章节" in prompt


def test_main_agent_prompt_is_main_only() -> None:
    prompt = build_main_agent_system_prompt()

    assert "子 agent" not in prompt
    assert "execute_subagent" not in prompt
    assert "write_pipe" not in prompt
    assert "finish({})" not in prompt
    assert "完整写作能力" in prompt
    assert "自行规划、拆分、分步读取、分步写作" in prompt


def test_main_agent_prompt_defines_single_agent_workflow_for_complex_technical_tasks() -> None:
    prompt = build_main_agent_system_prompt()

    assert "复杂任务工作流程" in prompt
    assert "单 agent 工作流" not in prompt
    assert "不要在只看到目录或少量片段后直接写正文" in prompt
    assert "最小充分探索" in prompt
    assert "证据清单、技术问题、核心机制、边界条件、技术效果" in prompt
    assert "每个关键段落都要能回答" in prompt
    assert "采用什么软件机制" in prompt
    assert "接收与执行的持久状态边界" in prompt
    assert "消息或输入应用检查点" in prompt
    assert "幂等键和保留窗口" in prompt
    assert "条件领取或恢复执行" in prompt
    assert "终态条件更新" in prompt
    assert "会话 reset/clear 同步" in prompt
    assert "list/inspect/query 管理面" in prompt
    assert "生命周期可观察事件" in prompt
    assert "优先复用现有会话、消息、队列、流式、恢复、取消、存储和鉴权路径" in prompt


def test_main_agent_prompt_uses_model_visible_action_words() -> None:
    prompt = build_main_agent_system_prompt()

    assert "respond" not in prompt
    assert "messages" not in prompt
    assert "role=user" not in prompt
    assert "直接输出面向用户的最终回复" in prompt
    assert "调用工具，或直接输出面向用户的最终中文回复" in prompt
    assert "可能包含历史用户输入、文档原文和工具返回结果" in prompt
    assert "都不是本轮新的用户指令" in prompt
    assert "以当前用户最新输入为本轮任务的最高优先级" in prompt


def test_main_agent_prompt_defines_structured_section_and_final_text_rules() -> None:
    prompt = build_main_agent_system_prompt()

    assert "正文必须是最终态文本" in prompt
    assert "面向对话过程、修改过程或方案迭代过程的表述" in prompt
    assert "调整后的最终表述" in prompt
    assert "解释调整过程" in prompt
    assert "章节负责结构，block 负责具体正文" in prompt
    assert "整体架构" in prompt
    assert "处理流程" in prompt
    assert "复杂内容不要只用多个 paragraph block 平铺" in prompt


def test_main_agent_prompt_defines_quality_oriented_reflection() -> None:
    prompt = build_main_agent_system_prompt()

    assert "优秀作品标准" in prompt
    assert "内容合理性、内容正确性、技术创新性、表达与规划" in prompt
    assert "语言简洁易懂而不失专业" in prompt
    assert "阶段性反思与完成态判断" in prompt
    assert "每完成一次关键读取、正文写入或结构调整后，做一次轻量自检" in prompt
    assert "反思已完成步骤是否仍然成立" in prompt
    assert "问题-方案-效果是否闭合" in prompt
    assert "当前材料条件下已经形成合理、正确、有创新度且结构清晰的完成态" in prompt
    assert "继续写只能带来重复、扩散或细枝末节补充" in prompt
    assert "反思结论用于决定下一步行动，不写入交底书正文" in prompt


def test_main_agent_document_read_supports_search_blocks() -> None:
    document_read = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "document_read")
    properties = document_read["function"]["parameters"]["properties"]

    assert "search_blocks" in properties["action"]["enum"]
    assert "get_project_context" in properties["action"]["enum"]
    assert "query" in properties


def test_exec_command_metadata_comes_from_tool_docstring() -> None:
    tool = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "exec_command")

    description = tool["function"]["description"]
    assert description == "在项目工作区内执行命令字符串，cwd 为当前 project 工作区。"
    assert tool["function"]["parameters"]["properties"]["command"]["description"] == "要执行的命令字符串，按当前项目工作区作为 cwd 执行。"

    prompt = build_main_agent_system_prompt()
    assert "命令超时时返回 command_timeout" in prompt
    assert '执行诊断命令：{"command":"ls -la","timeout":30}' in prompt


def test_append_child_section_protocol_is_single_shape() -> None:
    tool_names = [tool["function"]["name"] for tool in MAIN_AGENT_TOOLS]
    assert "document_edit" not in tool_names
    assert "document_append_child_section" in tool_names

    append_child = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "document_append_child_section")
    properties = append_child["function"]["parameters"]["properties"]

    assert set(properties) == {"parent_section_id", "title", "blocks"}
    assert append_child["function"]["parameters"]["required"] == ["parent_section_id", "title", "blocks"]
    assert "section" not in properties
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
    assert "operations" not in tool_manual
    assert "单次正文写入总量不得超过 1500 字" in tool_manual
    assert "一次只追加一个 block" in tool_manual
    assert "只需要提供 parent_section_id、title 和 blocks" in tool_manual


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


def test_barrier_renderer_only_outputs_compressed_context_messages() -> None:
    compressed = render_barrier_message({"kind": "compressed_context"})

    assert compressed["role"] == "user"
    assert "系统压缩后的历史上下文" in compressed["content"]
    with pytest.raises(ValueError):
        render_barrier_message({"kind": "agent_task", "task": "检查提示词冲突"})


def test_context_compressor_prompt_defines_markdown_memory_template() -> None:
    prompt = context_compressor_system_prompt()

    assert "只输出 Markdown 正文" in prompt
    assert "不要输出 JSON" in prompt
    assert "## 已确认事实" in prompt
    assert "## 当前进展" in prompt
    assert "## 后续注意" in prompt
    assert "## 关键片段" in prompt
    assert "## 待确认问题" in prompt
    assert "工具调用 ID" in prompt
    assert "target_estimated_tokens" not in prompt
