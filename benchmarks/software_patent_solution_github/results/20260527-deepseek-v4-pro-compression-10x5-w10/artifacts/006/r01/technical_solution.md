## 技术方案

本方案提出一种面向持久化 Agent 对话的客户端生命周期与服务端 Turn 取消语义分离机制。系统基于 WebSocket 长连接与 Durable Object（DO）持久化执行环境构建，客户端通过 useAgentChat 钩子与 WebSocketChatTransport 传输层发起对话请求；服务端通过 AIChatAgent（继承自 Agent/DO）在持久化 SQLite 存储之上管理对话 Turn 的完整生命周期。核心设计在于：将浏览器刷新、组件卸载、Reader Cancel、网络断开等本地生命周期清理事件，与用户主动点击「停止」按钮或应用调用 stop() 发起的服务端 Turn 取消明确区分为两条独立路径——前者仅清理客户端本地资源，服务端 Agent Turn 在 DO 中继续执行并通过 ResumableStream 持久化所有流式 chunk；后者通过 WebSocket 协议发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，经 AbortRegistry 将取消意图精确传递到服务端正执行中的推理流，触发 AbortSignal 中断模型调用。系统同时提供默认 Durable 模式与可选 Request-Lifetime 模式的配置开关，使不同应用可按需选择「客户端断开即取消」还是「客户端断开不影响服务端执行」。

### 整体架构

系统由三层组成：（1）客户端层——useAgentChat React 钩子与 WebSocketChatTransport 传输层；（2）传输协议层——基于 WebSocket 的 CF_AGENT 协议族，包含 12 种消息类型；（3）服务端层——AIChatAgent 运行于 Cloudflare Durable Object 中，配备 ResumableStream（流式 chunk 持久化）、AbortRegistry（取消控制器注册表）、TurnQueue（串行 Turn 队列）和 ContinuationState（Tool 续接状态机）等核心组件。

客户端通过 useAgent（基于 PartySocket 的 WebSocket 连接管理）建立到特定 DO 实例的持久连接。useAgentChat 在此基础上封装 AI SDK 的 useChat，将 WebSocket 消息映射为 ReadableStream<UIMessageChunk>，使上层 React 组件无需感知底层协议。WebSocketChatTransport 实现 ChatTransport 接口，负责将 sendMessage、stop、resumeStream 等操作转化为对应的协议消息。

服务端 AIChatAgent 在 DO 的 onMessage 中拦截所有 CF_AGENT 协议消息，执行路由分发：CF_AGENT_USE_CHAT_REQUEST 入队 TurnQueue 后调用子类的 onChatMessage 启动推理流；CF_AGENT_CHAT_REQUEST_CANCEL 通过 AbortRegistry 触发对应推理流的 AbortController；CF_AGENT_STREAM_RESUME_REQUEST 触发 ResumableStream 的 chunk 回放。TurnQueue 提供串行化保证，支持 queue/latest/merge/drop/debounce 五种并发策略。

### 客户端生命周期清理与取消语义区分

本方案的核心创新在于将五类客户端本地事件与服务端 Turn 取消进行语义分离。当前主流方案将客户端 Reader Cancel、AbortSignal 触发、组件卸载时的清理统一视为「取消」语义，导致服务端推理被意外中断。本方案对每种事件进行精确区分。

（1）浏览器刷新 / 页面切换：WebSocket 连接断开触发 onClose 事件。服务端仅清理连接相关状态（_pendingResumeConnections、ContinuationState 中的 connectionId），不调用 AbortRegistry.cancel()。推理流继续在 DO 中执行，chunk 持续写入 ResumableStream 的 SQLite 缓冲区并广播到其他已连接标签页。

（2）组件卸载（React unmount）：useAgentChat 的 useEffect 清理函数移除 message 事件监听器、重置 streamStateRef 并清空 localResponseIds，但不发送任何取消消息。WebSocket 连接保持（由 useAgent 管理），服务端完全无感知。

（3）Reader Cancel（ReadableStream.cancel()）：在 WebSocketChatTransport.sendMessages 中，stream.cancel() 触发 onAbort 回调。onAbort 向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，但仅当 cancel 是由用户 stop() 调用传递的 AbortSignal 触发时。传输层通过 keepId=true 参数保留 requestId 在 activeRequestIds 中，确保 onAgentMessage 跳过后续迟到 chunk，直到服务端 done:true 自然清理。

（4）网络短暂断开：底层 PartySocket 自动重连，重连成功后触发 reconnectToStream 流程，通过 CF_AGENT_STREAM_RESUME_REQUEST / CF_AGENT_STREAM_RESUMING / CF_AGENT_STREAM_RESUME_ACK 三步握手恢复流式接收。ResumableStream 将断开期间缓冲的 chunk 以 replay:true 标记回放，客户端 StreamAccumulator 批量应用后通过 replayComplete 信号切换到实时流。

（5）用户主动停止（stop()）：通过 AbortSignal 传递到 WebSocketChatTransport.sendMessages，触发 onAbort → CF_AGENT_CHAT_REQUEST_CANCEL。服务端 AbortRegistry.cancel(requestId) 触发对应 AbortController.abort()，onChatMessage 中的 abortSignal 被触发，streamText 推理流中断。已流式的 chunk 仍然持久化，ChatResponseResult 的 status 为 "aborted"。stop 同时调用 customTransport.abortActiveToolContinuation() 中断正在进行的 Tool 续接流。

### 服务端取消信号传递机制

取消信号在服务端的传递采用 AbortRegistry 模式。AbortRegistry 维护一个 Map<string, AbortController>，以 requestId 为键。当 WebSocket 消息处理器接收到 CF_AGENT_CHAT_REQUEST_CANCEL 时，调用 AbortRegistry.cancel(data.id)，触发对应 AbortController.abort()。onChatMessage 在被调用时接收 options.abortSignal，该 signal 由 AbortRegistry.getSignal(requestId) 返回，子类将其传递给 streamText 的 abortSignal 参数。

关键设计在于 requestId 的生命周期管理。客户端发送的每个 CF_AGENT_USE_CHAT_REQUEST 携带唯一的 requestId（nanoid 生成）。服务端在 TurnQueue.enqueue 之前注册 AbortController，Turn 完成后在 finally 块中调用 AbortRegistry.remove(requestId)。这种设计确保取消信号总能精确命中目标 Turn，不会误取消其他请求。TurnQueue 提供 generation 机制防止 race condition：当 CF_AGENT_CHAT_CLEAR 清空对话时，generation 自增，旧 epoch 的 Turn 即使完成也会被标记为 skipped 而非覆盖新对话状态。

### 流式响应持久化与断线恢复

ResumableStream 是实现「客户端断开不影响服务端执行」的关键组件。每个推理流启动时，ResumableStream.start(requestId) 在 SQLite 的 cf_ai_chat_stream_metadata 表中创建 status='streaming' 的记录，并生成唯一 streamId。流式 chunk 以 10 个为一批（CHUNK_BUFFER_SIZE=10），通过 storeChunk 方法批量写入 cf_ai_chat_stream_chunks 表，包含 chunk_index 实现有序回放。超过 1.8MB 的异常大 chunk 被跳过 SQLite 存储但仍广播至在线客户端。

断线恢复采用三步握手协议：（1）客户端发送 CF_AGENT_STREAM_RESUME_REQUEST；（2）服务端若存在活跃流，发送 CF_AGENT_STREAM_RESUMING（携带 activeRequestId），否则发送 CF_AGENT_STREAM_RESUME_NONE；（3）客户端收到 STREAM_RESUMING 后发送 CF_AGENT_STREAM_RESUME_ACK，服务端调用 replayChunks 按 chunk_index 升序回放所有已持久化 chunk（标记 replay:true），回放完成后发送 replayComplete:true 信号。客户端通过 StreamAccumulator 批量应用回放数据，避免逐 chunk 触发 React 重渲染。

DO 休眠后恢复（hibernation）时，ResumableStream.restore() 从 SQLite 查询 status='streaming' 的记录并重建 _activeStreamId 和 _streamChunkIndex。此时 _isLive=false 表示已无活跃 LLM Reader。若客户端重连并触发回放，replayChunks 检测到 !_isLive 后发送 done:true 并调用 complete() 终结该流，返回孤儿 streamId 供上层 onChatRecovery 钩子重建部分响应。

### 多标签页协同与迟到消息处理

服务端在广播流式 chunk 时排除 _pendingResumeConnections 中的连接（这些连接正在通过 replayChunks 回放历史数据），其余所有连接接收实时 chunk。客户端通过两个机制处理不同来源的 chunk：（1）WebSocketChatTransport 管理的 ReadableStream 直接注入 AI SDK 的 useChat 管线，对应本标签页发起的请求；（2）useAgentChat 的 onAgentMessage 处理器通过 broadcastTransition 状态机处理跨标签页或服务端发起的流（saveMessages、auto-continuation），使用 StreamAccumulator 将 chunk 组装为消息后通过 setMessages 合并到本地状态。

localRequestIdsRef 集合是关键的去重机制：本标签页通过 transport 发出的请求 ID 被记录在该集合中。onAgentMessage 收到 CF_AGENT_USE_CHAT_RESPONSE 时，先检查 data.id 是否在 localRequestIdsRef 中——若在，说明 transport 的 ReadableStream 已经处理该 chunk，直接跳过避免重复。对于跨标签页流，broadcastTransition 创建独立的 StreamAccumulator 实例，按 streamId 路由。

迟到服务端消息处理：当客户端 stop() 后，服务端可能仍有在途 chunk。onAbort 使用 keepId=true 保留 requestId 在 activeRequestIds 中，onAgentMessage 继续跳过这些迟到 chunk。当服务端最终发送 done:true 时，onAgentMessage 删除该 requestId，完成清理。对于已 cancel 的请求，服务端发送的后续 CF_AGENT_MESSAGE_UPDATED（Tool 结果更新）仍可通过消息 ID 匹配更新本地状态。

### 工具调用续接兼容

本方案完整兼容 Tool 调用续接（continuation）场景。当 Agent Turn 中模型调用工具且该工具需要客户端执行（通过 onToolCall 回调或 needsApproval 审批流程）时，客户端通过 addToolOutput 或 addToolApprovalResponse 将结果发送至服务端，服务端自动继续推理流。这一续接流程在客户端断开场景下有两种路径：

路径一：同一标签页保持连接。addToolOutput 发送 CF_AGENT_TOOL_RESULT 消息（携带 autoContinue:true），服务端 _applyToolResult 更新 Tool Part 状态后，通过 10ms 合并窗口（AUTO_CONTINUATION_COALESCE_MS）将相邻 Tool 结果合并为单次续接 Turn。续接 Turn 在 TurnQueue 中排队，使用 continuation:true 标志调用 onChatMessage，流式 chunk 标记 continuation:true 以追加到同一 assistant 消息。

路径二：客户端在 Tool 等待期间断开并重连。服务端的 ContinuationState 追踪续接状态：pending 状态记录正在等待 Tool 结果的 Turn；awaitingConnections 记录已发送 STREAM_RESUMING 但尚未 ACK 的连接。重连客户端通过 CF_AGENT_STREAM_RESUME_REQUEST 触发握手，若存在 pending continuation，服务端将连接放入 awaitingConnections。Tool 结果到达后，ContinuationState 通知 awaitingConnections 中的连接续接流已开始，同时通过 WebSocketChatTransport 的 _expectToolContinuation 标志使 reconnectToStream 返回延迟 ReadableStream——AI SDK 的 status 可立即转为 "submitted"，在服务端开始流式前即显示加载状态。

### 可配置的取消策略

系统通过 resume 选项和 messageConcurrency 配置为开发者提供灵活的取消策略控制：

默认 Durable 模式（resume=true）：客户端断开不取消服务端 Turn。WebSocketChatTransport.reconnectToStream 在重连时自动发送 CF_AGENT_STREAM_RESUME_REQUEST，ResumableStream 回放已持久化 chunk。服务端通过 ResumableStream 在 SQLite 中持续缓冲所有流式数据，DO 休眠后可通过 restore() 恢复。该模式适用于长时间 Agent 对话、多标签页观察、昂贵推理等场景。

可选 Request-Lifetime 模式（resume=false）：客户端断开时，服务端继续执行但不再为断开的连接缓冲回放数据。onAgentMessage 在收到 CF_AGENT_STREAM_RESUMING 时检查 resume 标志，若为 false 且 transport 不在等待 resume，则忽略该消息。该模式适用于对实时性要求高、不需要断线恢复的短对话场景。

messageConcurrency 进一步控制重叠提交策略：queue（默认）串行执行所有 Turn；latest 仅保留最新提交；merge 合并重叠提交的用户消息为单次 Turn；drop 忽略重叠提交；debounce 策略提供尾端防抖。这些策略仅影响 submit-message 类型请求，Tool 续接、审批、saveMessages 等保持串行行为不变。

### 技术效果

本方案相比现有方案具有以下技术效果：

（1）精确的取消语义：将客户端生命周期事件（刷新、卸载、Reader Cancel、网络断开）与服务端 Turn 取消明确分离，避免「刷新即取消」导致 Durable Object 中昂贵推理被意外中断。每个取消路径有独立的协议消息和状态管理。

（2）零丢失流式恢复：通过 ResumableStream 的 SQLite 持久化和三步握手回放协议，客户端断开重连后可完整恢复已流式但未展示的 chunk，不丢失任何推理结果。回放使用 replay:true 标记，客户端可批量应用优化渲染性能。

（3）多标签页一致性：通过 broadcast 机制和 localRequestIdsRef 去重，同一 DO 实例的多标签页可同时观察同一流式响应，各自独立重连互不干扰。任何标签页的 stop() 仅取消该标签页的本地流，不影响其他标签页和服务端执行。

（4）Tool 续接透明化：客户端断开期间 Tool 结果可正常送达服务端，续接 Turn 自动排队执行。重连客户端通过 ContinuationState 和 _expectToolContinuation 机制无缝接入续接流，UI 状态（isToolContinuation、isServerStreaming）正确反映续接进度。

（5）可配置的持久化策略：通过 resume 选项为不同应用提供默认 Durable 模式和可选 Request-Lifetime 模式，开发者无需修改服务端代码即可切换「客户端断开即取消」与「客户端断开不影响服务端执行」两种行为。messageConcurrency 提供五种并发策略适配不同交互场景。

### 风险与待确认问题

（1）当前 stop() 函数在取消服务端 Turn 后仍需等待服务端 done:true 帧完成最终清理。若服务端在收到 CF_AGENT_CHAT_REQUEST_CANCEL 后因异常未能发送 done:true（如 DO 在 cancel 处理期间被 evict），客户端 activeRequestIds 中的 requestId 将永久残留。建议增加客户端超时清理机制。

（2）ResumableStream 的 CHUNK_BUFFER_MAX_SIZE=100 限制在极端长响应（如连续数小时的流式输出）下可能成为瓶颈。超过 1.8MB 的单 chunk 被跳过存储，导致回放时缺失该段内容。建议增加分片存储策略或提升为可配置参数。

（3）当前 resume 选项仅控制客户端行为，服务端无论 resume 为何值都会执行 ResumableStream 持久化。若开发者明确选择 Request-Lifetime 模式，服务端仍消耗 SQLite 写入资源。建议服务端同步感知 resume 配置以优化存储开销。
