## 技术方案

### 核心控制模型：观察通道与取消通道解耦

本方案将长时间 agent 对话中的“观察关系”和“执行取消”拆分为相互独立的两个控制通道。观察通道用于把浏览器标签页、React 组件实例、WebSocket 连接、ReadableStream reader 或恢复后的客户端连接附着到同一个服务端 agent turn 上，其作用是接收流式片段、工具调用状态和最终结果；取消通道用于表达用户明确停止、应用主动中止、会话清空或删除等会改变服务端执行状态的控制意图。二者通过同一 $requestId$、$turnId$ 或 $runId$ 建立关联，但观察通道的关闭不自动等同于取消通道的触发。

在该控制模型下，服务端 agent turn 被视为具有独立生命周期的执行对象，客户端连接只是该执行对象的观察者。客户端刷新页面、组件卸载、路由切换、reader cancel 或短暂网络断开时，默认只解除对应观察者与服务端 turn 的绑定并释放本地资源，服务端仍可继续生成、缓冲并持久化流式片段。只有当客户端发送携带目标标识和取消原因的显式取消控制消息时，服务端才将该意图映射到对应 turn 的中止信号。

本方案中以 $turnId$ 作为服务端执行对象的主键；$requestId$ 表示客户端一次发起请求，可映射到一个 $turnId$；$runId$ 表示模型推理、工具任务或 continuation 的运行实例，隶属于同一 $turnId$；$streamId$ 表示一次可观察的流；$observerId$ 表示某个标签页、组件实例或连接观察者；$generationId$ 用于区分同一客户端上的新旧本地流，防止过期回调修改当前状态。上述标识共同出现在观察、恢复和取消消息中，使本地流生命周期与服务端执行生命周期可被分别寻址。

| 消息类型 | 主要字段 | 服务端输出 |
| --- | --- | --- |
| attach/detach | targetType、turnId、streamId、observerId、generationId、reasonCode、timestamp、clientEpoch、session 标识 | accepted、currentState、observerCount、errorCode |
| resume | turnId 或 streamId、observerId、lastAckSeq 或 lastEventId、generationId、clientEpoch、session 标识 | accepted、replayFrom、replayTo、currentState、replayComplete 或 resumeUnavailable |
| cancel | targetType、turnId 或 requestId、runId、observerId、reasonCode、timestamp、clientEpoch、session 标识 | accepted、alreadyTerminal、notFound、currentState、terminalReason |
| state/terminal 响应 | turnId、streamId、stateVersion、terminalReason、lastPersistedSeq、observerCount | 用于客户端更新本地状态、停止恢复或展示终态 |

### 客户端本地清理与取消意图分类

客户端侧对本地终止原因进行分类，并根据分类决定是否进入服务端取消通道。第一类为本地清理类原因，包括组件卸载、页面切换、reader cancel、旧 generation 被新 generation 替换、恢复流切换以及普通连接释放；该类原因只关闭本地读取器、移除事件监听、清理 pending resolver 或从观察者集合中 detach，不发送服务端请求取消消息。第二类为可恢复连接类原因，包括浏览器刷新、WebSocket 短暂断开、移动网络切换或服务端休眠后重连；该类原因触发重连和流恢复流程，不改变服务端 turn 的执行状态。第三类为执行取消类原因，包括用户点击“停止”、应用调用主动取消、清空当前会话、删除会话或策略判定应终止后台执行；该类原因才生成面向服务端的取消控制帧。

在一种实现中，客户端 transport 为普通请求流、恢复流和工具 continuation 流分别维护本地关闭处理器，但这些处理器不直接复用同一个 abort 分支，而是先生成本地原因码，例如 observer-detach、network-loss、resume-timeout、user-stop、app-abort 或 clear-delete。transport 或上层 hook 根据原因码和应用策略进行映射：observer-detach、network-loss 等原因仅结束本地流；user-stop、app-abort、clear-delete 等原因携带目标 $requestId$ 或 $turnId$ 发送取消控制帧。由此避免把浏览器或框架生命周期事件误解释为用户要求停止服务端推理。

| reasonCode | 触发来源 | 客户端动作 | 是否进入服务端取消通道 |
| --- | --- | --- | --- |
| observer-detach | 组件卸载、页面切换、reader cancel、旧 generation 替换 | 关闭本地 reader、移除监听、发送或记录 detach | 否 |
| network-loss | WebSocket close、网络切换、浏览器刷新后的连接丢失 | 保持本地恢复意图，重连后发送 resume | 否 |
| resume-timeout | 重连或恢复握手超时 | 结束本地恢复尝试，可按策略转为空闲或重新 attach | 默认否，除非策略升级 |
| user-stop | 用户点击停止按钮 | 立即发送 cancel，随后关闭本地读取器 | 是 |
| app-abort | 应用主动中止当前 turn | 发送 cancel 并记录应用原因 | 是 |
| clear-delete | 清空或删除会话 | 校验权限后发送 cancel，并清理本地会话视图 | 是 |
| policy-timeout/low-priority-abort | 无观察者超时、资源限额或低优先级策略命中 | 由服务端或协调层生成策略取消原因 | 是 |

同一 $generationId$ 内若多个原因几乎同时出现，客户端按“执行取消类优先于可恢复连接类，可恢复连接类优先于本地清理类”的顺序归并。例如 reader cancel 与 user-stop 同时发生时，以 user-stop 为准并发送 cancel；network-loss 后用户在重连前点击停止时，客户端在恢复连接可用后补发 cancel，或由备用控制连接发送 cancel；旧 $generationId$ 的 observer-detach 不得覆盖新 $generationId$ 已建立的 resume 或 live stream 状态。

### 服务端 turn 状态、观察者集合与恢复缓冲

服务端为每个 agent turn 维护执行状态、观察者集合、恢复缓冲和取消控制器。执行状态至少区分 pending、streaming、waiting-tool、completed、error、aborted 等状态；观察者集合记录当前连接、标签页或恢复请求与该 turn 的附着关系；恢复缓冲保存已经生成但可能尚未被某些客户端确认接收的流式片段；取消控制器按 $requestId$ 或 $turnId$ 与服务端推理、工具 continuation 或后续续写流程的 AbortSignal 关联。上述状态由服务端统一管理，使执行生命周期不依赖任一客户端连接是否仍然存在。

| 记录对象 | 关键字段 | 更新时机 |
| --- | --- | --- |
| turnRecord | turnId、requestId、state、stateVersion、ownerSession、priority、createdAt、lastActiveAt、terminalReason | 创建请求、状态迁移、终态写入时更新 |
| observerRecord | observerId、turnId、streamId、generationId、clientEpoch、sessionId、lastAckSeq、attachedAt、detachedAt | attach、detach、ACK、连接抢占或恢复完成时更新 |
| chunkRecord | turnId、streamId、seq、offset、eventId、payloadHash、createdAt、stateVersion、persisted、replayableUntil | 每个流式 chunk 生成并写入缓冲或持久化日志时更新 |
| cancelRecord | turnId、requestId、runId、reasonCode、sourceObserverId、timestamp、accepted、stateVersion | 收到显式取消、策略取消或重复取消审计时写入 |

| 状态 | 进入条件 | 允许操作 | 退出或终态规则 |
| --- | --- | --- | --- |
| pending | 请求已登记但模型或工具链尚未开始 | attach、detach、resume 查询、cancel | 模型启动成功后进入 streaming；显式取消可进入 aborted |
| streaming | 模型正在输出或服务端正在生成 chunk | attach、detach、resume、cancel、ACK | 工具调用发出后进入 waiting-tool；正常结束进入 completed；异常进入 error；显式取消进入 aborted |
| waiting-tool | 等待客户端工具结果、approval 或服务端工具任务完成 | attach、detach、resume、cancel、工具结果处理 | 工具结果有效且未取消时回到 streaming；approval 拒绝可进入 aborted 或 completed-without-continuation；异常进入 error |
| completed | turn 正常完成并写入最终消息 | attach 读取终态、resume 读取已持久化结果 | 不可被 cancel 覆盖，只能返回 alreadyTerminal |
| error | 服务端异常或不可恢复写入失败 | attach 读取错误状态、resume 读取已持久化片段 | 不可被 cancel 覆盖，只能返回 alreadyTerminal |
| aborted | 显式取消或策略取消已被接受 | attach/resume 读取 partial chunks 和取消原因 | 不可重新启动同一 turn，迟到 chunk 或工具结果不得追加为有效输出 |

恢复请求的前置条件是目标 turn 存在、观察者具有会话权限、恢复缓冲或持久化日志仍在保留期内，且客户端提交的 $lastAckSeq$、$lastEventId$ 或 cursor 未落在已清理区间之外。服务端以 chunkRecord 中的 $seq$ 或 $eventId$ 为有序游标，计算 $(lastAckSeq, lastPersistedSeq]$ 的缺失区间，按序回放缺失 chunk，并在回放期间继续把新生成 chunk 写入缓冲；正在回放的观察者被临时排除在 live broadcast 之外。

当缺失区间回放到服务端开始处理该恢复请求时刻的高水位序号后，服务端发送 replayComplete，并把 observerRecord 的 lastAckSeq 更新到已回放位置，然后将该观察者并入 live stream。若回放期间又生成新 chunk，该 chunk 因序号大于高水位而在并入 live stream 后继续发送，或者在并入前作为扩展回放区间发送，但同一 $seq$ 只允许被确认一次。若 cursor 无效、缓冲过期、chunk 缺口不可补齐或持久化日志不可读，服务端返回 resumeUnavailable、cursorExpired 或 gapDetected，并可降级返回终态摘要、要求客户端重新 attach，或在无法保证一致性时进入 error。

对于多个浏览器标签页或多个组件同时观察同一 turn 的场景，服务端不为每个观察者重复启动 agent turn，而是将其作为 fan-out 观察关系处理。一个标签页关闭或 reader cancel 只影响该观察者；其他标签页仍可继续接收广播，后续新标签页也可通过恢复缓冲追赶到当前进度。该机制使“是否还有观察者”和“服务端是否继续执行”成为可独立判断的状态条件，而不是通过单个连接关闭事件隐式决定。

### 显式取消传播、幂等处理与终态保护

取消请求处理按“校验—定位—原子更新—传播—通知”的顺序执行。服务端先校验 session 或 owner 权限，再依据 $turnId$、$requestId$ 或 $runId$ 定位 turnRecord 和对应取消控制器；若目标不存在，返回 notFound；若目标已处于 completed、error 或 aborted，返回 alreadyTerminal 及当前 terminalReason；若目标仍为 pending、streaming 或 waiting-tool，则以 compare-and-set 方式将 stateVersion 对应的非终态原子更新为 aborted，并写入 cancelRecord。原子更新成功后，服务端才向模型推理、消息保存流程、工具 continuation 或关联子任务传播 AbortSignal，并向所有观察者广播 aborted 状态、取消原因和已持久化的 partial chunks 高水位。

并发终态采用唯一写入规则。模型正常完成、服务端异常和取消控制帧几乎同时到达时，以服务端持久化的 stateVersion 和 compare-and-set 结果决定唯一终态：completed 或 error 已写入后，迟到 cancel 只能追加审计记录，不得覆盖终态；aborted 先写入后，迟到 chunk、工具结果或 continuation 输出被标记为 late-output 并丢弃或仅作诊断记录，不得追加为有效消息。重复 cancel 使用相同 $turnId$ 和 reasonCode 去重，只返回第一次接受的终态信息。

### 无观察者策略配置

无观察者策略的前置条件是 turn 仍处于 pending、streaming 或 waiting-tool，且 observerRecord 中不存在有效观察者。grace window 从最后一个观察者 detach 或半开连接被判定失效的时刻起算；在窗口内若有同一 session 或授权观察者重新 attach/resume，则窗口清零或续期。策略判定同时参考会话级配置、任务优先级、模型或工具运行成本、最大后台执行时长、恢复缓冲容量上限和系统资源限额；当策略冲突时，按显式用户取消、应用取消、资源安全限制、会话配置、默认恢复优先的顺序决策。

策略输出可以是 continue-background、pause-observation、cancel-after-grace 或 cancel-immediately。若输出为继续后台执行，服务端继续生成并写入 chunkRecord，但对缓存大小、保留时间和未确认 chunk 数设置上限，超过上限时可压缩为终态摘要、停止可恢复回放或按 policy-timeout、buffer-limit、low-priority-abort 等独立 reasonCode 进入取消流程。策略触发的取消与用户取消使用同一取消通道和终态保护规则，但取消原因保持区分，以便客户端展示、审计和后续调参。

### 多标签页及工具 continuation 兼容处理

多标签页观察的前置条件是不同 observer 具有同一会话或授权范围，并附着到同一 $turnId$ 或 $streamId$。单个标签页关闭、reader cancel 或 detach 只移除该 observerRecord，不影响其他观察者；某一标签页点击“停止”时，若其具备 owner 或可取消权限，则该操作被解释为对整个 turn 的全局执行取消，服务端广播 aborted 状态、取消原因、最后持久化序号和 partial chunks 可读范围；若该标签页无全局取消权限，则服务端拒绝 cancel 或降级为仅 detach 当前观察者。

工具 continuation 以 $turnId$ 作为主关联键，并记录 toolCallId、approvalId、toolRunId、continuationRunId、parentRunId、stateVersion 和 abortLink。waiting-tool 状态下收到显式取消时，服务端先将 turn 原子更新为 aborted，再向可中断的工具任务或 continuationRun 传播 AbortSignal；若工具任务不可中断，其后到达的 result 仅作为 late-result 记录，不触发续写。approval 被拒绝时，可按应用规则进入 aborted、completed-without-continuation 或等待其他输入，但不得伪装成网络断开；工具结果在 turn 已 completed、error 或 aborted 后到达时，不再生成 continuation stream。

工具调用 pending、approval、result 和 continuation chunk 均作为同一 turn 的事件写入缓冲或状态日志，并向已授权观察者广播。正在恢复回放的观察者暂不接收实时 continuation 广播，待其按序回放到高水位并收到 replayComplete 后再并入 live stream。由此，工具结果、人工审批和续写输出可在多标签页之间保持一致，同时避免因某个观察者断开而误取消工具链。

### 异常恢复与边界处理

服务端重启或运行实例迁移后，先从持久化 turnRecord、chunkRecord 和 cancelRecord 恢复非终态 turn 的状态版本、最后持久化序号和取消标记；若能确认模型或工具运行仍可继续，则重建观察者为空的 active turn 并等待 attach/resume；若无法确认运行实例，返回 recoverUnknown 或将 turn 转入 error，避免客户端误认为仍可无缝恢复。WebSocket 半开连接通过心跳、写入失败或超时检测判定为 detach，起算无观察者 grace window，但不直接触发 cancel。

取消消息丢失时，客户端可使用同一 $turnId$、reasonCode 和 clientEpoch 重试，服务端通过 cancelRecord 幂等去重；取消消息重复时，服务端返回第一次接受的状态结果。恢复缓冲写入失败但持久化日志成功时，以最后持久化序号为恢复基准；持久化写入失败且无法保证 chunk 顺序完整时，服务端停止继续广播新的有效 chunk，并把 turn 转入 error 或返回 gapDetected。客户端确认位置回退时，服务端可重复回放未超过保留期的 chunk，但以 payloadHash 和 $seq$ 去重；同一 observerId 被新连接携带更高 clientEpoch 抢占时，旧连接被 detach，旧连接后续 ACK 或 cancel 仅在通过权限和 generation 校验后才被接受。
