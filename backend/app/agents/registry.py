from __future__ import annotations

from ..core import ApiError
from .types import SubagentDeclaration

SUBAGENTS: dict[str, SubagentDeclaration] = {
    "material_analyst": SubagentDeclaration(
        id="material_analyst",
        description="从当前聊天材料和已有正文中抽取技术事实、术语和待确认问题。",
        input_expectation="提供用户输入、必要的参考章节和分析目标。",
        output_contract="通过 pipe 返回事实、术语、风险和待确认项。",
        usage_guidance="适合在写作前提炼事实、术语、约束和待确认问题；不要要求它直接生成完整正文或落盘编辑。",
        tool_permissions=("document_read", "exec_command"),
    ),
    "solution_refiner": SubagentDeclaration(
        id="solution_refiner",
        description="将零散事实收敛成可继续讨论或继续写作的技术方案骨架。",
        input_expectation="提供事实摘要、目标方向以及必要章节上下文。",
        output_contract="通过 pipe 返回方案骨架、模块关系、关键流程和待确认点。",
        usage_guidance="适合收敛技术问题、核心手段、模块关系和流程骨架；当需要完整章节正文时，由主 agent 基于骨架继续规划和落盘。",
        tool_permissions=("document_read", "exec_command"),
    ),
    "section_writer": SubagentDeclaration(
        id="section_writer",
        description="面向指定 section 或 block 生成轻量局部候选正文。",
        input_expectation="提供明确的局部目标，例如一个子章节、一个短段落或一个短列表，以及必要的当前章节内容。",
        output_contract="通过 pipe 返回局部候选正文。",
        usage_guidance="轻量局部写作工具；不要用于完整技术方案、完整实施例或多子章节整章生成。单次正文一般不超过 800 个中文字符。",
        tool_permissions=("document_read", "exec_command"),
    ),
}


def get_subagent(agent_id: str) -> SubagentDeclaration:
    try:
        return SUBAGENTS[agent_id]
    except KeyError as exc:
        raise ApiError(404, "subagent_not_found", f"不存在的子 agent：{agent_id}") from exc
