## 技术方案

本方案提出一种基于 WebSocket 的 Agent 对话生命周期管理方法，在客户端与运行于 Durable Object 的服务端 Agent 之间引入"对话模式"（durable 模式与 request-lifetime 模式）配置项，将浏览器端的本地生命周期清理（组件卸载、页面刷新、reader cancel、网络断开等）与用户意图驱动的服务端 turn 取消（点击"停止"按钮、应用主动取消）在语义和实现层面明确分离。在 durable 模式下，客户端断开连接不会触发服务端取消，Agent 对话在 Durable Object 中持续运行并支持后续恢复；用户显式取消时，系统通过一条带请求标识的取消消息将取消意图精准传递到服务端，仅终止当前请求对应的推理循环。同时，方案通过流式恢复机制、跨标签页广播和工具调用延续状态的持久化，保证在连接变化、多标签页和迟到服务端消息等场景下的正确性。

### 整体架构

系统由客户端 React Hook（useAgentChat）、WebSocket 传输层（WebSocketChatTransport）、服务端 Agent（AIChatAgent，运行于 Durable Object 中）、以及一组支撑模块（AbortRegistry、TurnQueue、ResumableStream、ContinuationState、BroadcastState）组成。客户端通过 WebSocket 与服务端通信，消息体遵循 cf_agent_chat_* 协议。核心交互路径包括：用户发送消息（CF_AGENT_USE_CHAT_REQUEST → CF_AGENT_USE_CHAT_RESPONSE 流式分块响应）、用户取消（CF_AGENT_CHAT_REQUEST_CANCEL）、流恢复（CF_AGENT_STREAM_RESUME_REQUEST → CF_AGENT_STREAM_RESUMING → CF_AGENT_STREAM_RESUME_ACK）、跨标签页消息同步（CF_AGENT_CHAT_MESSAGES / CF_AGENT_MESSAGE_UPDATED）、以及历史清除（CF_AGENT_CHAT_CLEAR）。

### 对话生命周期模式（durable vs request-lifetime）

useAgentChat 新增 lifetime 配置项，支持两种模式：durable（默认）和 request-lifetime。该配置项决定了客户端本地生命周期事件（组件卸载、浏览器刷新、reader cancel、WebSocket 断开）是否触发对服务端 agent turn 的取消操作。

在 durable 模式下，客户端执行本地清理时（包括 React useEffect 清理函数中移除 WebSocket 事件监听器、重置本地流状态为 idle、清空本地响应 ID 映射等操作），不会向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。服务端的推理循环（streamText 调用）继续在 Durable Object 中运行，其输出块被 ResumableStream 持续写入 SQLite。当用户刷新页面或重新打开标签页后，useAgentChat 通过 WebSocketChatTransport.reconnectToStream() 发起 CF_AGENT_STREAM_RESUME_REQUEST，服务端检测到活跃流后回复 CF_AGENT_STREAM_RESUMING 并重放已持久化的块，客户端 ACK 后继续接收实时块。仅当用户主动调用 stop() 方法（如点击"停止"按钮）时，客户端才构造一条包含当前请求 ID 的 CF_AGENT_CHAT_REQUEST_CANCEL 消息发送到服务端，服务端 AbortRegistry 根据请求 ID 找到对应的 AbortController 并执行 abort()，推理循环检测到 signal.aborted 后终止并返回 status: "aborted"，已流式输出的部分块仍被持久化。

在 request-lifetime 模式下，客户端生命周期与当前 HTTP 请求语义对齐：组件卸载或 WebSocket 断开时，WebSocketChatTransport 内部的 ReadableStream.cancel() 被触发，onAbort 回调向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL，服务端终止对应 turn。此模式适合不需要恢复能力的短对话场景。

该配置项通过 options.lifetime 传入，WebSocketChatTransport 在构造时记录该值，并在 onAbort 逻辑中据此决定是否发送取消消息。stop() 方法不受 lifetime 模式影响——它在两种模式下都始终发送取消消息，因为 stop 代表用户明确的取消意图。

### 客户端本地清理与服务端 Turn 取消的语义分离

系统通过定义六类客户端事件的语义归属，将本地清理与取消意图彻底分离。

本地生命周期清理类事件包括：（1）组件卸载——React 组件从 DOM 移除时，useEffect 清理函数移除 WebSocket message 事件监听器，并将 BroadcastStreamState 重置为 idle，不做任何服务端通信；（2）浏览器刷新/页面切换——浏览器发起页面导航时，WebSocket 连接被关闭，客户端不做取消发送，服务端 onClose 钩子仅清理 ContinuationState 中与该连接相关的 pending/awaiting 记录，不调用 abortRegistry.cancel()；（3）reader cancel——AI SDK 内部 ReadableStream 被取消时（如竞态条件导致的前一个请求被新请求替换），WebSocketChatTransport 在 durable 模式下跳过 CF_AGENT_CHAT_REQUEST_CANCEL 发送，仅执行本地流清理；（4）网络短暂断开——PartySocket 自动重连时，旧连接关闭不触发取消，新连接建立后通过 reconnectToStream 恢复流。

用户取消意图类事件包括：（1）stop() 调用——由用户点击"停止"按钮触发，内部依次调用 AI SDK 的 stop() 方法和 WebSocketChatTransport.abortActiveToolContinuation()（如果有活跃的工具延续流），stop() 触发 abortSignal.abort()，transport 的 onAbort 回调构造 CF_AGENT_CHAT_REQUEST_CANCEL 消息（含请求 ID），服务端 AbortRegistry.cancel(id) 精确取消该请求的推理循环；（2）clearHistory() 调用——向服务端发送 CF_AGENT_CHAT_CLEAR，服务端 resetTurnState() 依次调用 turnQueue.reset()（递增代际计数器使队列中旧代际的待处理 turn 被标记为 stale）、abortRegistry.destroyAll()（取消所有活跃请求）、continuation.sendResumeNone()（通知等待延续的连接无流可恢复）。

### 取消消息的端到端传递机制

取消消息的端到端传递链路如下：客户端 WebSocketChatTransport.sendMessages() 在创建 ReadableStream 时，为每次请求生成唯一的 requestId（8 位 nanoid），并将其注册到 localRequestIdsRef 中。当外部 abortSignal（由 AI SDK 的 stop() 触发）触发 onAbort 回调时，transport 构造消息 { type: "cf_agent_chat_request_cancel", id: requestId } 通过 agent.send() 发送。消息到达服务端 onMessage 后，被路由到 AIChatAgent 的内置处理器：调用 this._abortRegistry.cancel(data.id)。

AbortRegistry 是一个按请求 ID 索引的 AbortController 映射表。getSignal(id) 方法在首次访问时创建 AbortController 并返回其 signal；cancel(id, reason) 方法调用对应 controller.abort(reason)。该 signal 被传递给 streamText 的 abortSignal 参数，以及 _runChatTurn 推理循环中的流读取逻辑。当 signal.aborted 为 true 时，推理循环在每次读取 LLM 流块的间隙检测到该状态，跳出循环并返回 "aborted" 状态。ResumableStream 在检测到 abort 后，将已持久化的块标记为 stream 完成状态，并向所有连接广播 done 信号。

关键设计在于 requestId 仅标识单次请求（一次 chat turn），而非整个对话或连接。因此取消一个 turn 不会影响之前已完成的历史 turn，也不会阻止后续新 turn 的提交。TurnQueue 的代际（generation）机制进一步保证：当 clearHistory 递增代际后，旧代际中排队的延续 turn 在到达队首时被标记为 stale 并跳过执行。

### 流式恢复机制

当客户端在 durable 模式下重新建立连接后，流式恢复由以下流程实现：useChat 的 resume 选项（默认为 true）触发 AI SDK 调用 WebSocketChatTransport.reconnectToStream()。该方法发送 CF_AGENT_STREAM_RESUME_REQUEST 到服务端，并设置两个解析器（_resumeResolver 和 _resumeNoneResolver）等待响应。

服务端收到 RESUME_REQUEST 后执行三条路径判断：（1）如果 ResumableStream.hasActiveStream() 为 true，且当前活跃 continuation 的连接 ID 与请求连接 ID 不同（即该流属于另一个标签页），则回复 CF_AGENT_STREAM_RESUME_NONE，客户端通过 broadcastTransition 进入 observing 状态监听跨标签页广播；（2）如果有活跃流且属于当前连接或非 continuation 流，则调用 _notifyStreamResuming(connection) 发送 CF_AGENT_STREAM_RESUMING 消息（含 stream requestId），同时服务端开始通过该连接重放 SQLite 中已持久化的块（replay: true），重放完成后发送 replayComplete: true，之后继续发送实时块；（3）如果无活跃流但有 pending continuation 等待当前连接，则将连接加入 awaitingConnections 等待延续启动；否则回复 RESUME_NONE。

客户端 transport 收到 RESUME_NONE 后，_resumeNoneResolver 被调用，reconnectToStream 返回 null，AI SDK 的 Chat 实例保持 ready 状态。收到 RESUMING 后，_resumeResolver 被调用，transport 发送 RESUME_ACK，创建 _createResumeStream 返回的 ReadableStream，AI SDK 将其接入 useChat 的流处理管道，状态转为 streaming。重放块和实时块统一通过 onAgentMessage 的 CF_AGENT_USE_CHAT_RESPONSE 分支处理：属于当前标签页发起的请求（localRequestIdsRef 中包含该 ID）的块由 transport 直接消费；其他块通过 broadcastTransition 状态机处理，累积到 StreamAccumulator 中并更新本地消息列表。5 秒超时保护防止服务端不可达时的永久挂起。

### 工具调用延续与 durable 模式的兼容

工具调用延续（tool continuation）是服务端在收到客户端工具结果或审批响应后自动继续推理的机制。在 durable 模式下，工具延续与连接生命周期解耦，通过以下设计保证兼容性。

客户端侧：当 onToolCall 回调或已弃用的自动工具解析完成工具执行后，sendToolOutputToServer 向服务端发送 CF_AGENT_TOOL_RESULT 消息（含 toolCallId、output、autoContinue 标志和 clientTools 模式）。如果 autoContinueAfterToolResult 为 true（默认），则调用 startToolContinuation()：设置 resumingToolContinuationRef 为 true 作为重入保护，递增 continuationGenerationRef 代际，调用 customTransport.expectToolContinuation() 标记下一次 reconnectToStream 调用应创建工具延续流而非页面加载恢复流，然后触发 resumeStream()。transport 的 _createToolContinuationStream() 立即返回一个延迟的 ReadableStream，使 AI SDK 状态从 ready 转为 submitted，然后等待服务端推送 CF_AGENT_STREAM_RESUMING 完成握手。代际机制保证：如果在延续流完成前触发了 clearHistory 或新的延续，旧代际的 .finally() 清理逻辑被跳过，避免状态污染。

服务端侧：收到 TOOL_RESULT 后，AIChatAgent 将工具结果应用到对应消息的 tool part 状态（input-available → output-available），然后调度延续 turn：通过 TurnQueue.enqueue() 将延续请求序列化入队。如果此时客户端连接已断开（durable 模式），延续仍正常执行，输出由 ResumableStream 持久化。当客户端重连时，ResumableStream 检测到活跃流并通过 RESUME_REQUEST → RESUMING 流程恢复。ContinuationState 维护 pending/deferred/active 三层状态以及 awaitingConnections 映射，保证连接离开和返回时延续调度的一致性。sendResumeNone() 方法在 clearHistory 或连接离开时向所有等待连接的客户端发送 RESUME_NONE，避免其无限等待。

stop() 调用时，除通过 AI SDK 取消当前流外，还调用 customTransport.abortActiveToolContinuation()。该方法内部检查是否有活跃的工具延续流（_abortToolContinuation 回调），如有则发送 CF_AGENT_CHAT_REQUEST_CANCEL 到服务端取消对应的延续 turn，或（如果握手尚未完成）直接关闭本地流并清理解析器。

### 多标签页观察与迟到消息处理

当多个浏览器标签页同时连接到同一个 Agent 实例时，只有一个标签页的请求驱动了服务端推理（其 requestId 在 localRequestIdsRef 中），其他标签页通过 broadcastTransition 状态机观察流。

broadcastTransition 是一个纯函数状态机，接收当前 BroadcastStreamState 和事件，返回新的状态、消息更新函数和 isStreaming 标志。状态包括 idle（空闲）和 observing（观察中，含 streamId 和 StreamAccumulator）。事件类型包括：response（收到流块）、resume-fallback（非当前标签页触发的流恢复）、clear（对话清除）。在 observing 状态下，StreamAccumulator 累积从 CF_AGENT_USE_CHAT_RESPONSE 消息中解析的块数据，通过 messagesUpdate 函数更新本地消息列表。当收到 done: true 或 replayComplete: true 时，状态转回 idle。

迟到服务端消息处理：当客户端在 durable 模式下重连并发送 RESUME_REQUEST 后，如果服务端没有活跃流（例如前一 turn 恰好在重连前完成），服务端回复 RESUME_NONE。客户端 transport 通过 _resumeNoneResolver 立即解析 reconnectToStream 为 null（不等待 5 秒超时），AI SDK 的 Chat 保持 ready 状态。如果服务端在客户端连接期间通过 onConnect 主动推送 CF_AGENT_STREAM_RESUMING（例如另一个标签页触发了新的延续），onAgentMessage 首先检查 transport.isAwaitingResume() 和 resumingToolContinuationRef，决定由 transport 处理（当前标签页主动等待时）还是作为跨标签页广播由 broadcastTransition 处理。对于请求 ID 已存在于 localRequestIdsRef 中的消息，onAgentMessage 直接跳过，避免重复处理——这些消息已由 transport 的内部 ReadableStream 消费。请求 ID 在流的 done 信号到达时从 activeIds 中移除。

### 服务端持久化与崩溃恢复

ResumableStream 负责将流式输出块批量写入 Durable Object 的 SQLite 存储。每 10 个块（或缓冲区达到 100 个块上限时）执行一次批量 flush，以减少 SQLite 写入次数。每个块记录包含：id、stream_id、body（JSON 序列化的 UIMessageChunk）、chunk_index、created_at。流元数据记录包含：id、request_id、status（streaming/completed/error）、created_at、completed_at。

当 Durable Object 因休眠/驱逐而重启时，AIChatAgent 的 init 方法调用 ResumableStream.restoreActiveStreams()：从 SQLite 查询 status = 'streaming' 的流，将其恢复到内存中的活跃流映射。对于已恢复的孤立流（_isLive = false），原有的 ReadableStream 已在 DO 驱逐时丢失，但已持久化的块可以用于向重连客户端重放。定期清理任务（每 10 分钟）删除 completed_at 超过 24 小时的已完成流及其块数据。

服务端 onClose 钩子（在 AIChatAgent 初始化时被包装）执行连接级别的清理而不影响 turn：从 _pendingResumeConnections 中移除该连接 ID，从 _continuation.awaitingConnections 中移除该连接，如果该连接是当前 pending continuation 或 active continuation 的连接，则清空对应字段。这些清理仅移除"该连接作为等待者"的记录，不影响服务端正执行的推理循环和已持久化的流数据。

### 技术效果

本方案带来的技术效果包括：（1）通过 lifetime 配置项将客户端生命周期与服务端 turn 生命周期解耦，在 durable 模式下，浏览器刷新、组件卸载、网络断开等本地事件不再导致正在执行的 Agent 对话被取消，避免了因前端操作丢失服务端计算资源的浪费；（2）取消语义精确化——通过带 requestId 的 CF_AGENT_CHAT_REQUEST_CANCEL 消息和 AbortRegistry 的按请求 ID 索引，取消仅影响目标 turn 的推理循环，不影响历史 turn 和后续新 turn；（3）流式恢复机制使 durable 模式下的对话在连接重建后无缝继续，用户感知不到中断，已输出的块不会重复显示（通过 replayHydratedAssistantMessageIdsRef 去重和 collapseHydratedReplayTextParts 文本折叠）；（4）跨标签页广播和 broadcastTransition 状态机使多个标签页能同时观察同一个 Agent 对话的实时状态，且各标签页的本地清理操作互不干扰；（5）工具延续与连接生命周期解耦，即使客户端在工具执行期间断开，服务端仍可完成延续推理并持久化结果，供恢复时重放；（6）TurnQueue 的代际机制和 ContinuationState 的三层状态管理，避免了并发场景下的重复执行和状态错乱。

### 风险与待确认问题

本方案存在以下待确认的技术风险：（1）durable 模式下服务端 Durable Object 的资源占用——如果客户端频繁刷新但不显式取消，可能导致多个实质上已被用户放弃的 turn 继续占用 DO 的 CPU 时间和内存，直到推理自然完成或 DO 因空闲被驱逐。建议增加可配置的服务端 turn 超时时间（如 5 分钟无客户端连接时自动 abort）或基于 Wall Clock 的兜底超时机制；（2）跨标签页的 stop() 语义——当前设计中 stop() 仅取消当前标签页关联的请求。如果用户从标签页 B 想取消标签页 A 发起的对话，需要额外的跨标签页通信机制（如 BroadcastChannel API 或通过服务端广播取消意图）；（3）网络断开与浏览器刷新在 WebSocket 层面的不可区分性——服务端收到 onClose 时，无法区分该关闭是由网络断开、浏览器刷新还是标签页关闭导致。本方案通过在 durable 模式下统一不取消来处理，但如果未来需要差异化行为（如网络断开保留 turn 但标签页关闭取消），需要客户端在 beforeunload 事件中发送带语义标记的关闭消息（受浏览器 sendBeacon 限制，实际可靠性有限）；（4）ResumableStream 的 SQLite 存储增长——高频对话场景下，已持久化块在 24 小时清理周期内可能积累大量数据，需评估存储上限和压缩策略。
