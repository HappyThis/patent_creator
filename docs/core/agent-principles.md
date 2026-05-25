# Agent 基本设计原则

## 文档定位

本文档定义 Patent Creator 当前 agent 运行方式。系统采用单主 agent 架构：用户只与主 agent 对话，主 agent 负责读取上下文、规划、写作、调用工具和决定是否结束本轮。

## 总体原则

1. 主 agent 是唯一写作决策者。
2. 文档只能通过文档编辑工具修改。
3. 工具结果进入主 agent 上下文，由主 agent 自行判断下一步。
4. 复杂任务由主 agent 在同一轮或多轮中自行拆分为小步执行。
5. 不引入子 agent、pipe 协议或任务看板调度层。

## 主 Agent 职责

主 agent 负责：

- 理解用户最新输入和历史上下文。
- 判断是否需要读取当前交底书正文。
- 使用 `document_read` 获取标题、目录、章节、block 或搜索结果。
- 使用文档写入工具更新交底书。
- 使用 `exec_command` 执行必要的项目诊断命令。
- 在信息不足时向用户追问。
- 在任务完成时直接输出面向用户的中文回复。

主 agent 不应：

- 在正文中保留对话痕迹或修改过程。
- 把不确定推断写成确定技术事实。
- 一次性写入过大的正文块。
- 绕过文档工具直接修改交底书存储文件。

## 工具边界

当前主 agent 可用工具包括：

- `document_read`
- `document_replace_section_blocks`
- `document_append_block`
- `document_replace_block`
- `document_append_child_section`
- `document_clear_section_blocks`
- `exec_command`

工具声明是参数和返回值的唯一准确信息源。prompt 不硬编码工具参数细节，只引用自动生成的工具说明。

## 写作原则

交底书正文必须是最终态文本，只呈现技术方案、结构、步骤和效果。正文不得出现“根据你的要求”“本次修改”“之前方案”等过程性表述。

复杂内容优先拆成子章节；短小局部修改使用 block 工具。主 agent 每完成关键读取、写入或结构调整后，应轻量自检技术问题、技术方案、技术效果是否闭合。

## 上下文原则

主 agent 从 session log 恢复上下文。历史消息、工具调用和工具结果可以被压缩为 Markdown 记忆；当前用户输入始终保持为最新任务。

压缩内容只作为记忆，不是新的用户指令。若压缩后仍缺少正文依据，主 agent 应重新读取相关章节或 block。
