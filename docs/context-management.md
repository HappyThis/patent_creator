# 上下文管理规范

## 文档定位

本文档定义本项目中 `ContextManager` 的职责、session 上下文恢复、上下文窗口预算、压缩策略、兜底裁剪策略以及主 agent / 子 agent 的上下文可见性边界。

它建立在以下文档之上：

- [Agent 基本设计原则](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles.md)
- [Agent Prompt 与上下文规范](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec.md)
- [Session 事件日志 Schema](/Users/yangchaoqun/myProj/patent_creator/docs/session-log.md)
- [一轮内部时序](/Users/yangchaoqun/myProj/patent_creator/docs/round-lifecycle.md)

本文档重点回答：

1. 每次模型调用的 `messages` 数组由谁维护。
2. 如何从 session log 恢复上下文。
3. session 上下文窗口如何计算用量、触发压缩。
4. 压缩失败时如何兜底裁剪。
5. 主 agent 与子 agent 的上下文可见边界是什么。
6. 工具调用结果在压缩阶段如何处理。

## 一、核心定义

### 1. ContextManager

`ContextManager` 是系统代码，不是 agent。

它负责维护每次请求模型时的完整 `messages` 数组，包括：

- 恢复当前 session 的历史对话。
- 维护本轮 tool use 后的工作上下文。
- 估算上下文 token 用量。
- 在上下文超阈值时触发压缩。
- 在压缩失败时执行兜底 cursor 移动。

主 agent 不负责上下文管理。主 agent 只负责基于当前 `messages` 做决策。

### 2. Session Log

session log 是上下文恢复的权威来源。

每次构造主 agent 上下文时，`ContextManager` 应从当前 session 的 jsonl 文件读取事件：

```text
projects/{project_id}/sessions/{session_id}.jsonl
```

内存中的 SSE buffer 不作为上下文恢复来源。

### 3. Context Cursor

每个 session 应维护一个上下文 cursor。

cursor 表示当前可展开上下文窗口的起点：

```text
context_cursor_seq -> session 最新事件
```

早于 cursor 的历史由最近一次 `context_summary` 中的 `compressed_messages` 表达。

### 4. 当前上下文用量

每个 session 应维护当前上下文窗口的估算用量。

系统使用粗估 token 算法估算上下文用量；模型 tokenizer 可作为更精确的实现替换该估算器。

配置项：

```text
PATENT_CREATOR_CONTEXT_MAX_TOKENS
PATENT_CREATOR_CONTEXT_COMPRESS_THRESHOLD_RATIO
PATENT_CREATOR_CONTEXT_COMPRESSION_TIMEOUT
PATENT_CREATOR_CONTEXT_RESERVED_OUTPUT_TOKENS
PATENT_CREATOR_CONTEXT_RECENT_FULL_ROUNDS
```

示例：

```json
{
  "max_tokens": 128000,
  "used_tokens": 85000,
  "used_ratio": 0.66,
  "reserved_output_tokens": 8000,
  "status": "ok"
}
```

## 二、主 Agent 的 messages 恢复策略

主 agent 的 `messages` 应从当前 session log 投影恢复，而不是只构造一条 JSON user message。

推荐形态：

```text
system: 主 agent 系统提示词

user: 历史用户输入 1
assistant: 历史主 agent 最终回复 1

user: 历史用户输入 2
assistant(tool_calls): 主 agent 历史工具调用
tool: 历史工具返回结果
assistant: 历史主 agent 最终回复 2

user: 当前用户输入
```

说明：

- 历史对话应尽量按 OpenAI-compatible 多轮对话格式恢复。
- `scope=main` 的历史工具调用必须恢复为 `assistant(tool_calls)`，对应工具结果必须恢复为紧随其后的 `tool` message。
- 当前用户输入必须作为最后一条真实 `user` message。
- 主 agent 默认 `messages` 不注入项目标题、目录树或正文。
- 主 agent 需要项目标题和完整目录树时，通过 `document_read(action=get_project_context)` 获取。
- 压缩后的历史消息等系统生成信息不能伪装成用户原话。

## 三、Agent Scope 可见性

session log 是全量事件事实，但上下文恢复必须按 agent scope 投影。

### 1. 主 agent 可见

主 agent 可以进入上下文的内容：

- `scope=main` 的 `user_input`
- `scope=main` 的最终 `agent_output`
- `scope=main` 的 tool call 原始协议结构
- `scope=main` 的 tool result 原始返回结果
- `execute_subagent` 的调用及最终工具返回结果
- `context_summary`
- `document_read(action=get_project_context)` 的工具返回结果

说明：

- 主 agent 的工具结果不仅进入 session log，也进入后续主 agent 上下文。
- 这些结果按工具返回原文进入，不在跨轮恢复时改写成摘要。
- 它们会持续保留，直到上下文压缩或兜底 cursor 移动改变可见窗口。

### 2. 主 agent 不可见

主 agent 默认不应看到：

- 子 agent 内部的 `document_read`
- 子 agent 内部的 `exec_command`
- 子 agent 内部压缩过程
- 子 agent 的中间调试细节

这些事件进入 session log，用于 UI、debug 和审计，但不投影到主 agent 上下文。

### 3. 子 agent 可见

子 agent 每次调用都由上下文管理器装配独立 `messages`，不从 session log 自行恢复长期 messages。

子 agent 可见：

- 调用方当前可见且已闭合的 OpenAI-compatible `messages`。
- 子 agent 自己的 system prompt。
- 由 `agent_task` barrier 渲染出的本次任务说明 message。
- 本次子 agent run 内部的 tool call / tool result。

子 agent 不可见：

- 其他 session 的历史。
- 其他子 agent 的内部过程。
- 调用方的 system prompt。
- 未投影到调用方 `messages` 的 session raw events。
- 当前尚未闭合的 `execute_subagent` tool call。

已闭合 messages 指：消息前缀中的每个 `assistant.tool_calls[*].id` 都存在紧随其后的对应 `role=tool` 结果。上下文管理器只继承这个已闭合前缀，避免把正在启动当前子 agent 的未完成工具调用传入子 agent。

## 四、上下文窗口与压缩触发

每次用户输入后，推荐流程：

```text
1. 后端写入 user_input 到 session log。
2. ContextManager 从 session log 恢复 messages。
3. 估算 messages token 用量。
4. 如果未超阈值，直接调用模型。
5. 如果超阈值，触发 context_compressor。
6. 压缩完成后写入 context_summary 事件。
7. 更新 session cursor。
8. 重新恢复 messages。
9. 当前用户输入仍作为最后一条 user message。
```

当前触发压缩的用户输入不应被压缩掉。

压缩范围应截止到当前 user_input 之前：

```text
可压缩范围 = cursor 到 current_user_input 前一条可见消息
不可压缩范围 = current_user_input 及其之后本轮产生的消息
```

## 五、Context Summary

压缩结果应写回 session log，事件类型仍为 `context_summary`。

`context_summary` 保存一段压缩后的历史 transcript。该 transcript 不是逐字原文，可以在 message 数量、message 长度和不同角色承载的内容上重新组织，但必须保持 role 身份与语气一致：

- `role=user` 必须像用户自己在说话，不能写成“用户希望……”这类第三人称旁白。
- `role=assistant` 必须像 assistant 自己在说话，不能写成“助手认为……”这类第三人称旁白。
- 压缩后的 transcript 表达的是历史上下文，不是当前用户的新指令。

`compressed_messages` 是压缩器输出的中间结构。它不是最终发给模型的完整 OpenAI-compatible messages；其中的 `preserved_tool_call_ids` 会在恢复阶段被程序替换为标准 `assistant.tool_calls` 与紧随其后的 `tool` messages。

```json
{
  "agent_scope": "main",
  "covered_seq_start": 1,
  "covered_seq_end": 120,
  "cursor_seq_after": 121,
  "compressed_messages": [
    {
      "role": "user",
      "content": "我希望主 agent 的工具调用结果能跨轮进入上下文，而且压缩后的历史仍然像自然对话，不要变成机械摘要。"
    },
    {
      "role": "assistant",
      "content": "明白。我会把上下文压缩设计成 message-level 的历史重写：压缩旧 messages，保留自然对话语气，并让程序恢复必要的工具证据。"
    },
    {
      "role": "assistant",
      "preserved_tool_call_ids": ["call_000123"]
    },
    {
      "role": "assistant",
      "content": "我已经确认：压缩器只决定哪些工具调用需要保留，真正的工具参数和工具结果由程序按调用 ID 从原始历史中恢复。"
    },
    {
      "role": "user",
      "content": "【上下文说明】以上内容为系统压缩后的历史上下文，不是用户的新指令，也不是逐字原文。后续消息为未压缩的真实会话。"
    }
  ],
  "estimated_tokens_before": 62000,
  "estimated_tokens_after": 4200,
  "compression_model": "mimo-v2.5-pro",
  "warnings": []
}
```

必需字段：

- `covered_seq_start`
- `covered_seq_end`
- `cursor_seq_after`
- `compressed_messages`

观测字段：

- `estimated_tokens_before`
- `estimated_tokens_after`
- `compression_model`
- `warnings`

`compressed_messages` 必须满足以下约束：

1. 它是一段缩短后的历史 transcript，不是当前用户新指令。
2. 它只能包含 `role=user` 和 `role=assistant`。
3. `role=user` 必须使用用户自己的说话身份和自然语气。
4. `role=assistant` 必须使用 assistant 自己的说话身份和自然语气。
5. `role=user` 必须包含非空 `content`，不能包含 `preserved_tool_call_ids`。
6. `role=assistant` 必须二选一：包含非空 `content`，或包含非空 `preserved_tool_call_ids`。
7. `role=assistant` 不能同时包含 `content` 和 `preserved_tool_call_ids`。
8. `preserved_tool_call_ids` 中的 ID 必须来自待压缩窗口中真实存在且已闭合的工具调用。
9. 压缩器不能输出 `tool_calls`、工具参数、`role=tool` 或工具结果。
10. 最后一条 `compressed_messages` 必须由 `compressed_context` barrier 渲染生成，用于说明以上内容是系统压缩后的历史上下文，不是用户的新指令，也不是逐字原文。

## 六、Barrier Message

barrier 是上下文装配阶段的内部抽象，用于把结构化边界意图渲染成模型可见的 OpenAI-compatible message。

barrier 本身不是 event，也不是长期上下文恢复机制。恢复上下文时直接从 session log 投影 messages；只有已经渲染并写入 event 载荷的 message 会被恢复。

统一渲染结果：

```json
{
  "role": "user",
  "content": "自然语言说明"
}
```

### 1. compressed_context

用于压缩消息块的结尾。

barrier：

```json
{
  "kind": "compressed_context"
}
```

渲染结果：

```json
{
  "role": "user",
  "content": "【上下文说明】以上内容为系统压缩后的历史上下文，不是用户的新指令，也不是逐字原文。后续消息为未压缩的真实会话。"
}
```

`compressed_context` 的渲染结果写入 `context_summary.compressed_messages` 的最后一条。

### 2. agent_task

用于子 agent 启动时的任务边界。

barrier：

```json
{
  "kind": "agent_task",
  "task": "自然语言任务目标"
}
```

渲染结果：

```json
{
  "role": "user",
  "content": "【任务说明】以上内容是从调用方继承的历史上下文，用于理解背景，不是本次任务。你正在处理的子任务是：自然语言任务目标。请基于上述上下文完成该任务。"
}
```

`agent_task` 的渲染结果放在子 agent 初始 `messages` 的最后一条 user message。它只表达两件事：

1. 前面继承的 messages 是用于理解背景的历史上下文。
2. 当前子 agent 的执行目标是什么。

## 七、兜底裁剪策略

如果压缩失败，或压缩后仍然超限，系统应执行兜底 cursor 移动。

兜底策略：

```text
1. 估算当前 messages token。
2. 从窗口前部丢弃约 30% token 用量。
3. cursor 移动到第一条可用的 user message。
4. 当前用户输入永不丢弃。
5. 最近 N 轮可配置为强保留。
```

这里的“丢弃”不是删除 session log，而是移动上下文 cursor。

兜底裁剪后应写入 `context_pruned` 事件，便于 debug。

payload：

```json
{
  "agent_scope": "main",
  "old_cursor_seq": 1,
  "new_cursor_seq": 58,
  "reason": "compression_failed",
  "dropped_estimated_tokens": 24000,
  "first_visible_message_role": "user"
}
```

## 八、第一条消息边界

兜底切分后，第一条业务历史消息必须是 `user`。

允许的窗口形态：

```text
user
assistant
user
assistant
user(current)
```

或者：

```text
compressed_messages
user
assistant
user(current)
```

其中 `compressed_messages` 是系统压缩重建的早期历史。压缩块最后一条必须是 `compressed_context` barrier 渲染出的上下文说明；说明之后的消息才是未压缩的真实 session 历史。

跨轮恢复主 agent messages 时，必须恢复主流程历史原始 `assistant(tool_calls)` / `tool` 协议消息。

例外只有两类：

1. 事件在 `context_cursor_seq` 之前，已经被压缩或裁剪。
2. 事件属于 `scope=subagent:*`，是子 agent 内部过程。

## 九、工具结果压缩策略

正常运行时，工具结果可以进入当前 agent 的上下文。

执行上下文压缩时，压缩 agent 应能看到完整待压缩上下文，包括工具结果原文。这样它才能判断哪些工具调用对后续任务仍有价值，以及这些工具调用应位于压缩后消息块中的哪个语义位置。

压缩 agent 不输出工具调用结构、工具参数和工具结果。它只在压缩后的 transcript 中用 `preserved_tool_call_ids` 标记需要保留的工具调用：

```json
{
  "role": "assistant",
  "preserved_tool_call_ids": ["call_000123"]
}
```

恢复 `context_summary` 时，程序负责把该占位替换为标准 OpenAI-compatible 工具消息块。恢复后的 assistant 工具调用消息使用原始工具调用参数：

```json
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {
      "id": "call_000123",
      "type": "function",
      "function": {
        "name": "document_read",
        "arguments": "{\"action\":\"get_section\",\"section_id\":\"technical_solution\"}"
      }
    }
  ]
}
```

并紧随原始工具结果：

```json
{
  "role": "tool",
  "tool_call_id": "call_000123",
  "content": "原始工具结果"
}
```

恢复规则：

1. 遍历 `compressed_messages`。
2. 遇到 `role=assistant` 且包含 `preserved_tool_call_ids` 时，按 ID 从待压缩原始 messages 或 session log 中查找原始工具调用及对应工具结果。
3. 将该压缩消息替换为一条独立的 `assistant` 工具调用消息，`content` 固定为空字符串，`tool_calls` 使用原始工具调用结构。
4. 在其后紧跟对应 `role=tool` 消息，`content` 使用原始工具结果。
5. 如果找不到对应工具调用、工具结果，或工具调用未闭合，则本次压缩结果视为不可用，进入兜底裁剪。

工具是否保留不按工具类型硬编码。压缩 agent 应根据语义价值判断：当某个工具调用的原始结果对后续对话、任务完成、事实判断、文档或代码修改、用户意图延续有明显帮助，且压缩文本不足以可靠承载时，才保留该工具调用 ID。

## 十、Context Compressor

压缩上下文由 `context_compressor` 完成。

它的职责：

- 将调用方当前历史 messages 重写为更短的自然对话 transcript。
- 可以重新组织 message 数量、message 长度和不同角色承载的内容。
- 保持每条 message 的 role 身份和语气一致。
- 生成高密度、可继续工作的项目记忆，而不是逐轮聊天复盘。
- 大幅减少冗余文本，但不做极端压缩；优先保证后续任务连续性和关键信息保真。
- 保留后续任务需要的事实、用户偏好、决策、重要修改、事实核查结论、未解决问题和必要工具证据。
- 不编造新事实。
- 不把压缩后的历史消息伪装成用户新指令。
- 输出结构化的 `compressed_messages`。
- 当保留工具调用时，只输出 `preserved_tool_call_ids`，由程序恢复原始工具调用和工具结果。

压缩 agent 的输入应是结构化 JSON，至少包含：

```json
{
  "task": "compress_agent_context_messages",
  "current_user_message": {
    "content": "当前用户问题",
    "usage": "仅用于判断压缩重点，不得写入 compressed_messages。"
  },
  "rules": [
    "把 compressible_messages 重写成更短的自然对话 transcript。",
    "压缩输出只允许 role=user + content、role=assistant + content、role=assistant + preserved_tool_call_ids。",
    "不要逐轮复盘历史对话；合并重复意图、重复确认、相同结论和已经完成的执行细节。",
    "大幅减少冗余文本，但不要极端压缩；优先保证后续任务连续性和关键信息保真。",
    "覆盖已经出现且对后续有价值的项目/文档状态、用户稳定偏好、已确认技术方案、重要修改、事实核查和纠偏、未决问题、必要工具证据。",
    "role=user 只承载用户的目标、要求、偏好、纠偏和确认；不要把 assistant 已完成的工作写成 user 的话。",
    "role=assistant + content 承载 assistant 已完成的工作、判断、总结和后续注意事项。",
    "必须输出完整合法 JSON；信息较多时用高密度要点合并，避免输出到一半被截断。",
    "事实核查、版本号、来源、文档读取、代码检查类工具结果如后续可能追溯，优先保留 preserved_tool_call_ids。"
  ],
  "compressible_messages": [],
  "output_schema": {
    "compressed_messages": [
      {
        "role": "user|assistant",
        "content": "自然语言内容；role=user 必填；role=assistant 与 preserved_tool_call_ids 二选一",
        "preserved_tool_call_ids": ["role=assistant 保留工具调用时使用"]
      }
    ],
    "warnings": []
  }
}
```

输入规则：

1. `compressible_messages` 是 cursor 到当前用户输入之前的完整待压缩 message transcript。
2. `compressible_messages` 应包含完整工具结果原文，便于压缩 agent 判断是否保留对应工具调用。
3. `current_user_message` 只作为保留重点参考，不属于压缩范围，不得写入 `compressed_messages`。
4. 压缩 agent 不直接接收未投影的 `scope=subagent:*` 内部过程。
5. 压缩 agent 不接收比例预算字段；系统不把理想压缩比例作为模型可见的软限制或硬限制。

压缩 agent 的事件写入 session log，方便 debug。

但压缩 agent 的内部过程不进入调用方上下文；调用方只接收最终 `context_summary.compressed_messages`。

## 十一、子 Agent 的上下文压缩

子 agent run-local 压缩只服务于单次子 agent 调用。

- 主 agent 有 session cursor，可跨 round 恢复。
- 子 agent 只有 run-local cursor，不跨调用恢复。
- 子 agent 每次被主 agent 调用时都是全新上下文。
- 子 agent 内部压缩结果只服务于本次 run。
- 子 agent 压缩过程写入 session log，便于 debug。

## 十二、Context State

每个 session 的 context state 由 session log 中最近的 marker 事件恢复：

- `context_summary` 表示 cursor 之前的历史已被压缩为 `compressed_messages`。
- `context_pruned` 表示压缩失败或仍超限后，cursor 已移动到新的可见起点。

系统不额外维护独立 `.context.json` 文件，避免 session log 与 state 文件出现双写不一致。

恢复时以最近一条 `context_summary` 或 `context_pruned` 为准：

```text
context_summary -> 注入 compressed_messages，按 preserved_tool_call_ids 恢复原始工具调用与工具结果，并从 cursor_seq_after 继续展开后续事件
context_pruned  -> 不注入摘要，直接从 new_cursor_seq 继续展开后续事件
```

session log 继续保持 append-only，是上下文恢复的唯一权威来源。

## 十三、最终原则

1. session log 是权威事实源。
2. ContextManager 负责维护发送给模型的 `messages`。
3. 每个 session 有独立 cursor 和上下文用量。
4. 压缩后的历史消息不能伪装成用户新指令。
5. 当前用户输入永远作为最后一条真实 user message。
6. 主 agent 不接收子 agent 内部过程。
7. 子 agent run-local 压缩只作用于单次子 agent run。
8. 压缩失败不能阻塞主流程，必须有 cursor 移动兜底。
9. 兜底窗口第一条业务消息必须是 user。
10. 压缩 agent 可以看到完整待压缩上下文，但不得复写工具调用参数或工具结果；工具证据由程序按 `preserved_tool_call_ids` 恢复为原始工具调用与工具结果。
