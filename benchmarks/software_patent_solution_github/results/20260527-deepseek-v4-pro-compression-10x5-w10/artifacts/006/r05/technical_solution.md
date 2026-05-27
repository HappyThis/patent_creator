## 技术方案

### 问题背景与设计目标

在基于 Durable Object（DO）的 agent 对话系统中，AIChatAgent 作为服务端 agent 运行在 Cloudflare Workers 的 DO 内，通过 WebSocket 与浏览器客户端 useAgentChat 通信。DO 天然支持长生命周期和 SQLite 持久化——agent 对话一旦启动，即使所有客户端断开，DO 内部的推理、工具调用和状态更新仍可持续进行。

然而，现有客户端 hook useAgentChat 未明确区分两类性质不同的断开事件：（1）浏览器刷新、组件卸载、页面切换或网络短暂断开——这些是客户端本地生命周期清理，不应导致服务端 agent turn 终止；（2）用户主动点击「停止」按钮——这是明确的取消意图，应可靠传递到服务端并终止正在执行的 turn。当前实现中，客户端 Reader 的 cancel() 和 AI SDK useChat 的 stop() 仅关闭本地流，虽会尝试发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，但缺乏一套完整的语义区分机制和可配置策略。

本方案的设计目标是：在 AIChatAgent / useAgentChat 架构上引入 turn 所有权模式（Turn Ownership Mode），以 WebSocket 协议层的显式取消信令和 DO 侧的 AbortRegistry 为基础，将客户端本地生命周期清理与服务端 turn 取消明确分离。同时提供默认持久模式（durable）和可选请求生命周期模式（request-lifetime），兼容现有的流式响应、断线重连、工具继续执行和多标签页观察场景。

### 客户端生命周期与取消语义的分离机制

本方案的核心是将客户端事件分为三个独立通道，各自对应不同的服务端行为：

- 客户端本地清理通道：浏览器刷新、组件卸载（useEffect cleanup）、页面切换、Reader.cancel()、网络断开导致的 WebSocket close。这些事件触发客户端侧的 AbortController/Reader 清理和 React 状态重置，但不向服务端发送取消信令。服务端 agent turn 在 DO 内持续执行，不受影响。
- 显式取消通道：用户点击「停止」按钮触发 stop()，客户端通过 WebSocket 发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息（携带 requestId），服务端 AbortRegistry 收到后 abort 对应 AbortController，进而中断 onChatMessage 中的 LLM 推理流。同时，客户端通过 AI SDK 的 stop() 终止本地 Reader 并更新 UI 状态为 'ready'。
- 静默断开通道：网络短暂断开（如 Wi-Fi 切换）导致 WebSocket 断开但不触发组件卸载。此时客户端不发送取消信令，服务端 turn 继续执行。当网络恢复后，useAgentChat 的 resume 机制通过 CF_AGENT_STREAM_RESUME_REQUEST / CF_AGENT_STREAM_RESUMING / CF_AGENT_STREAM_RESUME_ACK 三次握手，从 ResumableStream 的 SQLite 缓冲区中回放错过的 chunk，实现无缝续流。

### Turn 所有权模式：持久模式与请求生命周期模式

为满足不同应用场景的偏好，系统在 AIChatAgent 和 useAgentChat 中引入 turnOwnership 配置项，支持两种模式：

- 持久模式（durable，默认）：服务端 turn 的所有权归属于 DO 自身。无论客户端连接状态如何变化——浏览器刷新、组件卸载、网络断开——只要 DO 未因空闲超时被回收，正在执行的 onChatMessage 及其工具调用链将持续运行至自然完成或收到显式 CF_AGENT_CHAT_REQUEST_CANCEL。此模式依赖 ResumableStream 的 SQLite chunk 缓冲机制：流式输出的每个 chunk 在生成时即持久化，客户端重连后通过 resume 握手回放。
- 请求生命周期模式（request-lifetime）：turn 的所有权绑定到发起请求的客户端连接。当 useAgentChat 所在组件的 useEffect cleanup 触发或 WebSocket 断开时，客户端自动发送 CF_AGENT_CHAT_REQUEST_CANCEL 到服务端。服务端 AbortRegistry.cancel(requestId) 中止正在执行的 LLM 推理，TurnQueue 的活跃条目被标记为完成。此模式适用于短对话、无恢复需求的场景，可节省 DO 计算资源。

两种模式的切换通过 useAgentChat 的 turnOwnership 选项控制。服务端 AIChatAgent 在处理 onChatMessage 时，通过 request 上下文中携带的 ownership 标志决定是否在连接断开时自动注入取消信号。模式选择在每次 sendMessage 时生效，同一 agent 实例的不同 turn 可使用不同模式。

### 取消信令传播路径

取消信令从用户点击「停止」到服务端 turn 实际中止的完整路径如下：

1. 用户点击停止按钮，触发 useAgentChat 返回的 stopWithToolContinuationAbort 方法。该方法首先调用 AI SDK useChat 的 stop()——终止本地 ReadableStream reader 并将 UI 状态置为 'ready'；然后在 finally 块中调用 customTransport.abortActiveToolContinuation()，确保若当前处于工具继续执行等待状态，该等待也被取消。
2. stop() 内部通过 WebSocket 发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，消息体包含 { id: requestId, type: 'cf_agent_chat_request_cancel' }。该消息通过 ws-chat-transport 的 onAbort 处理器发送。
3. 服务端 AIChatAgent 在 onMessage 协议分发中匹配到 CF_AGENT_CHAT_REQUEST_CANCEL 类型，调用 this._abortRegistry.cancel(data.id)。AbortRegistry 内部通过 Map<string, AbortController> 查找对应 requestId 的 AbortController 并调用其 abort() 方法。
4. onChatMessage 中通过 this._abortRegistry.getSignal(requestId) 获取的 AbortSignal 触发 abort 事件，正在执行的 streamText / generateText 等 AI SDK 调用接收到 abort 信号后停止 LLM 推理。
5. finally 块中 AbortRegistry.remove(requestId) 清理已完成的 AbortController；若 turn 被 abort，_runExclusiveChatTurn 返回 status: 'aborted'，TurnQueue 释放下一个排队的 turn。

关键设计点：AbortRegistry 是 DO 实例级别的内存结构，不持久化到 SQLite。如果 DO 在取消信令到达前被回收，该信令自然失效——但这与持久模式的语义一致：DO 回收意味着所有 turn 已自然终止。对于跨 DO 生命周期的取消需求（如父 agent 取消子 agent 的 turn），使用 AbortRegistry.linkExternal() 方法将外部 AbortSignal 链接到内部 AbortController。

### 与流式响应、断线重连及工具继续执行的兼容设计

本方案与现有以下机制完全兼容，无需修改核心执行路径：

流式响应兼容：持久模式下，客户端断开后 ResumableStream 继续将 chunk 写入 SQLite。当客户端重连并完成 resume 握手后，StreamAccumulator 通过 replay 标志批量应用缓冲 chunk，然后无缝切换到实时 chunk。replay 阶段使用 broadcastTransition 状态机管理 accumulator 生命周期，避免 UI 闪烁。

断线重连兼容：useAgentChat 的 resume 握手在 useEffect 中自动触发。服务端 onConnect 检查 _resumableStream.hasActiveStream() 状态，若存在活跃流则发送 CF_AGENT_STREAM_RESUMING 通知。客户端收到后发送 CF_AGENT_STREAM_RESUME_ACK，服务端通过 replayChunks 回放缓冲数据。在持久模式下，resume 握手独立于取消信令——重连不会发送取消消息，而是主动恢复流。

工具继续执行兼容：当 autoContinueAfterToolResult 为 true 时，客户端工具执行完毕后服务端自动调用 continueLastTurn()。continueLastTurn 内部调用 _runExclusiveChatTurn，将新 continuation turn 入队 TurnQueue。若用户在工具执行期间点击停止，stopWithToolContinuationAbort 中的 abortActiveToolContinuation() 会中止 ws-chat-transport 中的 _abortToolContinuation 回调，阻止工具继续执行的等待流程。服务端 AbortRegistry 中对应的 AbortController 也会被 abort，若 continuation turn 尚未开始则被 TurnQueue 自动跳过（generation 不匹配）；若已开始则通过 AbortSignal 中断。

### 多标签页协同与迟到消息处理

多标签页场景下，同一 DO 实例可能被多个浏览器标签页同时连接。所有连接共享同一个 AIChatAgent 实例和 DO 状态。本方案对此场景的处理如下：

- 持久模式下，所有标签页的 WebSocket 连接都是只读观察者——它们通过 broadcast 接收实时流式 chunk 和状态更新，但断开连接不影响服务端 turn 执行。只有一个标签页的「停止」操作会触发取消信令。
- 取消信令仅为主动发送方处理：CF_AGENT_CHAT_REQUEST_CANCEL 到达服务端后，AbortRegistry.cancel(requestId) 中止当前 turn。其他标签页通过 broadcast 收到 done: true 或 error 帧后更新 UI 状态。为避免多标签页竞态，取消信令发送时携带发起连接的 connectionId，服务端可记录发起取消的连接，避免重复广播取消事件。
- 迟到消息处理：当 DO 内的 turn 已完成（无论正常完成、被取消或出错），而某个标签页因网络延迟才刚建立连接时，服务端 onConnect 检查 _resumableStream.hasActiveStream() 为 false 且 _continuation.pending 为 null，直接发送 CF_AGENT_STREAM_RESUME_NONE。客户端收到后不启动 resume 流程，而是通过 getInitialMessages 从 SQLite 获取完整消息历史进行同步。

### 技术效果

本方案相比现有方式具有以下技术效果：

- 语义精确分离：首次在 DO agent 对话系统中明确区分客户端本地生命周期清理与显式服务端取消意图，消除'刷新即取消'的歧义，避免用户误操作导致长时间 agent 任务丢失。
- 可配置的执行策略：通过 turnOwnership 选项提供持久模式与请求生命周期模式，开发者可根据应用场景选择。默认持久模式保护有价值的 agent 推理不被意外中断；可选请求生命周期模式节省计算资源。
- 全链路取消可靠性：取消信令从客户端 stop() 到服务端 AbortRegistry.cancel() 再到 AI SDK AbortSignal 的完整链路，确保取消意图不会在中途丢失。ws-chat-transport 通过 keepId 标志确保取消后仍正确跳过服务端广播的残余 chunk。
- 与现有机制无侵入兼容：不修改 ResumableStream、TurnQueue、StreamAccumulator 和 broadcastTransition 的核心逻辑。取消信令复用现有 CF_AGENT_CHAT_REQUEST_CANCEL 协议消息，turn 所有权模式作为元数据附加在现有请求上下文中。
- 多标签页安全：取消操作仅影响发起取消的连接对应的 turn，其他观察标签页通过 broadcast 获得一致的状态更新。迟到连接通过 CF_AGENT_STREAM_RESUME_NONE + getInitialMessages 路径获取最终消息历史。

### 风险与待确认问题

以下为当前方案中需要后续确认的风险点和开放问题：

- DO 回收边界：持久模式依赖 DO 不被回收。DO 默认空闲超时约 70-140 秒。若客户端断开后 DO 在 turn 完成前被回收，turn 将丢失。可与现有 keepAlive / runFiber 机制集成——在持久模式下自动调用 keepAliveWhile() 包裹 onChatMessage 执行，确保 turn 期间 DO 不被回收；超长 turn（如 10 分钟以上）可结合 runFiber 实现断点续传。
- request-lifetime 模式与多标签页冲突：若标签页 A 以 request-lifetime 模式发起 turn，标签页 B 同时也在观察同一对话，A 的断开将取消服务端 turn，B 会看到一个意外的'被取消'状态。需在文档中明确建议：request-lifetime 模式适用于单标签页场景。
- 取消信令的竞态窗口：stop() 发送取消信令与 turn 自然完成之间存在竞态——取消信令到达时 turn 可能刚完成。AbortRegistry.cancel() 对已完成的 AbortController 调用 abort() 是安全的（no-op），但客户端需处理 stop() 后仍可能收到最终 done 帧的情况，当前 AI SDK 的 stop() 已处理此情形。
- 与 Think agent 的兼容性：本方案基于 AIChatAgent 设计。Think agent 使用相同的 AbortRegistry、TurnQueue 和 ResumableStream 共享层，turn 所有权模式的元数据传递路径需在 Think 的 chat() 和 _handleChatRequest 中相应适配。
