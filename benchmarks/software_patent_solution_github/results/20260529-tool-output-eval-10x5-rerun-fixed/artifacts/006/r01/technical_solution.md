## 技术方案

本方案提出一种面向基于 Durable Object（DO）的持久化智能体对话系统的 Agent Turn 生命周期控制机制。方案核心在于严格区分两类语义：客户端本地生命周期清理（浏览器刷新、组件卸载、reader cancel、网络断开等）与用户主动取消服务端 Agent Turn 的意图（点击"停止"按钮或应用调用 stop()）。系统以"客户端断开不取消服务端任务"为默认策略（Durable 模式），同时提供可选的 Request-Lifetime 模式，将客户端 WebSocket 连接生命周期与服务端 Turn 生命周期绑定，并保留明确的 stop() 取消通道。该机制与现有的流式响应、断线重连（stream resumption）、工具调用自动续接（tool continuation）、多标签页广播观察以及迟到服务端消息处理完全兼容。

### 技术问题

在基于 Cloudflare Durable Object 的智能体对话系统中，Agent 运行在持久化执行环境中，支持 SQLite 状态存储和流式响应。现有 useAgentChat 客户端 hook 使用 WebSocket 与服务端 AIChatAgent 通信。当用户刷新浏览器页面、React 组件卸载或网络短暂断开时，客户端 WebSocket 连接断开，触发本地 effect cleanup 逻辑：移除事件监听器、重置流状态标记，但不会向服务端发送任何取消消息。这意味着服务端仍在 DO 内部继续执行 Agent Turn 推理，并将流式 chunk 写入 ResumableStream 持久化存储。

这种默认行为具有合理性：DO 的持久化特性使得断线后重新连接即可通过 CF_AGENT_STREAM_RESUME_REQUEST / STREAM_RESUMING 协议恢复流接受，用户不会丢失已生成的内容。然而，当前系统存在以下不足：（1）客户端 stop() 调用虽然会通过 ReadableStream 的 abort 信号触发 CF_AGENT_CHAT_REQUEST_CANCEL 消息发送到服务端来取消 AbortRegistry 中对应的 AbortController，但浏览器刷新、组件卸载等本地清理路径在 cleanup 中仅做本地状态重置，未区分"我只是暂时离开"与"我真的想停止"；（2）不同应用场景对"客户端断开是否意味着取消服务端任务"有不同偏好——例如实时协作场景希望断开即取消，而长时间后台推理场景希望断开后继续执行并支持恢复；（3）目前缺乏一个统一的配置入口让开发者声明其 Agent Turn 的生命周期策略。

### 核心技术方案

本方案在 AIChatAgent（服务端）与 useAgentChat（客户端）之间引入一个显式的 Agent Turn 生命周期策略（TurnLifetimePolicy），对客户端本地清理事件和服务端 Turn 取消事件进行语义解耦。

策略定义为一个联合类型："durable"（默认）和"request-lifetime"。在 durable 模式下，客户端 WebSocket 断开（无论原因：刷新、卸载、网络中断、reader cancel）不向服务端发送取消消息；服务端 Agent Turn 持续在 DO 内执行，流式 chunk 写入 ResumableStream，后续任意客户端连接均可通过 STREAM_RESUME_REQUEST 恢复接收。在 request-lifetime 模式下，客户端 WebSocket 断开自动触发 CF_AGENT_CHAT_REQUEST_CANCEL 发送，将客户端的连接生命周期绑定到服务端 Turn 生命周期，断开即取消。两种模式下，用户主动调用 stop() 始终发送取消消息，不受策略影响。

### 关键模块与处理流程

以下从客户端到服务端分层描述各模块的职责与处理流程。

一、客户端生命周期策略执行。在 useAgentChat hook 中，新增 turnLifetime 配置项，接受 "durable"（默认）或 "request-lifetime"。该配置通过 WebSocketChatTransport 在连接建立时或首次 sendMessages 时作为元数据发送给服务端，同时影响客户端 cleanup 逻辑的行为。客户端 effect cleanup（组件卸载、依赖变更导致的 effect 重新执行）中增加策略判断分支：若 turnLifetime 为 "request-lifetime" 且当前存在活跃的本地请求（localRequestIdsRef 非空），则在移除事件监听器之前，遍历所有活跃请求 ID，通过 agent.send() 发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息；若为 "durable"（默认），cleanup 仅执行现有逻辑（移除监听器、重置本地流状态、清理 localResponseIds），不发送任何取消消息。关键区分点：用户主动调用 stop() 的路径不受 turnLifetime 影响——stop() 内部的 onAbort 回调始终发送 CF_AGENT_CHAT_REQUEST_CANCEL。Cleanup 中的自动取消仅在 "request-lifetime" 模式下触发，且仅针对该客户端发起的请求（通过 localRequestIdsRef 判断），跨标签页场景中一个标签页的卸载不会影响其他标签页发起的请求。

二、服务端 TurnLifetimePolicy 接收与执行。服务端 AIChatAgent 在 onMessage 处理中解析客户端发送的 turnLifetime 参数并存储到 connection 上下文中（或作为 Agent 实例属性）。当 onClose 事件触发（客户端 WebSocket 断开）时，服务端检查该连接对应的策略：若为 "request-lifetime"，遍历该连接关联的所有活跃请求 ID，调用 AbortRegistry.cancel(id) 取消对应的 AbortController，触发推理循环的 abort signal 终止 LLM 调用；已流式输出的部分 chunk 仍持久化到 ResumableStream，但 AbortRegistry 的 abort 信号使后续推理停止。若为 "durable"（默认），onClose 不做任何取消操作，Agent Turn 继续执行至完成或超时。

三、取消通道与 AbortRegistry 交互。取消操作始终通过 AbortRegistry 进行，这是一个按 requestId 索引的 AbortController 映射表。每条请求在 TurnQueue 中入队执行时，通过 AbortRegistry.getSignal(requestId) 获取专属 AbortSignal 并传递给 LLM 推理循环。取消路径有两条：（1）客户端通过 WebSocket 发送 CF_AGENT_CHAT_REQUEST_CANCEL（由 stop() 或 request-lifetime 模式的 cleanup 触发），服务端 onMessage 调用 AbortRegistry.cancel(id)；（2）服务端 onClose 在 request-lifetime 模式下遍历该连接的活跃请求逐一 cancel。AbortRegistry 的 linkExternal 方法还支持将外部 AbortSignal（如 AI SDK 工具 execute 的 abortSignal）桥接到注册表，使父级 Agent Turn 取消能级联到子 Agent 的 saveMessages 调用。

四、与断线重连及流恢复的兼容。durable 模式天然兼容现有的 stream resumption 机制。客户端重连后发送 CF_AGENT_STREAM_RESUME_REQUEST，服务端检测 ResumableStream 中存在活跃流时回复 CF_AGENT_STREAM_RESUMING，客户端通过 WebSocketChatTransport.reconnectToStream() 建立 ReadableStream 接收重放 chunk。ResumableStream 基于 SQLite 存储流式 chunk，支持 DO 休眠后恢复（通过 Fiber Recovery 机制），确保即使 DO 在 Turn 执行期间被驱逐，恢复后仍可通过 onChatRecovery 钩子继续推理。在 durable 模式下，即使所有客户端都已断开，只要未显式取消，ResumableStream 中的流保持活跃，Turn 继续执行。

五、多标签页与迟到消息处理。服务端通过 broadcast 方法将 CF_AGENT_USE_CHAT_RESPONSE 消息广播给所有连接（除发送请求的标签页外）。多标签页场景下，任一标签页发起的 Turn 执行过程中，其他标签页通过 onAgentMessage handler 的 broadcastTransition 状态机跟踪跨标签页流状态（observing 状态），实时显示其他标签页发起的流式内容。迟到消息（客户端重连后到达的、属于已结束请求的 chunk）由 requestId 过滤机制处理：localRequestIdsRef 跟踪本标签页发起的请求 ID，不属于当前活跃请求集合的 CHAT_USE_RESPONSE 消息由 broadcastTransition 的 fallback 路径处理，确保不产生幽灵消息。

### 策略配置与开发者接口

开发者在 useAgentChat 选项中通过 turnLifetime 字段声明策略，默认值为 "durable"。服务端 AIChatAgent 也可通过类属性 defaultTurnLifetime 设置 Agent 级别的默认策略，当客户端未显式声明时生效。配置优先级：客户端 useAgentChat 的 turnLifetime > 服务端 AIChatAgent 的 defaultTurnLifetime > 系统硬编码默认值 "durable"。

两种模式的技术语义对照：（1）durable 模式——客户端断开时服务端 Turn 继续执行，用户刷新或切换页面后可通过 stream resumption 恢复接收，适用于长时间推理、后台报告生成、多标签页协作等场景；（2）request-lifetime 模式——客户端断开时服务端自动取消关联 Turn，WebSocket 连接生命周期等于 Turn 生命周期，适用于实时对话、资源敏感或需要严格断开即停的场景。

两种模式下的 stop() 行为一致：用户点击停止按钮或应用调用 stop() 始终发送 CF_AGENT_CHAT_REQUEST_CANCEL，立即终止当前流（通过 ReadableStream 的 abort 机制）并通知服务端取消 AbortRegistry 中的对应 AbortController。stop() 通过 stopWithToolContinuationAbort 封装，在 AI SDK stop() 之后额外调用 customTransport.abortActiveToolContinuation() 以确保工具续接流也被取消。

### 技术效果

本方案通过引入 TurnLifetimePolicy 实现了客户端本地生命周期清理与服务端 Agent Turn 取消的语义解耦，相比现有方案带来以下技术效果：

（1）语义明确性：浏览器刷新、组件卸载、reader cancel、网络断开等本地清理事件在 durable 模式下不产生服务端副作用，而 stop() 调用始终是明确的取消意图表达，消除了当前设计中"哪些操作会取消 Turn"的模糊性。

（2）持久化推理保护：durable 模式下，Agent Turn 不受客户端连接状态影响，充分利用 DO 的持久化执行特性，即使用户关闭浏览器，服务端推理仍在进行（受 DO 超时和 wall-clock 限制），重连后可恢复全部已生成内容。

（3）灵活的可配置性：不同应用场景可通过 turnLifetime 配置选择最适合的策略，而非一刀切行为。SDK 和平台开发者可设置默认策略并允许应用层覆盖。

（4）与现有机制的完全兼容：方案不改变现有的 WebSocket 协议、AbortRegistry 架构、ResumableStream 持久化、TurnQueue 序列化、工具续接（tool continuation）、多标签页广播（broadcastTransition）和 Fiber Recovery 恢复机制，仅在 cleanup 和 onClose 路径增加策略判断分支。

（5）取消的精确性：request-lifetime 模式下仅取消该连接关联的请求，不影响同一 DO 实例中其他连接（其他标签页）发起的独立 Turn；stop() 取消同样精确到 requestId 级别。

### 风险与待确认问题

（1）连接-请求关联的准确性：request-lifetime 模式依赖服务端维护"连接→请求"的映射关系。当前 AIChatAgent 中 connection 对象通过 onMessage 闭包访问，但在 TurnQueue 异步执行过程中 connection 可能已失效。建议在 TurnQueue.enqueue 时捕获 connection.id 并在 onClose 时按 connectionId 查找和取消对应的活跃请求。

（2）网络短暂断开与 request-lifetime 的交互：request-lifetime 模式下，短暂的网络波动（Wi-Fi 切换、移动网络切换）可能意外取消正在执行的 Turn。建议考虑引入一个可配置的宽限期（grace period，如 5-10 秒），在宽限期内重连成功则不触发取消。

（3）工具续接（tool continuation）中的取消传播：当 Turn 正在执行服务端工具调用（tool-call → tool-result → auto-continue）时，request-lifetime 模式下的断开需要确保工具执行结果（如已完成的数据库写入）不被回滚——取消应停止后续 LLM 推理但不撤销已完成工具调用的副作用。当前设计通过 AbortRegistry.cancel 仅 abort LLM 推理的 AbortSignal，不涉及工具执行的回滚，但需在文档中明确此行为。

（4）DO 驱逐与策略持久化：turnLifetime 策略应在 DO 休眠/驱逐后保持。建议将策略作为 Agent 实例属性（类属性 defaultTurnLifetime）存储，而非依赖连接上下文（连接在 DO 驱逐后丢失）。客户端每次连接时重新声明策略值。
