## 技术方案

本技术方案针对基于 Cloudflare Durable Objects（DO）的 AI Agent 对话系统，提出一种客户端连接生命周期与服务端 Agent Turn 取消的语义分离机制，使开发者可配置控制浏览器刷新、组件卸载、网络断开等本地清理行为是否触发服务端任务取消。

本方案基于现有 @cloudflare/ai-chat 包中的 useAgentChat 钩子和 WebSocketChatTransport 传输层，以及 @cloudflare/agents 包中的 AIChatAgent 基类和 AbortRegistry 取消注册表进行增强设计。

### 技术问题

在现有架构中，useAgentChat 内部使用 WebSocketChatTransport 作为 AI SDK useChat 的传输层。当 useChat 的 AbortSignal 被触发时（原因包括：React 组件卸载、浏览器刷新、网络断开导致 WebSocket 关闭、AI SDK 内部 reader.cancel() 被调用、以及用户调用 stop()），传输层的 onAbort 回调统一向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。服务端 AIChatAgent 收到此消息后，通过 AbortRegistry.cancel() 取消对应请求的 AbortController，导致正在执行的 LLM 推理流中断。

这种统一处理的后果是：浏览器刷新、组件卸载或网络短暂断开等纯客户端生命周期事件，会被错误地转译为服务端任务取消信号。由于 Agent 对话已在 DO 中持久化运行，并支持通过 ResumableStream 机制在客户端重连后恢复流式输出，无差别取消导致用户失去恢复对话的能力，浪费已消耗的 LLM Token 和计算资源。

### 核心技术方案：连接模式与取消语义分离

核心思路是在客户端传输层引入连接模式（ConnectionMode）配置，将触发 AbortSignal 的原因分为两类：用户取消意图（user-initiated cancel）和客户端生命周期清理（client-side lifecycle cleanup）。仅用户取消意图才向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL；生命周期清理仅关闭本地流读取器和事件监听器，不通知服务端取消。

连接模式通过 useAgentChat 的 options.connectionMode 配置项传入，默认值为 'durable'。该值向下传递至 WebSocketChatTransport 构造函数，存储在私有字段 _connectionMode 中。

durable 模式下的行为：当 useChat 的 AbortSignal 触发且不是由用户调用 stop() 引起时，传输层的 onAbort 回调不发送 CF_AGENT_CHAT_REQUEST_CANCEL，仅执行本地清理——关闭 ReadableStream controller、移除事件监听器、标记请求完成。服务端 DO 中的 Agent Turn 继续执行，流式块通过 ResumableStream 持久化到 SQLite。当客户端重新连接时（同一标签页刷新或新标签页），reconnectToStream 发出 CF_AGENT_STREAM_RESUME_REQUEST，服务端从 SQLite 回放已缓冲的块并继续转发实时块。

request-lifetime 模式下的行为：保持与现有实现一致。任何原因导致的 AbortSignal 触发均发送 CF_AGENT_CHAT_REQUEST_CANCEL。服务端 AbortRegistry.cancel() 取消对应 AbortController，LLM 推理流中断，部分已流式输出的块仍被持久化但不再继续生成新块。

### 取消意图识别：stop() 与生命周期清理的分离

为实现取消语义分离，在 WebSocketChatTransport 的 sendMessages 方法中，将触发 onAbort 的路径分为两条：

- 服务端取消通道（serverCancelPath）：由用户显式调用 stop() 触发。stopWithToolContinuationAbort 在调用 AI SDK stop() 之前，先通过 transport.sendCancelIntent(requestId) 标志当前请求为「用户取消」。onAbort 检测到此标志后，向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL。
- 本地清理通道（localCleanupPath）：由 AbortSignal 触发但未设置取消标志。onAbort 仅执行本地清理——调用 finish() 关闭流控制器、移除事件监听器、从 activeRequestIds 中移除请求 ID（或根据模式保留 ID 以跳过迟到块的广播处理）。

具体实现：WebSocketChatTransport 中新增私有字段 _cancelIntentFlags: Set<string>，用于记录哪些请求 ID 已被用户标记为取消。新增公开方法 sendCancelIntent(requestId: string): void，由 stopWithToolContinuationAbort 在调用 AI SDK stop() 之前调用。onAbort 回调中，首先检查 _cancelIntentFlags.has(requestId)：若为 true，则发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息并从集合中移除；若为 false 且 connectionMode 为 'durable'，则跳过取消发送，仅做本地清理。

### 关键模块修改：WebSocketChatTransport

WebSocketChatTransport 的 sendMessages 方法需要以下修改：

- 新增构造函数参数 connectionMode: 'durable' | 'request-lifetime'，存入私有字段 _connectionMode。
- 新增私有字段 _cancelIntentFlags: Set<string>，追踪哪些请求 ID 已被用户显式标记为取消。
- 新增公开方法 sendCancelIntent(requestId: string): void，供 useAgentChat 的 stopWithToolContinuationAbort 调用。
- onAbort 回调逻辑修改：检查 _cancelIntentFlags 和 _connectionMode，决定是否发送 CF_AGENT_CHAT_REQUEST_CANCEL。
- 在 durable 模式下，finish() 调用时若因本地清理触发（非用户取消），保留 requestId 在 activeRequestIds 中直到服务端发送 done:true 或超时清理，以正确跳过后续迟到块的广播处理。

### 关键模块修改：useAgentChat

useAgentChat 钩子需要以下修改：

- UseAgentChatOptions 新增 connectionMode?: 'durable' | 'request-lifetime' 字段，默认值为 'durable'。
- connectionMode 向下传递至 WebSocketChatTransport 构造函数。
- 修改 stopWithToolContinuationAbort：在 await stop() 之前，获取当前活跃请求 ID（从 activeRequestIds 中），调用 customTransport.sendCancelIntent(requestId)，然后执行 AI SDK 的 stop()。此顺序确保 stop() 触发的 AbortSignal 被 onAbort 捕获时，_cancelIntentFlags 已包含该请求 ID。
- stopWithToolContinuationAbort 在 finally 块中仍然调用 abortActiveToolContinuation()，以处理正在等待服务端握手响应的工具延续流。

### 关键模块修改：服务端 AIChatAgent

服务端 AIChatAgent 本身不需要结构性修改——现有的 AbortRegistry、ResumableStream、TurnQueue 和 CF_AGENT_CHAT_REQUEST_CANCEL 处理路径均可复用。但建议以下增强：

- AbortRegistry 新增 cancelReason 记录：cancel 方法已支持 reason 参数，在用户取消路径中传入结构化原因（如 { source: 'user-stop' }），以便 onChatResponse 的 ChatResponseResult.status 正确上报 'aborted'，区别于其他异常终止。
- 新增 observable 事件 message:cancel（已存在 emit 调用），允许开发者在 onChatResponse 或事件监听中区分用户取消与其他终止原因。

### 多标签页场景兼容

本方案与现有多标签页广播机制（BroadcastState）兼容。在 durable 模式下，一个标签页的浏览器刷新或关闭仅触发本地清理，不通知服务端取消。其他标签页通过 CF_AGENT_USE_CHAT_RESPONSE 广播继续接收流式块，streamStateRef 状态机中的 observing 状态允许旁观标签页不参与取消决策。仅发起请求的标签页（拥有 localRequestIdsRef 中的请求 ID）才有权调用 stop() 触发服务端取消，其他标签页的 stop() 调用仅清理本地流观察状态。

### 工具延续兼容

工具延续（tool continuation）场景中，客户端发送 addToolOutput 或 addToolApprovalResponse 后，服务端自动继续 LLM 推理并生成新的流式响应。在 durable 模式下，若客户端在工具延续等待期间断开连接（如浏览器刷新），服务端仍继续执行推理，流式块通过 ResumableStream 持久化。客户端重连后，reconnectToStream 中 _expectToolContinuation 标志的处理路径不受影响——服务端仍然通过 onConnect 中的 _notifyStreamResuming 或客户端 RESUME_REQUEST 回复 CF_AGENT_STREAM_RESUMING，触发 ACK 和流回放。

### 迟到服务端消息处理

在 durable 模式下，客户端断开后服务端可能继续发送 CF_AGENT_USE_CHAT_RESPONSE 块。处理策略如下：

- 服务端通过 sendIfOpen 检测 WebSocket 是否已关闭（捕获 'WebSocket send() after close' TypeError），安全跳过无法送达的块。
- ResumableStream 的 SQLite 持久化独立于 WebSocket 连接状态，所有块均被写入数据库。
- 客户端重连后通过 RESUME_REQUEST → RESUME_ACK → 流回放路径获取断开期间的所有块。
- onAgentMessage 处理器中，对不属于 localRequestIdsRef 且 streamStateRef 非 observing 的响应块，进行安全跳过，避免迟到块污染本地状态。

### 技术效果

本方案相比现有统一取消机制，具有以下技术效果：

- 语义正确性：首次在客户端传输层明确区分「用户取消意图」与「客户端生命周期清理」，避免浏览器刷新等被动事件被错误转译为服务端任务取消。
- 对话连续性：在 durable 模式下，Agent 对话在 DO 中持续运行，用户可刷新页面或切换标签页后无缝恢复流式输出，不损失已生成的 LLM Token。
- 资源效率：避免因客户端短暂断开（如网络抖动、移动端切换应用）导致正在执行的 LLM 推理被取消后需要重新发起，减少重复计算和 API 调用成本。
- 开发者可控性：通过 connectionMode 配置项，应用开发者可根据业务场景选择 durable 或 request-lifetime 策略，而非被框架行为强制约束。
- 向后兼容：request-lifetime 模式保持现有行为，现有应用无需修改即可继续工作；connectionMode 默认为 'durable'，新应用自动获得改进体验。
- 多标签页安全：通过请求所有权检查（localRequestIdsRef），确保只有发起请求的标签页能取消服务端任务，旁观标签页的 stop() 不会误伤其他标签页的对话。

### 风险与待确认问题

本方案存在以下需要后续确认的风险点：

- activeRequestIds 访问时机：stopWithToolContinuationAbort 在调用 stop() 前需要从 activeRequestIds 获取当前请求 ID。需要确认在 AI SDK 的 useChat 中，stop() 调用时 activeRequestIds 是否仍然包含该 ID，或需使用独立 Ref 追踪。
- 工具延续取消：在 durable 模式下，若用户在工具延续等待握手期间调用 stop()，_abortToolContinuation 中也需要区分模式——若 requestId 尚未从服务端获得（握手未完成），durable 模式下不应发送取消。
- DO 资源占用：durable 模式下，没有客户端连接的 Agent Turn 将持续占用 DO 资源（内存、CPU 时间），需依赖现有的 keepAlive 和 fiber 机制控制最长执行时间。建议增加可配置的孤儿 Turn 超时时间。
- 多标签页取消归属：当前方案通过 localRequestIdsRef 判断请求所有权，但若同一用户从两个标签页分别发起两个独立请求，第二个标签页不应取消第一个标签页的请求。此场景已由 TurnQueue 序列化保证同一时间只有一个活跃 Turn，但需确保 cancelIntent 的请求 ID 校验正确。
- 与 AI SDK 的耦合度：方案依赖 useChat 的 AbortSignal 行为特征。若 AI SDK 未来版本修改 AbortSignal 触发时机或 stop() 内部实现，需同步调整 _cancelIntentFlags 的设置时机。
