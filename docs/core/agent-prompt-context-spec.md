# Agent Prompt 与上下文规范

## 文档定位

本文档定义主 agent prompt 和上下文恢复规则。

## Prompt 组成

主 agent system prompt 包含：

1. 能力边界。
2. 决策顺序。
3. 写作与编辑原则。
4. 优秀交底书标准。
5. 阶段性反思与完成态判断。
6. 工具使用边界。
7. 输出格式。
8. 自动生成工具声明。

工具声明来自 `MAIN_AGENT_TOOL_NAMES` 对应的工具元数据。prompt 不手写工具参数 schema。

## 上下文恢复

`ContextManager` 从 session log 中恢复主 agent messages：

- `user_input` 恢复为 user message。
- `agent_message` 恢复为 assistant message。
- `tool_call` 与 `tool_result` 恢复为 assistant tool call 与 tool result。
- `agent_output` 在无工具调用时恢复为 assistant 文本。

当前用户输入始终作为最后一条 user message。

## 上下文压缩

当上下文使用量超过阈值时，系统调用上下文压缩 agent，将较早历史压缩为 Markdown 记忆。压缩结果包含：

- `## 已确认事实`
- `## 当前进展`
- `## 后续注意`
- 可选的 `## 关键片段`
- 可选的 `## 待确认问题`

压缩记忆不是新的用户指令，只用于延续背景。若主 agent 需要当前文档原文，应重新调用 `document_read`。

## 主 Agent 决策原则

主 agent 每一步只能选择一种行为：

- 调用一个或多个工具。
- 直接输出最终中文回复。

如果任务依赖当前交底书正文且上下文没有足够依据，先读取相关章节或搜索正文。如果缺少用户意图、真实技术事实、实施条件或取舍偏好，向用户追问。
