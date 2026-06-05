## 技术方案

本方案提出一种应用于智能体对话系统的客户端本地清理与服务端智能体轮次（agent turn）取消之间的解耦控制方法。在基于持久化对象（Durable Object）的智能体服务端与浏览器客户端之间，通过 WebSocket 协议承载流式对话响应、断线恢复（resume）及多标签页观察（broadcast observe）能力。本方案的核心在于：在客户端传输层引入取消意图区分机制，将客户端侧的被动清理（组件卸载、页面导航、reader cancel、短暂断线）与主动取消（用户点击停止按钮、应用调用 stop）解耦为不同的控制语义，并在服务端建立可配置的断线-取消映射策略，从而在保障可恢复性的同时，保留明确的取消控制通路。

### 整体架构

系统整体架构包含三个核心层次：客户端传输层（WebSocketChatTransport）、服务端智能体层（AIChatAgent / Durable Object）以及连接-取消策略配置层。客户端传输层负责创建和维护 WebSocket 连接、向服务端发送聊天请求、接收流式响应块（chunk）、以及管理 ReadableStream 的生命周期。服务端智能体层运行在持久化对象（Durable Object）中，通过 TurnQueue 序列化聊天轮次、通过 AbortRegistry 管理每请求的中止控制器（AbortController）、通过 ResumableStream 将流式响应块持久化到 SQLite 以支持断线恢复。连接-取消策略配置层作为横切关注点，定义了客户端连接断开事件到服务端轮次取消之间的映射规则。

### 客户端取消意图区分机制

客户端传输层中，每次 sendMessages 调用生成一个唯一请求标识（requestId），并创建对应的 AbortController 和 ReadableStream。在现有方案中，ReadableStream 的 cancel() 方法和调用方传入的 abortSignal 均会触发 onAbort 回调，该回调向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，导致服务端轮次被取消。本方案的关键改进是：将客户端的流终止原因区分为两类——被动清理（abandon）和主动取消（cancel），并为每类原因定义不同的服务端交互行为。

具体而言，在 WebSocketChatTransport 中引入 teardownReason 枚举，包含 'abandon'（被动清理）和 'cancel'（主动取消）两个取值。当 ReadableStream 因组件卸载、页面导航或浏览器刷新而触发 cancel() 时，系统以 abandon 模式执行清理：仅关闭本地流控制器、移除事件监听器并释放本地资源，但不向服务端发送取消消息。当用户通过 UI 调用 stop() 方法或应用显式调用取消接口时，系统以 cancel 模式执行：在释放本地资源的同时，向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，携带请求标识和可选的取消原因。

### 服务端取消接收与 abandon 模式下的恢复保持

服务端通过 AbortRegistry 管理每个请求对应的 AbortController。当服务端收到 CF_AGENT_CHAT_REQUEST_CANCEL 消息时，调用 AbortRegistry.cancel(requestId) 中止对应请求的 AbortController，进而在 _streamSSEReply 的读取循环中通过 abortSignal 检测到中止状态，终止对 LLM 流式响应的读取，并将该轮次的结果状态标记为 'aborted'。当客户端以 abandon 模式断开时，由于不发送取消消息，服务端的 AbortRegistry 中对应的 AbortController 保持有效，流式响应继续被读取、持久化和广播。

为支持客户端重连后恢复（resume），服务端的 ResumableStream 在客户端断开期间持续将响应块批量写入 SQLite。当旧客户端或同一会话的其他标签页重新连接并发送 CF_AGENT_STREAM_RESUME_REQUEST 时，服务端通过 _notifyStreamResuming 通知客户端有可恢复的活跃流，随后将已缓冲的块以 replay 模式发送，并继续转发实时块。对于其他标签页（观察者），通过 broadcast-state 状态机以 observing 模式接收广播，无需独立触发恢复流程。这一机制在 abandon 模式下完全不受影响，因为服务端轮次持续运行。

### 可配置的断线-取消策略

不同应用对“客户端断开是否代表取消服务端任务”有不同偏好，因此本方案引入可配置的断线-取消策略。该策略定义为一个枚举类型 DisconnectPolicy，包含以下可选值：

（1）'abandon'（默认策略）：任何客户端连接断开均不导致服务端轮次取消。客户端断开时仅执行本地清理，服务端继续运行轮次并将响应块持久化到 ResumableStream。适合对恢复体验要求高、允许服务端在无客户端连接时继续完成推理的场景。

（2）'cancel-on-originator-disconnect'：仅当发起当前轮次的原始连接（originator connection）断开时，才向服务端发送取消消息。观察者标签页的连接断开不触发取消。该策略要求服务端和客户端共同追踪 originator connection ID：在服务端，由 ContinuationState 维护 activeConnectionId；在客户端，传输层在 sendMessages 时将当前连接标识与请求绑定，当该连接关闭时触发 cancel。

（3）'cancel-on-all-disconnect'：当智能体实例的所有 WebSocket 连接均断开时，向服务端发送取消消息。该策略需要客户端能够感知当前智能体实例的连接总数。可通过服务端在 onConnect 和 onClose 时向所有连接广播连接数变更事件来实现，或由客户端在决定 cancel 前通过 WebSocket 查询当前连接数。

（4）'cancel-on-timeout'：客户端断开后启动一个可配置的超时计时器（如 30 秒），若在超时前没有任何客户端重新连接（同标签页恢复或其他标签页接入），则在超时后向服务端发送取消消息；若在超时前有客户端重连并恢复流，则取消计时器，不触发取消。该策略在 abandon 和 cancel 之间提供了一种折中方案。

以上策略通过 DisconnectPolicy 配置项在 useAgentChat 或 Agent 级别指定，默认值为 'abandon'。策略的具体执行由客户端传输层负责——传输层在 WebSocket onClose 事件或 ReadableStream cancel 事件中，根据当前策略决定是否发送 CF_AGENT_CHAT_REQUEST_CANCEL。

### 取消原因传播与状态区分

在主动取消路径中，客户端通过 CF_AGENT_CHAT_REQUEST_CANCEL 消息携带可选的取消原因（cancel reason），该原因可以是用户可见的文本描述或结构化的取消元数据。服务端 AbortRegistry.cancel(requestId, reason) 调用将 reason 传递给 AbortController.abort(reason)，从而使信号接收方（如 onChatMessage 中传递给 streamText 的 abortSignal）能够通过 signal.reason 获取取消原因。这使得服务端可以在 onChatResponse 回调中区分不同的取消原因：例如，用户点击停止导致的取消（status: 'aborted'）与因模型调用超时或系统保护策略导致的取消。

在 abandon 路径下，由于不经过 AbortRegistry.cancel，AbortSignal 保持未中止状态，LLM 推理正常完成。客户端重连后，通过 ResumableStream 回放和实时续传获取完整的流式响应，该轮次的 ChatResponseResult 状态为 'completed'，与正常完成的轮次无异。

### 工具调用延续的兼容性设计

在智能体对话中，当 LLM 返回需要客户端执行工具调用的响应时，服务端暂停流式输出并等待客户端工具执行结果（tool result）或审批响应（tool approval）。此后服务端通过 _enqueueAutoContinuation 自动发起延续轮次（continuation turn），继续调用 LLM 将工具结果纳入上下文生成后续响应。延续轮次同样经过 TurnQueue 序列化和 AbortRegistry 管理。

本方案在工具延续场景下的关键设计要点如下：当客户端在等待工具执行结果期间以 abandon 模式断开（如浏览器刷新），服务端的 ContinuationState 中的 pending 记录和 awaitingConnections 映射保留有效状态。客户端重连后，通过 stream resume 流程恢复对工具审批 UI 的展示（因服务端在工具进入 approval-requested 状态时已通过 early persist 将消息快照写入 SQLite），用户完成工具审批后，服务端继续执行延续轮次，不间断。只有当客户端以 cancel 模式发送取消消息时，服务端才调用 resetTurnState 清空延续状态、中止当前轮次并通知所有等待中的连接。

在 WebSocketChatTransport 中，工具延续流通过 expectToolContinuation 标记和 _createToolContinuationStream 方法独立管理。abandon 模式下，延续流的 ReadableStream cancel 不触发服务端取消，仅做本地清理；cancel 模式下，通过 abortActiveToolContinuation 向服务端发送对应请求的取消消息。

### 多标签页观察的协同控制

多标签页观察场景中，同一智能体实例可同时被多个 WebSocket 连接访问：发起当前轮次的原始连接（originator）和仅接收广播的观察者连接（observer）。服务端通过 broadcast-state 状态机管理观察者的流状态，通过 _broadcastChatMessage 将流式响应块广播到除发起者外的所有连接。

本方案对该场景的处理如下：当观察者标签页断开时（abandon），不触发任何服务端轮次取消行为——观察者的连接关闭仅影响其自身，不影响原始连接和其他观察者。当原始连接以 abandon 模式断开时，服务端轮次继续运行并将响应块持久化；其他观察者标签页持续通过广播接收新块，不受原始连接断开的影响。原始连接重连后通过 stream resume 协议恢复接收。当原始连接以 cancel 模式发送取消消息时，服务端取消轮次，并通过 broadcast 向所有连接（包括观察者）广播带有 done 和 error 标记的终止消息，使所有标签页的 UI 同步更新为取消状态。

在 cancel-on-originator-disconnect 策略下，客户端传输层在 WebSocket onClose 时检查当前连接是否为 originator（通过与请求绑定的 originator connection ID 比较），仅当匹配时才发送取消消息，观察者连接关闭时不发送。

### 关键实现要点

上述方案的具体实现涉及客户端传输层、服务端智能体基类和共享抽象层的协同修改，核心改动如下：

在客户端 WebSocketChatTransport 的 sendMessages 方法中，将原有的 onAbort 回调拆分为 onCancel（发送 CF_AGENT_CHAT_REQUEST_CANCEL 并终止流）和 onAbandon（仅终止流，不发送取消消息）两个独立函数。用户调用 stop() 时触发 onCancel；ReadableStream.cancel()、abortSignal.abort（来自组件卸载）和 WebSocket onClose 事件根据当前 DisconnectPolicy 决定触发 onCancel 或 onAbandon。

在传输层构造函数或配置选项中增加 disconnectPolicy 字段，接受 DisconnectPolicy 枚举值，默认值为 'abandon'。增加 cancelReason 可选参数，由 stop() 调用方传入。增加 originatorConnectionId 内部状态，在 sendMessages 调用时记录当前连接的标识。

在服务端 AIChatAgent 的 onClose 包装逻辑中，当连接关闭时，根据当前策略检查是否需要取消轮次。对于 cancel-on-originator-disconnect，比较关闭连接的 ID 与 ContinuationState.activeConnectionId；对于 cancel-on-all-disconnect，在 onClose 中检查剩余连接数是否为零。

在 AbortRegistry 中，cancel 方法已支持可选的 reason 参数，通过 AbortController.abort(reason) 传递，无需修改。在 AbortRegistry 中可增加 cancelIfOriginator(requestId, connectionId) 便利方法，供 cancel-on-originator-disconnect 策略使用。
