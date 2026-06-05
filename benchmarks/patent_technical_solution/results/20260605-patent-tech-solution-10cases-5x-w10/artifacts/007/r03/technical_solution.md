## 技术方案

本方案提出一种客户端本地清理与服务端 agent turn 取消之间的解耦控制机制。核心构思是：在 agent 服务端引入可配置的"断开行为策略"（Disconnect Behavior Policy），将 WebSocket 连接断开这一传输层事件与"是否应取消正在运行的 agent turn"这一业务决策分离开来。客户端在断开连接前，根据用户操作类型决定是否发送显式取消意图；服务端在连接关闭时，根据预设策略和是否已收到显式取消意图，决定继续运行、取消当前 turn 或进入等待恢复状态。该机制通过策略枚举、取消意图消息协议字段扩展、连接观察者引用计数及与 ResumableStream 的协同，实现断开不中断、显式停止可取消、多标签页不误取消、流式响应可恢复的完整解耦方案。

### 断开行为策略定义

在服务端 AIChatAgent 的配置中新增 disconnectBehavior 策略字段，类型为枚举，至少包含以下三种策略值：

- persist（保持运行）：连接断开时不取消当前 agent turn，服务端继续执行并缓冲输出至 ResumableStream；适用于对任务完成率要求高、允许短暂断连后恢复的场景。此策略为默认值。
- cancel-on-disconnect（断连即取消）：连接断开时立即取消当前 agent turn，通过内部 AbortRegistry 触发 abort，清理 ContinuationState 和 TurnQueue；适用于对 stale 结果容忍度低、希望及时释放资源的场景。
- cancel-on-explicit-stop-only（仅显式停止时取消）：连接断开时默认保持运行，仅当客户端在断开前发送了显式取消消息时才取消 turn；适用于需要区分"用户主动停止"与"连接意外断开"的场景，如协作编辑、长时间推理任务等。

### 取消意图的显式传达机制

为解决"连接断开"与"用户停止意图"的语义混淆，方案在现有 CF_AGENT_CHAT_REQUEST_CANCEL 消息中扩展一个 cancelSource 字段，用于区分取消来源：

- explicit-user-stop：用户点击"停止"按钮或应用调用 stop() 主动取消。发送此消息后客户端可关闭连接，服务端依据策略处理。
- connection-cleanup：客户端因卸载、刷新、页面切换等原因关闭连接时的伴随清理信号，不表达用户取消意图。此类型仅在 cancel-on-disconnect 策略下触发服务端取消。

客户端 useAgentChat 中的 stop() 方法调整为：先通过 WebSocket 发送带 cancelSource: "explicit-user-stop" 的 CF_AGENT_CHAT_REQUEST_CANCEL 消息，等待服务端确认（或超时后直接断开），再关闭本地 ReadableStream。对于非用户主动触发的断开（组件卸载、页面切换），客户端不发送任何取消消息，或根据策略仅在 cancel-on-disconnect 模式下发送 cancelSource: "connection-cleanup"。该字段采用可选扩展设计，未设置时默认行为与现有 cancel 消息一致，保证向后兼容。

### 服务端策略执行逻辑

服务端在 onClose 生命周期中集成策略执行逻辑。当 WebSocket 连接关闭时，执行以下判断流程：

1. 读取 agent 配置的 disconnectBehavior 策略值。
2. 检查是否在连接关闭前的可配置时间窗口内收到过 cancelSource 为 explicit-user-stop 的取消消息（服务端在收到取消消息时将其 requestId 和时间戳记录到 pendingExplicitCancellations 集合中，onClose 时查询该集合）。
3. 根据策略与取消状态做出决策：persist → 不做任何取消操作，保留当前 turn 运行，保持 ContinuationState 为 pending；cancel-on-disconnect → 对当前所有活跃 requestId 调用 _abortRegistry.cancel()，清理 ContinuationState；cancel-on-explicit-stop-only → 仅当 pendingExplicitCancellations 中存在匹配记录时才执行取消，否则行为同 persist。
4. 执行完取消决策后，统一执行连接清理：从连接观察者集合中移除当前连接、清理 pendingResumeConnections 中该连接的条目、更新多标签页引用计数。

关键点：cancel-on-explicit-stop-only 策略下，服务端在 onClose 中不主动调用 _abortRegistry.destroyAll()（区别于 resetTurnState），而是仅清理连接资源；agent turn 的取消仅由收到 explicit-user-stop 消息时触发。这使得"断开连接"和"取消任务"成为两个独立事件，仅在策略和取消意图同时满足时才耦合。

### 多标签页场景兼容

多标签页同时连接同一 agent 时，一个标签页关闭不应错误触发服务端 turn 取消。方案引入连接观察者引用计数机制：

- 服务端维护一个 connectionObserver 映射表，以 agent sessionId 为键，记录当前活跃的 WebSocket 连接数及其各自的连接 ID。
- 当 onClose 触发时，先从 connectionObserver 中减去当前连接，再检查剩余连接数。若剩余连接数 > 0，说明仍有其他标签页在观察，此时即使 disconnectBehavior 为 cancel-on-disconnect，也不执行取消操作，仅做当前连接的清理。只有当剩余连接数降为 0 时，才依据策略判断是否取消 turn。
- 对于 cancel-on-explicit-stop-only 策略，explicit-user-stop 取消消息的效力不受多标签页影响——任何一个标签页发出的显式停止消息，经 connectionObserver 确认后均可生效（或可配置为需要所有标签页确认，取决于应用偏好）。
- connectionObserver 的增删操作与现有 CF_AGENT_CHAT_MESSAGES 广播机制协同：新标签页连接时通过 RESUME_REQUEST 注册到 observer，断开时通过 onClose 注销。

### 与流式恢复的协同

断开行为策略需与 ResumableStream 流式恢复机制协同工作。当策略为 persist 或 cancel-on-explicit-stop-only（且未收到显式取消）时，服务端继续运行 agent turn，ResumableStream 持续将输出缓冲至 SQLite。客户端重新连接后，通过现有 RESUME_REQUEST → STREAM_RESUMING 流程恢复流式输出。具体协同规则：

- 策略为 persist 时：ResumableStream 保持活跃缓冲，ContinuationState 保持在 pending 状态，等待客户端 resume 或超时自动完成。此模式下工具调用继续执行，不会被中断。
- 策略为 cancel-on-disconnect 时：onClose 中取消 turn 的同时，标记 ResumableStream 为 cancelled 状态并清理缓冲区，避免恢复时重放过期内容。
- 策略为 cancel-on-explicit-stop-only 时：若收到 explicit-user-stop，行为同 cancel-on-disconnect；若未收到，行为同 persist。
- 在 resume 流程中，客户端 reconnectToStream 发送 RESUME_REQUEST 时，服务端先检查当前 turn 是否已被取消（通过 AbortRegistry 状态判断）。若已取消则返回 STREAM_RESUME_NONE，客户端进入空闲状态；若仍在运行则返回 STREAM_RESUMING 并继续推送缓冲消息。

该协同确保：短暂的网络抖动或页面切换不会导致正在执行的 agent turn 和已缓冲的流式响应被丢弃；而用户明确停止时，恢复流程也能正确识别并终止。
