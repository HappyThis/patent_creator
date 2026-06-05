## 技术方案

本方案提出一种客户端本地清理与服务端 agent turn 取消之间的解耦控制机制。核心思路是：在 WebSocket 连接关闭事件（onClose）与服务端取消操作之间，引入一个可配置的"断开策略引擎"（Disconnection Policy Engine），使应用能够根据断开原因、重连状态、多标签页观察者数量以及当前 turn 的执行阶段，差异化地决定是否触发服务端取消。

该方案建立在现有架构的三层基础之上：AbortRegistry（按请求 ID 管理 AbortController 的注册与取消）、TurnQueue（基于代际失效机制的串行异步队列）和 ResumableStream（基于 SQLite 持久化流块的断线重播机制）。方案不改动上述组件的内部实现，而是在 AIChatAgent 的 onClose 处理路径中插入策略决策层，将原本"断开即不做任何取消"的硬编码行为，替换为可由应用按场景配置的决策逻辑。

### 断开场景分类与识别机制

断开策略引擎首先对客户端连接断开事件进行分类。断开原因通过 WebSocket 的 CloseEvent.code 和客户端在断开前发送的最后一帧消息类型进行区分，共划分为以下场景：

- 主动取消（Active Cancel）：客户端通过 CF_AGENT_CHAT_REQUEST_CANCEL 消息显式请求取消当前 turn。该消息在 WebSocket 关闭前发出，服务端在收到消息时直接调用 AbortRegistry.cancel()，不经过策略引擎。
- 被动断开-页面刷新/导航离开（Page Unload）：浏览器触发 beforeunload 事件导致 WebSocket 关闭。客户端在 beforeunload 中可发送一条 CF_AGENT_CHAT_DISCONNECT 消息，携带 reason=unload 和当前 stableChatId，供服务端策略引擎判断。
- 被动断开-组件卸载（Component Unmount）：前端框架组件卸载（如 React unmount）导致 WebSocket 传输层销毁。客户端在销毁前发送 CF_AGENT_CHAT_DISCONNECT 消息，携带 reason=unmount。
- 被动断开-网络抖动（Network Flap）：TCP 连接异常中断，客户端无法发送任何前置消息。服务端通过 CloseEvent.code（如 1006 异常关闭）识别此类断开。
- 多标签页场景（Multi-Tab）：同一 stableChatId 对应多个活跃的 WebSocket 连接（不同标签页各自建立连接）。服务端维护每个 chatId 的连接计数，当仅剩一个连接关闭时才进入策略决策，其余情况仅减少计数。

### 可配置的断开策略引擎

断开策略引擎对外暴露一组可配置的策略选项，应用在创建 AIChatAgent 实例时通过 options.disconnectionPolicy 传入。引擎在 onClose 触发时，根据识别出的断开场景和当前 turn 状态，按优先级逐条匹配策略规则，得出最终决策：立即取消、延迟取消或保持执行。

策略选项包括以下可配置维度：

- cancel_on_unload（默认值：false）：页面刷新或导航离开时是否取消服务端 turn。设为 false 时，turn 继续执行并在完成后将结果持久化到 ResumableStream，用户返回页面时可通过重连恢复读取。设为 true 时，立即调用 AbortRegistry.cancel() 释放服务端资源。
- cancel_on_unmount（默认值：true）：前端组件卸载时是否取消服务端 turn。默认开启，因为组件卸载通常意味着用户已离开当前对话视图且不预期返回。
- disconnect_grace_period_ms（默认值：10000）：网络抖动场景下的重连宽限期，单位为毫秒。断开后在该时间内若客户端通过同一 stableChatId 重新建立连接并完成流恢复握手（CF_AGENT_STREAM_RESUME_REQUEST），则取消决策。超时后若未重连，触发取消。该值设为 0 表示立即取消，设为 -1 表示永不自动取消。
- multi_tab_mode（可选值：last_exit 或 ignore，默认值：last_exit）：多标签页场景下的决策模式。last_exit 模式下，仅当同一 chatId 的最后一个活跃连接关闭时才进入策略评估；ignore 模式下，每个连接关闭独立评估。
- tool_continuation_grace_multiplier（默认值：3.0）：当 turn 当前正在执行工具调用（Agent 调用外部工具且尚未返回结果）时，断开宽限期乘以该系数，给予更长的重连窗口，避免因短暂断网导致长时间运行的工具调用结果丢失。

### 多标签页协调机制

多标签页场景下，同一对话的多个标签页各自建立独立的 WebSocket 连接，但共享同一个 stableChatId。为避免一个标签页关闭即触发取消而导致其他标签页的观察中断，方案在服务端引入连接引用计数和跨标签页协调机制。

协调机制的工作流程如下：

1. 服务端为每个 stableChatId 维护一个 ConnectionTracker，记录活跃连接数（activeCount）和每个连接的元数据（connectionId、建立时间、最后活动时间等）。
2. 新 WebSocket 连接建立并通过握手后，ConnectionTracker 将 activeCount 加 1，并注册该连接的 onClose 回调。
3. 当任一连接关闭时，ConnectionTracker 先将 activeCount 减 1。若减后 activeCount > 0，说明仍有其他标签页在观察，直接跳过策略评估，不触发取消。
4. 仅当 activeCount 降至 0（最后一个连接关闭）时，才将关闭事件连同累计的断开原因（取所有关闭中最保守的——优先识别为 unload/unmount 而非 network_flap）提交给策略引擎评估。
5. 若 multi_tab_mode 设为 ignore，则跳过引用计数逻辑，每个连接的关闭事件独立进入策略引擎。

在客户端侧，各标签页之间可通过 BroadcastChannel API 感知其他标签页的存在。当标签页 A 关闭前检测到仍有其他标签页存活，可在发送的 CF_AGENT_CHAT_DISCONNECT 消息中附带 remaining_tabs 字段，辅助服务端判断。但最终的引用计数以服务端 ConnectionTracker 为准，防止客户端异常退出导致计数错误。

### 与现有取消和恢复链路的集成

断开策略引擎插入在 AIChatAgent 的 onClose 与 AbortRegistry.cancel() 之间，不改动现有 cancel 消息的直接处理路径。集成方式如下：

1. onClose 触发后，首先通过 CloseEvent 和最后收到的客户端消息识别断开场景。
2. 若场景为主动取消（已有 CF_AGENT_CHAT_REQUEST_CANCEL 消息），直接调用 AbortRegistry.cancel()，不进入策略引擎。这与现有行为一致。
3. 若场景为被动断开（unload、unmount 或 network_flap），将事件提交给断开策略引擎。引擎根据配置的策略选项和当前 turn 状态，在以下决策中选一：立即取消、启动宽限期定时器、或不做任何操作。
4. 决策为立即取消时，调用 AbortRegistry.cancel() 取消当前请求的 AbortController，触发下游 AI SDK 的中止逻辑。
5. 决策为启动宽限期时，服务端保持 ResumableStream 的 live 状态不变，同时启动一个定时器。若在宽限期内收到同一 stableChatId 的 CF_AGENT_STREAM_RESUME_REQUEST，清除定时器并恢复流传输。若定时器到期，执行取消并标记流为 orphaned。
6. 决策为不做任何操作时，turn 继续执行至完成，结果写入 ResumableStream 供后续重连恢复。这与当前 onClose 不做取消的默认行为兼容。

与 TurnQueue 的集成：当策略引擎决定取消时，除了调用 AbortRegistry.cancel()，还需调用 TurnQueue.reset() 使队列中所有挂起 turn 变为 stale 跳过。这确保取消当前 turn 后，已排队的后续请求不会意外执行。策略引擎决定保持执行时，TurnQueue 不受影响，当前 turn 继续完成后，队列中的下一个 turn 正常启动。

### 流式响应与工具调用的兼容处理

本方案对现有流式响应机制和工具调用继续执行能力保持完全兼容，并通过策略配置增强其在断开场景下的鲁棒性。

流式响应恢复的兼容：ResumableStream 的持久化和重播机制保持不变。当策略引擎选择"保持执行"时，AI SDK 的流式输出持续写入 SQLite 流块存储。客户端重连后通过 CF_AGENT_STREAM_RESUME_REQUEST 触发重播，ResumableStream 将持久化的流块按序发送。当策略引擎选择"宽限期"时，ResumableStream 在宽限期内维持 _isLive 为 true，确保重连后以 live 模式继续；若宽限期超时触发取消，则将流标记为 completed 或 error 状态（取决于取消时的流位置），重连客户端收到 CF_AGENT_STREAM_RESUME_NONE 通知。

工具调用继续执行的兼容：当 Agent 调用外部工具且工具正在执行时发生断开，策略引擎通过检查 TurnQueue 中当前 turn 的执行阶段（是否处于 tool_call 状态）来识别此场景。在宽限期计算中，将 disconnect_grace_period_ms 乘以 tool_continuation_grace_multiplier，为工具调用提供更长的重连窗口。工具调用完成后，若客户端仍未重连且宽限期已过，取消决策照常执行，但工具调用结果已持久化到消息历史中，不会丢失。若工具调用支持中断信号且 AbortRegistry 的 AbortSignal 已传递至工具层（通过 linkExternal），取消操作可进一步将中断信号传播至正在执行的工具，实现工具层的优雅中止。

此外，方案在 disconnect_grace_period_ms 中提供了两个边界值：设为 0 时，网络抖动场景直接触发取消，等价于将被动断开一律视为取消意图；设为 -1 时，永不自动取消，等价于当前 onClose 不做取消的行为。这两个边界值确保方案向后兼容现有行为的两个极端偏好。
