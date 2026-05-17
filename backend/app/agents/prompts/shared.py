from __future__ import annotations

from ...core.command_platform import current_command_platform, exec_command_prompt_examples

_EXEC_COMMAND_PLATFORM = current_command_platform()
_EXEC_COMMAND_PROMPT_GUIDANCE = exec_command_prompt_examples()
_EXEC_COMMAND_FIRST_EXAMPLE = _EXEC_COMMAND_PLATFORM.examples[0]

SUBAGENT_TOOL_ARGUMENT_EXAMPLES = """工具调用参数 JSON 示例：
- document_read 读取项目上下文：
  {"action":"get_project_context"}
- document_read 读取兼容目录：
  {"action":"get_outline"}
- document_read 读取章节：
  {"action":"get_section","section_id":"sec_000007","include_children":true}
- document_read 读取 block：
  {"action":"get_block","block_id":"blk_000001"}
- document_read 搜索正文：
  {"action":"search_blocks","query":"消息平台"}
- exec_command 执行诊断命令：
  {"command":"__PLATFORM_COMMAND_EXAMPLE__","timeout":30}
- submit_result 提交最终结果：
  {"summary":"已完成任务。","reply":"已整理结果。","rationale":"基于当前上下文整理。","proposal_type":"analysis_result","proposal":{},"questions":[],"warnings":[]}

工具调用要求：
- 调用工具时，arguments 必须是严格 JSON 对象。
- JSON 字符串必须使用双引号；不能使用单引号、注释、尾随逗号或未转义换行。
- 中文正文中的双引号、反斜杠和换行必须正确转义。
- 最终结果必须通过 submit_result 工具提交，不要直接回复 JSON 或正文。
__PLATFORM_EXEC_COMMAND_GUIDANCE__
""".replace("__PLATFORM_COMMAND_EXAMPLE__", _EXEC_COMMAND_FIRST_EXAMPLE).replace(
    "__PLATFORM_EXEC_COMMAND_GUIDANCE__",
    _EXEC_COMMAND_PROMPT_GUIDANCE,
)


MAIN_AGENT_TOOL_ARGUMENT_EXAMPLES = """工具调用参数 JSON 示例：
- document_read 读取项目上下文：
  {"action":"get_project_context"}
- document_read 读取章节：
  {"action":"get_section","section_id":"sec_000004","include_children":true}
- document_read 读取 block：
  {"action":"get_block","block_id":"blk_000001"}
- document_read 搜索正文：
  {"action":"search_blocks","query":"候选区域"}
- document_edit 替换章节正文：
  {"operations":[{"op":"replace_section_blocks","section_id":"sec_000004","blocks":[{"type":"paragraph","text":"这里写入新的段落正文。"}]}]}
- document_edit 重组章节和子章节：
  {"operations":[{"op":"replace_section","section_id":"sec_000007","section":{"type":"technical_solution","title":"技术方案","blocks":[{"type":"paragraph","text":"这里写入章节总述。"}],"children":[{"type":"custom","title":"整体架构","blocks":[{"type":"paragraph","text":"这里写入架构说明。"}],"children":[]},{"type":"custom","title":"处理流程","blocks":[{"type":"list","ordered":true,"items":["步骤一。","步骤二。"]}],"children":[]}]}}]}
- document_edit 追加段落：
  {"operations":[{"op":"append_block","section_id":"sec_000007","block":{"type":"paragraph","text":"这里写入追加段落。"}}]}
- document_edit 追加子章节：
  {"operations":[{"op":"append_child_section","parent_section_id":"sec_000007","section":{"type":"custom","title":"关键模块","blocks":[{"type":"paragraph","text":"这里写入子章节正文。"}],"children":[]}}]}
- execute_subagent 调度章节写作：
  {"agent_id":"section_writer","goal":"基于已继承的上下文，为“技术方案”章节生成最终态候选正文，并通过 proposal.operations 指定写入的 section_id。"}
- execute_subagent 调度资料分析：
  {"agent_id":"material_analyst","goal":"基于已继承的上下文，提炼用户材料中的技术问题、技术方案和技术效果。"}
- exec_command 执行诊断命令：
  {"command":"__PLATFORM_COMMAND_EXAMPLE__","timeout":30}

工具调用要求：
- 调用工具时，arguments 必须是严格 JSON 对象。
- JSON 字符串必须使用双引号；不能使用单引号、注释、尾随逗号或未转义换行。
- 中文正文中的双引号、反斜杠和换行必须正确转义。
- document_edit 的 blocks/block 正文只能放在 text 字段，不要使用 content 字段。
- append_child_section 只能使用 parent_section_id 和 section 字段，不得使用 section_id、child 或 child_section 表示父章节或子章节。
- section_id 必须使用 document_read 返回的系统生成 id；不要把 technical_solution、technical_effects 等章节语义当作 section_id。
- 新增或替换 section 的 section 对象不允许携带 id；新增子章节使用 type:"custom" 和 title 表达语义。
__PLATFORM_EXEC_COMMAND_GUIDANCE__
""".replace("__PLATFORM_COMMAND_EXAMPLE__", _EXEC_COMMAND_FIRST_EXAMPLE).replace(
    "__PLATFORM_EXEC_COMMAND_GUIDANCE__",
    _EXEC_COMMAND_PROMPT_GUIDANCE,
)


FINAL_TEXT_RULES = """- 交底书正文必须是最终态文本，只呈现最终技术方案、结构和效果。
- 正文不得描述对话过程、修改过程或方案迭代过程。
- 正文不得出现“根据你的要求”“本次修改”“现在改为”“之前方案”“不再采用之前方案”等过程性表述。
- 如果需要替换旧方案，直接输出替换后的最终表述，不要在正文中解释旧方案如何被新方案取代。"""


DOCUMENT_ACCESS_RULES = """- 你会看到从调用方继承的历史 messages，以及最后一条【任务说明】；历史 messages 用于理解背景，不是本次任务本身。
- 本次要执行的目标以后置的【任务说明】为准。
- 默认上下文不包含项目标题和目录树；需要了解当前交底书结构时，先调用 document_read(action=get_project_context)。
- 如果任务依赖当前交底书原文，而上下文没有提供足够依据，先调用 document_read 读取相关章节或 block。
- 如果不知道某个概念、术语或技术点位于哪个章节，先用 document_read 的 search_blocks 搜索，再读取相关章节。
- 如果缺的是用户意图、真实技术事实、实施条件或取舍偏好，将缺口放入 questions；不要用猜测代替确认。"""


STRUCTURED_WRITING_RULES = """- 章节负责结构，block 负责具体正文；复杂内容不要只用多个 paragraph block 平铺。
- 当目标内容包含多个独立部分，或用户要求“整体架构 / 处理流程 / 实现方式 / 步骤 / 模块 / 原理 / 拓展方案”时，优先生成子章节。
- 当目标章节的 type 是 technical_solution、embodiments 等复杂标准章节，或内容超过 3 个自然段并包含模块与流程时，优先使用 replace_section 生成 children。
- 适合拆成子章节的常见标题包括：整体架构、处理流程、核心模块、关键规则、数据处理、异常处理、实施步骤、效果说明。
- 只有当修改短小、局部、没有独立标题价值时，才只使用 block，例如补一段说明、改写一个段落、补一个短列表。"""


SECTION_WRITER_SUBMIT_RESULT_EXAMPLE = """submit_result 参数示例：
{"summary":"已生成候选正文。","reply":"已补充目标章节候选内容。","rationale":"根据用户诉求和当前章节内容生成。","proposal_type":"document_edit_proposal","proposal":{"intent":"replace_section_content","confidence":0.75,"operations":[{"op":"replace_section_blocks","section_id":"sec_000007","blocks":[{"type":"paragraph","text":"这里写入新的段落正文。"}]}]},"questions":[],"warnings":[]}

结构化章节示例：
{"summary":"已生成结构化技术方案候选正文。","reply":"已补充技术方案结构化内容。","rationale":"目标章节包含整体架构和处理流程，适合拆分子章节。","proposal_type":"document_edit_proposal","proposal":{"intent":"structure_section_content","confidence":0.78,"operations":[{"op":"replace_section","section_id":"sec_000007","section":{"type":"technical_solution","title":"技术方案","blocks":[{"type":"paragraph","text":"本方案采用端侧轻量化检测架构，对输入图像进行候选区域筛选、轻量特征提取和结果校正。"}],"children":[{"type":"custom","title":"整体架构","blocks":[{"type":"paragraph","text":"系统包括图像获取模块、候选区域筛选模块、轻量推理模块和结果校正模块。"}],"children":[]},{"type":"custom","title":"处理流程","blocks":[{"type":"list","ordered":true,"items":["获取待检测图像。","筛选高价值候选区域。","对候选区域执行轻量化检测。","结合时序信息校正检测结果。"]}],"children":[]}]}}]},"questions":[],"warnings":[]}
"""


MATERIAL_ANALYST_SUBMIT_RESULT_EXAMPLE = """submit_result 参数示例：
{"summary":"已完成材料分析。","reply":"已提炼技术事实和待确认项。","rationale":"依据用户材料和当前文档提炼。","proposal_type":"analysis_result","proposal":{"facts":[{"kind":"technical_problem","text":"低算力设备上推理延迟高。"}],"candidate_terms":["低算力设备"],"recommended_next_actions":[{"action":"write_section","section_id":"sec_000006"}]},"questions":[],"warnings":[]}
"""


SOLUTION_REFINER_SUBMIT_RESULT_EXAMPLE = """submit_result 参数示例：
{"summary":"已整理技术方案骨架。","reply":"已收敛方案模块和关键约束。","rationale":"根据现有事实归纳方案结构。","proposal_type":"analysis_result","proposal":{"solution_outline":"整体方案包括采集、筛选和反馈。","modules":[{"name":"筛选模块","responsibility":"筛出高价值候选区域。"}],"key_constraints":["端侧算力有限"],"innovations":["复用时序信息降低重复推理"],"open_questions":[]},"questions":[],"warnings":[]}
"""


CONSISTENCY_REVIEWER_SUBMIT_RESULT_EXAMPLE = """submit_result 参数示例：
{"summary":"已完成一致性审查。","reply":"已列出术语和逻辑问题。","rationale":"对照目标章节与上下游章节检查。","proposal_type":"review_report","proposal":{"issues":[{"severity":"medium","section_id":"sec_000010","block_id":null,"message":"技术效果未呼应实时性问题。","suggested_fix":"补充低延迟收益描述。"}]},"questions":[],"warnings":[]}
"""
