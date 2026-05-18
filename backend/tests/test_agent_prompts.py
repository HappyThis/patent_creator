from __future__ import annotations

from app.agents import SUBAGENTS, get_subagent
from app.agents.prompts import build_main_agent_system_prompt, build_section_writer_system_prompt
from app.agents.prompts.consistency_reviewer import build_consistency_reviewer_system_prompt
from app.agents.prompts.main_agent import (
    build_main_agent_system_prompt as build_split_main_agent_system_prompt,
    render_subagent_catalog,
)
from app.agents.prompts.material_analyst import build_material_analyst_system_prompt
from app.agents.prompts.section_writer import build_section_writer_system_prompt as build_split_section_writer_system_prompt
from app.agents.prompts.solution_refiner import build_solution_refiner_system_prompt
from app.agents.workers.main_agent import MAIN_AGENT_TOOLS
from app.core.command_platform import current_command_platform
from app.runtime.context.barrier import render_barrier_message
from app.runtime.context.prompts import context_compressor_system_prompt
from app.runtime.executor.engine import SUBAGENT_TOOLS


def test_agent_prompts_are_split_by_agent_module() -> None:
    assert build_main_agent_system_prompt is build_split_main_agent_system_prompt
    assert build_section_writer_system_prompt is build_split_section_writer_system_prompt
    assert "material_analyst" in build_material_analyst_system_prompt(get_subagent("material_analyst"))
    assert "solution_refiner" in build_solution_refiner_system_prompt(get_subagent("solution_refiner"))
    assert "consistency_reviewer" in build_consistency_reviewer_system_prompt(get_subagent("consistency_reviewer"))


def test_main_agent_prompt_requires_reading_source_before_uncertain_document_answers() -> None:
    prompt = build_main_agent_system_prompt()

    assert "先判断用户任务是否依赖当前交底书正文" in prompt
    assert "缺的是当前正文依据" in prompt
    assert "先用 document_read 读取相关章节或 block" in prompt
    assert "先用 search_blocks 搜索" in prompt
    assert "include_children=true" in prompt


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


def test_prompts_define_structured_section_and_final_text_rules() -> None:
    main_prompt = build_main_agent_system_prompt()
    writer_prompt = build_section_writer_system_prompt(get_subagent("section_writer"))

    for prompt in (main_prompt, writer_prompt):
        assert "正文必须是最终态文本" in prompt
        assert "根据你的要求" in prompt
        assert "本次修改" in prompt
        assert "章节负责结构，block 负责具体正文" in prompt
        assert "整体架构" in prompt
        assert "处理流程" in prompt
        assert "不要把多个应有标题的内容平铺成 blocks" in prompt or "复杂内容不要只用多个 paragraph block 平铺" in prompt


def test_subagent_prompts_use_model_visible_context_and_document_rules() -> None:
    prompts = [
        build_main_agent_system_prompt(),
        build_section_writer_system_prompt(get_subagent("section_writer")),
        build_material_analyst_system_prompt(get_subagent("material_analyst")),
        build_solution_refiner_system_prompt(get_subagent("solution_refiner")),
        build_consistency_reviewer_system_prompt(get_subagent("consistency_reviewer")),
    ]

    for prompt in prompts:
        assert "disclosure.json" not in prompt
        assert "search_blocks" in prompt
        assert "用户意图" in prompt

    assert "先判断用户任务是否依赖当前交底书正文" in prompts[0]
    for prompt in prompts[1:]:
        assert "不能直接写入当前交底书文档" in prompt
        assert "如果任务依赖当前交底书原文" in prompt


def test_solution_refiner_document_edit_prompt_uses_final_text_and_structure_rules() -> None:
    prompt = build_solution_refiner_system_prompt(get_subagent("solution_refiner"))

    assert "正文写作要求" in prompt
    assert "交底书正文必须是最终态文本" in prompt
    assert "章节负责结构，block 负责具体正文" in prompt
    assert "不要生成 document_edit operations" in prompt


def test_subagent_prompts_define_task_execution_methods() -> None:
    material_prompt = build_material_analyst_system_prompt(get_subagent("material_analyst"))
    solution_prompt = build_solution_refiner_system_prompt(get_subagent("solution_refiner"))
    reviewer_prompt = build_consistency_reviewer_system_prompt(get_subagent("consistency_reviewer"))

    assert "用户明确陈述的事实" in material_prompt
    assert "可从上下文合理归纳的技术关系" in material_prompt
    assert "不要把推断写成事实" in material_prompt
    assert "technical_problem" in material_prompt
    assert "建议下一步" in material_prompt

    assert "技术问题和约束" in solution_prompt
    assert "核心技术手段" in solution_prompt
    assert "模块和流程" in solution_prompt
    assert "检查因果链" in solution_prompt
    assert "只足够形成骨架" in solution_prompt

    assert "审查清单" in reviewer_prompt
    assert "术语一致性" in reviewer_prompt
    assert "问题-方案闭环" in reviewer_prompt
    assert "方案-效果因果链" in reviewer_prompt
    assert "severity" in reviewer_prompt


def test_main_agent_prompt_defines_writing_boundary_and_real_context_shape() -> None:
    prompt = build_main_agent_system_prompt()

    assert "你具备写作能力" in prompt
    assert "完整章节规划" in prompt
    assert "可用子 agent 清单" in prompt
    assert "选择子 agent 时，必须依据下方" in prompt
    assert render_subagent_catalog() in prompt
    for declaration in SUBAGENTS.values():
        assert declaration.id in prompt
        assert declaration.description in prompt
        assert declaration.input_expectation in prompt
        assert declaration.output_contract in prompt
        assert declaration.usage_guidance in prompt
    assert "短小、明确、低创造性的最终态正文编辑" in prompt
    assert "默认上下文不包含项目标题、目录树或完整正文" in prompt
    assert "document_read(action=get_project_context)" in prompt


def test_main_agent_document_read_supports_search_blocks() -> None:
    document_read = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "document_read")
    properties = document_read["function"]["parameters"]["properties"]

    assert "search_blocks" in properties["action"]["enum"]
    assert "get_project_context" in properties["action"]["enum"]
    assert "query" in properties


def test_exec_command_metadata_uses_current_platform() -> None:
    profile = current_command_platform()
    main_tool = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "exec_command")
    subagent_tool = next(tool for tool in SUBAGENT_TOOLS if tool["function"]["name"] == "exec_command")

    for tool in (main_tool, subagent_tool):
        description = tool["function"]["description"]
        assert profile.platform in description
        assert profile.shell in description
        assert profile.examples[0] in description

    prompt = build_main_agent_system_prompt()
    assert profile.platform in prompt
    assert profile.shell in prompt
    assert profile.examples[0] in prompt


def test_section_writer_is_limited_to_lightweight_local_writing() -> None:
    prompt = build_section_writer_system_prompt(get_subagent("section_writer"))

    assert "轻量局部写作任务" in prompt
    assert "不是整章技术方案生成器" in prompt
    assert "一般不超过 800 个中文字符" in prompt
    assert "不要生成 document_edit operations" in prompt
    assert "不要用 replace_section 生成包含多个 children 的完整标准章节" in prompt
    assert "最终态文本" in prompt


def test_append_child_section_protocol_is_single_shape() -> None:
    document_edit = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "document_edit")
    operations_description = document_edit["function"]["parameters"]["properties"]["operations"]["description"]

    assert "append_child_section 必须使用 parent_section_id 和 section" in operations_description


def test_subagent_prompts_use_pipe_protocol() -> None:
    prompts = [
        build_section_writer_system_prompt(get_subagent("section_writer")),
        build_material_analyst_system_prompt(get_subagent("material_analyst")),
        build_solution_refiner_system_prompt(get_subagent("solution_refiner")),
        build_consistency_reviewer_system_prompt(get_subagent("consistency_reviewer")),
    ]

    for prompt in prompts:
        assert "write_pipe" in prompt
        assert "finish({})" in prompt
        assert "submit_result" not in prompt
        assert "proposal_type" not in prompt


def test_execute_subagent_schema_only_uses_agent_id_and_goal() -> None:
    tool = next(tool for tool in MAIN_AGENT_TOOLS if tool["function"]["name"] == "execute_subagent")
    params = tool["function"]["parameters"]

    assert params["required"] == ["agent_id", "goal"]
    assert set(params["properties"]) == {"agent_id", "goal"}
    assert params["properties"]["agent_id"]["enum"] == list(SUBAGENTS)
    for declaration in SUBAGENTS.values():
        assert declaration.id in tool["function"]["description"]
        assert declaration.description in tool["function"]["description"]
        assert declaration.input_expectation in tool["function"]["description"]
        assert declaration.output_contract in tool["function"]["description"]
        assert declaration.usage_guidance in tool["function"]["description"]


def test_subagent_tool_schema_uses_pipe_protocol() -> None:
    tool_names = [tool["function"]["name"] for tool in SUBAGENT_TOOLS]

    assert "write_pipe" in tool_names
    assert "finish" in tool_names
    assert "submit_result" not in tool_names


def test_barrier_renderer_outputs_user_messages() -> None:
    compressed = render_barrier_message({"kind": "compressed_context"})
    task = render_barrier_message({"kind": "agent_task", "task": "检查提示词冲突"})

    assert compressed["role"] == "user"
    assert "系统压缩后的历史上下文" in compressed["content"]
    assert task["role"] == "user"
    assert "从调用方继承的历史上下文" in task["content"]
    assert "检查提示词冲突" in task["content"]


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
