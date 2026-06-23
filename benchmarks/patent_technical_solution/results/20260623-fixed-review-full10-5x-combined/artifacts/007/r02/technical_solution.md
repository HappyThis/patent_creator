## 技术方案

### 总体架构与取消意图分层

本方案要解决的核心问题是：在基于浏览器 WebSocket 接收 agent 流式响应的场景中，组件卸载、页面刷新、路由切换、reader.cancel()、短暂网络断开等本地消费端生命周期事件，可能被误传播为服务端 agent turn 的执行取消，导致本应可恢复的模型调用、工具调用或 continuation 被提前 abort。本方案在现有 ChatTransport、可恢复流、AbortRegistry 和工具 continuation 基础上增加取消意图判定层，使客户端连接生命周期、流消费生命周期和服务端 turn 执行生命周期分别建模，并通过 $requestId$ 或 $turnId$ 贯穿客户端状态、协议帧、服务端执行、chunk 缓存和工具 continuation。

取消控制遵循以下不变量：第一，detach 不得触发服务端 AbortRegistry.cancel，普通 close 事件也不得直接 abort 对应 turn；第二，显式用户停止、应用主动取消、全局清理或可信外部 AbortSignal 必须能够携带 $requestId$ 和取消原因到达服务端；第三，同一 $requestId$ 的终态只能从 running、waiting-tool、continuing 或 cancelling 进入 completed、cancelled 或 failed 之一，terminal chunk 已确认后不得被后到取消覆盖；第四，恢复 replay 与 live broadcast 必须通过单调 chunk 序号和连接级 replaying 标记保证不重复、不乱序消费。

取消意图至少可以分为本地消费端清理、临时连接中断、显式用户停止、应用主动取消和全局清理五类。本地消费端清理包括组件卸载、页面切换、刷新前释放 reader、ReadableStream 的普通 cancel 等，仅表示当前浏览器上下文不再消费该流；临时连接中断表示 WebSocket close 或短暂网络不可达；显式用户停止和应用主动取消表示用户或业务逻辑要求终止服务端正在运行的 agent turn；全局清理表示清空会话、重置 agent 或销毁运行上下文。该分层使系统能够默认保护长时间运行且可恢复的 turn，同时仍保留明确停止请求的可达性。

### 核心数据结构与协议字段

客户端为每个本地请求或恢复流维护 ClientTurnState，至少包括 $requestId$、$turnId$、$transportConnectionId$、$localReaderState$、$stopReason$、$resumeAttemptId$、$resumeToken$、$isReplay$、$lastReceivedChunkSeq$、$activeContinuationRequestId$ 和 $terminalObserved$。其中 $localReaderState$ 可取 idle、streaming、detaching、detached、cancelling、cancelled、completed、failed；$lastReceivedChunkSeq$ 记录客户端已按序消费的最大 chunk 序号；$resumeAttemptId$ 用于区分多次恢复尝试；$terminalObserved$ 用于防止后到 chunk 或取消结果覆盖已完成状态。

服务端为每个 agent turn 维护 ServerTurnState，至少包括 $requestId$、$turnState$、$observerConnections$、$abortEntryId$、$resumeCacheId$、$lastChunkSeq$、$pendingResumeAcks$、$graceDeadline$、$continuationState$、$cancelReason$、$finalChunkSent$ 和 $createdAt$。$turnState$ 可取 created、running、waiting-tool、continuing、resuming、cancelling、cancelled、completed、failed、cleaned；$observerConnections$ 是以 $connectionId$ 为键的观察者映射，记录该连接是否具备取消权限、是否处于 replaying、其 $lastAckedSeq$ 与最近活跃时间；$pendingResumeAcks$ 记录已发送 stream-resuming 但尚未收到 ACK 的连接及超时时间。

恢复缓存 resumeCache 以 $requestId$ 或 $resumeCacheId$ 为索引保存 chunk 记录，每条记录包含 $seq$、$body$、$createdAt$、$isTerminal$、$streamId$ 和可选校验摘要。服务端生成每个 chunk 时先递增 $lastChunkSeq$，再写入缓存并广播；客户端只接受 $seq = lastReceivedChunkSeq + 1$ 的 chunk，重复 $seq$ 直接丢弃，跳号则暂停消费并重新发起 resume。AbortRegistry entry 记录 $requestId$、controller、generation、cancelled、cancelReason 和 createdAt，generation 用于区分同一标识在异常重试或清理后产生的新旧 controller。

协议帧可以统一包含 type、$requestId$、$connectionId$、timestamp 和可选 $resumeToken$。send 帧携带消息和请求 body；chunk 帧携带 $seq$、body、isReplay、isTerminal；stream-resuming 帧携带 $requestId$、$resumeToken$、$lastChunkSeq$；resume-ack 帧携带 $resumeAttemptId$、$lastReceivedChunkSeq$ 和 $resumeToken$；resume-none 表示无可恢复流；server-cancel 帧携带 $cancelReason$、source、cancelSeq；cancel-ack 帧返回 accepted、already-finished、not-found 或 rejected；terminal-chunk 帧携带 completed、cancelled 或 failed 终态；continuation-start 和 continuation-result 帧用于标识工具 continuation 的请求建立和结果回传。

### 客户端本地清理与显式取消判定

在客户端传输层中，sendMessages 生成新的 $requestId$ 后建立本地 ReadableStream，并把服务端返回的 chunk 映射为供前端状态机消费的流。本方案将该本地 ReadableStream 的终止动作拆分为“detach”与“server-cancel”两条路径：detach 只关闭或报错当前 reader、移除本地消息监听器、释放本地 AbortController 和 active request 标记，不向服务端发送取消帧；server-cancel 则在执行本地清理之前或同时，携带 $requestId$ 发送请求取消消息。由此，reader.cancel()、组件卸载、页面刷新、路由切换等前端生命周期事件默认进入 detach 路径，避免把 UI 清理误解释为要求服务端中断推理或工具执行。

显式用户停止可以由 useAgentChat 暴露的 stop 操作、应用层主动取消 API 或带原因的 AbortSignal 触发。客户端在触发 server-cancel 时应记录取消来源，例如 user-stop、app-cancel、clear-history 或 shutdown，并将其与 $requestId$ 一并编码到协议消息或本地策略上下文中。若当前处于工具 continuation 阶段，客户端先终止本地 continuation reader，再在已获知 continuation 的 $requestId$ 时发送同一类服务端取消消息；若 continuation 握手尚未完成，则只清理握手 resolver 和本地状态，避免对未知请求误发取消。

detach 路径的触发输入包括 reader.cancel()、组件卸载、页面切换、刷新前清理、WebSocket close 以及恢复流本地超时。其前置条件是本地已存在 $requestId$ 或恢复尝试上下文，但取消来源未被策略判定为可传播的服务端取消。执行顺序为：将 $localReaderState$ 从 streaming 或 resuming 原子置为 detaching；停止当前 reader 拉取并忽略后续 enqueue；注销 message、close、abort 等本地监听器；标记该连接的 observer 为 detached 或删除本地 observer；释放本地 AbortController；删除 active request 标记；最后置为 detached 或等待后续 resume。该流程不发送 server-cancel 帧，且允许重复调用，重复调用只返回当前 detached 状态，不再次释放已释放资源。

server-cancel 路径的前置条件包括：$requestId$ 已知，turn 未进入 completed、cancelled 或 failed 终态，取消来源属于 user-stop、app-cancel、clear-history、shutdown 或可信外部 AbortSignal，且当前客户端的 $connectionId$ 与该 turn 存在会话关联或具备取消权限。执行顺序为：记录 $stopReason$ 和单调 $cancelSeq$；将 $localReaderState$ 置为 cancelling；发送携带 $requestId$、source、$cancelReason$、$connectionId$、timestamp 的 server-cancel 帧；关闭或报错当前本地 reader 并保留 $requestId$ 的去重上下文；等待 cancel-ack 或 terminal-chunk。若未收到 cancel-ack，可以按同一 $cancelSeq$ 重试，服务端对重复帧返回相同结果或 already-cancelling，保证幂等。

### 服务端 turn 生命周期与 AbortRegistry 控制

服务端以 $requestId$ 为键为每个 agent turn 建立或获取 AbortRegistry 中的 AbortController，并将其 signal 传入 onChatMessage、模型调用、工具执行或子 agent 调用链。服务端收到普通 WebSocket close 时，仅清理该连接对应的 pending resume、continuation 等连接状态，不直接调用 AbortRegistry.cancel；收到请求取消消息后，先通过取消策略判断该消息是否属于可传播到服务端执行链路的取消意图，再对对应 $requestId$ 执行 abort，并向内部事件总线发出 message:cancel 事件。turn 正常完成、错误结束或被取消后的 finally 阶段删除对应 controller，防止后续请求复用已中止的 signal。

对于程序化调用或父子 agent 场景，外部 AbortSignal 可以通过 linkExternal 绑定到同一 $requestId$ 的 registry controller，使父任务取消、系统关闭等明确上游取消能够沿链路传播；但浏览器连接关闭不直接作为该外部 signal 的来源。这样，服务端的执行中止由“已识别的取消意图”驱动，而不是由单个传输连接的存活状态驱动，从而支持长时间推理、异步工具调用和 Durable Object 休眠恢复期间的连续执行。

服务端 turn 状态机按事件驱动转移：send 请求创建 ServerTurnState 后进入 created，取得 AbortRegistry signal 并启动 onChatMessage 后进入 running；发出客户端工具请求或等待审批时进入 waiting-tool；工具结果触发自动 continuation 时进入 continuing；收到恢复请求并开始 replay 时，对该连接标记 replaying，turn 可保持 running 或标记 resuming；收到有效 server-cancel 后进入 cancelling；模型或工具链路观察到 abort 并输出取消终态后进入 cancelled；正常 terminal chunk 持久化且 $finalChunkSent$ 为 true 后进入 completed；不可恢复错误进入 failed；完成缓存清理和 observer 清理后进入 cleaned。completed、cancelled、failed、cleaned 之间不得互相覆盖。

AbortRegistry 的创建和删除采用 generation 保护。服务端仅在 turn 进入 created 或 running 前为当前 $requestId$ 创建 controller，并把 generation 写入 ServerTurnState；重复进入同一 $requestId$ 时，如果已有未完成 controller，则复用同一 entry；如果已有 entry 已 cancelled 或 terminal，则拒绝复用并要求生成新的 $requestId$。取消时仅对命中且未 terminal 的 entry 调用 abort，重复取消只更新或返回既有 cancelReason，不重复触发下游清理。finally 删除 controller 时需同时匹配 $requestId$ 与 generation，避免旧 turn 的 finally 删除同一 $requestId$ 下新 turn 的 controller。

服务端处理 server-cancel 时先校验 $requestId$ 是否对应活动 turn、$connectionId$ 是否属于同一会话或授权观察者、source 是否属于可传播取消来源，并检查 $finalChunkSent$ 与 $turnState$。若 terminal chunk 已持久化且 $finalChunkSent$ 为 true，则不调用 abort，返回 already-finished；若 controller 不存在但恢复缓存仍有 terminal 记录，则返回 already-finished 或 not-found；若处于 running、waiting-tool 或 continuing，则记录 $cancelReason$，转入 cancelling，调用 AbortRegistry.cancel，并返回 accepted。由于显式取消必须携带 $requestId$ 和 reason 且经策略门控，服务端能够精确中止对应 turn，而不会把普通连接断开传播到模型和工具链路。

### 断线恢复、多标签观察和工具 continuation 协同

当浏览器刷新、短暂断网或重新创建 WebSocket 后，客户端通过 reconnectToStream 发起恢复请求。服务端若存在 active stream，则先向该连接发送 stream-resuming 通知并把连接加入 pending resume 集合；客户端在本地消息处理器已就绪后返回 ACK；服务端随后依据 $requestId$ 从可恢复流缓存中 replay 已存储 chunk，并在 replay 期间把该连接排除在 live broadcast 之外，避免历史 chunk 与实时 chunk 重复到达。replay 完成后，该连接继续接收 live chunk；如果没有可恢复流，则返回 resume-none，使客户端退出恢复状态。

恢复流采用 chunkSeq、resumeToken 和连接级 replaying 标记实现去重与顺序控制。客户端发起 resume-ack 时携带 $resumeToken$、$resumeAttemptId$ 和 $lastReceivedChunkSeq$；服务端验证 token 与会话关联后，仅 replay $seq > lastReceivedChunkSeq$ 的缓存 chunk。处于 replaying 的连接被加入 pendingResumeAcks 或 replayingConnections，live broadcast 对该连接排除；replay 完成后，服务端把该连接的 $lastAckedSeq$ 更新为 replay 的最大 $seq$，清除 replaying 标记，并从 $lastAckedSeq+1$ 开始接收 live chunk。若 replay 中途断开，服务端删除该连接的 pending 记录但保留 active stream 和 resumeCache，客户端下次以最新已消费 $seq$ 重试，重复 replay 的 chunk 由客户端按 $seq$ 丢弃。

resume ACK 超时、resume-none 和 replay 失败按不同边界处理。服务端发送 stream-resuming 后为该 $connectionId$ 设置 ackDeadline，超时未收到 ACK 时仅移除 pendingResumeAcks 和 replaying 标记，不取消 active turn；客户端在等待超时后把本地 $localReaderState$ 置为 detached 或 idle，并可在新连接建立后重新发起 resume。若服务端返回 resume-none，表示无活动可恢复流，客户端结束恢复状态但不推断服务端已被取消。若 replay 期间同时产生 live chunk，服务端仍先写入 resumeCache 并向非 replaying 连接广播，对 replaying 连接延迟到 replay 完成后按 $seq$ 补齐。

多标签页或多组件观察同一会话时，服务端把 $observerConnections$ 作为连接引用计数和权限集合，而不是把每个标签页视为独立 turn。单个观察者 detach 或 socket close 只删除其 observer 记录；最后一个观察者离开时，根据策略启动 grace-period 定时器并写入 $graceDeadline$；宽限期内若新连接携带有效 resumeToken 恢复，则增加 observer 计数并撤销尚未触发的定时器。任一具备取消权限的观察者发出 user-stop 时，该取消针对整个 $requestId$ 生效，其他标签页接收 cancel-ack 或 terminal-chunk 后转入 cancelled；跨标签重复 stop 以 $cancelSeq$ 和 $turnState$ 幂等处理。

工具 continuation 与恢复机制共享取消状态机。若 continuation-start 已返回并形成 $activeContinuationRequestId$，显式停止直接映射到该 request；若用户停止发生在 continuation 握手完成前，客户端记录 pendingStopForContinuation，并清理本地 continuation reader。随后若服务端晚到 continuation-start，客户端或服务端依据该标记立即对该 continuation 返回 cancelled 或发送 server-cancel，避免未知 request 被误取消又避免停止意图丢失。若工具执行本身不可中断，服务端将 turnState 标记为 cancelling，停止接收后续工具结果或把晚到结果标记为 ignored，最终输出 cancelled terminal；若工具结果与取消同时到达，按原因优先级和到达时的 terminal 状态判定，terminal 已发送则保持完成，否则取消优先进入 cancelling。

### 并发与异常处理

取消原因按 shutdown、clear-history 或 reset、可信外部 AbortSignal、user-stop、app-cancel、disconnect-derived、detach 的顺序确定优先级，高优先级原因不得被低优先级事件覆盖。用户显式停止与服务端正常完成并发时，若 terminal chunk 已写入 resumeCache 且 $finalChunkSent$ 为 true，则后到 cancel 返回 already-finished，不覆盖 completed；若 abort 已进入模型或工具链路且尚未发送 terminal chunk，则输出 cancelled terminal 并持久化对应终态。重复取消、重复 detach、重复 resume-ack 均以 $requestId$、$cancelSeq$、$resumeAttemptId$ 和 $turnState$ 幂等处理。

最后一个观察者离开并启动 grace-period 后，服务端只设置定时取消任务，不立即 abort。若宽限期内有新 observer 恢复，则清除 $graceDeadline$ 并保持 running、waiting-tool 或 continuing；若定时器触发时仍无 observer 且策略输出为服务端取消，则转入 cancelling。若定时器已触发并已调用 AbortRegistry.cancel，则后续恢复只能 replay 已缓存 chunk 和取消终态，不再回退到 running。resume token 过期、缓存过期或会话校验失败时，服务端返回 rejected 或 resume-none，并可按策略清理孤立缓存，但不把该失败解释为用户停止。

全局清理事件与本地 detach 同时出现时，全局清理优先。clear-history、resetTurnState、agent 销毁或 shutdown 的处理顺序为：冻结新 send 和 continuation-start；对 pending turn 写入高优先级 cancelReason；调用 AbortRegistry.destroyAll 或逐项 cancel；向可达 observer 发送 terminal 或 clear 帧；停止 continuation 队列并拒收晚到工具结果；清理 pendingResumeAcks、observerConnections、resumeCache 和本地 active request 标记；最后把 turnState 置为 cleaned。若 terminal chunk 已在清理前持久化，则保留该 terminal 供恢复读取；若清理要求删除会话历史，则同时删除 terminal 与普通 chunk 缓存。

### 可配置策略与边界处理

本方案保留“客户端断开是否代表取消服务端任务”的可配置空间。策略可以包括：explicit-only，只有用户停止、应用主动取消、清空会话或系统关闭才中止服务端 turn；disconnect-cancels，在特定低成本或不可恢复场景中把连接断开视为取消；grace-period，在最后一个观察者离开后等待预设时间，若无新连接恢复再取消；no-observer-and-nonresumable，仅在无观察者且当前 turn 不具备可恢复流缓存时取消。不同策略可以按会话、agent 类型、请求 body、工具风险等级或运行成本进行选择。

策略判定可以输出本地动作和服务端动作两个结果：本地动作决定是否关闭 reader、清理 resolver、删除 active request 标记或进入恢复等待；服务端动作决定是否发送取消帧、是否调用 AbortRegistry.cancel、是否保留可恢复流缓存以及是否广播终止 chunk。对于清空会话、resetTurnState、agent 销毁等全局清理事件，系统可以无条件终止 pending turn、清理恢复缓存和 continuation 状态；对于普通 close、resume 超时或 replay 连接中断，系统保留 active stream 与 request 上下文，等待后续恢复或由策略的宽限期处理。

| 策略 | 触发输入 | 前置条件 | 本地动作 | 服务端动作 | 缓存与终态 |
| --- | --- | --- | --- | --- | --- |
| explicit-only | detach、close、user-stop、app-cancel、clear-history | 只有显式取消或全局清理可传播 | detach 仅清理本地；stop 进入 cancelling | 仅对显式取消调用 AbortRegistry.cancel | 保留可恢复缓存；显式取消写入 cancelled 终态 |
| disconnect-cancels | WebSocket close 或最后观察者离开 | 请求成本低、不可恢复或业务配置为断开即取消 | 关闭 reader 并标记 detached | 连接断开被映射为 server-cancel | 可删除未完成缓存或写入 cancelled |
| grace-period | 最后一个 observer 离开 | turn 未 terminal 且存在可恢复缓存或长任务 | 启动恢复等待；新连接恢复则撤销 | 超时仍无 observer 才 cancel | 宽限期内保留 resumeCache；超时后写入 cancelled 或 failed |
| no-observer-and-nonresumable | 无 observer 且无有效 resumeCache | lastChunkSeq 不可 replay 或 token 失效 | 本地进入 detached 或 idle | 取消或清理孤立 turn | 清理不可恢复缓存；返回 resume-none 或 rejected |

策略引擎的输入为事件类型、$requestId$、$connectionId$、source、$cancelReason$、$turnState$、observer 数量、是否存在 resumeCache、$finalChunkSent$、工具 continuation 状态和运行配置；输出为 localAction、serverAction、cacheAction 和 terminalAction。localAction 可以是 detach、wait-resume、close-reader 或 show-terminal；serverAction 可以是 none、send-cancel、abort-registry、reject-cancel 或 clear-all；cacheAction 可以是 keep、replay、expire 或 delete；terminalAction 可以是 none、completed、cancelled、failed 或 already-finished。由于策略输出同时覆盖本地动作和服务端动作，系统能够在保持显式停止可达的同时，避免把普通 UI 生命周期变化误传播到服务端执行链路。
