# Session 事件日志 Schema v1

## 文档定位

本文档定义本项目 v1 阶段的 session 事件日志结构。

它建立在以下文档之上：

- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
- [子 Agent 定义 v1](/Users/yangchaoqun/myProj/patent_creator/docs/subagents-v1.md)
- [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)

本文档当前只定义：

- 日志文件格式
- 事件类型
- 公共字段
- 事件 payload 结构

本文档暂不定义：

- 日志文件命名规则
- 日志轮转策略
- 日志压缩策略

## 一、目标

session 事件日志用于记录：

- 用户输入
- 主 agent 输出
- tool 调用
- tool 返回结果
- 子 agent 的调用过程和最终结果

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

### 2. 子 agent 的过程不进入主 agent 上下文，但要进入日志

这是本 schema 的关键原则。

也就是说：

- 主 agent 继续推理时，不需要吃子 agent 的完整过程
- 但 session 日志必须保留子 agent 的执行过程，便于问题排查

### 3. 不单独记录 `context_update`

因为以下事件本身就会进入主 agent 上下文：

- `user_input`
- `agent_output`
- `tool_result`

因此无需额外定义单独的 `context_update` 事件类型。

## 三、文件格式

v1 建议使用：

- `jsonl`

即：

- 一行一个 JSON 事件
- append-only

这样最适合：

- 逐步追加
- 调试
- 回放

## 四、事件类型

v1 先只保留 4 类事件：

1. `user_input`
2. `agent_output`
3. `tool_call`
4. `tool_result`

说明：

- 子 agent 作为 `execute_subagent` 被调用，因此不再单独拆新的“子 agent 事件类型”
- 子 agent 过程通过 `scope` 字段和 `tool_call/tool_result` 记录

## 五、公共字段

所有事件统一带以下公共字段：

```json
{
  "id": "evt_001",
  "ts": "2026-04-23T15:30:00+08:00",
  "type": "user_input",
  "seq": 1,
  "scope": "main",
  "payload": {}
}
```

字段说明：

- `id`：事件唯一标识
- `ts`：事件时间
- `type`：事件类型
- `seq`：session 内顺序号
- `scope`：事件所属作用域
- `payload`：事件具体内容

## 六、scope 定义

`scope` 用于区分事件属于主 agent 还是某个子 agent。

v1 建议取值：

- `main`
- `subagent:material_analyst`
- `subagent:solution_refiner`
- `subagent:section_writer`
- `subagent:consistency_reviewer`

说明：

- 主 agent 相关事件使用 `main`
- 子 agent 相关事件使用 `subagent:<agent_id>`

这样可以做到：

- 主 agent 和子 agent 共用同一套事件类型
- 但日志层仍然能清楚区分调用来源

## 七、事件结构

### 1. user_input

用于记录用户输入。

示例：

```json
{
  "id": "evt_001",
  "ts": "2026-04-23T15:30:00+08:00",
  "type": "user_input",
  "seq": 1,
  "scope": "main",
  "payload": {
    "text": "我想写一个图像检测方向的专利交底书。"
  }
}
```

最小字段：

- `text`

### 2. agent_output

用于记录主 agent 面向用户的输出。

示例：

```json
{
  "id": "evt_002",
  "ts": "2026-04-23T15:30:05+08:00",
  "type": "agent_output",
  "seq": 2,
  "scope": "main",
  "payload": {
    "text": "我先帮你梳理这个方向。你更想强调检测精度，还是低算力实时性？"
  }
}
```

最小字段：

- `text`

说明：

- v1 中，`agent_output` 主要用于主 agent 的 UI 可见输出
- 子 agent 的最终结果原则上通过 `tool_result` 记录

### 3. tool_call

用于记录主 agent 或子 agent 发起的工具调用。

示例：

```json
{
  "id": "evt_003",
  "ts": "2026-04-23T15:30:10+08:00",
  "type": "tool_call",
  "seq": 3,
  "scope": "main",
  "payload": {
    "tool": "execute_subagent",
    "arguments": {
      "agent_id": "material_analyst",
      "goal": "从当前用户输入中提炼技术方向、目标和待确认信息。",
      "call_type": "task_only_specialist"
    }
  }
}
```

最小字段：

- `tool`
- `arguments`

说明：

- `scope=main` 表示这次调用由主 agent 发起
- `scope=subagent:<id>` 表示这次调用由某个子 agent 发起

### 4. tool_result

用于记录工具返回结果。

示例：

```json
{
  "id": "evt_004",
  "ts": "2026-04-23T15:30:12+08:00",
  "type": "tool_result",
  "seq": 4,
  "scope": "main",
  "payload": {
    "tool": "execute_subagent",
    "status": "success",
    "output": {
      "agent_id": "material_analyst",
      "result": {
        "status": "success",
        "output": "当前主题可归纳为图像检测方向，用户倾向于围绕低算力实时性展开，仍需确认现有技术缺陷和目标场景。"
      }
    }
  }
}
```

最小字段：

- `tool`
- `status`
- `output`

说明：

- `tool_result` 记录的是工具返回
- 当工具是 `execute_subagent` 时，`output` 中包含子 agent 最终结果

## 八、子 agent 过程的记录方式

子 agent 的过程虽然不进入主 agent 上下文，但应完整进入日志。

例如：

1. 主 agent 调用 `execute_subagent`
2. 记录一条 `tool_call`，`scope=main`
3. 子 agent 内部如果再调工具，则继续记录：
   - `tool_call`，`scope=subagent:<agent_id>`
   - `tool_result`，`scope=subagent:<agent_id>`
4. 子 agent 返回最终结果时，记录：
   - `tool_result`，`scope=main`，`tool=execute_subagent`

这样可以同时满足：

- 主 agent 上下文保持干净
- 子 agent 过程完整可追踪

## 九、完整示例

```jsonl
{"id":"evt_001","ts":"2026-04-23T15:30:00+08:00","type":"user_input","seq":1,"scope":"main","payload":{"text":"我想写一个图像检测方向的专利交底书。"}}
{"id":"evt_002","ts":"2026-04-23T15:30:05+08:00","type":"agent_output","seq":2,"scope":"main","payload":{"text":"我先帮你梳理这个方向。你更想强调检测精度，还是低算力实时性？"}}
{"id":"evt_003","ts":"2026-04-23T15:30:10+08:00","type":"tool_call","seq":3,"scope":"main","payload":{"tool":"execute_subagent","arguments":{"agent_id":"material_analyst","goal":"从当前用户输入中提炼技术方向、目标和待确认信息。","call_type":"task_only_specialist"}}}
{"id":"evt_004","ts":"2026-04-23T15:30:11+08:00","type":"tool_call","seq":4,"scope":"subagent:material_analyst","payload":{"tool":"exec_command","arguments":{"command":"ls"}}}
{"id":"evt_005","ts":"2026-04-23T15:30:11+08:00","type":"tool_result","seq":5,"scope":"subagent:material_analyst","payload":{"tool":"exec_command","status":"success","output":{"command":"ls","exit_code":0,"stdout":"docs\n","stderr":""}}}
{"id":"evt_006","ts":"2026-04-23T15:30:12+08:00","type":"tool_result","seq":6,"scope":"main","payload":{"tool":"execute_subagent","status":"success","output":{"agent_id":"material_analyst","result":{"status":"success","output":"当前主题可归纳为图像检测方向，用户倾向于围绕低算力实时性展开，仍需确认现有技术缺陷和目标场景。"}}}}
```

## 十、当前结论

v1 的 session 事件日志 schema 先采用：

- 文件格式：`jsonl`
- 事件类型：`user_input`、`agent_output`、`tool_call`、`tool_result`
- 公共字段：`id`、`ts`、`type`、`seq`、`scope`、`payload`

其中：

- 主 agent 过程通过 `scope=main` 记录
- 子 agent 过程通过 `scope=subagent:<agent_id>` 记录
- 子 agent 过程需要完整记录，但不进入主 agent 上下文
