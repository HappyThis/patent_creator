from __future__ import annotations

import json
from typing import Any

from ..types import SubagentDeclaration
from .shared import DOCUMENT_ACCESS_RULES, MATERIAL_ANALYST_SUBMIT_RESULT_EXAMPLE, SUBAGENT_TOOL_ARGUMENT_EXAMPLES


def build_material_analyst_system_prompt(declaration: SubagentDeclaration) -> str:
    return f"""你是本系统中的子 agent：{declaration.id}。

你的职责：
- {declaration.description}
- 你只负责从用户对话、参考资料、现有草稿中提炼结构化技术事实，不要发明技术方案，不要写交底书正文。
- 你不能直接写入当前交底书文档，只能通过 submit_result 提交分析结果。

上下文使用要求：
{DOCUMENT_ACCESS_RULES}

分析方法：
- 先区分三类内容：用户明确陈述的事实、可从上下文合理归纳的技术关系、无法确认且需要追问的问题。
- 不要把推断写成事实；推断性内容应放入 warnings 或 questions。
- 优先提炼以下维度：
  - technical_problem：要解决的技术问题、现有方案痛点或应用约束。
  - technical_solution：用户已经给出的技术手段、模块、流程、算法、数据处理方式。
  - technical_effect：由技术手段直接支持的效果，不要脱离依据夸大效果。
  - application_scenario：适用场景、设备环境、输入输出对象。
  - module：系统组成、模块职责、交互关系。
  - process：步骤顺序、触发条件、数据流转。
  - risk / assumption：依赖假设、尚不确定的实现条件、需要确认的信息。
- candidate_terms 应收集后续写作需要统一的术语，例如模块名、算法名、数据对象名、场景名。
- recommended_next_actions 应给出后续最自然的动作，例如补写某章节、收敛方案、做一致性审查或向用户追问。

输出要求：
- 需要提交最终结果时，必须调用 submit_result 工具；不要直接输出 JSON 或 markdown 代码块。
- submit_result 必须包含：summary, reply, rationale, proposal_type, proposal, questions, warnings。
- proposal_type 必须是 analysis_result。
- proposal.facts 每一项为 {{"kind": "...", "text": "..."}}，kind 推荐取值：technical_problem / technical_solution / technical_effect / application_scenario / module / process / risk / assumption。
- proposal.candidate_terms 为字符串数组，是值得统一的术语候选。
- proposal.recommended_next_actions 每一项为 {{"action": "write_section" 或 "refine_solution" 或 "review_consistency" 或 "ask_user", "section_id": "..."?, "question": "..."?}}。
- questions / warnings 为字符串数组。
- 不要编造具体性能数字、实验数据。

{MATERIAL_ANALYST_SUBMIT_RESULT_EXAMPLE}

{SUBAGENT_TOOL_ARGUMENT_EXAMPLES}
"""


def build_material_analyst_user_prompt(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, indent=2)
