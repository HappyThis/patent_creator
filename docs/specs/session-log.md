# Session Log

## 文档定位

本文档定义 session event 的记录与恢复方式。当前系统只有主 agent scope，不再记录子 agent 内部过程。

## Scope

当前有效 scope：

- `main`

历史日志中可能存在旧版本的其他 scope，但当前运行时不会继续产生这些事件。

## 主要事件

- `user_input`：用户输入和激活章节/block。
- `agent_message`：模型返回的 assistant message，包括 tool calls。
- `agent_output`：展示给用户的文本输出。
- `tool_call`：主 agent 发起的工具调用。
- `tool_result`：工具返回结果。
- `context_summary`：上下文压缩结果。
- `context_pruned`：上下文裁剪锚点。

## 工具事件

工具调用事件包含：

```json
{
  "type": "tool_call",
  "scope": "main",
  "call_id": "call_001",
  "payload": {
    "tool": "document_read",
    "arguments": {}
  }
}
```

工具结果事件包含：

```json
{
  "type": "tool_result",
  "scope": "main",
  "call_id": "call_001",
  "payload": {
    "tool": "document_read",
    "status": "success",
    "output": {}
  }
}
```

`call_id` 必须能对应 assistant message 中的 tool call id，以便恢复为 OpenAI-compatible messages。

## 上下文恢复

恢复规则：

1. 从最近的 `context_summary` 或 `context_pruned` 锚点开始。
2. 恢复 `scope=main` 的用户输入、assistant message、工具调用和工具结果。
3. 只恢复已经闭合的工具调用。
4. 当前用户输入追加为最后一条 user message。

## 压缩事件

`context_summary` 记录压缩后的 Markdown 记忆、覆盖范围、压缩前后估算 token 和 cursor。它不代表用户新输入。

`context_pruned` 记录兜底裁剪后的新 cursor。它用于恢复上下文窗口，不代表业务动作。
