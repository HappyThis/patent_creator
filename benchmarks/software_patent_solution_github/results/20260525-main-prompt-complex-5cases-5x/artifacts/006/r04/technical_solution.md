## 技术方案

本方案针对基于 WebSocket 的 AI Agent 对话系统中，客户端生命周期事件（浏览器刷新、组件卸载、reader cancel、网络断开等）与服务端 agent turn 取消意图之间的语义混同问题，提出一种"生命周期-取消语义分离"的技术方案。核心思路是：在 WebSocket 传输层引入可配置的 turn 生命周期模式（durable 模式与 request-lifetime 模式），将客户端本地流清理（reader.cancel、组件卸载时的流关闭）与用户显式取消意图（点击"停止"按钮触发 stop()）解耦为两条独立的控制路径。在默认 durable 模式下，客户端断开不自动取消服务端 Durable Object 中的执行；仅当应用显式调用 stop() 时，才通过专用协议消息向服务端传达取消意图。该方案兼容现有的流式响应、断线重连、工具调用 continuation 和多标签页场景，并为开发者提供偏好配置空间。

### 核心技术问题

在基于 Durable Object 的 AI Agent 对话系统中，服务端 agent turn 的执行生命周期独立于客户端 WebSocket 连接生命周期。然而现有实现中，客户端传输层的 ReadableStream.cancel() 与显式 stop() 调用共享同一 onAbort 处理路径：均向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 协议消息，触发 AbortRegistry.cancel() 从而终止服务端执行。这导致浏览器刷新、React 组件卸载、页面切换或网络短暂断开时，服务端正运行的 agent turn 被意外取消，浪费已在 Durable Object 中进行的推理计算，且用户刷新后无法通过流恢复机制继续接收结果。

### 传输层模式隔离机制

方案在 WebSocketChatTransport 中引入一个 turnLifetime 配置选项，取值为 "durable"（默认）或 "request-lifetime"。该选项控制客户端传输层在流终止时是否将取消意图传播到服务端。

durable 模式（默认）：客户端断开、reader.cancel()、组件卸载导致的流关闭仅执行本地清理——关闭本地 ReadableStream、移除事件监听器、清理 activeRequestIds 集合——但不向服务端发送 CHAT_REQUEST_CANCEL。服务端 Durable Object 中的 agent turn 继续执行，已流式输出的分块（chunk）通过 SQLite 持久化，供客户端重连后通过 STREAM_RESUME_REQUEST / STREAM_RESUMING 协议恢复。仅当应用显式调用 stop()（映射为 AI SDK 的 stop 函数）时，才通过 WebSocket 发送 CHAT_REQUEST_CANCEL，触发服务端 AbortRegistry.cancel()。

request-lifetime 模式：与现有行为一致。任何导致流终止的客户端事件（包括组件卸载、reader cancel、网络断开等）均向服务端发送 CHAT_REQUEST_CANCEL。该模式适用于希望客户端生命周期与服务端执行严格绑定的应用场景。

### 客户端生命周期语义分层

方案将客户端侧的流终止事件划分为两类语义，在 WebSocketChatTransport.sendMessages() 的 finish/onAbort 闭包中实现分流处理。

第一类——本地生命周期清理：包括浏览器刷新（beforeunload）、React 组件卸载（useEffect cleanup 中移除事件监听器）、ReadableStream.cancel()（AI SDK 内部在新请求到达或组件卸载时自动调用）、网络断开导致 WebSocket 关闭。这些事件触发 finish() 闭包，但 finish() 根据 turnLifetime 模式决定是否执行服务端通知分支。在 durable 模式下，finish() 跳过 CHAT_REQUEST_CANCEL 的发送，仅完成本地资源回收：设置 completed=true、从 activeRequestIds 删除 requestId、abort 本地 AbortController。

第二类——显式取消意图：用户点击 UI 中的"停止"按钮，或应用代码调用 stop()。该路径在调用 AI SDK 的 stop() 之后，最终触发 WebSocketChatTransport 的 onAbort 回调。onAbort 无条件发送 CHAT_REQUEST_CANCEL 消息到服务端——不受 turnLifetime 模式影响。这确保显式取消意图始终到达服务端。

为实现上述分流，在 WebSocketChatTransport 中新增内部方法 cancelRequestToServer(requestId)，该方法的调用受 turnLifetime 守卫。finish() 通过模式判断是否调用 cancelRequestToServer；onAbort() 始终调用。

### 服务端取消意图处理

服务端通过 AbortRegistry（位于 agents/chat/abort-registry.ts）管理每个 agent turn 的 AbortController。当收到 CHAT_REQUEST_CANCEL 消息时，AIChatAgent.onMessage 调用 AbortRegistry.cancel(requestId)，触发对应 AbortSignal 变为 aborted 状态。该 signal 通过 OnChatMessageOptions.abortSignal 传递给 onChatMessage 中的 streamText 调用，使 AI SDK 推理循环终止。

服务端不因单个 WebSocket 连接关闭而主动取消正在执行的 turn。AIChatAgent.onClose 仅清理该连接相关的簿记状态（pendingResumeConnections、continuation 记录），不影响 AbortRegistry 中的活跃控制器。Durable Object 的生命周期（由 Cloudflare 运行时管理）与客户端连接生命周期完全解耦。仅当 Durable Object 自身被销毁（destroy()）时，才调用 AbortRegistry.destroyAll() 清理所有控制器。

取消后的部分结果处理：即使 turn 被取消，已流式输出并持久化到 SQLite 的分块仍然保留。onChatResponse 回调中 status 字段被设置为 "aborted"，开发者可据此实现自定义后处理（如标注该轮对话被取消，或保留部分输出供用户参考）。

### 与可恢复流及工具 Continuation 的兼容

方案与现有 ResumableStream 机制完全兼容。在 durable 模式下，浏览器刷新后 useAgentChat 重新挂载，WebSocketChatTransport.reconnectToStream() 发送 STREAM_RESUME_REQUEST，服务端通过 ResumableStream 检测活跃流后回复 STREAM_RESUMING，客户端回复 STREAM_RESUME_ACK 后进行分块重放和实时续传。由于服务端 agent turn 未被取消，ResumableStream 的状态保持为 "streaming"，恢复流程不受影响。

工具调用 continuation 兼容：当 agent turn 中需要客户端工具执行时，服务端暂停流式输出等待工具结果。在 durable 模式下，即使客户端在此期间刷新页面或断开连接，服务端 turn 保持活跃，等待工具结果超时机制独立运作。客户端重连后，通过 onAgentMessage 中的 CF_AGENT_MESSAGE_UPDATED 处理工具状态更新，并通过 STREAM_RESUMING 机制恢复工具 continuation 的流式输出。

### 多标签页场景与迟到消息处理

多标签页场景中，每个标签页通过独立的 WebSocket 连接到同一 Durable Object 实例。方案通过以下机制保证多标签页下的语义正确性：

（1）取消操作的作用域：每个标签页的 stop() 调用仅取消该标签页发起的 agent turn（通过 requestId 匹配），不影响其他标签页的独立 turn 或同一 turn 在其他标签页上的观察流。服务端 AbortRegistry 以 requestId 为键，cancel() 操作精确作用于对应控制器。

（2）跨标签页观察：当标签页 A 在 durable 模式下因刷新断开时，标签页 B 通过 onAgentMessage 中的 CF_AGENT_USE_CHAT_RESPONSE 广播继续接收服务端流式输出，isServerStreaming 状态保持为 true。这确保多标签页场景下至少有一个观察者能完整接收输出。

（3）迟到服务端消息处理：当标签页 A 在 durable 模式下断开后重新连接，服务端可能在重连窗口内完成了 agent turn 并广播了 final done:true 消息。标签页 A 重连后通过 STREAM_RESUME_REQUEST 查询服务端状态：若 ResumableStream 状态为 "completed"，服务端通过已持久化的 completed chunks 重放完整历史分块并标记 replayComplete:true；若无活跃流，服务端回复 STREAM_RESUME_NONE，客户端通过 getInitialMessages 拉取持久化消息。

### 技术效果

（1）避免误取消：浏览器刷新、组件卸载、网络抖动等客户端生命周期事件不再导致服务端 Durable Object 中正在执行的 agent turn 被意外终止，减少推理计算浪费，提升系统韧性。

（2）语义清晰化：显式区分"用户想停止本次回答"与"客户端暂时离开但希望服务端继续"两种意图，API 语义与用户心智模型对齐——stop() 真正停止，页面关闭不影响服务端。

（3）开发者可配置：通过 turnLifetime 选项，不同应用可根据自身场景选择 durable 或 request-lifetime 模式。例如，实时协作应用选择 durable 以保持会话连续性；一次性查询应用选择 request-lifetime 以自动回收服务端资源。

（4）兼容现有流程：方案不改变现有协议（CHAT_REQUEST_CANCEL、STREAM_RESUME_REQUEST 等消息类型保持不变），不破坏流式响应、断线重连、工具 continuation、多标签页广播等已有机制。现有使用 request-lifetime 行为（即默认关闭 durable）的应用无需任何修改。

### 风险与待确认问题

（1）Durable 模式下的资源占用：服务端 agent turn 在客户端断开后继续执行，可能长时间占用 Durable Object 的计算资源和内存。需确认 Cloudflare Durable Object 的 wall-clock 执行限制（当前 30 秒 CPU 时间）是否足够覆盖典型断开-重连窗口。建议结合现有 runFiber 机制确保长时间执行的 turn 具备检查点恢复能力。

（2）取消延迟感知：在 durable 模式下，用户关闭标签页后若想真正取消服务端 turn，需通过其他标签页或 API 调用发出显式取消。需评估是否需要在服务端增加超时自动取消机制（如客户端断开超过 N 分钟后自动取消 turn）。

（3）AI SDK 兼容性：方案依赖 AI SDK 的 useChat 内部 stop() 与 ReadableStream.cancel() 的分流，需确认 AI SDK 版本中 stop() 和 stream.cancel() 确实走不同的代码路径。若 AI SDK 在某些条件下通过 stream.cancel() 实现 stop()，需调整分流策略。

（4）turnLifetime 选项的暴露层级：该选项可作为 useAgentChat 的新参数暴露给应用开发者，也可作为 AIChatAgent 的服务端配置。需确认哪种暴露方式更符合当前 API 设计惯例（参考已有 resume、autoContinueAfterToolResult 等客户端选项）。
