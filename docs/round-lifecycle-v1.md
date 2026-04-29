# 一轮内部时序 v1

## 文档定位

本文档定义“用户一次输入到主 agent 完成一次响应”这一轮内部处理时序。

该文档用于统一以下内容的时序关系：

- 主 agent loop
- 子 agent loop
- tool 调用
- session 日志记录
- SSE 推送
- 文档写入
- git commit

相关文档：

- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)
- [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)
- [API 设计规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/api-design-v1.md)
- [前端交互规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/frontend-interaction-v1.md)

## 目标

本文档回答两个问题：

1. 一轮请求在系统内部是如何运行的
2. 文档修改、日志记录、SSE 推送和 git commit 在什么时机发生

## 一、回合定义

一轮定义为：

**用户一次输入，到主 agent 给出本轮最终回复，并完成本轮所有落盘与事件收束。**

也就是说，一轮的边界是：

```text
用户输入 -> 主 agent 完成本轮 -> round_finished
```

## 二、参与者

一轮内部涉及以下角色：

1. 用户
2. 主 agent
3. 子 agent
4. 执行器
5. 上下文管理器

一轮内部还会操作以下对象：

- `disclosure.json`
- `session log`
- `git`

## 三、总体时序

推荐的一轮内部时序如下：

```text
user_input
-> log(user_input)
-> main_agent loop
   -> tool_call / respond
   -> if tool_call:
      -> log(tool_call)
      -> sse(tool_call_started)
      -> execute
      -> if execute_subagent:
         -> subagent loop
         -> subagent tool calls/results
         -> subagent final result
      -> log(tool_result)
      -> sse(tool_call_finished)
      -> if document_edit succeeded:
         -> collect changed_section_ids / changed_block_ids
         -> sse(document_changed)
      -> context update
      -> continue main loop
-> 记录 final agent_output
-> if changed ids exist: git commit
-> sse(round_finished)
```

## 四、Phase 1：进入回合

1. 用户在 Agent Chat 区发送消息。
2. 前端调用 `POST /api/projects/{project_id}/chat/messages`。
3. 后端创建：
   - `round_id`
   - `message_id`
   - `session_id`，如果请求未提供
4. 后端把本轮 `user_input` 写入 session 日志。
5. 前端持续读取该 `POST /api/projects/{project_id}/chat/messages` 返回的 SSE 流。

并发约束：

- 同一 project 任意时刻只允许一个 session 处于执行中。
- 只要存在未完成的 round，新的消息请求直接拒绝，不进入排队。
- 当前活跃 session 定义为最近聊过天的 session。

## 五、Phase 2：主 agent 启动

1. 上下文管理器组装主 agent 本轮输入。
2. 输入包括：
   - 系统提示词
   - 当前用户输入
   - 默认上下文
   - 必要时补充的相关历史
3. 主 agent 开始本轮 loop。

主 agent loop 的基本模式为：

```text
思考 -> 输出结构化动作 -> 执行 -> 拿结果 -> 再思考
```

## 六、Phase 3：主 agent loop

主 agent 每一步只能做两类事：

1. `respond`
2. `tool_call`

### 情况 A：主 agent 直接回复

1. 主 agent 输出 `respond`。
2. 后端记录 `agent_output`。
3. SSE 推送主 agent 输出。
4. 回合结束。

### 情况 B：主 agent 调用工具

1. 主 agent 输出 `tool_call`。
2. 后端记录 `tool_call`。
3. SSE 推送 `tool_call_started`。
4. 执行器执行对应 tool。
5. tool 返回结果。
6. 后端记录 `tool_result`。
7. SSE 推送 `tool_call_finished`。
8. 上下文管理器把结果纳入主 agent 当前工作上下文。
9. 主 agent 继续下一轮 loop。

说明：

- `tool_result.status=failed` 表示该工具调用失败，但不等同于整个 round 失败。
- 工具失败结果应回填给主 agent，由主 agent 决定换一种方式、向用户说明限制，或结束本轮。
- 只有运行时异常、协议不合法、模型无法收束等不可恢复错误，才推送 `round_failed`。

## 七、如果 tool 是 `execute_subagent`

这是主流程中的调度工具。

### 主 agent 发起

1. 主 agent 发出 `tool_call(name=execute_subagent)`。
2. 执行器检查主 agent 权限。
3. 执行器启动对应子 agent。
4. 上下文管理器按 `call_type` 装配子 agent 上下文。

### 子 agent 输入

子 agent 接收：

- 自己的 system prompt
- `goal`
- `target_section_id`
- `target_block_id`
- 按 `call_type` 装配好的上下文

## 八、子 agent loop

子 agent 同样支持 agent loop。

但子 agent 的权限边界是：

- 可以多轮调用允许范围内的 tools
- 可以调用 `document_read`
- 不允许调用 `document_edit`
- 不允许调用 `execute_subagent`

因此，子 agent 内部时序如下：

1. 子 agent 开始 loop。
2. 如果需要工具：
   - 记录 `tool_call`，`scope=subagent:<id>`
   - SSE 推送 `tool_call_started`
   - 执行器执行 tool
   - 记录 `tool_result`
   - SSE 推送 `tool_call_finished`
3. 工具结果进入子 agent 工作上下文。
4. 子 agent 继续 loop。
5. 子 agent 最终输出统一 envelope：
   - `status`
   - `summary`
   - `proposal`
   - `questions`
   - `warnings`

当前实现要求：

- 子 agent 内部工具事件的 `parent_call_id` 指向主流程的 `execute_subagent` 调用。
- 子 agent 只能调用 `document_read` 和 `exec_command`。
- 子 agent 结束时必须返回合法 JSON envelope；不合法时本轮进入 `round_failed`。

### 子 agent 收束

1. 执行器把子 agent 最终结果包装成 `execute_subagent` 的 tool result。
2. 记录 `tool_result`，`scope=main`。
3. SSE 推送主流程视角的 `tool_call_finished`。
4. 主 agent 拿到这个结果，决定是否采纳 proposal。

## 九、文档修改时序

文档修改只通过 `document_edit` 发生。

当主 agent 决定采纳候选修改时：

1. 主 agent 发出 `tool_call(name=document_edit)`。
2. 执行器检查权限。
3. 执行器调用 `document_edit`。
4. `document_edit` 原子执行 operations。
5. `document_edit` 返回：
   - `changed_section_ids`
   - `changed_block_ids`
   - `operations_applied`
   - `primary_section_id`
   - `primary_block_id`
   - `change_scope`
6. 后端记录 `tool_result`。
7. 当前 round 累积 changed ids。

## 十、文档修改后的即时行为

文档一旦发生修改，建议立即刷新预览，不等整轮结束。

具体动作：

1. 后端推送 `document_changed`。
2. 前端刷新：
   - `outline`
   - `render`
3. 前端滚动到：
   - `active_section_id`
   - 或 `active_block_id`
   - 或最近修改位置
4. 前端对最近修改位置做短暂高亮。

`document_changed` 至少包含：

```json
{
  "changed": true,
  "changed_section_ids": ["technical_solution"],
  "changed_block_ids": ["blk_000014"],
  "primary_section_id": "technical_solution",
  "primary_block_id": "blk_000014",
  "change_scope": "block_appended",
  "active_section_id": "technical_solution",
  "active_block_id": "blk_000014"
}
```

## 十一、回合结束条件

主 agent 一轮结束，满足以下任一条件即可：

1. 主 agent 输出了最终 `respond`。
2. 主 agent 判断本轮任务已经完成。
3. 主 agent 判断当前需要等待用户补充信息，不能继续推进。

## 十二、回合收束

当主 agent 决定结束本轮时，执行以下动作：

1. 记录最终 `agent_output`。
2. 如果本轮 changed ids 非空：
   - 执行一次 `git commit`
3. 推送 `round_finished`。

如果 `git commit` 失败：

1. 不回滚已经写入的 `disclosure.json`。
2. 本轮仍然通过 `round_finished` 收束。
3. `committed` 置为 `false`。
4. 附带 `commit_error`。

`round_finished` 建议至少包含：

```json
{
  "reply": "本轮最终回复",
  "changed": true,
  "changed_section_ids": ["technical_solution"],
  "changed_block_ids": ["blk_000014"],
  "primary_section_id": "technical_solution",
  "primary_block_id": "blk_000014",
  "change_scope": "block_appended",
  "active_section_id": "technical_solution",
  "active_block_id": "blk_000014",
  "committed": false,
  "commit_error": {
    "code": "git_commit_failed",
    "message": "git commit 执行失败。"
  }
}
```

## 十三、日志与 SSE 的关系

建议明确区分：

### 1. session 日志

作用：

- 追求完整
- 便于回放
- 便于调试
- 便于审计

### 2. SSE

作用：

- 追求用户可感知
- 让前端知道当前正在做什么
- 让前端知道什么时候刷新目录和渲染区

说明：

- 不是所有日志内容都需要原样推给前端。
- 所有影响用户体验的重要状态变化，都应有对应 SSE 事件。
- 请求已被接受但本轮中途失败时，应推送 `round_failed`。

## 十四、回合中的多次 agent 输出

主 agent 在一轮中可以通过 `assistant_delta` 多次向前端输出自然语言片段。

最终必须有一个 `round_finished.reply` 作为本轮收束；`agent_output` 只写入 session log，用于历史恢复与回放。

这样可以兼顾：

1. 中途过程可见
2. 最终回合有明确结束语义

## 十五、当前结论

V1 的一轮内部时序采用以下原则：

1. 主 agent 支持 loop。
2. 子 agent 也支持 loop。
3. 子 agent 只允许多轮 tool use，不允许继续调 agent。
4. 子 agent 不允许调用 `document_edit`。
5. 文档修改只能通过 `document_edit` 发生。
6. 文档一旦修改，立即刷新渲染区。
7. git commit 只在本轮结束时执行一次。
8. session 日志追求完整，SSE 追求可感知。
9. 回合最终必须通过 `round_finished` 收束。
