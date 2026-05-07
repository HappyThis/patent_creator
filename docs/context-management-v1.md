# 上下文管理规范 v1

## 文档定位

本文档定义本项目中 `ContextManager` 的职责、session 上下文恢复、上下文窗口预算、压缩策略、兜底裁剪策略以及主 agent / 子 agent 的上下文可见性边界。

它建立在以下文档之上：

- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
- [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)
- [一轮内部时序 v1](/Users/yangchaoqun/myProj/patent_creator/docs/round-lifecycle-v1.md)

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
- 注入必要的项目上下文与文档状态。
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

cursor 之前的历史不再逐条恢复，而是由最近一次 `context_summary` 表达。

### 4. 当前上下文用量

每个 session 应维护当前上下文窗口的估算用量。

第一版可使用粗估 token 算法，后续再替换为模型 tokenizer。

建议配置：

```text
PATENT_CREATOR_CONTEXT_MAX_TOKENS
PATENT_CREATOR_CONTEXT_COMPRESS_THRESHOLD_RATIO
PATENT_CREATOR_CONTEXT_TARGET_RATIO
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

user: 以下是系统提供的项目上下文，不是用户的新指令：
      ...

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
- 项目上下文、压缩摘要等系统生成信息不能伪装成用户原话。
- 如果必须以 `role=user` 承载系统上下文，内容必须明确标注“不是用户的新指令，也不是用户原文”。

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
- 与当前文档状态相关的摘要

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

子 agent 每次调用都是全新上下文，不从历史 session 恢复长期 messages。

子 agent 可见：

- 主 agent 传入的任务。
- 根据 `call_type` 装配的局部文档上下文。
- 本次子 agent run 内部的 tool call / tool result。

子 agent 不可见：

- 其他 session 的历史。
- 其他子 agent 的内部过程。
- 主 agent 的完整历史 messages，除非通过 `forked_context` 明确传入压缩后的调用现场。

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

压缩结果应写回 session log，事件类型为 `context_summary`。

建议 payload：

```json
{
  "agent_scope": "main",
  "covered_seq_start": 1,
  "covered_seq_end": 120,
  "summary": "压缩摘要文本...",
  "estimated_tokens_before": 62000,
  "estimated_tokens_after": 4200,
  "preserved_tool_result_ids": [],
  "referenced_tool_result_ids": [],
  "absorbed_tool_result_ids": [],
  "compression_model": "deepseek-v4-pro",
  "cursor_seq_after": 121,
  "warnings": []
}
```

进入模型时，summary 应以明确的上下文消息出现：

```text
以下是系统从本 session 早期上下文压缩得到的摘要，不是用户的新指令，也不是用户原文：

...
```

## 六、兜底裁剪策略

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

建议 payload：

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

## 七、第一条消息边界

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
context_summary
user
assistant
user(current)
```

其中 `context_summary` 虽然在 API 中可能以 `role=user` 承载，但必须标注为系统压缩摘要，不是用户原话。

跨轮恢复主 agent messages 时，必须恢复主流程旧的原始 `assistant(tool_calls)` / `tool` 协议消息。

例外只有两类：

1. 事件在 `context_cursor_seq` 之前，已经被压缩或裁剪。
2. 事件属于 `scope=subagent:*`，是子 agent 内部过程。

## 八、工具结果压缩策略

正常运行时，工具结果可以进入当前 agent 的上下文。

但执行上下文压缩时，工具结果不应直接交给压缩 agent 原文处理。

压缩前应先编码工具结果：

```text
[tool_result_ref id=call_000001 tool=document_read status=success]
```

压缩 agent 输出结构化策略：

```json
{
  "summary": "压缩后的上下文摘要...",
  "preserved_tool_result_ids": ["call_000003"],
  "referenced_tool_result_ids": ["call_000001"],
  "warnings": []
}
```

三种模式：

- `absorbed`：结论已进入摘要，不保留原始工具结果。
- `referenced`：保留 ref，可按需从 session log 复核原文。
- `preserved`：尝试恢复原始工具结果进入上下文。

压缩 agent 只能看到工具调用 id、工具名、状态等元信息，不能看到工具返回原文。它必须根据上下文任务判断哪些工具结果需要继续原文保留，并把对应 `call_id` 写入 `preserved_tool_result_ids`。

恢复 `preserved` 原文时必须再次检查 token 预算。

如果恢复后超限，`preserved` 自动降级为 `referenced`。

## 九、Context Compressor

压缩上下文由一个特殊子 agent 完成。

建议声明：

```text
agent_id: context_compressor
call_type: forked_context
```

它的职责：

- 压缩调用方当前上下文。
- 保留后续任务需要的事实、用户偏好、决策、未解决问题。
- 不编造新事实。
- 不把系统摘要伪装成用户原话。
- 输出结构化压缩结果与工具结果保留策略。

压缩 agent 的事件应写入 session log，方便 debug。

但压缩 agent 的内部过程不进入调用方上下文；调用方只接收最终 `context_summary`。

## 十、子 Agent 的上下文压缩

子 agent run-local 压缩是目标能力，当前实现尚未接入。

区别：

- 主 agent 有 session cursor，可跨 round 恢复。
- 子 agent 只有 run-local cursor，不跨调用恢复。
- 子 agent 每次被主 agent 调用时都是全新上下文。
- 接入后，子 agent 内部压缩结果只服务于本次 run。
- 接入后，子 agent 压缩过程仍写入 session log，便于 debug。

## 十一、Context State

每个 session 的 context state 由 session log 中最近的 marker 事件恢复：

- `context_summary` 表示 cursor 之前的历史已被摘要吸收。
- `context_pruned` 表示压缩失败或仍超限后，cursor 已移动到新的可见起点。

当前实现不额外维护独立 `.context.json` 文件，避免 session log 与 state 文件出现双写不一致。

恢复时以最近一条 `context_summary` 或 `context_pruned` 为准：

```text
context_summary -> 注入摘要消息，并从 cursor_seq_after 继续展开后续事件
context_pruned  -> 不注入摘要，直接从 new_cursor_seq 继续展开后续事件
```

session log 继续保持 append-only，是上下文恢复的唯一权威来源。

## 十二、实现优先级

第一阶段：

1. 从 session log 恢复主 agent 多轮 user / assistant messages。
2. 当前 user query 作为最后一条 user message。
3. 排除子 agent 内部过程。
4. 支持 token 粗估与上下文统计。
5. 通过 session log marker 事件恢复 cursor。

第二阶段：

1. 实现超阈值压缩。
2. 实现 `context_summary` 事件。
3. 实现压缩失败后的 `context_pruned` 兜底。
4. 将压缩过程作为 fork 类型子 agent 的完整可观测 run 记录。

第三阶段：

1. 工具结果编码与三档策略。
2. 子 agent run-local 压缩。
3. 接入更准确 tokenizer。

## 十三、最终原则

1. session log 是权威事实源。
2. ContextManager 负责维护发送给模型的 `messages`。
3. 每个 session 有独立 cursor 和上下文用量。
4. 压缩摘要不能伪装成用户原话。
5. 当前用户输入永远作为最后一条真实 user message。
6. 主 agent 不接收子 agent 内部过程。
7. 子 agent run-local 压缩是后续能力，接入后不跨次恢复。
8. 压缩失败不能阻塞主流程，必须有 cursor 移动兜底。
9. 兜底窗口第一条业务消息必须是 user。
10. 工具结果压缩时先编码，再由压缩 agent 输出保留策略。
