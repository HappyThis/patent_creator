# Session 事件日志 Schema

## 文档定位

本文档定义本项目的 session 事件日志结构。

它建立在以下文档之上：

- [Agent 基本设计原则](../core/agent-principles.md)
- [Agent Prompt 与上下文规范](../core/agent-prompt-context-spec.md)
- [子 Agent 定义](../core/subagents.md)
- [Tools 设计](../core/tools.md)

本文档定义：

- 日志文件格式
- 事件类型
- 公共字段
- 事件 payload 结构
- 子 agent 调用记录方式
- 文档变更记录方式

本文档不定义日志轮转策略和上下文压缩策略。上下文恢复、压缩与 cursor 管理见：

- [上下文管理规范](context-management.md)

## 一、目标

session 事件日志用于记录：

- 用户输入
- 主 agent 输出
- tool 调用
- tool 返回结果
- 子 agent 的调用过程和最终结果
- 文档变更结果

它的作用包括：

1. 回放 session 过程
2. 调试问题
3. 追踪调用链路
4. 支撑未来 UI 重建
5. 支撑问题排查与审计

说明：

- 当前交底书文档负责表达“现在正文是什么”
- session 事件日志负责表达“这个 session 里发生了什么”

## 二、核心原则

### 1. 只要影响上下文或 UI，就必须记录

至少要记录：

- 用户输入
- 主 agent 面向用户的输出
- tool 调用
- tool 返回结果
- 文档变更结果

### 2. 主 agent 工具调用结果既进入日志，也进入主 agent 上下文

`scope=main` 的 `tool_call` 与 `tool_result` 不只是审计记录。

它们也是后续主 agent 上下文恢复的权威来源：

- `tool_call` 恢复为 OpenAI-compatible 的 `assistant(tool_calls)`。
- `tool_result` 恢复为紧随其后的 `tool` message。
- `execute_subagent` 的最终返回结果按普通主流程工具结果恢复。

这些结果持续保留到上下文压缩或 cursor 裁剪发生为止。

### 3. 子 agent 的过程不进入主 agent 上下文，但要进入日志

主 agent 继续推理时，通常只需要子 agent 的最终结构化结果。

session 日志必须保留子 agent 的执行过程，便于问题排查。

子 agent 内部 `scope=subagent:*` 的 `tool_call` 与 `tool_result` 不投影到主 agent 历史上下文。

### 4. 日志定位使用 id

日志中涉及文档内容时，统一使用：

- `section_id`
- `block_id`
- `changed_section_ids`
- `changed_block_ids`

## 三、文件格式

文件格式使用：

- `jsonl`

即：

- 一行一个 JSON 事件
- append-only

## 四、事件类型

基础事件包括：

1. `user_input`
2. `agent_message`
3. `agent_output`
4. `tool_call`
5. `tool_result`

上下文管理扩展事件包括：

1. `context_summary`
2. `context_pruned`

扩展事件的具体 payload 由上下文管理规范定义。

说明：

- 子 agent 作为 `execute_subagent` 被调用。
- 子 agent 过程通过 `scope`、`call_id` 和 `parent_call_id` 记录。
- barrier 是上下文装配阶段的内部抽象，不作为独立 event 写入 session log。
- `compressed_context` barrier 不作为 event 单独写入；恢复 `context_summary.compressed_markdown` 时由程序追加到压缩记忆 message 后。
- `agent_task` barrier 渲染后的 message 只进入子 agent 本次运行的 `messages`，不作为主流程 event 写入。

## 五、公共字段

所有事件统一带以下公共字段：

```json
{
  "id": "evt_000001",
  "ts": "2026-04-23T15:30:00+08:00",
  "type": "user_input",
  "seq": 1,
  "scope": "main",
  "round_id": "round_000001",
  "message_id": "msg_000001",
  "call_id": null,
  "parent_call_id": null,
  "payload": {}
}
```

字段说明：

- `id`：事件唯一标识
- `ts`：事件时间
- `type`：事件类型
- `seq`：session 内顺序号
- `scope`：事件所属作用域
- `round_id`：本轮处理标识
- `message_id`：触发本轮的用户消息标识
- `call_id`：工具调用标识
- `parent_call_id`：父级工具调用标识
- `payload`：事件具体内容

说明：

- 非工具事件的 `call_id` 可以为 `null`。
- 主 agent 发起的 tool call 的 `parent_call_id` 通常为 `null`。
- 子 agent 内部 tool call 的 `parent_call_id` 指向主流程中的 `execute_subagent` 调用。

## 六、scope 定义

`scope` 用于区分事件属于主 agent 还是某个子 agent。

固定取值：

- `main`
- `subagent:material_analyst`
- `subagent:solution_refiner`
- `subagent:section_writer`
- `subagent:consistency_reviewer`

## 七、事件结构

### 1. user_input

用于记录用户输入。

示例：

```json
{
  "id": "evt_000001",
  "ts": "2026-04-23T15:30:00+08:00",
  "type": "user_input",
  "seq": 1,
  "scope": "main",
  "round_id": "round_000001",
  "message_id": "msg_000001",
  "call_id": null,
  "parent_call_id": null,
  "payload": {
    "text": "我想写一个图像检测方向的专利交底书。"
  }
}
```

最小字段：

- `text`

### 2. agent_message

用于记录主 agent 从模型返回的原始 assistant message。该事件面向运行时恢复，不直接作为用户可见输出。

示例：

```json
{
  "id": "evt_000002",
  "ts": "2026-04-23T15:30:05+08:00",
  "type": "agent_message",
  "seq": 2,
  "scope": "main",
  "round_id": "round_000001",
  "message_id": "msg_000001",
  "call_id": null,
  "parent_call_id": null,
  "payload": {
    "model": "deepseek-reasoner",
    "provider": "deepseek",
    "thinking": "enabled",
    "message": {
      "role": "assistant",
      "content": "",
      "reasoning_content": "需要先读取当前目录。",
      "tool_calls": [
        {
          "id": "call_000001",
          "type": "function",
          "function": {
            "name": "document_read",
            "arguments": "{\"action\":\"get_outline\"}"
          }
        }
      ]
    }
  }
}
```

最小字段：

- `message`
- `model`
- `provider`
- `thinking`

说明：

- `agent_message` 尽量保存模型原始 assistant message，包括 `reasoning_content` 和 `tool_calls`。
- 后续请求是否回放 `reasoning_content` 由当前 provider profile 决定。
- 如果同一轮还需要向用户展示可见文本，应同时写入 `agent_output`。

### 3. agent_output

用于记录主 agent 面向用户的输出。

示例：

```json
{
  "id": "evt_000002",
  "ts": "2026-04-23T15:30:05+08:00",
  "type": "agent_output",
  "seq": 2,
  "scope": "main",
  "round_id": "round_000001",
  "message_id": "msg_000001",
  "call_id": null,
  "parent_call_id": null,
  "payload": {
    "text": "我先帮你梳理这个方向。你更想强调检测精度，还是低算力实时性？"
  }
}
```

最小字段：

- `text`

说明：

- `agent_output` 用于记录主 agent 最终回复或工具调用前的可见前置说明。
- 上下文恢复优先使用 `agent_message` 中的原始 assistant message；没有 `agent_message` 时，才使用 `agent_output` 与工具事件重建 assistant message。
- 正常完成时，本轮最终回复也会在 SSE 的 `round_finished.reply` 中收束；取消或失败时分别通过 `round_cancelled.reply`、`round_failed.reply` 收束。

### 4. tool_call

用于记录主 agent 或子 agent 发起的工具调用。

示例：

```json
{
  "id": "evt_000003",
  "ts": "2026-04-23T15:30:10+08:00",
  "type": "tool_call",
  "seq": 3,
  "scope": "main",
  "round_id": "round_000001",
  "message_id": "msg_000001",
  "call_id": "call_000001",
  "parent_call_id": null,
  "payload": {
    "tool": "execute_subagent",
    "arguments": {
      "agent_id": "material_analyst",
      "goal": "从当前用户输入中提炼技术方向、目标和待确认信息。"
    }
  }
}
```

最小字段：

- `tool`
- `arguments`

### 5. tool_result

用于记录工具返回结果。

示例：

```json
{
  "id": "evt_000004",
  "ts": "2026-04-23T15:30:12+08:00",
  "type": "tool_result",
  "seq": 4,
  "scope": "main",
  "round_id": "round_000001",
  "message_id": "msg_000001",
  "call_id": "call_000001",
  "parent_call_id": null,
  "payload": {
    "tool": "execute_subagent",
    "status": "success",
    "output": {
      "agent_id": "material_analyst",
      "content": "## 技术方向\n\n- 当前主题可归纳为图像检测方向。\n\n## 待确认问题\n\n- 是否强调低算力实时性？"
    }
  }
}
```

最小字段：

- `tool`
- `status`
- `output`

## 八、document_edit 结果记录

当工具为 `document_edit` 时，`tool_result.payload.output` 必须包含文档变更 ids。

示例：

```json
{
  "tool": "document_edit",
  "status": "success",
  "output": {
    "changed_section_ids": [
      "technical_solution"
    ],
    "changed_block_ids": [
      "blk_000014"
    ],
    "operations_applied": 1
  }
}
```

说明：

- `changed_section_ids` 记录本次变更影响的章节。
- `changed_block_ids` 记录本次新增或替换的 block。
- 这些 id 用于 SSE、前端高亮、commit message 和调试回放。

## 九、子 agent 过程的记录方式

子 agent 的过程完整进入日志。

记录顺序：

1. 主 agent 调用 `execute_subagent`
2. 记录一条 `tool_call`，`scope=main`
3. 子 agent 内部如果再调工具，则继续记录：
   - `tool_call`，`scope=subagent:<agent_id>`
   - `tool_result`，`scope=subagent:<agent_id>`
4. 子 agent 返回最终结果时，记录：
   - `tool_result`，`scope=main`，`tool=execute_subagent`

子 agent 内部事件的 `parent_call_id` 指向主流程中的 `execute_subagent` 调用。

主 agent 恢复上下文时：

- 恢复第 1 步和第 4 步，即 `scope=main` 的 `execute_subagent` 调用和最终结果。
- 不恢复第 3 步，即 `scope=subagent:<agent_id>` 的内部工具过程。

## 十、完整示例

```jsonl
{"id":"evt_000001","ts":"2026-04-23T15:30:00+08:00","type":"user_input","seq":1,"scope":"main","round_id":"round_000001","message_id":"msg_000001","call_id":null,"parent_call_id":null,"payload":{"text":"我想写一个图像检测方向的专利交底书。"}}
{"id":"evt_000002","ts":"2026-04-23T15:30:10+08:00","type":"tool_call","seq":2,"scope":"main","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000001","parent_call_id":null,"payload":{"tool":"execute_subagent","arguments":{"agent_id":"material_analyst","goal":"从当前用户输入中提炼技术方向、目标和待确认信息。"}}}
{"id":"evt_000003","ts":"2026-04-23T15:30:11+08:00","type":"tool_call","seq":3,"scope":"subagent:material_analyst","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000002","parent_call_id":"call_000001","payload":{"tool":"document_read","arguments":{"action":"get_outline"}}}
{"id":"evt_000004","ts":"2026-04-23T15:30:11+08:00","type":"tool_result","seq":4,"scope":"subagent:material_analyst","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000002","parent_call_id":"call_000001","payload":{"tool":"document_read","status":"success","output":{"sections":[]}}}
{"id":"evt_000005","ts":"2026-04-23T15:30:12+08:00","type":"tool_call","seq":5,"scope":"subagent:material_analyst","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000003","parent_call_id":"call_000001","payload":{"tool":"write_pipe","arguments":{"content":"## 技术方向\n\n- 当前主题可归纳为图像检测方向。\n\n## 待确认问题\n\n- 是否强调低算力实时性？"}}}
{"id":"evt_000006","ts":"2026-04-23T15:30:12+08:00","type":"tool_result","seq":6,"scope":"subagent:material_analyst","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000003","parent_call_id":"call_000001","payload":{"tool":"write_pipe","status":"success","output":{"status":"ok","part_index":1,"written_chars":52,"total_chars":52,"stored_preview":"## 技术方向\n\n- 当前主题可归纳为图像检测方向。\n\n## 待确认问题\n\n- 是否强调低算力实时性？","next":"如果还有内容，继续调用 write_pipe；如果已经完成，调用 finish({})。"}}}
{"id":"evt_000007","ts":"2026-04-23T15:30:13+08:00","type":"tool_call","seq":7,"scope":"subagent:material_analyst","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000004","parent_call_id":"call_000001","payload":{"tool":"finish","arguments":{}}}
{"id":"evt_000008","ts":"2026-04-23T15:30:13+08:00","type":"tool_result","seq":8,"scope":"subagent:material_analyst","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000004","parent_call_id":"call_000001","payload":{"tool":"finish","status":"success","output":{"status":"done","parts":1,"total_chars":52}}}
{"id":"evt_000009","ts":"2026-04-23T15:30:13+08:00","type":"tool_result","seq":9,"scope":"main","round_id":"round_000001","message_id":"msg_000001","call_id":"call_000001","parent_call_id":null,"payload":{"tool":"execute_subagent","status":"success","output":{"agent_id":"material_analyst","content":"## 技术方向\n\n- 当前主题可归纳为图像检测方向。\n\n## 待确认问题\n\n- 是否强调低算力实时性？"}}}
```

## 十一、设计结论

session 事件日志 schema 采用：

- 文件格式：`jsonl`
- 事件类型：`user_input`、`agent_message`、`agent_output`、`tool_call`、`tool_result`、`context_summary`、`context_pruned`
- 公共字段：`id`、`ts`、`type`、`seq`、`scope`、`round_id`、`message_id`、`call_id`、`parent_call_id`、`payload`

其中：

- 主 agent 过程通过 `scope=main` 记录。
- 子 agent 过程通过 `scope=subagent:<agent_id>` 记录。
- 主 agent 的工具调用和工具结果需要进入主 agent 上下文。
- 子 agent 内部过程需要完整记录，但不进入主 agent 上下文。
- 文档变更记录使用 `changed_section_ids` 和 `changed_block_ids`。
