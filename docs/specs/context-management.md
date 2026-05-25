# Context Management

## 文档定位

本文档定义当前单主 agent 架构下的上下文管理。系统不再区分主 agent / 子 agent 可见性，也不再为子任务生成独立上下文。

## 职责

`ContextManager` 负责：

- 从 session log 恢复主 agent messages。
- 在需要时压缩较早历史。
- 在压缩失败或仍超预算时裁剪历史。
- 估算上下文 token 使用量。
- 保证当前用户输入保留在最后一条 user message。

## 可恢复事件

主上下文恢复以下事件：

- `user_input`
- `agent_message`
- `agent_output`
- `tool_call`
- `tool_result`

`context_summary` 和 `context_pruned` 用作恢复锚点，不直接恢复为普通对话轮次。

## 压缩策略

当上下文达到阈值后，系统选择当前用户输入之前、锚点之后的主流程事件进行压缩。压缩结果以 Markdown 记忆形式写入 session log，并推进恢复 cursor。

压缩结果必须保留：

- 已确认事实。
- 当前进展。
- 后续注意。

若压缩失败或压缩后仍超预算，系统使用最近用户轮次边界做裁剪兜底。

## 工具结果

工具调用和工具结果属于主 agent 上下文的一部分。主 agent 后续可以直接看到已闭合的工具调用结果；未闭合的 assistant tool call 不会被恢复为可见历史。

## 当前边界

当前系统没有子 agent 内部事件、pipe 输出或任务看板上下文。所有上下文压力都由主 agent 的恢复、压缩和裁剪机制处理。
