from __future__ import annotations

from ..core import ApiError
from .types import SubagentDeclaration

SUBAGENTS: dict[str, SubagentDeclaration] = {
    "material_analyst": SubagentDeclaration(
        id="material_analyst",
        description="从当前聊天材料和已有正文中抽取技术事实、术语和待确认问题。",
        default_type="rich_context_specialist",
        allowed_types=("rich_context_specialist", "task_only_specialist"),
        input_expectation="提供用户输入、必要的参考章节和分析目标。",
        output_contract="返回 analysis_result，包含事实、术语、风险和待确认项。",
        tool_permissions=("document_read", "exec_command"),
        default_proposal_type="analysis_result",
    ),
    "solution_refiner": SubagentDeclaration(
        id="solution_refiner",
        description="将零散事实收敛成可继续讨论或继续写作的技术方案骨架。",
        default_type="rich_context_specialist",
        allowed_types=("rich_context_specialist", "task_only_specialist"),
        input_expectation="提供事实摘要、目标方向以及必要章节上下文。",
        output_contract="返回 analysis_result 或 document_edit_proposal。",
        tool_permissions=("document_read", "exec_command"),
        default_proposal_type="analysis_result",
    ),
    "section_writer": SubagentDeclaration(
        id="section_writer",
        description="面向指定 section 或 block 生成候选 blocks 或 document_edit operations。",
        default_type="rich_context_specialist",
        allowed_types=("rich_context_specialist", "task_only_specialist"),
        input_expectation="提供目标章节、当前章节内容、目录结构和用户原始诉求。",
        output_contract="返回 document_edit_proposal，必要时附带问题和警告。",
        tool_permissions=("document_read", "exec_command"),
        default_proposal_type="document_edit_proposal",
    ),
    "consistency_reviewer": SubagentDeclaration(
        id="consistency_reviewer",
        description="检查术语、逻辑链路和章节闭环的一致性，给出 review_report。",
        default_type="rich_context_specialist",
        allowed_types=("rich_context_specialist", "task_only_specialist"),
        input_expectation="提供待审查章节、相关上下游章节和审查目标。",
        output_contract="返回 review_report，列出问题、风险和建议。",
        tool_permissions=("document_read", "exec_command"),
        default_proposal_type="review_report",
    ),
}


def get_subagent(agent_id: str) -> SubagentDeclaration:
    try:
        return SUBAGENTS[agent_id]
    except KeyError as exc:
        raise ApiError(404, "subagent_not_found", f"不存在的子 agent：{agent_id}") from exc
