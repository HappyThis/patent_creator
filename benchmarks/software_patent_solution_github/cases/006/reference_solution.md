# 隐藏参考方案

## 技术问题

`useAgentChat` 支持通过 WebSocket 接收 agent 的流式响应，并已具备断线重连和 stream resume 能力。对于运行在 Durable Object 中的 agent turn，浏览器组件卸载、页面刷新、React 清理、网络断开或本地 stream cancel 并不一定代表用户希望服务端停止推理。若把所有客户端 abort 都直接映射为服务端取消，会破坏长运行 turn 的 durable/resumable 语义，导致刷新页面或暂时断线时服务端任务被误杀。

但如果客户端永远只做本地清理，用户点击明确的停止按钮时又无法真正取消服务端执行，造成资源浪费和错误的用户反馈。工具调用 continuation、跨标签页恢复和 fallback observer 等路径也可能让服务端 turn 不完全由当前 transport stream 持有。因此需要一种区分客户端本地清理和服务端显式取消的控制机制。

## 核心技术构思

在 `useAgentChat` 的 WebSocket transport 中建立“本地 stream 生命周期与服务端 turn 生命周期解耦 + 显式取消通道 + 可配置 abort 策略 + 活动 turn 跟踪”的 durable cancellation 机制。

系统默认把普通客户端 abort、组件卸载和 stream cancel 视为本地清理，只关闭当前客户端的可读流和监听器，不向服务端发送取消指令，使服务端 turn 可以继续运行并被后续 resume 重新接上。系统同时维护活动服务端 turn 标识，当用户执行明确停止操作或应用选择 request-lifetime 语义时，才通过独立取消消息向服务端发送取消请求。对于工具调用 continuation、恢复观察路径等非当前 stream 持有的 turn，系统也能记录或观察其 turn id，并通过统一取消入口传递取消意图。

## 必要技术特征

1. 活动 turn 跟踪：transport 记录当前服务端 turn 的 request id，以及与该 turn 关联的本地 stream 清理函数。
2. 本地清理语义：默认情况下，客户端 abort 或 stream cancel 只终止本地 ReadableStream 和监听器，不发送服务端取消帧。
3. 显式取消入口：提供独立的 server turn cancellation 方法，用于用户点击停止或应用明确取消时发送服务端取消消息。
4. 可配置策略：允许开发者选择客户端 abort 是否同时取消服务端 turn，以支持 request-lifetime 或节省 token 的场景。
5. 取消帧协议：显式取消时通过 WebSocket 向服务端发送包含 request id 的取消消息。
6. 活动 id 保留：显式取消后在合适时机保留或清理 active request id，避免后续服务端完成帧或残余 chunk 被错误处理。
7. 工具 continuation 处理：当客户端工具结果会触发服务端继续流时，transport 能跟踪并取消该 continuation。
8. fallback / observed turn 处理：对于不是当前 transport stream 直接创建但由 hook 观察到的服务端 turn，也可注册并显式取消。
9. resume 兼容：本地清理后服务端 turn 继续运行，后续重连可通过既有 resume 流程接收缓冲或继续流式输出。

## 关键流程

### 默认本地清理流程

1. 客户端通过 `useAgentChat` 发起一次服务端 agent turn。
2. transport 创建 request id 并登记为活动服务端 turn。
3. 浏览器刷新、组件卸载或本地 stream cancel 触发客户端 abort。
4. 系统关闭本地 ReadableStream、移除监听器并清理本地请求状态。
5. 系统不发送服务端取消消息，服务端 turn 继续运行。
6. 客户端重新连接时，通过 stream resume 机制恢复服务端输出。

### 显式取消流程

1. 用户点击停止或应用调用显式取消入口。
2. transport 查找当前活动服务端 turn id。
3. 系统通过 WebSocket 发送服务端取消消息。
4. 本地 attached stream 被终止，并根据策略保留 request id 直到服务端最终消息被处理。
5. 服务端收到取消请求后终止对应 turn。

### 可配置 abort 策略流程

1. 应用初始化 chat hook 时声明是否希望客户端 abort 取消服务端 turn。
2. 当客户端 abort 发生时，transport 读取该策略。
3. 若策略为本地清理，则只关闭本地 stream。
4. 若策略为服务端取消，则发送取消帧并终止本地 stream。
5. 显式停止始终走服务端取消，不受默认策略影响。

## 技术效果

- 页面刷新、组件卸载或短暂网络中断不会误杀 Durable Object 中正在执行的长运行 agent turn。
- 用户仍然拥有明确的停止能力，可以真正取消服务端执行。
- 应用可以根据使用场景选择“durable turn”或“request lifetime”语义。
- stream resume、工具 continuation 和多标签页观察路径与取消逻辑保持一致。
- 减少长任务误取消、资源泄漏和用户界面状态误导。

## 目标能力边界

必须解决的是客户端本地 stream 生命周期与服务端 agent turn 生命周期的解耦。默认组件卸载、页面刷新或本地 ReadableStream cancel 不应等价于服务端取消；用户明确点击停止或应用调用显式取消入口时，才应取消服务端 turn。

方案可以允许应用配置“客户端 abort 是否取消服务端”，但必须区分默认 durable 语义和 request-lifetime 语义。若答案把所有 abort 都取消服务端，或所有 abort 都只本地清理，均不完整。

## 核心状态与协议

- 客户端 transport 维护 `activeRequestId` 或等价 server turn id。
- 维护 attached local stream cleanup，用于取消当前本地 reader/stream/listener。
- 配置项类似 `cancelOnClientAbort`，默认 durable 模式为 false。
- 显式取消方法类似 `cancelActiveServerTurn()`，不依赖组件卸载触发。
- WebSocket 取消消息应携带 server turn id，避免取消错误 turn。
- 工具 continuation 或 fallback observer 若产生新的 server turn，也要更新或登记 active id。

状态区分：

- `local_attached`：当前页面连接正在消费服务端输出。
- `server_running`：Durable Object 内 turn 仍在执行。
- `local_detached/server_running`：刷新或卸载后的正常 durable 状态。
- `server_cancel_requested`：显式停止后等待服务端终态。
- `server_terminal`：完成、错误或取消。

## 关键边界处理

- 本地 stream cancel 时应释放 reader、listener、pending promise，避免前端资源泄漏。
- 默认本地清理不发送取消帧，允许后续 resume 获取 buffered chunks。
- 显式取消即使没有 attached stream，也应尝试使用记录的 active id 发送服务端取消。
- 多标签页场景下，一个标签页卸载不应取消其他标签页仍可观察的服务端 turn。
- 当服务端完成帧在显式取消后迟到时，客户端应按 request id 判断是否处理或忽略。
- 若配置为 request-lifetime 模式，普通 client abort 可以发送取消，但该策略必须是显式配置。

## 项目集成点

方案应落在 WebSocket chat transport、React `useAgentChat` stop/abort 行为、resume 测试、tool continuation 和文档说明。只改 UI stop 按钮不够；必须在 transport 层改变 abort 与 server cancel 的映射。

## 必须命中的评分锚点

- 明确区分本地清理和服务端取消。
- 默认刷新/卸载不杀 durable turn。
- 有显式服务端取消入口和取消帧。
- 有 active server turn id 跟踪。
- 有可配置 abort 策略。
- 兼容 resume、continuation、多标签页或 observer 场景。

## 常见错误方案

- 把 `AbortController.abort()` 直接等价为服务端 cancel。
- 只说“断线重连恢复”，没有明确用户停止如何取消服务端。
- 只在 UI 层隐藏 stop，transport 仍会误杀服务端 turn。
- 没有 request id，取消可能作用于错误 turn。
- 不考虑工具结果 continuation 触发的后续服务端流。

## 对应真实实现

真实 PR #1484 采用了如下实现方向：

- 在 `WebSocketChatTransport` 中增加 `cancelOnClientAbort` 策略，默认关闭。
- 增加活动服务端 turn id、attached stream cancel 函数和显式 `cancelActiveServerTurn()`。
- 显式取消时发送 `CF_AGENT_CHAT_REQUEST_CANCEL` 消息。
- 默认 abort / stream cancel 仅做本地 stream cleanup，使服务端 turn 可继续并支持 resume。
- 在 `useAgentChat` 和文档中暴露策略说明，明确 explicit stop 始终取消服务端 turn。
- 增加 cancellation policy、abort、resume 和工具 continuation 相关测试。
