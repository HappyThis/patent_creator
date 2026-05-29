## 技术方案

本技术方案针对基于 WebSocket 的 agent 对话系统（以 @cloudflare/ai-chat 中的 useAgentChat 与 AIChatAgent 为典型实现），提出一种将客户端本地生命周期清理与服务端 agent turn 取消进行语义解耦的软件机制。方案引入“语义取消信号”（Semantic Cancel）与“可配置断开策略”（Disconnect Policy）两个核心概念，使系统能够在浏览器刷新、组件卸载、ReadableStream reader cancel、网络短暂断开等本地清理场景下保持服务端 agent 持续执行，同时在用户明确点击“停止”按钮时可靠地将取消意图送达服务端。

### 技术问题分析

在现有 WebSocket 驱动的 agent 对话系统中，客户端存在多种会导致本地流读取器终止的场景：浏览器刷新/关闭标签页导致 WebSocket 断开、React 组件卸载触发 useEffect cleanup、ReadableStream 的 reader.cancel() 被 AI SDK 内部调用、网络短暂断开等。这些场景的共性是在客户端本地清理了流资源，但使用者并不意图取消服务端正在执行的 agent turn。然而在现有实现中，上述任一场景都可能触发同一个 abort 路径——向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，导致正在 Durable Object 中运行的 agent 推理被意外终止，已持久化的上下文和流式数据被浪费。

另一方面，用户确实需要一个明确的“停止生成”能力：当用户点击界面上的停止按钮时，系统应当将取消意图传达到服务端，使 AIChatAgent 通过 AbortRegistry 取消对应请求的 AbortSignal，从而中断正在进行的 LLM 推理流。现有方案的困境在于：这两种截然不同的语义（“本地清理”与“用户取消”）共用同一条代码路径，缺乏可区分的信号载体和可配置的策略层。

### 核心技术方案

核心方案基于现有 @cloudflare/ai-chat 架构，在 WebSocketChatTransport（传输层）、useAgentChat（客户端 Hook 层）和 AIChatAgent（服务端 Agent 层）三个层面分别引入改进，实现客户端本地清理与服务端 turn 取消的语义解耦。

1. 传输层（WebSocketChatTransport）：引入 cancelIntent 标记，在 ReadableStream 终止路径中区分 "none"（本地清理）与 "cancel-turn"（服务端取消），使 WebSocket close 事件、reader cancel 与用户 stop 三种终止触发产生不同的服务端行为。
2. 客户端 Hook 层（useAgentChat）：新增 disconnectPolicy 配置项，默认 "durable" 模式（断开不取消），可选 "request-lifetime" 模式（断开即取消），并增强 stop 函数的语义——通过 stopWithToolContinuationAbort 将取消意图同时送达主请求流和工具 continuation 流。
3. 服务端 Agent 层（AIChatAgent）：增强 CF_AGENT_CHAT_REQUEST_CANCEL 处理，在调用 AbortRegistry 取消推理的同时广播取消事件到所有已连接客户端，并与 ResumableStream、TurnQueue、chatRecovery 等现有基础设施无缝集成。

### 语义取消信号通道

在 WebSocketChatTransport 中引入“意图标记”（cancelIntent），区分本地流清理与用户主动取消。cancelIntent 取值包括："none"（仅清理本地资源，不通知服务端）与 "cancel-turn"（通知服务端终止 agent turn）。具体地，重构 sendMessages 返回的 ReadableStream 的终止路径：ReadableStream.cancel() 被 AI SDK 内部调用（如组件卸载导致 reader cancel）时 cancelIntent 为 "none"，仅关闭本地控制器并清理 activeRequestIds；用户调用 stop() 触发的 AbortSignal 路径中 cancelIntent 为 "cancel-turn"，先向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 再清理本地资源。对 WebSocket close 事件，不再无条件触发取消，而是根据 Disconnect Policy 决定行为。

### 可配置断开策略（Disconnect Policy）

在 useAgentChat 的选项中新增 disconnectPolicy 配置项，支持两种模式："durable"（默认）与 "request-lifetime"。该策略控制当客户端 WebSocket 断开（浏览器刷新、标签页关闭、网络中断）时，是否将已发送的 chat request 对应的服务端 agent turn 标记为取消。

在 durable 模式下，当客户端断开时：WebSocketChatTransport 的 sendMessages 中注册的 WebSocket close 事件监听器不会触发取消消息发送，cancelIntent 保持为 "none"。服务端 AIChatAgent 的 onChatMessage 中的 abortSignal 不会因为该连接的断开而被 abort——因为客户端未发送 CF_AGENT_CHAT_REQUEST_CANCEL。agent turn 继续在 Durable Object 中执行，流式 chunks 持续写入 ResumableStream（SQLite 持久化），并通过 broadcast 向其他已连接的标签页推送。客户端重新连接时通过 reconnectToStream / CF_AGENT_STREAM_RESUME_REQUEST 协议恢复接收。

在 request-lifetime 模式下，当客户端断开时：WebSocket close 事件触发 cancelIntent 为 "cancel-turn"，向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL。服务端 AbortRegistry 取消对应请求，LLM 推理被中断，onChatResponse 收到 status: "aborted"。此模式适用于需要严格将客户端会话与服务端任务绑定的应用场景。

### 服务端取消处理与多标签页状态同步

服务端 AIChatAgent 在 onMessage 中处理 CF_AGENT_CHAT_REQUEST_CANCEL 时，除调用 AbortRegistry.cancel(requestId) 外，还需通过 broadcastChatMessage 向所有已连接客户端广播该取消事件（排除发送者自身）。这样，在多标签页场景下，一个标签页点击“停止”后，其他标签页也能观察到 turn 被取消的状态变化，更新各自的 UI 状态（如将 isServerStreaming 置为 false）。同时，服务端在 turn 因取消而终止时，onChatResponse 的 status 字段为 "aborted"，使开发者可以在该生命周期钩子中执行清理或通知逻辑。

为处理“迟到服务端消息”问题：当客户端已发送 CF_AGENT_CHAT_REQUEST_CANCEL 但服务端在取消生效前仍推送了剩余 chunks，客户端通过 activeRequestIds 集合判断：若 requestId 仍在集合中，则跳过该 chunk 的 UI 更新（因为请求已完成/已取消），避免控制台警告和状态不一致。

### 与工具调用继续执行的兼容

本方案与现有的工具调用继续执行（tool continuation）机制完全兼容。当 agent turn 因用户明确取消而中断时，如果当前正在等待客户端工具结果（hasPendingInteraction 为 true），取消操作的处理流程为：用户调用 stopWithToolContinuationAbort，先通过 AI SDK 的 stop() 终止当前流，再通过 WebSocketChatTransport.abortActiveToolContinuation() 终止正在等待的 tool continuation 流，该 abort 路径携带 cancelIntent "cancel-turn"，向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL。服务端 AbortRegistry 取消对应 continuation request，AIChatAgent 的 _continuation 状态被清理。

### 典型处理流程

整体流程以一次用户发送消息并遭遇浏览器刷新的典型场景为例：

1. 用户在标签页 A 中输入消息并发送。useAgentChat 调用 sendMessage，WebSocketChatTransport.sendMessages 生成 requestId，cancelIntent 初始为 "none"，通过 WebSocket 发送 CF_AGENT_USE_CHAT_REQUEST。
2. 服务端 AIChatAgent.onMessage 收到请求，通过 TurnQueue 入队，执行 onChatMessage。LLM 开始流式生成，chunks 经 ResumableStream 持久化到 SQLite 并广播到已连接客户端。
3. 用户在标签页 A 中刷新浏览器。WebSocket 断开，WebSocketChatTransport 中的 close 事件触发。由于 disconnectPolicy 为 "durable"（默认），cancelIntent 保持 "none"，不发送 CF_AGENT_CHAT_REQUEST_CANCEL。服务端 agent turn 继续执行。
4. 标签页 A 重新加载后，useAgentChat 初始化，resume 选项为 true。WebSocketChatTransport.reconnectToStream 发送 CF_AGENT_STREAM_RESUME_REQUEST。
5. 服务端检测到有活跃流（ResumableStream.hasActiveStream 为 true），发送 CF_AGENT_STREAM_RESUMING 携带当前 requestId。客户端发送 CF_AGENT_STREAM_RESUME_ACK，服务端先重放（replay）已持久化的历史 chunks，再继续推送实时 chunks。
6. 用户在标签页 B 中观察到正在进行的流式输出，点击停止按钮。标签页 B 的 stop() 触发 AbortSignal，cancelIntent 为 "cancel-turn"，发送 CF_AGENT_CHAT_REQUEST_CANCEL。
7. 服务端 AbortRegistry.cancel(requestId) 取消 LLM 推理，onChatResponse 收到 status: "aborted"，广播取消事件到所有标签页。标签页 A 的 onAgentMessage 收到取消广播，更新 isServerStreaming 为 false。

### 技术效果

本方案带来的技术效果包括：

- 语义解耦：首次在 WebSocket 驱动的 agent 对话系统中将“客户端本地流清理”与“服务端 agent turn 取消”解耦为独立的信号通道。cancelIntent 机制确保只有用户明确意图才会触发服务端取消，浏览器刷新、组件卸载等被动清理不再意外终止正在 Durable Object 中执行的 agent 推理。
- 可配置策略：disconnectPolicy 提供 durable 与 request-lifetime 两种模式，使应用开发者可以根据业务场景（长时间运行的助手 vs 请求绑定的问答）选择断开行为，无需修改传输层代码。
- 多标签页一致性：服务端取消事件通过 broadcastChatMessage 广播到所有已连接客户端，确保同一会话的多个标签页在 turn 取消时状态同步。迟到消息通过 activeRequestIds 集合过滤，避免已取消请求的残留 chunk 污染 UI。
- 兼容现有基础设施：方案完全兼容现有 ResumableStream 持久化与恢复机制、TurnQueue 序列化执行、AbortRegistry 取消传播、工具调用继续执行（tool continuation）及 chatRecovery 持久化恢复。断开策略为新增可选参数，默认 durable 模式保持向后兼容。
- 减少无效计算：仅在用户明确取消时终止服务端推理，避免了因网络波动或页面刷新导致的 LLM 推理浪费，同时保留了服务端继续执行、结果可恢复的能力。

### 风险与待确认问题

以下为待确认的风险点和技术边界：

- cancelIntent 标记在 ReadableStream 构造中如何准确区分 "reader.cancel() 由组件卸载触发" 与 "reader.cancel() 由用户 stop() 间接触发"：建议通过 stop() 调用前设置一个同步标记（如 transport 上的 _userInitiatedStop flag），使 cancel() 回调能读取该标记决定 cancelIntent。
- disconnectPolicy 在 WebSocket 层面的粒度：当同一客户端同时有多个活跃 request（如 submit-message 与 tool continuation 并发），durable 模式下是所有请求均保持还是仅保持最近一个，需明确语义。当前建议为按请求粒度：durable 模式下所有活跃请求均不取消，request-lifetime 模式下所有活跃请求均取消。
- 服务端在收到取消后是否立即清理 ResumableStream：当前方案建议保留已持久化的 stream chunks 直至 turn 正式标记为 aborted。这确保在取消操作与最终广播之间存在延迟时，迟到连接的客户端仍能看到部分输出。清理策略可复用现有的 5 分钟过期机制。
- 与 chatRecovery / runFiber 的交互：当 chatRecovery 为 true 时，agent turn 在 Durable Object 的 Fiber 中执行。取消操作需确保 Fiber 能正确响应 AbortSignal，且在 Fiber 恢复（DO 从休眠中唤醒）后，取消状态能正确传递——当前 AbortRegistry 在 DO 构造时重新创建，恢复后需从持久化状态重建取消信息。
