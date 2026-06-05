## 技术方案

本方案提出一种客户端本地清理与服务端 agent turn 取消之间的解耦控制机制。核心思路是：在客户端传输层引入取消意图分类器，将断开事件按来源区分为「临时断开」（如浏览器刷新、组件卸载、页面切换、reader cancel、短暂网络断开）和「显式取消」（如用户点击停止按钮、应用主动调用取消接口）两类。传输层在感知到断开事件时，依据可配置的取消策略决定是否向服务端发送取消指令：临时断开场景下抑制取消消息的发送，允许服务端 agent turn 继续运行；显式取消场景下将取消意图传达到服务端，触发 AbortRegistry 终止对应 turn。该机制与现有可恢复流机制协同工作——临时断开后客户端可通过 RESUME_REQUEST 握手恢复接收流式数据；同时兼容只读连接的多标签页观察模式。

### 取消意图分类机制

取消意图分类是本方案的基础设施，位于客户端传输层（WebSocketChatTransport）的 abort 处理路径中。其核心是将原本统一触发服务端取消的断开事件，按来源拆分为两条路径。

断开来源分类。方案定义 DisconnectReason 枚举，包含两类值：TRANSIENT（临时断开）和 EXPLICIT（显式取消）。TRANSIENT 对应：浏览器标签页刷新或关闭、单页应用路由切换导致组件卸载、ReadableStream 被浏览器自动 cancel、WebSocket 连接因网络波动短暂断开、以及只读观察标签页的断开。EXPLICIT 对应：用户交互触发的停止（如点击 UI 上的「停止生成」按钮）、应用程序通过 agent API 主动调用的取消操作。

分类触发机制。方案通过 AbortController.abort(reason) 的 reason 参数传递分类信息。WebSocketChatTransport 在创建 per-request AbortController 时，对其 signal 的 abort 事件注册分类检查逻辑：检查 abort reason 对象中的 type 字段，若为 'explicit-cancel' 则判定为 EXPLICIT，否则（包括无 reason、reason 为 DOMException 等默认情况）判定为 TRANSIENT。ReadableStream.cancel() 因浏览器行为触发时通常不携带自定义 reason，自然落入 TRANSIENT 分支。应用层显式取消时通过 agent.cancelTurn({reason:'user-stop'}) 方法设置 reason.type='explicit-cancel'，经同一 AbortController 路径进入 EXPLICIT 分支。

### 策略配置接口

方案在 AIChatAgent 配置层引入 CancelPolicy 策略对象，允许应用根据自身场景选择取消行为的严格程度。策略配置是客户端本地清理与服务端取消之间解耦的「开关」，决定分类器在 TRANSIENT 分支上的实际行为。

CancelPolicy 接口定义如下关键字段：（1）cancelOnTransientDisconnect：布尔值，默认 false。为 false 时，分类为 TRANSIENT 的断开事件不向服务端发送取消指令，服务端 turn 继续运行；为 true 时，TRANSIENT 断开仍发送取消指令（退化为当前系统的全量取消行为）。（2）transientCancelDelayMs：数值，默认 0。当 cancelOnTransientDisconnect 为 true 时，可在延迟毫秒数后再发送取消指令，给客户端一个重连窗口期。若在延迟窗口内客户端恢复连接，则取消指令被撤销。（3）explicitCancelBehavior：枚举 'immediate'|'deferred'，默认 'immediate'，控制显式取消指令的发送时机。deferred 模式下先发送取消意图标记，待服务端完成当前工具调用或流式输出缓冲后再终止。（4）multiTabMode：枚举 'owner-cancel-only'|'any-tab-can-cancel'，默认 'owner-cancel-only'，控制多标签页场景下哪些标签页的取消操作可影响服务端 turn。

策略对象挂载在 AIChatAgent 实例的 cancelPolicy 属性上，在 WebSocketChatTransport 初始化时传入。传输层在 abort 处理逻辑中读取策略：先通过分类器确定 DisconnectReason 类型，再查询当前 CancelPolicy 决定是否构造并发送取消消息。策略支持运行时动态更新，应用可在用户切换设置后实时调整取消行为。

### 传输层解耦改造

传输层解耦是本方案的核心改造点，对 WebSocketChatTransport 的 abort 处理路径进行重构，将原本「断开即取消」的单一路径拆分为「本地清理」与「远程取消」两条独立路径。

重构后的 onAbort 处理流程如下。WebSocketChatTransport.sendMessages() 在创建 AbortController 时，向其 signal 注册 abort 事件处理器。事件触发时，处理器按以下顺序执行：（1）从 signal.reason 中提取 DisconnectReason，调用分类器确定类型；（2）读取当前 CancelPolicy，判断是否需要发送服务端取消指令；（3）若需发送——即 EXPLICIT 类型，或 TRANSIENT 类型且 cancelOnTransientDisconnect 为 true——则构造 CF_AGENT_CHAT_REQUEST_CANCEL 消息，通过 WebSocket 发送到服务端，消息体中携带 cancelReason 字段以区分取消来源；（4）无论是否发送取消指令，都执行本地清理：关闭本地 ReadableStream、释放 AbortController 引用、清理请求级状态。本地清理与远程取消完全解耦，互不阻塞。

新增 cancelTurn 方法。WebSocketChatTransport 暴露 cancelTurn(reason?: string) 方法，专用于显式取消路径。该方法内部创建携带 {type:'explicit-cancel', reason} 的 abort reason 对象，调用对应 AbortController.abort(reason)，触发上述 onAbort 流程进入 EXPLICIT 分支。应用层通过 AIChatAgent.cancelCurrentTurn(reason?) 代理调用该方法。

WebSocket 连接断开时的特殊处理。当传输层检测到 WebSocket 自身断开（非主动取消导致），若 CancelPolicy 指示不应取消服务端 turn，则仅执行本地清理，不额外发送取消消息。此时服务端 turn 继续运行，客户端在 WebSocket 重连后通过可恢复流协议恢复数据接收。

### 服务端取消控制集成

服务端需要配合客户端的取消意图分类，对 AbortRegistry 和 AIChatAgent 的 turn 管理逻辑进行适配，使服务端能够根据取消消息中的意图信息执行差异化处理。

AbortRegistry 增强。在现有 AbortRegistry 中，cancel(id, reason) 方法增加对 reason 参数的语义解析。当 reason 中包含 cancelSource:'transient-disconnect' 标记时，AbortRegistry 不执行实际的 AbortController.abort() 操作，而是将该 request 标记为 disconnected 状态，保留其 AbortController 和相关资源，使 agent turn 继续运行。当 reason 中 cancelSource 为 'explicit-cancel' 或该字段缺失时，执行原有的完整取消流程：调用 AbortController.abort() 终止对应 turn 的异步操作链。

CF_AGENT_CHAT_REQUEST_CANCEL 消息扩展。该消息的载荷中新增 cancelSource 字段，取值为 'transient-disconnect' 或 'explicit-cancel'，由客户端传输层在构造取消消息时根据 DisconnectReason 填入。服务端 AIChatAgent 在收到该消息后，从载荷中提取 cancelSource，传递给 AbortRegistry.cancel()，由后者决定实际行为。

resetTurnState 的语义保持。resetTurnState() 方法在收到 CF_AGENT_CHAT_CLEAR 消息时被调用，其行为不变：清空 _turnQueue 并调用 _abortRegistry.destroyAll() 强制终止所有进行中的 turn。这一路径对应「会话级别清理」，与单个 turn 的解耦取消控制相互独立。CF_AGENT_CHAT_CLEAR 通常由新对话发起或显式重置操作触发，不经过取消意图分类器。

### 与可恢复流的协同

解耦控制机制与现有可恢复流机制深度协同，共同实现对临时断开场景的透明恢复。当客户端因 TRANSIENT 原因断开且 CancelPolicy 抑制了取消消息时，服务端 agent turn 继续运行，可恢复流基础设施确保客户端重连后不丢失任何数据。

协同流程如下。（1）客户端发生 TRANSIENT 断开，传输层执行本地清理但不发送取消消息。（2）服务端感知到 WebSocket 连接断开，但未收到取消指令；AbortRegistry 将对应 request 标记为 disconnected，agent turn 继续执行工具调用和 LLM 推理。（3）服务端持续将生成的流式 chunk 持久化到 SQLite 存储中，每个 chunk 附带递增序列号和 turn 标识。（4）客户端 WebSocket 重连后，发起 CF_AGENT_STREAM_RESUME_REQUEST 消息，携带上次接收到的最大 chunk 序列号。（5）服务端返回 CF_AGENT_STREAM_RESUMING 确认进入恢复模式，随后从 SQLite 中读取序列号之后的 chunk 进行重放，并通过 replay 标记区分重放 chunk 与实时 chunk。（6）重放完成后服务端发送 CF_AGENT_STREAM_RESUME_ACK，客户端无缝切换回实时流接收模式。

与显式取消的互斥。当客户端发送的是显式取消（EXPLICIT 分支），服务端 AbortRegistry 执行完整取消，终止 agent turn 并清理 SQLite 中的持久化 chunk。此时客户端即使重连并发起 RESUME_REQUEST，服务端返回 CF_AGENT_STREAM_RESUME_NONE，表示无可恢复的 turn。两类场景在协议层面明确互斥。

chatRecovery 的兼容。AIChatAgent 的 chatRecovery 属性控制是否用 runFiber 包裹 turn 以支持跨连接恢复。本方案不解耦或修改 chatRecovery 的内部机制，而是在其上层通过取消策略决定是否触发 turn 终止。当 chatRecovery 启用且取消策略抑制取消消息时，runFiber 包裹的 turn 持续运行直至自然完成。

### 多标签页场景处理

多标签页场景下，同一用户可能在一个标签页中发起 agent turn 后，在另一个标签页中打开同一对话进行观察或操作。本方案结合只读连接机制，对多标签页下的取消权限进行精细控制。

标签页角色划分。方案将标签页分为「主控标签页」（owner tab）和「观察标签页」（observer tab）。主控标签页是发起当前 agent turn 的标签页，持有读写 WebSocket 连接；观察标签页通过只读连接观察 turn 进展，其 WebSocket 连接在建立时即通过 shouldConnectionBeReadonly hook 标记为只读模式。只读连接可以接收服务端推送的流式 chunk 和状态更新，但不能发送消息（包括取消消息）。

取消权限控制。CancelPolicy.multiTabMode 字段控制取消权限模型。（1）owner-cancel-only 模式（默认）：仅主控标签页的显式取消操作可以触发服务端取消。观察标签页的断开事件——无论 TRANSIENT 还是用户操作——均不影响服务端 turn。当主控标签页断开时，服务端可将一个观察标签页升级为主控标签页（通过 CF_AGENT_STATE 消息中的 ownershipTransfer 字段），新的主控标签页获得取消权限。（2）any-tab-can-cancel 模式：任意标签页的显式取消操作均可影响服务端 turn。该模式适用于协作场景，但需要应用层自行处理取消冲突。

多标签页 TRANSIENT 断开。当所有标签页均以 TRANSIENT 方式断开（例如用户关闭了所有标签页但未点击停止），服务端 turn 在 cancelOnTransientDisconnect 为 false 时继续运行。应用可结合 transientCancelDelayMs 设置一个最大存活时间，超时后服务端自动终止无人监听的 turn。

### 异常与边界处理

方案对解耦控制引入的异常路径和边界条件提供明确处理策略，确保机制在各种异常场景下的行为可预期。

网络波动与快速重连。当客户端网络短暂断开后快速重连（在服务端感知 WebSocket close 之前重连成功），新 WebSocket 连接直接接管原请求的 AbortController 和 ReadableStream，不触发任何取消分类逻辑。当服务端先感知到 WebSocket close 但未收到取消消息时，进入 disconnected 标记状态；客户端随后重连，通过 RESUME_REQUEST 恢复。方案在 AbortRegistry 中引入 disconnected 状态的超时机制（默认 30 秒可配置）：若超时内未收到重连，服务端自动执行资源清理，终止 turn 并释放持久化 chunk。

取消与重连的竞态。可能出现客户端发送显式取消消息后立即重连的竞态。方案规定：取消消息具有最高优先级。若服务端在 disconnected 状态下收到 CF_AGENT_CHAT_REQUEST_CANCEL（cancelSource='explicit-cancel'），立即终止 turn 并清理持久化数据，后续 RESUME_REQUEST 返回 RESUME_NONE。若取消消息与 RESUME_REQUEST 在传输中交错到达，服务端按消息接收顺序处理，先到先生效。

服务端重启场景。若服务端在 turn 处于 disconnected 状态时重启，SQLite 中的持久化 chunk 可能丢失（取决于部署模式）。方案建议应用层在 CancelPolicy 中设置 serverRestartBehavior 字段：'fail-turn'（默认，重启后 turn 不可恢复，客户端 RESUME_REQUEST 返回 RESUME_NONE）或 'restart-turn'（服务端重启后根据持久化的 turn 状态快照重建执行上下文并继续）。

资源管理。disconnected 状态下的 turn 持续占用服务端内存和计算资源（LLM 推理、工具调用）。方案引入 maxDisconnectedTurnDuration 配置（默认 5 分钟可配置），超时后服务端强制终止 turn 并清理资源。同时，工具调用在 disconnected 期间继续执行，但其结果在无客户端接收时仅持久化存储，不尝试推送。
