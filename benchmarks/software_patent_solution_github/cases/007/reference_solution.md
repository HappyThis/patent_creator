# 参考技术方案

## 方案概述

该方案面向父 agent 在单次会话处理中调度多个子 agent 的场景，提出一种保留式流式 agent 工具编排机制。系统将子 agent 的一次执行抽象为父 agent 下的可保留运行记录，使子 agent 在独立 Durable Object 中执行，同时将其生命周期事件和输出片段复用父 agent 的连接返回给客户端。客户端在同一主会话中展示子 agent 时间线，并可在刷新或重连后通过父侧记录和子侧持久流恢复执行过程。

## 核心组件

1. 父侧 agent 工具适配器

父 agent 提供工具包装接口，将某个 chat/Think 子 agent 类包装成可由模型调用的工具，也提供命令式调用接口供服务端流程主动触发。包装器负责生成运行标识、规范化输入、建立父工具调用与子运行的关联，并把子 agent 的最终摘要或结构化失败返回给父模型。

2. 子 agent 运行注册表

父 agent 持久化维护子运行注册表，记录运行 id、子 agent 类型、子 agent 名称、父工具调用 id、显示顺序、输入摘要、状态、创建时间、结束时间、错误信息和输出摘要。该注册表用于去重、恢复、访问控制、清理、UI 聚合和调试。

3. 独立子 agent 执行上下文

每个子 agent 运行映射到独立子 Durable Object 或等价隔离执行单元，拥有自身消息、工具、存储和流式输出能力。父 agent 不直接把子 agent 的内部状态混入自身对话，而是通过运行记录和事件转发关联两者。

4. 父连接事件转发层

子 agent 执行期间产生的开始、输出片段、状态变更、完成、失败、取消等事件，被封装为父会话可识别的 agent 工具事件，通过父 agent 的 WebSocket 或等价推送通道发送给客户端。事件包含运行 id、父工具调用 id、显示顺序、事件类型、片段内容和序号信息。

5. 客户端事件聚合器

客户端在主 agent 连接上订阅 agent 工具事件，按运行 id 去重，按父工具调用 id 分组，将子 agent 的输出片段累积为可渲染时间线。对于无父工具调用的命令式运行，客户端放入独立后台运行列表。

## 执行流程

1. 父 agent 接收用户请求并开始生成主对话响应。
2. 模型或服务端流程决定调用某个子 agent 工具。
3. 系统为本次子 agent 执行生成运行 id，并在父侧注册表中写入 starting/running 状态。
4. 父 agent 启动或连接对应子 agent 执行上下文，把规范化输入写入子 agent。
5. 子 agent 流式生成消息或工具执行结果。
6. 父连接事件转发层将子 agent 进度事件转为父会话事件，并推送给客户端。
7. 子 agent 完成后，父侧注册表写入 completed 状态和结果摘要，工具调用向父模型返回摘要。
8. 若子 agent 失败、被取消或父侧转发中断，则注册表写入 error/aborted/interrupted 状态，并向父模型返回结构化失败。
9. 客户端根据父工具调用 id 展示一个或多个子运行，并允许用户查看 retained 子 agent 详情。

## 恢复与重放机制

- 父侧注册表保留每个子运行的元数据，使刷新后客户端可以知道哪些子运行曾属于当前主会话或当前工具调用。
- 子 agent 自身保留流式消息或对话状态，支持从已持久化片段重放。
- 客户端重连后先加载父侧运行列表，再按运行 id 合并历史事件和后续实时事件。
- 对于实时事件与重放事件同时到达的情况，通过运行 id、片段序号或事件 id 去重。
- 对于父 agent 中途失去转发能力而子 agent 已不可继续跟踪的情况，系统将运行标记为 interrupted，避免客户端误以为其仍然正常完成。

## 并发与幂等

- 系统以运行 id 作为幂等键；相同运行 id 的重复调用不得启动重复子 agent。
- 一个父工具调用可以关联多个子运行，并通过显示顺序稳定渲染。
- 多个子运行可以并行执行，事件转发层按运行 id 隔离状态。
- 父 agent 清理会话时，可按状态、时间或父工具调用范围清理 retained 子运行。
- 清理仍在运行的子运行时，先发送取消信号，再删除运行记录或子执行上下文。

## 访问控制

用户从主会话进入子 agent 详情时，系统通过父侧注册表验证该子运行确实属于当前父 agent 和当前用户会话。若访问的子 agent 名称或运行 id 不存在于注册表，则拒绝访问，避免用户猜测子 agent 地址而创建或读取无关执行上下文。

## 兼容性

该方案不要求重写已有 chat/Think agent。对于支持服务端工具和保存消息的 agent，系统通过适配器将输入转为子 agent 可接受的消息形式，并将子输出转回父工具调用结果。对于依赖浏览器本地工具的子 agent，需通过服务端状态或父 agent 中介流程补充，不默认在无浏览器上下文中执行客户端工具。

## 目标能力边界

必须解决的是“父 agent 在一次主会话 turn 中保留式调度子 agent，并把子 agent 过程流回主会话 UI”。这不同于普通子 agent 路由，也不同于父 agent 同步调用一个函数。子 agent 应是可独立存储、可重放、可 drill-in、可取消/清理的运行实体。

方案不要求支持无限递归子 agent UI，也不必支持浏览器 client tool 在 headless 子 agent 中直接执行，但必须清楚说明这些边界。

## 核心数据结构与事件协议

父侧运行注册表至少应包含：

- `runId`、`childClassName`、`childName`。
- `parentToolCallId`、`displayOrder`、`parentMessageId`。
- `status`：`starting/running/completed/error/aborted/interrupted`。
- `inputSummary`、`outputSummary`、`error`。
- `requestId/streamId`、`createdAt/updatedAt/completedAt`。
- 清理相关字段：retention policy、deletedAt 或 tombstone。

事件协议至少覆盖：

- run started / metadata event。
- child output chunk，携带 run id、序号和可应用到 UI message part 的内容。
- run completed/error/aborted/interrupted。
- replay 标记或事件 id，用于客户端去重。

## 恢复、并发与取消细节

- 相同 run id 的重复调用不得启动第二个 child turn，应返回已有运行或其终态。
- 一个父工具调用可以并行关联多个 child run，客户端按 displayOrder 稳定展示。
- 父连接断开后，历史事件应能由父注册表和子 agent 持久流恢复；实时和 replay 事件要去重。
- 父侧转发 loop 丢失且无法重新接上 child live tail 时，运行应标记 interrupted，而不是假装 completed。
- 清理 retained run 时，如果 child 仍 running，应先取消 child，再删除注册表和 facet。
- drill-in 访问必须检查父注册表，未知 run id 不得唤醒或创建 child facet。

## 项目集成点

方案应连接 `agentTool` 包装、命令式 `runAgentTool`、Think 子适配、AIChatAgent 子适配、父 WebSocket 广播、React 聚合 hook、sub-agent 路由门禁和清理 API。只在示例里写 helper event 不足以成为框架级方案。

## 必须命中的评分锚点

- 子 agent 是独立执行上下文，不是普通函数。
- 父侧有 retained run registry。
- 子输出通过父连接事件回到同一主会话。
- 支持 replay/reconnect 和事件去重。
- 支持并行 fan-out、幂等 run id、取消和清理。
- 有 drill-in 访问控制。
- 兼容 Think 和 AIChatAgent 或说明适配边界。

## 常见错误方案

- 只让父 agent 调用一个 summarizer 函数，没有 retained child run。
- 子 agent 直接把消息写入父会话，缺少身份和事件隔离。
- 刷新后丢失子 agent 过程，只保留最终文本。
- 没有父侧注册表，无法访问控制和清理。
- 把 client tools 默认带入 headless child，忽略浏览器上下文缺失。

## 与源码实现的对应参考

该参考方案对应 `cloudflare/agents` 的 `Add retained streaming agent tools (#1421)`：

- 方案提交：`1b65ff5550f904e2a59bd6015703f82b02f85e4f`
- 快照提交：`06fb49b47ac19bd93d0a1064a03839d1187cb170`
- 关键能力：`agentTool()`、`runAgentTool()`、父侧运行注册表、`agent-tool-event`、React `useAgentToolEvents()`、Think 与 AIChatAgent 子适配、清理与取消逻辑。

## 等价机制说明

子运行身份、父子关联和事件协议可以使用不同命名；只要能稳定表达 runId/requestId/streamId 或等价映射、实时/重放事件去重、无父工具调用的后台运行、访问门禁和清理保留策略，应视为等价有效机制。
