## 技术方案

本技术方案针对基于 Durable Object 的 agent 对话系统中，客户端生命周期清理事件（浏览器刷新、组件卸载、ReadableStream reader cancel、网络断开）与服务端 agent turn 取消意图之间的语义混淆问题，提出一种可配置的取消语义区分机制。方案在客户端 stop 调用路径中引入显式的服务端取消消息发送，并通过 durability mode 配置项让开发者控制「客户端断开是否等同于取消服务端任务」，在保持 Durable Object 默认 durable 语义的同时，兼容流式响应恢复、工具调用继续执行、多标签页观察和迟到服务端消息处理等既有机制。

### 技术问题与语义区分

在基于 WebSocket 的 agent 对话系统中，客户端与 Durable Object（DO）服务端通过 WebSocket 连接交换消息。当前系统存在两类不同语义的「停止」事件被混为一谈的问题：

- 本地生命周期清理：浏览器刷新、React 组件卸载、ReadableStream reader.cancel() 调用、网络短暂断开等事件导致客户端本地连接关闭或读取中断。这些事件的本质是客户端资源回收，不应自动推导出「用户希望取消服务端正在执行的 agent 推理」。
- 服务端 turn 取消：用户点击「停止生成」按钮或应用层主动调用取消方法，表达的意图是希望终止当前正在服务端执行的 agent turn，包括中止 LLM 推理流、停止工具调用链、并向所有观察该对话的标签页广播取消结果。

当前系统中，useAgentChat 导出的 stop() 方法仅执行本地 reader 取消（通过 AI SDK useChat 内置的 stop），同时调用 WebSocketChatTransport.abortActiveToolContinuation() 中止工具继续执行流，但并未向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。服务端的 AbortRegistry 已具备按 requestId 取消 AbortController 的能力，且 onMessage 处理器已注册了对 CF_AGENT_CHAT_REQUEST_CANCEL 消息类型的响应（调用 _abortRegistry.cancel 并触发 message:cancel 事件），但客户端缺少将用户取消意图编码为该消息类型并发送的路径。同时，浏览器刷新或组件卸载时 WebSocket 连接断开，DO 因其 durable 特性继续执行——这是合理默认行为，但现有系统未给开发者提供将「连接断开」映射为「取消」的可配置选项。

### 核心技术方案

本方案在 useAgentChat 客户端与 AIChatAgent 服务端之间建立三条互补的取消传播路径，并通过 durabilityMode 配置项实现本地生命周期清理与服务端 turn 取消的语义分离。

路径一：stop() → 服务端取消消息。当用户点击停止按钮触发 stop() 时，系统在取消本地 reader（AI SDK 现有行为）和中止工具继续执行流（WebSocketChatTransport 现有行为）之后，增加一步：通过当前 WebSocket 连接向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，携带当前活跃的 requestId。具体实现为：在 useAgentChat 的 stopWithToolContinuationAbort 回调中，调用 customTransport.sendCancel(activeRequestId)，其中 sendCancel 为 WebSocketChatTransport 新增方法，负责构造 { type: "cf_agent_chat_request_cancel", id: requestId } 并通过 agent.send() 发送。服务端 onMessage 处理器收到该消息后，调用 _abortRegistry.cancel(requestId) 使该请求对应的 AbortController 触发 abort，进而使传递给 onChatMessage 的 abortSignal 变为 aborted 状态，LLM 推理流的 reader 读取被中断，TurnQueue 中该 turn 以 status: "aborted" 结束。

路径二：durabilityMode 配置。在 useAgentChat 的选项中新增 durabilityMode 字段，接受 "durable"（默认）或 "request-lifetime" 两种值。当值为 "durable" 时，客户端 WebSocket 因浏览器刷新、组件卸载、网络断开等原因关闭时，不发送取消消息，服务端 DO 继续执行 agent turn —— 这是现有的默认行为，与 ResumableStream 的 chunk 持久化和恢复机制一致。当值为 "request-lifetime" 时，系统监听 WebSocket 的 close 事件和页面的 beforeunload 事件：当 WebSocket 关闭且 close code 不是正常关闭（1000）且不是由 stop() 触发的主动关闭时，在连接关闭前尽力发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，将当前活跃 requestId 对应的服务端 turn 标记为取消。网络断开场景下该消息可能无法送达，此时服务端通过心跳超时机制检测连接丢失，并根据 durabilityMode 自行决定是否取消活跃 turn。

路径三：多标签页取消广播。当任一标签页的 stop() 触发服务端取消时，AIChatAgent 的 _emit("message:cancel", { requestId }) 事件被触发。该事件通过 DO 的广播机制向所有已连接客户端发送消息更新通知，使其他观察同一对话的标签页同步获知 turn 已被取消，UI 同步停止加载动画并在消息列表中标记取消状态。这避免了多标签页场景下仅发起取消的标签页 UI 更新而其他标签页仍显示「生成中」的不一致问题。

### 关键模块与处理流程

本方案涉及的核心模块如下，均基于现有代码架构进行增量修改。

客户端模块——WebSocketChatTransport：在现有 WebSocketChatTransport 类（packages/ai-chat/src/ws-chat-transport.ts）上新增两个方法。sendCancel(requestId): 构造 { type: MessageType.CF_AGENT_CHAT_REQUEST_CANCEL, id: requestId } 并通过 this.agent.send() 发送；当 WebSocket 未连接时静默丢弃。setDurabilityMode(mode): 根据 mode 值决定是否在 close 事件中发送取消消息。当 mode 为 "request-lifetime" 时，注册 beforeunload 事件监听器和 WebSocket close 事件监听器，在非主动关闭、非正常关闭码的条件下尝试发送取消消息。该模块同时维护一个 isStopTriggered 标志位，在 sendCancel 被调用时置位，在 close 监听器中检查该标志以区分 stop() 触发的关闭和被动关闭。

客户端模块——useAgentChat（react.tsx）：在 stopWithToolContinuationAbort 回调中增加 customTransport.sendCancel(activeRequestId) 调用。activeRequestId 通过 WebSocketChatTransport 暴露的 getter 或通过 useChat 返回的 status 推导取得——当 status 为 "streaming" 或 "submitted" 时，表示有活跃请求。在 useAgentChat 的选项类型中新增 durabilityMode?: "durable" | "request-lifetime" 字段，默认值为 "durable"，在 hook 初始化时传递给 customTransport.setDurabilityMode()。

服务端模块——AIChatAgent（index.ts）：现有 onMessage 处理器（约第 828-833 行）已完整实现 CF_AGENT_CHAT_REQUEST_CANCEL 的处理逻辑：_abortRegistry.cancel(data.id) 取消请求对应的 AbortController，_emit("message:cancel", { requestId: data.id }) 触发取消事件。无需修改此路径。需要增加的是：在收到 CF_AGENT_CHAT_REQUEST_CANCEL 且取消成功后，通过 ResumableStream 将已缓冲的 chunk 标记为终态（status: "completed" 且包含取消标记），确保后续通过 reconnectToStream 恢复的客户端收到完整的状态信息。

服务端模块——AbortRegistry（abort-registry.ts）：现有实现已完整支持本方案所需功能。getSignal(id) 懒创建 AbortController，cancel(id, reason) 以可选 reason 触发 abort，linkExternal(id, signal) 将外部 AbortSignal 链接到注册表中的控制器。当 stop() 发送的 CF_AGENT_CHAT_REQUEST_CANCEL 到达时，cancel() 使得传递给 streamText 的 abortSignal 变为 aborted，AI SDK 的 doStream 在每次 reader.read() 之后检查信号并抛出 AbortError，从而终止 LLM 推理流。

服务端模块——TurnQueue（turn-queue.ts）：TurnQueue 基于生成代际（generation）的无效化机制天然支持「取消当前 turn 并丢弃后续排队 turn」的场景。当 stop() 触发取消且开发者希望同时清空排队中的 turn 时，可调用 resetTurnState()（现有方法，约第 1728 行），该方法依次：递增 TurnQueue 的 generation（使所有排队 turn 变为 stale）、调用 _abortRegistry.destroyAll() 取消所有活跃控制器、清除 ContinuationState 中的 pending/deferred、发送 STREAM_RESUME_NONE 通知等待中的客户端。对于仅取消单个 turn 的场景，_abortRegistry.cancel(requestId) 仅中止该请求的 AbortController，TurnQueue 中的后续 turn 不受影响。

### 与现有机制的兼容性

本方案与现有系统的以下关键机制保持兼容，无需修改既有代码路径。

- 流式响应恢复（ResumableStream）：取消后的 turn 将已缓冲的 chunk 持久化并标记终态，后续通过 reconnectToStream 恢复的客户端可重放已生成的部分内容并获知取消状态。ResumableStream 的 startStream / completeStream / errorStream 生命周期方法保持不变。
- 工具调用继续执行（ContinuationState + client-tools）：当 turn 因取消而中止时，如果当前 assistant 消息中有未完成的 tool-call part，该 tool-call 被标记为取消状态而非等待输入。ContinuationState 中的 pending/deferred 被清除（通过 resetTurnState 或 abortActiveTurn），已注册的 awaitingConnections 收到 STREAM_RESUME_NONE。如果取消发生在工具结果已返回、auto-continue 正准备发起下一次推理的窗口期，ContinuationState 的 pending.prerequisite 检查失败，工具继续执行被跳过。
- 多标签页观察：AIChatAgent 的 onConnect 在所有新连接建立时检查 ResumableStream 的活跃状态。如果上一个 turn 已被取消且 stream 已标记为完成，新连接的客户端通过 CF_AGENT_STREAM_RESUMING → CF_AGENT_STREAM_RESUME_ACK 流程重放已缓存的 chunk 并获取取消终态。同时，message:cancel 事件的广播确保已连接的标签页实时同步取消状态。
- 迟到服务端消息处理：在 "durable" 模式下，如果客户端断开后服务端继续执行并完成 turn，新消息通过 persistMessages 写入 SQLite。当客户端重新连接时，getInitialMessages 拉取完整消息历史，ResumableStream 重放缓冲的 chunk。客户端通过消息 ID 和 reconcileMessages 去重，确保不产生重复消息。在 "request-lifetime" 模式下，由于断连时已尽力发送取消，迟到消息的窗口被最小化——只有在取消消息未能送达的网络断开场景才可能出现，此时服务端心跳超时检测作为兜底。

### 技术效果

本方案带来的技术效果包括：

- 语义精确区分：首次在 useAgentChat 层面明确区分「本地生命周期清理」（reader cancel、组件卸载、WebSocket 关闭）与「服务端 turn 取消」（用户主动停止），消除了现有系统中两种语义混淆导致的「刷新页面后服务端继续跑但 UI 无法恢复」或「想取消但只停止了本地显示」问题。
- 开发者可控：通过 durabilityMode 配置项，不同应用可根据自身需求选择默认 durable 模式（适合长时间 agent 任务、支持断线恢复的场景）或 request-lifetime 模式（适合短请求、断开即取消的场景，如 API 网关后的 agent 服务）。
- 取消意图可靠传播：stop() → sendCancel() → CF_AGENT_CHAT_REQUEST_CANCEL → AbortRegistry.cancel() → abortSignal 的完整因果链，确保用户取消意图从 UI 按钮点击到 LLM 推理流终止的全链路传递，而非仅在客户端截断显示。
- 多标签页一致性：message:cancel 事件的广播机制确保同一 DO 实例的所有连接客户端同步获知取消状态，避免标签页间的 UI 不一致。
- 向后兼容：durabilityMode 默认值为 "durable"，与现有行为完全一致。stop() 新增的 sendCancel 调用在 WebSocket 不可用时静默丢弃，不破坏现有错误处理路径。不修改 AbortRegistry、TurnQueue、ResumableStream、ContinuationState 的任何公开接口。

### 风险与待确认问题

- 网络断开场景下 CF_AGENT_CHAT_REQUEST_CANCEL 的送达保证：在 TCP 连接已不可用时，sendCancel 调用无法送达服务端。对于 request-lifetime 模式，需要服务端心跳超时作为兜底检测。建议服务端在 DO 层面维护一个「客户端心跳最后接收时间」，当超过阈值（如 30 秒）未收到任何客户端消息且 durabilityMode 为 request-lifetime 时，自动取消活跃 turn。本方案当前文档描述了该兜底需求但未指定具体心跳协议细节，需后续确认。
- stop() 调用时 activeRequestId 的获取：useAgentChat 内部需要知道当前活跃的 requestId 以便发送取消消息。可通过 WebSocketChatTransport 暴露 activeRequestId getter（当前 transport 已有 activeRequestIds Set），或通过 useChat 返回的 status 和请求 ID 推导。具体实现方式需根据 AI SDK 版本确定。
- beforeunload 事件中 sendCancel 的可靠性：浏览器 beforeunload 事件中 sendBeacon 或同步 XHR 是唯一可靠的发送方式，但 WebSocket send 在 beforeunload 中可能不可靠。对于 request-lifetime 模式下的页面关闭场景，建议使用 navigator.sendBeacon 向 DO 的 HTTP 端点发送取消请求作为备选路径。
- 与现有 stop 行为的兼容性测试：stop() 新增 sendCancel 调用后，需验证在 WebSocket 连接断开、DO 已休眠、或 requestId 已失效等边界条件下的行为不产生未捕获异常。
