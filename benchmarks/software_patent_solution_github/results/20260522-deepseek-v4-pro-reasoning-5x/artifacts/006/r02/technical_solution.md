## 技术方案

本技术方案针对基于 WebSocket 的长时间 agent 对话场景，提出一种客户端生命周期清理与服务端 agent turn 取消的显式分离机制，使系统能够区分浏览器刷新、组件卸载、reader cancel、网络断开等本地事件与用户主动点击「停止」按钮的取消意图，并提供默认可持久（durable）模式和可选请求生命周期（request-lifetime）模式，兼容流式响应恢复、工具调用继续执行和多标签页观察场景。

### 1. 技术问题

在基于 WebSocket 的 agent 聊天系统中，客户端生命周期事件（浏览器刷新、组件卸载、页面切换、网络短暂断开等）与服务端 agent turn 之间存在语义混淆。当前方案中，客户端在卸载时可能触发 stream.cancel()，进而通过传输层向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，导致已在 Durable Object 中持续运行的 agent turn 被意外终止。然而，在默认的 durable 模式下，agent turn 本应独立于客户端连接而持续执行，并支持后续通过 WebSocket 重新连接恢复流式响应。另一方面，当用户确实希望取消本次 agent 回答时，系统需要一个明确的「停止」通道，将取消意图可靠地传达到服务端，而非仅关闭本地显示流。

### 2. 核心技术方案

核心技术方案引入一个显式的「取消模式」配置项（cancelMode），取值为 "durable"（默认）或 "request-lifetime"。两种模式下，客户端生命周期清理和用户主动取消采取截然不同的处理路径。

在 durable 模式下（默认），客户端组件卸载、浏览器刷新或网络断开时，传输层仅关闭本地 ReadableStream 读取器和事件监听，不向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。服务端 agent turn 在 Durable Object 中持续执行，其 ResumableStream 管理器将流式数据块（chunk）持久化到 SQLite 存储中。当客户端重新建立 WebSocket 连接后，通过流恢复协议（STREAM_RESUME_REQUEST → STREAM_RESUMING → STREAM_RESUME_ACK → replay chunks）恢复接收未完成的流式响应。该机制通过 TurnQueue 序列化保证同一时刻只有一个 turn 在执行，避免恢复时的竞态条件。

当用户主动点击「停止」按钮时，useAgentChat 暴露的 stop() 方法通过传输层向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，携带当前活跃的 requestId。服务端 AbortRegistry 查找对应的 AbortController 并触发 abort()，该信号传播到 onChatMessage 中 streamText/generateText 调用的 abortSignal 参数，终止底层 LLM 推理。同时，TurnQueue 中的排队 turns 若 generation 不匹配则自动跳过。

### 2.1 request-lifetime 模式

在 request-lifetime 模式下，系统将 agent turn 的生命周期绑定到发起请求的 WebSocket 连接。当客户端检测到组件卸载或网络断开时，传输层自动向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，服务端终止对应的 agent turn。该模式适用于对延迟敏感、不需要跨会话恢复的简单对话场景。开发者通过 useAgentChat({ cancelMode: "request-lifetime" }) 显式选择该模式。

### 3. 关键模块与处理流程

本方案涉及客户端钩子层、传输层、服务端 Agent 层和线协议层四个层次的协同配合。

### 3.1 客户端钩子层

useAgentChat 钩子新增 cancelMode 配置项（"durable" | "request-lifetime"，默认 "durable"）。钩子内部维护 intentRef 记录当前操作意图。

stop() 方法在 durable 模式下，设置 intentRef = "user-cancel"，通过传输层发送 CF_AGENT_CHAT_REQUEST_CANCEL（携带 requestId），并关闭本地流读取器。组件卸载时（useEffect cleanup）仅释放本地资源（移除监听器、取消 ReadableStream、清除定时器），不发送取消消息。request-lifetime 模式下 cleanup 额外调用 stop()。通过 intentRef 检查避免重复发送取消消息。

### 3.2 传输层

WebSocketChatTransport 在 sendMessages 方法中创建的 ReadableStream 内部维护一个 abortError 对象。当流被 cancel() 时，onAbort 回调被触发，该回调根据 cancelMode 决定是否向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL。

在 durable 模式下，onAbort 仅完成本地清理（close stream controller），不发送协议取消消息。在 request-lifetime 模式下或当 intentRef 标记为 "user-cancel" 时，onAbort 向服务端发送 JSON 编码的 { type: "cf_agent_chat_request_cancel", id: requestId } 消息。传输层的 reconnectToStream 方法在 durable 模式下通过 STREAM_RESUME_REQUEST → STREAM_RESUMING → STREAM_RESUME_ACK 三步握手恢复流。服务端通过 ResumableStream 按 chunk_index 顺序重放 SQLite 中持久化的数据块。

### 3.3 服务端 Agent 层

AIChatAgent 在 onMessage 中处理 CF_AGENT_CHAT_REQUEST_CANCEL 消息：调用 AbortRegistry.cancel(requestId) 触发对应 AbortController 的 abort()。该信号通过 OnChatMessageOptions.abortSignal 传递到 streamText/generateText 调用，终止底层 LLM 推理循环。被取消的 turn 通过 ChatResponseResult（status: "aborted"）通知 onChatResponse 钩子。

chatRecovery 属性为 true 时，chat turn 通过 runFiber 包裹执行，实现 Durable Object 级别的持久化。即使 DO 被逐出内存后重启，Fiber 恢复机制通过 onChatRecovery 钩子重建中断的流状态，从 ResumableStream 中读取已持久化的数据块重建部分响应消息，并可选择性地通过 continueLastTurn 继续执行未完成的 turn。在 durable 模式下，即使所有客户端连接均已断开，DO 仍然保持活跃（通过 keepAlive 机制），agent turn 不会被中断。

### 3.4 线协议层

本方案复用现有 WebSocket 线协议的消息类型体系，不做破坏性变更。关键消息类型及其语义：

- CF_AGENT_CHAT_REQUEST_CANCEL（客户端→服务端）：显式取消指定 requestId 对应的 agent turn。仅在用户主动停止或 request-lifetime 模式下自动发送，durable 模式下的客户端生命周期清理不发送此消息。
- CF_AGENT_STREAM_RESUME_REQUEST / CF_AGENT_STREAM_RESUMING / CF_AGENT_STREAM_RESUME_ACK：三步流恢复握手协议，在客户端重连后恢复接收未完成的流式响应。服务端仅在 ResumableStream.hasActiveStream() 为 true 时响应 STREAM_RESUMING。
- CF_AGENT_USE_CHAT_RESPONSE（continuation: true）：标记当前响应块是工具调用后的自动继续执行结果，客户端将其追加到同一 assistant 消息而非创建新消息。
- CF_AGENT_CHAT_CLEAR：清空对话历史，同时使所有排队 turns 因 generation 不匹配而自动跳过。

### 4. 工具调用继续执行兼容性

工具调用继续执行是本方案的重要兼容场景。在 agent turn 执行过程中，LLM 可能发起工具调用（tool call），部分工具需要客户端侧执行（如浏览器 API）或用户审批（needsApproval）。

在 durable 模式下，当客户端因刷新或网络断开而暂时不可用时，服务端通过 hasPendingInteraction() 检测是否存在等待客户端工具结果或审批的 assistant 消息。若有，服务端保持 turn 活跃但不推进，直到客户端重连后通过 onToolCall 回调或 addToolApprovalResponse 提交结果。服务端通过 autoContinueAfterToolResult 配置（默认 true）在收到工具结果后自动触发 continuation turn，追加 LLM 响应到同一 assistant 消息。

当用户执行显式 stop() 时，服务端不仅取消当前 LLM 推理，还清理 ContinuationState 中等待工具结果的延迟继续执行（deferred continuation），并通过 abortActiveToolContinuation() 终止客户端侧挂起的工具继续执行流。

### 5. 多标签页兼容性

多标签页场景通过以下机制兼容：每个标签页通过 useAgent 建立的 WebSocket 连接拥有独立的 connection.id。服务端在广播流式数据块时，将正在等待流恢复的连接的 connection.id 加入排除列表（_pendingResumeConnections），避免这些连接在完成 ACK 前收到重复块。

当一个标签页发送 CF_AGENT_CHAT_REQUEST_CANCEL 时，服务端 AbortRegistry 取消对应 turn，并通过 broadcast 向所有其他标签页广播 CF_AGENT_USE_CHAT_RESPONSE（done: true）帧，使它们同步更新 UI 状态。不同标签页的 stop() 调用互相独立——一个标签页的取消不影响其他标签页的独立请求。

CF_AGENT_CHAT_CLEAR 消息通过广播机制同步到所有标签页，各标签页的 onAgentMessage 处理器调用 resetLocalChatState() 清空本地消息状态。

### 6. 迟到服务端消息处理

迟到服务端消息（late server message）指客户端生命周期清理后、服务端仍在产生的流式数据块。在 durable 模式下，这些数据块被 ResumableStream 持久化到 SQLite 中。当客户端重连时，通过流恢复协议完整接收。服务端通过 _pendingResumeConnections 集合追踪正在等待 ACK 的连接，在 live 广播中排除这些连接，防止收到重复块。

localRequestIdsRef 机制确保同一标签页的传输层处理过的请求不会在 onAgentMessage 中重复处理。跨标签页的迟到消息通过 broadcastTransition 状态机管理，在 replay 标记为 true 的块中跳过已在 hydrated 消息中存在的文本前缀（collapseHydratedReplayTextParts）。服务端在流完成后通过 persistMessages 将完整 assistant 消息写入 SQLite，确保后续重连时通过 /get-messages HTTP 端点或 CF_AGENT_CHAT_MESSAGES 广播获取完整历史。

### 7. 必要技术特征

本方案的关键技术特征包括：

- 取消意图分化机制：通过 intentRef 和 cancelMode 配置，在传输层区分「客户端本地清理」与「用户主动取消服务端 turn」，前者在 durable 模式下仅关闭本地读取器不发送协议取消消息，后者发送 CF_AGENT_CHAT_REQUEST_CANCEL。
- 双模式架构：默认 durable 模式保证 agent turn 独立于客户端连接持续执行并支持流恢复；可选 request-lifetime 模式将 turn 生命周期绑定到请求连接，满足不同应用偏好。
- 流恢复协议：三步握手 + SQLite chunk 持久化 + replay 机制，使客户端重连后能完整恢复未完成的流式响应，包括工具调用中间状态（如审批请求的 approval-requested 状态已提前写入 SQLite）。
- 工具调用兼容：durable 模式下等待客户端工具结果时保持 turn 活跃；显式取消时清理 ContinuationState 中延迟继续执行和客户端挂起的工具继续执行流。
- 多标签页广播与排除：_pendingResumeConnections 集合确保正在恢复的连接不收到重复块；跨标签页取消通过 broadcast 同步 UI 状态。

### 8. 技术效果

相比现有方案，本方案带来以下技术效果：

- 避免意外终止：浏览器刷新、组件卸载、网络闪断等常见客户端事件不再导致服务端 agent turn 被意外终止，减少重复推理造成的 LLM token 浪费。
- 可控取消通道：用户点击「停止」按钮时，取消意图通过专用协议消息可靠传递到服务端并终止 LLM 推理循环，而非仅关闭本地 UI 流。
- 开发者可配置：通过 cancelMode 选项，不同应用可选择适合自己的生命周期策略——需要跨会话恢复的长对话使用 durable 模式，简单请求-响应场景使用 request-lifetime 模式。
- 流恢复完整性：durable 模式下的流恢复不仅恢复文本内容，还恢复工具调用状态（包括审批请求），使客户端重连后的 UI 状态与服务端完全一致。
- 与现有生态兼容：复用现有线协议消息类型、AI SDK useChat 接口和 Durable Object 基础设施，以增量方式实现而不破坏现有功能。

### 9. 与项目环境的对应关系

本方案基于当前项目环境中 @cloudflare/ai-chat 包的现有实现进行增强。核心改造点对应关系：

1. cancelMode 配置项：在 useAgentChat 的 UseAgentChatOptions 类型中新增，默认值为 "durable"。
2. intentRef 和 stop() 增强：在 react.tsx 的 useAgentChat 函数体中新增 useRef 维护取消意图；修改 stop 回调（当前为 stopWithToolContinuationAbort）的行为逻辑。
3. 传输层 cancel 路由：在 ws-chat-transport.ts 的 WebSocketChatTransport.sendMessages 方法中，修改 onAbort 回调，根据 cancelMode 和 intentRef 决定是否发送 CF_AGENT_CHAT_REQUEST_CANCEL。
4. 服务端 AbortRegistry：当前已存在 _abortRegistry 和 CF_AGENT_CHAT_REQUEST_CANCEL 处理逻辑，本方案在其基础上增加与 chatRecovery 模式的联动——durable 模式下 fiber 恢复后检查取消状态。
5. ResumableStream 和流恢复协议：当前已实现，本方案直接复用。chatRecovery 和 Fiber 恢复机制当前已实现，本方案确保在 durable 模式下默认启用。

### 10. 风险与待确认问题

以下事项建议在后续实施中确认：

- intentRef 与 AI SDK useChat 内部状态同步：useChat 的 stop() 和 status 状态由 AI SDK 内部控制，需确认 intentRef 的更新时序与 AI SDK 内部状态转换之间无竞态，尤其在快速连续点击「停止」按钮的场景。
- request-lifetime 模式下连接断开检测时机：WebSocket 的 onClose 事件触发时机受网络环境（如代理、负载均衡器超时）影响，需确认在 request-lifetime 模式下的取消消息发送可靠性，可考虑增加重试或确认机制。
- chatRecovery 与用户取消的交互：当 DO 从 hibernation 恢复并执行 onChatRecovery 时，如果原始 turn 在断线前已被用户取消，需确认恢复逻辑不会重新启动已取消的 turn。可通过在 SQLite 中持久化取消状态标记来解决。
- 多标签页 stop 的语义边界：不同标签页对同一 conversation 发送 stop 时，当前设计为每个标签页仅取消自己的请求。需确认是否需要在某些场景下支持「取消该 conversation 的当前活跃 turn」（跨标签页取消）。
- cancelMode 命名与 AI SDK 未来版本的兼容性：AI SDK 的 useChat 未来版本可能引入类似概念，需关注命名冲突和语义对齐。
