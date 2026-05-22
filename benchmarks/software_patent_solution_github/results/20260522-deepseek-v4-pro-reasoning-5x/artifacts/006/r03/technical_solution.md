## 技术方案

本技术方案针对基于 WebSocket 的长连接 agent 对话系统（以 useAgentChat 客户端钩子与 AIChatAgent 服务端 Durable Object 为典型架构），提出一种区分「客户端本地生命周期清理」与「服务端 agent turn 取消」的控制方法，使系统能够在浏览器刷新、组件卸载、页面切换或网络短暂断开时保持服务端 agent 对话继续执行，同时为用户提供明确的停止能力，将取消意图可靠地传播至服务端。

### 技术问题分析：断开与取消的语义混淆

在现有基于 WebSocket 的 agent 对话架构中，客户端存在多种触发本地流清理的场景：浏览器刷新导致页面重新加载、React 组件卸载（如导航离开对话页面）、ReadableStream 的 reader.cancel() 调用、以及 WebSocket 连接因网络波动而关闭。这些场景在现有实现中走不同的处理路径，形成断开与取消的语义混淆。

具体而言，WebSocket 的 onClose 事件处理器仅执行本地 controller.close() 来结束本地可读流，不向服务端发送任何取消消息，服务端的 Durable Object 继续执行 agent turn 并将流式数据块缓冲至 SQLite。而用户点击停止按钮（调用 stop() 函数）或 ReadableStream.cancel() 则触发 onAbort 路径，向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，服务端通过 AbortRegistry 查找对应请求的 AbortController 并调用 cancel()，从而终止正在执行的 LLM 流。

该混淆导致以下问题：第一，AI SDK 的 useChat 钩子在组件卸载时可能内部调用 reader.cancel()，使得「组件卸载」意外等同于「用户停止」，破坏了期望的 durable 语义。第二，应用开发者无法配置「客户端断开是否代表取消服务端任务」的策略，不同场景下的合理行为被硬编码。第三，多标签页场景下，一个标签页的组件卸载不应影响其他标签页正在观察的同一 agent turn。

### 核心技术方案：断开模式配置与取消传播分离

本方案引入一种「断开模式」（Disconnect Mode）配置机制，将客户端的 WebSocket 生命周期事件与服务端 agent turn 的取消控制解耦。系统支持两种模式：持久模式（durable）和请求生命周期模式（request-lifetime），默认为 durable 模式。

在 durable 模式下，客户端因浏览器刷新、组件卸载、页面切换、网络断开等原因导致的 WebSocket 连接关闭，仅执行本地资源清理（移除事件监听器、关闭本地 ReadableStream 读取端、清理 AbortController），不向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。服务端 Durable Object 继续执行 agent turn，通过 ResumableStream 将流式数据块持续缓冲至 SQLite。当客户端重新建立 WebSocket 连接后，通过 CF_AGENT_STREAM_RESUME_REQUEST / CF_AGENT_STREAM_RESUMING 握手协议恢复流，接收缓冲块和后续实时块。

在 request-lifetime 模式下，WebSocket 连接关闭的行为与 durable 模式相反：连接关闭时，客户端在本地清理之前先向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL，服务端 AbortRegistry 终止对应的 LLM 流。该模式适用于短对话、不需要恢复的简单问答场景。

### 停止意图的传播机制

为将用户停止意图与本地生命周期清理彻底分离，本方案在 WebSocket 传输层（WebSocketChatTransport）中引入「取消意图标记」（Cancel Intent Flag）机制。该标记为每个活跃请求维护一个布尔状态，区分三种触发源：

类型一：用户主动停止。当用户调用 stop() 函数或与之等效的 UI 操作时，设置取消意图标记为 true，然后通过 WebSocket 发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。服务端 AbortRegistry.cancel(requestId) 触发对应 AbortSignal 的 abort 事件，streamText 调用感知到 abortSignal 后终止 LLM 推理循环。同时，客户端的 abortActiveToolContinuation() 终止正在等待的工具继续执行流。

类型二：应用级主动取消。通过暴露 abortRequest(requestId)、abortActiveTurn()、abortAllRequests() 三级程序化接口，允许应用代码在不依赖用户 UI 操作的情况下主动取消特定请求、当前活跃 turn 或所有请求。这些接口同样设置取消意图标记并传播至服务端。

类型三：本地生命周期清理。在 durable 模式下，WebSocket onClose、组件卸载时的清理、reader.cancel() 均不设置取消意图标记，仅关闭本地 ReadableStream 并移除事件监听器。关键在于：组件卸载时，通过检查取消意图标记来决定是否将 reader.cancel() 所触发的 onAbort 回调实际发送 CANCEL 消息——如果标记为 false，onAbort 变为空操作，仅执行本地流关闭。

### 多标签页场景处理

多标签页场景下，同一 agent Durable Object 实例可被多个浏览器标签页通过各自独立的 WebSocket 连接同时观察。本方案需要确保：单个标签页的本地生命周期清理不影响其他标签页的观察，而单个标签页的停止操作应通过服务端广播使所有标签页及时感知。

具体机制如下：当标签页 A 的用户点击停止时，标签页 A 发送 CF_AGENT_CHAT_REQUEST_CANCEL 至服务端。服务端 AbortRegistry.cancel(requestId) 触发 LLM 流终止，当前消息以 done:true 和 status:aborted 结束。该终止状态通过 CF_AGENT_USE_CHAT_RESPONSE（done:true）广播至所有已连接的标签页（包括标签页 B、C），各标签页的 onAgentMessage 处理器接收该消息并更新本地 UI 状态。

当标签页 A 在 durable 模式下因刷新或关闭而断开时，其 WebSocket 连接关闭但服务端 agent turn 继续运行。标签页 B 和 C 不受任何影响——它们的 WebSocket 连接保持活跃，继续接收实时流式数据块。标签页 A 重新打开后，通过 CF_AGENT_STREAM_RESUME_REQUEST 发起恢复握手，接收从断开时刻起的缓冲数据块和后续实时数据块。

在实现层面，每个请求通过 activeRequestIds 集合跟踪，WebSocket 传输层的 onAgentMessage 处理器根据 activeRequestIds 判断收到的响应块是否来自本地发起的请求。对于来自服务端广播的、非本地发起的流（如其他标签页发起的 saveMessages 调用），则通过 broadcastTransition 状态机处理，确保跨标签页的消息列表同步。

### 与流式响应、工具延续及迟到消息的兼容

本方案与现有流式响应、断线重连、工具调用继续执行机制完全兼容，并对其进行语义增强。

流式响应兼容：在 durable 模式下，服务端 agent turn 不因客户端断开而终止，streamText 调用继续生成 UIMessageChunk 并通过 ResumableStream 缓冲至 SQLite。客户端重连后，通过 CF_AGENT_STREAM_RESUME_REQUEST → CF_AGENT_STREAM_RESUMING → CF_AGENT_STREAM_RESUME_ACK 三阶段握手获取缓冲块（带 replay:true 标记），随后接收实时块。该流程不因断开模式配置而改变，仅在 durable 模式下保证服务端不会因 onAbort 路径被意外触发而提前终止流。

工具继续执行兼容：当 agent turn 中包含客户端工具调用（onToolCall 回调模式）且 autoContinueAfterToolResult 为 true 时，客户端发送 CF_AGENT_TOOL_RESULT 后服务端自动排队一个 continuation turn。该 continuation 在 TurnQueue 中排队，等待当前流完成后执行。在 durable 模式下，即使发起工具调用的原始客户端在工具执行期间断开，服务端仍保留待处理的工具状态（input-available 或 approval-requested 状态持久化在 SQLite 消息中），重连的客户端可通过 hasPendingInteraction() 检测待处理交互，通过 waitUntilStable() 等待继续执行队列排空。

迟到服务端消息处理：在 durable 模式下，客户端断开后服务端可能继续产生新的消息（如 continuation turn 完成后的最终 assistant 消息）。当客户端重连时，这些消息已持久化至 SQLite。重连后，useAgentChat 通过 /get-messages 端点拉取完整的消息列表，与本地状态通过 reconcileMessages 逻辑合并：服务端已有的工具输出覆盖客户端的待处理状态，ID 匹配和内容键匹配双通道解决客户端的乐观 ID 与服务端 ID 不一致问题。该机制确保重连后的消息列表完整且无重复。

### 关键模块与处理流程

断开模式配置通过 useAgentChat 钩子的可选参数 disconnectMode 暴露给开发者，类型为 'durable' | 'request-lifetime'，默认值为 'durable'。配置通过 WebSocketChatTransport 的构造函数传入，存储在传输层实例的 _disconnectMode 字段中。

WebSocketChatTransport 的 sendMessages 方法中，现有 onAbort 和 onClose 两个处理路径被重构为统一的 finish(closeAction, opts) 辅助函数。opts 参数新增 cancelIntent 字段。onAbort 路径（用户停止/reader.cancel）调用 finish(closeAction, { keepId: true, cancelIntent: true })；onClose 路径（WebSocket 断开）根据 _disconnectMode 决定：durable 模式下调用 finish(closeAction, { keepId: false, cancelIntent: false })，不发送 CANCEL；request-lifetime 模式下调用 finish(closeAction, { keepId: true, cancelIntent: true })，发送 CANCEL。finish 内部检查 cancelIntent 标记：为 true 时通过 agent.send() 发送 CF_AGENT_CHAT_REQUEST_CANCEL；为 false 时跳过该发送。

在 react.tsx 的 useAgentChat 钩子中，stopWithToolContinuationAbort 函数保持发送 CANCEL 的行为不变——它始终设置 cancelIntent 为 true。新增的 cleanup 路径（useEffect 返回的清理函数）在调用 reader.cancel() 之前，通过 ref 检查 cancelIntentRef.current：若为 false（即当前断开由本地生命周期触发而非用户停止触发），则先调用 customTransport.preventCancelOnClose() 临时标记传输层跳过 CANCEL 发送，再执行 reader.cancel()，最后恢复标记。

### 技术效果

第一，语义清晰性：首次在 agent 对话系统中明确区分「客户端本地生命周期清理」与「服务端 agent turn 取消」两类操作，消除因 AI SDK reader.cancel() 隐式行为导致的取消语义泄漏。

第二，资源效率：在 durable 模式下，用户刷新页面或切换标签页后服务端 agent 继续执行并缓冲结果，用户返回时即时恢复——避免了重头开始执行 agent turn 所需的额外 LLM token 消耗和等待时间。Durable Object 的按需唤醒和 zero-idle-cost 特性使该模式在成本上可行。

第三，开发者可控性：通过 disconnectMode 配置参数，应用开发者可根据业务场景选择合适的断开策略。客服对话、长时研究 agent 等场景选择 durable 模式保留上下文连续性；简单问答、无状态查询场景选择 request-lifetime 模式及时释放服务端资源。

第四，多标签页一致性：停止操作通过服务端广播（done:true 帧）同步到所有标签页，断开操作仅影响本地标签页。两种操作在多标签页场景下的行为均确定且可预测。

第五，向后兼容：现有代码无需修改即可获得 durable 默认行为（当前架构中 onClose 本身就不发送 CANCEL）。request-lifetime 模式为显式选择加入。

### 风险与待确认问题

风险一：AI SDK useChat 内部行为依赖。AI SDK 的 useChat 钩子在组件卸载时是否调用 reader.cancel() 取决于其内部实现版本。若调用，则本方案需在 useEffect 清理函数中通过 cancelIntentRef 拦截。需通过阅读 AI SDK 源码或实测确认。若 AI SDK 不调用 reader.cancel()，则当前行为已经满足 durable 语义，本方案主要增量在于显式化配置和防御性编程。

风险二：AbortRegistry 与 cancelIntent 的竞态。在 request-lifetime 模式下，WebSocket onClose 和 AI SDK reader.cancel() 可能先后触发，导致 CANCEL 消息重复发送。AbortRegistry.cancel() 为幂等操作（重复 cancel 同一 AbortController 无副作用），但需确认 agent.send() 在 WebSocket 已关闭时抛出的异常被正确捕获。

风险三：Durable Object 资源管理。durable 模式下，断开后服务端继续运行 agent turn 直至 LLM 推理完成或工具链结束。若用户在断开后不再重连，产生的计算结果和缓冲的 SQLite 数据块将在一定时间后被垃圾回收（stale stream 超过 5 分钟清理，已完成 stream 超过 24 小时清理），不会无限积累。

风险四：reader.cancel() 触发 onAbort 的时序。ReadableStream 的 cancel() 回调是同步调用的，而 AI SDK 可能在其内部的 finally 块中调用。需确认 cancelIntentRef 的设置（置 false）在 reader.cancel() 之前完成，且恢复（置 true）在之后完成。建议使用 try/finally 确保恢复。
