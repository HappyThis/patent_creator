## 技术方案

本方案提出一种 AI 代理会话生命周期管理系统，用于解决浏览器端 WebSocket 连接变化时，服务端 AI 代理对话回合（turn）的控制语义混淆问题。系统在客户端传输层引入连接意图追踪机制，在服务端引入可配置的回合生命周期策略，从而将“客户端本地清理”（浏览器刷新、组件卸载、流读取器取消、网络断开）与“显式取消意图”（用户点击停止按钮、应用主动取消）明确区分为两类独立事件，并在默认模式下确保服务端回合不受客户端断开影响，同时保留显式取消的即时传达能力。

### 技术问题定位

在基于 WebSocket 的 AI 代理对话系统中，浏览器与运行在 Durable Object 上的服务端代理之间维持长连接以传输流式响应。客户端 WebSocket 断开是一个多因事件，不同原因对应截然不同的服务端语义：浏览器刷新、React 组件卸载、ReadableStream reader.cancel()、网络暂时断开等属于客户端本地生命周期清理，服务端对话回合应继续执行并将产出持久化以便后续恢复；而用户点击“停止”按钮或应用调用 stop() 则表达明确的取消意图，应由客户端即时传达到服务端并终止当前回合的 LLM 推理。现有方案中，两类事件均可能触发底层传输的 abort 路径，导致客户端仅在本地清理时却意外向服务端发送了取消指令，造成服务端回合被误杀，浪费已完成的计算并破坏恢复能力。

### 整体架构

系统采用客户端-服务端分层架构。客户端运行在浏览器中，包含 useAgentChat React Hook、WebSocketChatTransport 传输层和新增的连接意图追踪器（ConnectionIntentTracker）。服务端运行在 Cloudflare Durable Object 上，包含 AIChatAgent 代理基类、AbortRegistry 取消注册表、ResumableStream 可恢复流管理器、ContinuationState 工具继续状态机和新增的回合生命周期策略（TurnLifetimePolicy）。客户端与服务端通过 WebSocket 连接，使用 CF_AGENT 协议族进行消息交换。

### 客户端生命周期与取消语义分离机制

客户端侧的连接意图追踪器（ConnectionIntentTracker）是区分两类断开事件的核心机制。其维护一个状态机，包含三种状态：active（连接正常活跃）、disconnecting-durable（执行本地清理，服务端回合应继续）、disconnecting-cancel（显式取消，服务端应终止回合）。

状态转换规则如下：（1）用户点击停止按钮或应用调用 stop() 时，意图追踪器先置为 disconnecting-cancel，再由 WebSocketChatTransport 的 abort 路径发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息到服务端，携带请求 ID；（2）React 组件卸载（useEffect cleanup）、浏览器刷新（beforeunload）、页面切换（router navigation）或 ReadableStream reader.cancel() 触发时，意图追踪器置为 disconnecting-durable，传输层仅关闭本地 WebSocket 连接和本地流读取器，不向服务端发送任何取消消息。网络断开（如 Wi-Fi 丢失）属于被动断开，客户端无法发送任何消息，自然等效于 disconnecting-durable 路径。

WebSocketChatTransport 在原有的 sendMessages 方法的 abort 处理路径中增加意图判断：当 abort 原因来自调用方传入的 AbortSignal（由 AI SDK 的 useChat 在 stop() 时触发）且意图追踪器状态为 disconnecting-cancel 时，发送 CF_AGENT_CHAT_REQUEST_CANCEL；当意图追踪器状态为 disconnecting-durable 时，跳过 CANCEL 消息发送，仅关闭本地 ReadableStream。这确保传输层只将真正的取消意图传递到服务端。

### 服务端执行模式策略

服务端 AIChatAgent 新增回合生命周期策略属性 turnLifetime，支持两种模式：durable（默认）和 request-lifetime。

在 durable 模式下，客户端断开时服务端不取消当前回合。AIChatAgent 的 onClose 处理程序检测到连接关闭后，检查 turnLifetime 策略：若为 durable，则不做任何取消操作，LLM 推理循环继续执行。ResumableStream 持续将流式响应块（chunk）持久化到 SQLite 数据库的表 cf_ai_chat_stream_chunks 中，并记录流元数据到 cf_ai_chat_stream_metadata。回合完成后，最终消息通过 persistMessages 写入持久存储。当同一客户端或其他标签页重新连接时，通过 STREAM_RESUME_REQUEST → STREAM_RESUMING → RESUME_ACK 握手流程，ResumableStream.replayChunks 回放已持久化的块，然后继续推送实时块。若流在重连时已完成，服务端返回 STREAM_RESUME_NONE，客户端通过 getAgentMessages HTTP 端点获取完整消息历史。

在 request-lifetime 模式下，客户端断开即取消。onClose 中检测到策略为 request-lifetime 且当前无主动 CANCEL 标记（避免重复取消），则调用 abortAllRequests()，通过 AbortRegistry.destroyAll() 终止所有活跃请求的 AbortController。LLM 推理循环检测到 signal.aborted 后终止。已持久化的部分块保留在 SQLite 中，重连客户端收到 STREAM_RESUME_NONE。

两种模式均兼容现有的 chatRecovery 属性。chatRecovery=true 时，回合通过 runFiber 包裹实现 Durable Object 级别的持久化执行，服务端即便遭遇 Durable Object 休眠（hibernation）或重启，也能在恢复后通过 onChatRecovery 钩子重建被中断的流。

### 流式响应断线重连与恢复

系统在 durable 模式下的断线重连流程与现有 ResumableStream 机制完全兼容。核心流程为：（1）客户端重连后，WebSocketChatTransport.reconnectToStream() 发送 CF_AGENT_STREAM_RESUME_REQUEST；（2）服务端检测到 activeStreamId 非空，发送 CF_AGENT_STREAM_RESUMING { id }；（3）客户端回复 CF_AGENT_STREAM_RESUME_ACK { id }；（4）服务端调用 replayChunks 按 chunk_index 顺序回放已持久化的响应块到该连接，每条回放块标记 replay: true；（5）回放完毕后发送 replayComplete: true 信号，客户端将累积的块刷新到 React 状态；（6）LLM 推理产出的新块直接以实时模式推送。对于已完成的流（流状态为 completed），replayCompletedChunksByRequestId 回放所有块并以 done: true 结束。以上流程在 durable 模式下对浏览器刷新、组件卸载等断开场景透明可用。

### 工具调用继续执行兼容

工具调用继续执行（tool continuation）与双模式策略兼容。当 AI 模型在回合中生成工具调用（tool call）且工具在客户端执行时，客户端通过 addToolOutput() 将结果发送回服务端，服务端 ContinuationState 管理 auto-continue 生命周期。

在 durable 模式下，若客户端在工具调用执行期间断开（如刷新页面），服务端 ContinuationState.pending 中保留了连接 ID 和请求上下文（clientTools、body、prerequisite），等待同一客户端重连后继续。客户端重连并发送 STREAM_RESUME_REQUEST 时，ContinuationState.awaitingConnections 中的连接被通知，继续执行后续回合。若工具结果已经在断开前送达，服务端在回合完成后将结果持久化；若工具结果未送达，重连后的客户端可以重新发送。

在 request-lifetime 模式下，客户端断开导致 abortAllRequests() 终止回合，pending 的 continuation 被清除。ContinuationState.sendResumeNone() 通知所有等待连接的客户端，后续由应用层决定是否重新发起对话。

### 多标签页协同与迟到消息处理

系统支持同一 Durable Object 被多个浏览器标签页同时连接。流量通过 broadcast-state 状态机管理：当标签页 A 发起对话并接收流式响应时，其他标签页（B、C）通过 broadcastTransition 状态机进入 observing 状态，接收广播的 CF_AGENT_USE_CHAT_RESPONSE 消息块，在本地构建相同的消息视图。

在 durable 模式下，当标签页 A 关闭或刷新时，服务端回合不受影响，标签页 B 继续观察。标签页 A 重连后通过 resume 流程恢复。当所有标签页均断开时，回合仍在服务端执行并持久化。

迟到服务端消息（late-arriving messages）处理：当客户端在断开期间服务端完成回合并持久化了消息，客户端重连后通过 getInitialMessages 获取完整消息列表。若流仍在进行中，resume 流程确保客户端不会丢失断开期间的任何块。CF_AGENT_CHAT_MESSAGES 广播消息允许服务端主动推送消息更新到所有连接的客户端。标识 replay: true 的块在播放完成后通过 replayComplete 信号触发批量刷新，避免与实时块产生顺序冲突。

### 技术效果

本方案相比现有方式带来以下技术效果：

- 取消语义精确化：客户端连接断开不再被一律视为取消意图，消除浏览器刷新、组件卸载等本地清理操作误杀服务端回合的问题。已产出的推理结果和流式响应块被完整保留并持久化，避免计算资源浪费。
- 会话恢复能力增强：durable 模式下，无论客户端断开多少次，回合均可在服务端完整执行并持久化，客户端重连后无缝恢复流式响应或获取完整结果，用户体验从“中断-重试”提升为“无感恢复”。
- 开发者可配置空间：turnLifetime 策略允许不同应用根据自身场景选择“断开不取消”或“断开即取消”，例如对长耗时数据分析任务使用 durable 模式，对实时性要求高的即时问答使用 request-lifetime 模式。
- 多标签页一致性保证：广播机制确保任一标签页的连接变化不影响其他标签页的观察体验，所有标签页始终保持消息视图同步。
- 与现有基础设施兼容：方案在现有 AbortRegistry、ResumableStream、ContinuationState、broadcast-state 等模块基础上进行最小改动，新增意图追踪器和策略检查点，不影响现有流式响应、工具调用继续、断线重连等功能。

### 风险与待确认问题

以下为需要后续确认的技术风险点：

- React StrictMode 效应：React 在开发模式下会双重挂载组件（mount-unmount-mount），这可能导致意图追踪器在 unmount 阶段误置为 disconnecting-durable，随后 mount 时需正确恢复为 active。需要在意图追踪器与组件生命周期的绑定中增加 StrictMode 防护。
- request-lifetime 模式下的多标签页信号：当多标签页同时连接且某一标签页因 request-lifetime 模式断开导致 abortAllRequests 时，其他标签页的观察体验将被中断。需考虑是否按连接数（剩余活跃连接数 > 0 时不取消）或按标签页角色（主控标签页断开才取消）细化策略。
- chatRecovery 与双模式的交互：chatRecovery=true 时回合包裹在 runFiber 中，Durable Object 休眠后恢复的回合被视为孤儿流（orphaned）。需验证孤儿流在双模式下的行为是否符合预期——durable 模式下孤儿流应被恢复而非清除。
- resume=false 场景下的空白列表：当用户设置 resume=false 时，重连后不发起流恢复，客户端消息列表可能短暂为空。需确保 getInitialMessages 在此时正确填充消息，与双模式策略无冲突。
- SQLite 清理策略：持久化的流块和元数据在 cleanup 定时任务中清理（默认 24 小时），需确认该策略在长期运行的 durable 回合场景下不会过早清理未恢复的流数据。
