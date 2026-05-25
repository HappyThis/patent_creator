# Round Lifecycle

## 文档定位

本文档定义一次用户消息从接收到完成的后端生命周期。当前系统采用单主 agent loop，不包含子 agent loop。

## 流程

1. API 接收用户消息。
2. `ChatService.prepare_round` 创建 session、message 和 round，并写入 `user_input`。
3. `ContextManager.prepare_main_agent_messages` 恢复并必要时压缩上下文。
4. 主 agent 基于 system prompt、messages 和工具声明生成下一步动作。
5. 如果主 agent 返回工具调用，服务层依次执行工具并写入 `tool_call` / `tool_result`。
6. 如果文档工具成功写入，广播 `document_changed`。
7. 工具结果追加回主 agent messages，主 agent 继续下一步。
8. 如果主 agent 直接回复，写入 `agent_message` 和 `agent_output`。
9. 若文档有变更，工作区提交一次 git commit。
10. 广播 `round_finished`。

## 工具调用

工具调用统一由 `ExecutorEngine.execute_tool` 执行。执行器只处理普通工具，不再把某个工具解释为 agent 调度。

当前工具失败不会直接终止 round；失败结果会回填给主 agent，由主 agent 决定恢复、追问或结束。

## 事件顺序

典型工具调用轮次：

1. `user_input`
2. `agent_message`
3. `tool_call`
4. `tool_result`
5. `agent_message`
6. `agent_output`

如果 assistant 在工具调用前带有可见文本，会额外写入一条 `agent_output` 作为 preamble。

## 取消与失败

用户取消运行时，运行中的 asyncio task 被取消，项目状态恢复为空闲，并广播取消结果。

未捕获异常会写入失败的 `agent_output`，恢复项目空闲状态，并广播 `round_failed`。
