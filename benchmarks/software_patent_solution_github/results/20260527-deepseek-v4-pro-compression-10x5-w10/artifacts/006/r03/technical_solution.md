## 技术方案

本方案提出一种面向长时间 Agent 对话的客户端生命周期清理与服务端 Turn 取消分离系统。该系统在 WebSocket 传输层、客户端 React Hook 和服务端 Durable Object Agent 三层之间建立清晰的语义边界，将浏览器刷新、组件卸载、Reader 取消、网络断开等本地生命周期清理事件，与用户点击「停止」按钮或应用主动取消服务端 Turn 的意图明确区分，从而在保证用户可控性的前提下，最大化服务端 Agent 对话的连续性和可恢复性。

### 整体架构

系统由三层组成：（1）WebSocket 传输层（WebSocketChatTransport），负责消息的发送、接收、流式响应的 ReadableStream 抽象，以及断线后的流恢复握手；（2）客户端 React Hook 层（useAgentChat），管理 React 组件生命周期、向开发者暴露 stop() 等控制接口，并通过 onAgentMessage 处理器接收服务端广播；（3）服务端 Durable Object Agent 层（AIChatAgent），基于 Cloudflare Durable Object 运行，内部包含 AbortRegistry（请求级取消注册表）、TurnQueue（串行 Turn 队列）、ResumableStream（可恢复流管理器）和 ContinuationState（自动延续状态机）四个核心子模块。

### 两类语义的区分机制

本方案的核心是建立两类语义的明确区分：本地生命周期清理（Local Lifecycle Cleanup）与意向性 Turn 取消（Intentional Turn Cancellation）。

### 本地生命周期清理

本地生命周期清理涵盖以下场景：浏览器刷新或关闭标签页、React 组件卸载（useEffect 清理函数执行）、ReadableStream 的 reader.cancel() 调用、WebSocket 连接因网络波动断开等。这些场景的共同特征是：它们仅反映客户端本地资源的释放需求，并不蕴含用户希望终止服务端 Agent 正在执行的推理任务。

当客户端本地生命周期清理发生时，系统的处理流程为：（1）useAgentChat 的 effect 清理函数移除 onAgentMessage 事件监听器，重置本地流状态（streamStateRef 置为 idle），清除本地响应 ID 集合；（2）WebSocket 连接因页面关闭或网络断开而关闭，触发服务端 AIChatAgent 的 onClose 回调；（3）onClose 回调仅清理与该连接相关的恢复状态：从 _pendingResumeConnections 集合中移除该连接 ID、从 ContinuationState 的 awaitingConnections 中移除该连接、若该连接恰好是当前 pending 或 active 的延续连接则将其置空。onClose 回调不调用 AbortRegistry 的任何取消方法，也不操作 TurnQueue 中的活跃 Turn。因此，服务端 Durable Object 中正在执行的 Agent Turn 完全不受影响，继续运行至完成。

当用户在刷新后重新打开页面，或网络恢复后 WebSocket 重新连接时，useAgentChat 通过 resume 选项（默认为 true）触发 transport.reconnectToStream()。传输层向服务端发送 CF_AGENT_STREAM_RESUME_REQUEST 消息。服务端检查 ResumableStream 是否仍有活跃流——若有，则向该连接发送 CF_AGENT_STREAM_RESUMING（携带当前流的 requestId）；若无，则发送 CF_AGENT_STREAM_RESUME_NONE。客户端收到 CF_AGENT_STREAM_RESUMING 后，发送 CF_AGENT_STREAM_RESUME_ACK 确认，服务端随即调用 replayChunks() 将已持久化的流块重放给客户端，之后继续推送实时生成的流块。整个过程对最终用户透明，刷新后对话内容自动恢复。

### 意向性 Turn 取消

意向性 Turn 取消是用户或应用明确表达「终止当前 Agent 回答」的语义。触发路径包括：用户点击「停止」按钮（调用 useAgentChat 暴露的 stop() 方法）、应用代码调用 stop()、或工具延续场景下调用 abortActiveToolContinuation()。

取消流程分为传输层和服务端两层：（1）传输层：在 WebSocketChatTransport.sendMessages() 创建的每个流中，当外部 AbortSignal 触发或 ReadableStream 的 cancel() 被调用时，onAbort 处理器向服务端发送一条 CF_AGENT_CHAT_REQUEST_CANCEL 消息，消息体携带该请求的 requestId。发送完成后，传输层以 keepId=true 模式结束本地流（即保留 requestId 在 activeRequestIds 集合中），确保在服务端发出最终的 done:true 信号之前，所有飞行中的流块被跳过而不产生 UI 抖动。（2）服务端：AIChatAgent 收到 CF_AGENT_CHAT_REQUEST_CANCEL 后，调用 AbortRegistry.cancel(data.id) 中止该 requestId 对应的 AbortController。由于 _streamSSEReply 和 _sendPlaintextReply 的流处理循环在每个迭代中检查 abortSignal?.aborted，AbortController 的中止会导致 reader.cancel() 被调用，流读取循环退出。退出前，服务端发送 done:true 的广播消息通知所有连接的客户端流已终止。Turn 执行结果标记为 status: "aborted"。

stop() 方法还封装了对工具延续的特殊处理：stopWithToolContinuationAbort 在调用底层 stop() 之后，在 finally 块中调用 customTransport.abortActiveToolContinuation()。这确保在工具调用等待客户端执行期间用户点击停止时，不仅终止当前 Turn，也清理等待工具延续的延迟流（deferred ReadableStream），防止遗留的 handshake resolver 导致内存泄漏。

### 服务端关键子模块

服务端 AIChatAgent 内部包含四个支撑上述语义区分的核心子模块：

AbortRegistry：请求级取消注册表。以 requestId 为键，惰性创建 AbortController。提供 getSignal(id) 获取或创建信号、cancel(id, reason) 中止特定请求、linkExternal(id, signal) 将外部 AbortSignal 链接到注册表控制器、destroyAll() 中止全部请求。每次 chat turn 开始时通过 getSignal 获取信号并传入 _streamSSEReply；turn 结束时通过 remove 清理。当客户端发送 CF_AGENT_CHAT_REQUEST_CANCEL 时，cancel 被调用，信号中止，流处理循环退出。linkExternal 方法支持子 Agent 场景——父 Agent 的 AbortSignal 可通过此方法级联到子 Agent 的特定 Turn。

TurnQueue：基于 Promise 链的串行执行队列，带有世代（generation）失效机制。每个 chat turn 在入队时绑定当前世代编号。当 CF_AGENT_CHAT_CLEAR 被调用时，世代递增，所有尚在排队但属于旧世代的 Turn 在到达队首时返回 { status: "stale" } 而不执行。这确保了清空对话历史时不会出现旧请求覆盖新状态的问题。TurnQueue 还暴露 activeRequestId 属性，允许上层（如 abortActiveTurn）在不追踪具体 requestId 的情况下取消当前活跃 Turn。

ResumableStream：可恢复流管理器，负责将 Agent 推理产生的每个 SSE 流块持久化到 SQLite，并为重连客户端提供重放能力。当客户端发送 CF_AGENT_STREAM_RESUME_ACK 后，replayChunks() 从数据库读取已存储的流块并推送给客户端，标记 replay: true；重放完成后发送 replayComplete: true，后续实时流块正常推送。ResumableStream 还处理「孤儿流」场景——若 Durable Object 在流进行中发生休眠（hibernation），唤醒后 ResumableStream 从 SQLite 恢复状态，从已存储的块重建部分 assistant 消息并持久化。

ContinuationState：自动延续状态机，管理工具调用结果返回后的自动继续对话流程。包含 pending（等待执行的延续）、deferred（被推迟的延续）、activeRequestId/activeConnectionId（当前活跃延续的标识）和 awaitingConnections（等待延续流开始的连接集合）四种状态。与 ResumableStream 协同：当客户端在工具延续等待期间刷新页面，重连后 ContinuationState 的 awaitingConnections 中已有该连接信息，可直接通知其 STREAM_RESUMING 而非返回 RESUME_NONE。

### 可配置的运行模式

系统提供两种运行模式，通过两个正交的配置维度组合，满足不同应用场景的需求：

维度一：客户端 resume 选项。useAgentChat 的 resume 参数（默认 true）控制页面加载时是否自动尝试恢复活跃流。当 resume 为 true 时，useChat 内部调用 transport.reconnectToStream()，发送 CF_AGENT_STREAM_RESUME_REQUEST 并等待服务端响应。当 resume 为 false 时，即使服务端有活跃流，客户端也不发起恢复请求，onAgentMessage 中的 CF_AGENT_STREAM_RESUMING 消息也会被跳过。开发者可根据应用场景关闭此选项，例如在不需要跨页面恢复的简单问答场景中。

维度二：服务端 chatRecovery 标志。AIChatAgent 子类可设置 chatRecovery = true，此时每个 chat turn 的主体逻辑被包裹在 runFiber() 调用中执行。runFiber 是 Durable Object 提供的持久执行原语——即使 Durable Object 在 turn 执行期间发生休眠（例如因无外部请求而进入 idle 状态），Fiber 的状态被持久化到存储中，唤醒后从中断点继续执行。结合 ResumableStream 的 SQLite 持久化，chatRecovery 模式实现了「全持久」的 Agent 对话：不仅流块可恢复，执行上下文本身也可恢复。默认为 false 的 request-lifetime 模式则不使用 runFiber——turn 在单次 Durable Object 激活周期内完成，若中途休眠则 turn 丢失。

### 多标签页与迟到消息处理

系统通过以下机制保证多标签页场景下的正确性和迟到消息的妥善处理：

（1）请求 ID 追踪与消息跳过：每个 WebSocket 消息都携带 requestId。传输层维护 localRequestIdsRef（当前标签页发起的请求 ID 集合），onAgentMessage 处理器在收到 CF_AGENT_USE_CHAT_RESPONSE 时，检查 data.id 是否在 localRequestIdsRef 中——若在，说明该消息已由传输层的 ReadableStream 处理，跳过以避免重复处理。服务端广播给所有连接时，每个连接根据自己的 localRequestIdsRef 决定是否处理。

（2）跨标签页状态同步：服务端通过 _broadcastChatMessage 将消息变更（CF_AGENT_CHAT_MESSAGES）、流响应（CF_AGENT_USE_CHAT_RESPONSE）、清空指令（CF_AGENT_CHAT_CLEAR）等广播给所有连接的客户端。标签页 A 发送的消息会通过广播同步到标签页 B，标签页 B 的 onAgentMessage 处理器更新本地消息列表。CF_AGENT_CHAT_CLEAR 广播会触发所有标签页的 resetLocalChatState，清空本地消息和工具调用结果——但不会取消服务端正在执行的 Turn，因为清理是针对本地聊天状态而非服务端任务。

（3）迟到消息处理：当客户端在 Turn 已完成后才收到服务端的流块（例如因网络延迟导致到达顺序错乱），传输层通过 keepId 机制保证这些迟到块被安全忽略。在取消场景中，onAbort 以 keepId=true 调用 finish()，requestId 保留在 activeRequestIds 中。服务端在流终止时发送 done:true 广播，onAgentMessage 处理器收到 done:true 后从 activeRequestIds 中移除该 requestId。在移除之前到达的飞行中流块会被 activeRequestIds 拦截而不被处理；移除之后到达的消息（理论上不应存在）也会因无匹配的活跃请求而被忽略。

### 与工具调用延续的兼容

工具调用延续（Tool Continuation）是 Agent 对话中的关键流程：服务端在工具调用后暂停生成，等待客户端执行工具并返回结果，然后自动继续生成。该流程对取消和恢复机制提出了额外要求：

（1）工具延续期间的取消：stopWithToolContinuationAbort 在调用 AI SDK 的 stop() 之后，额外调用 customTransport.abortActiveToolContinuation()。后者检查是否存在活跃的工具延续流——若延续流的 ReadableStream 已建立（requestId 已知），则发送 CF_AGENT_CHAT_REQUEST_CANCEL 到服务端并终止本地流；若延续流尚在等待 handshake 阶段（requestId 未知），则直接关闭本地流并清理 handshake resolver，使后续到达的 onResume 回调成为空操作。

（2）工具延续期间的恢复：当客户端在工具调用等待期间刷新页面（例如服务端已发出工具调用请求但客户端尚未返回结果），重连后的流程为：服务端在 onConnect 中检测到有活跃的 ResumableStream，发送 STREAM_RESUMING；同时 ContinuationState 中该连接可能在 awaitingConnections 中（若延续尚未开始），或在 pending/active 状态中。传输层的 reconnectToStream 先检查 _expectToolContinuation 标志（由 addToolOutput 等操作设置）——若为 true，则创建延迟 ReadableStream 并等待服务端通过 STREAM_RESUMING 通知延续流的 requestId；否则走正常的流恢复流程。

### 技术效果

本方案相比现有技术产生以下技术效果：

（1）语义分离：将客户端生命周期事件（浏览器刷新、组件卸载、Reader 取消、网络断开）与意向性取消事件（用户点击停止）在协议层明确区分。前者仅做本地资源清理，服务端 Turn 不受影响；后者通过专用消息类型（CF_AGENT_CHAT_REQUEST_CANCEL）传递取消意图，精确中止特定 requestId 对应的 Turn。避免了现有方案中客户端断开即取消服务端任务的粗粒度问题。

（2）细粒度取消：AbortRegistry 以 requestId 为粒度管理 AbortController，支持取消单个 Turn 而不影响其他排队或等待中的 Turn。与 TurnQueue 的世代失效机制配合，chat-clear 场景通过 destroyAll 批量取消所有活跃请求并通过世代递增拒绝旧请求，保证状态一致性。

（3）透明恢复：ResumableStream 的持久化 + 重放机制使客户端刷新或网络短暂断开后，对话历史和正在生成中的流内容可无缝恢复。重放块标记 replay: true 以便客户端区分历史块和实时块，replayComplete: true 标志着重放结束、后续为实时流。

（4）可配置性：通过 resume 选项（客户端）和 chatRecovery 标志（服务端）两个正交维度提供灵活配置。开发者可按需组合：默认模式（resume=true, chatRecovery=false）适合大多数场景——流可恢复但执行不持久；全持久模式（resume=true, chatRecovery=true）适合需要最长执行连续性的场景；request-lifetime 模式（resume=false, chatRecovery=false）适合简单问答场景。

（5）多标签页一致性：通过服务端广播 + 客户端 requestId 过滤 + 跨标签页消息同步，多个标签页对同一 Agent 对话的观察保持一致。一个标签页的取消操作通过广播同步到其他标签页，一个标签页的刷新不影响其他标签页的流接收。

### 风险与待确认问题

（1）服务端 Turn 泄漏：当客户端在非取消场景下断开连接（如浏览器崩溃），服务端 Turn 继续执行至完成。若短时间内大量客户端断开并重连，服务端可能积累大量无消费者 Turn。当前方案中 TurnQueue 的世代失效和 chat-clear 路径的 abortAllRequests 提供了清理机制，但不存在基于「无消费者超时」的自动取消策略。这可能导致 Durable Object 的 CPU 和存储资源浪费。建议后续引入可选的 Turn 消费者追踪机制：当 Turn 的所有关联连接都断开且超过配置的 TTL 后，自动取消该 Turn。

（2）resume=false 与服务端状态不一致：当客户端设置 resume=false 但服务端有活跃流时，客户端不会发起恢复请求，但服务端的 Turn 仍在执行并广播流块。这些广播消息因客户端的 localRequestIdsRef 中无对应 requestId，会被 onAgentMessage 按「跨标签页广播」路径处理（resume-fallback 分支），可能导致客户端出现意外的流状态。当前实现中 onAgentMessage 检查 !resume && !customTransport.isAwaitingResume() 后直接返回，避免了此问题，但这意味着 resume=false 的客户端完全无法感知服务端正在进行的 Turn。

（3）chatRecovery 与 TurnQueue 的交互：当 chatRecovery=true 且 Turn 通过 runFiber 持久执行时，若 Durable Object 在 Turn 执行期间休眠，TurnQueue 的状态（包括世代编号、队列中的 Promise 链）不会随 Fiber 一起持久化。DO 唤醒后 TurnQueue 被重新初始化（世代重置为 0、队列为空），但 ResumableStream 从 SQLite 恢复了活跃流状态。这可能导致新请求在旧 Turn 尚未完成时进入队列，出现并发问题。当前代码中 runFiber 仅包裹单个 Turn 的业务逻辑（onChatMessage + _reply），TurnQueue 的 enqueue/dequeue 仍在 Fiber 外部——这意味着休眠发生后，队列语义丢失但活跃 Turn 的业务执行不受影响。
