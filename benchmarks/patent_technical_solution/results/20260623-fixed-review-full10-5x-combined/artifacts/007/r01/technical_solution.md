## 技术方案

### 总体架构与控制目标

本方案在既有浏览器端 agent chat、WebSocket 流式传输、服务端可恢复流和每请求取消控制的基础上，引入“本地观察连接生命周期”和“服务端 agent turn 生命周期”相分离的控制机制。浏览器端负责创建或恢复一个与服务端响应流对应的本地可读流，服务端以请求标识维护正在执行的 agent turn，并通过可恢复流保存已产生的响应分块；二者之间通过请求标识、恢复握手和显式取消消息进行关联，而不是把浏览器端 reader、组件或页面连接的关闭事件直接等同于服务端任务取消。

该控制机制包括客户端取消语义分类单元、传输层取消分派单元、服务端请求中止登记单元、可恢复流缓冲单元以及多观察者状态协调单元。客户端取消语义分类单元根据触发来源区分本地清理事件和服务端取消意图；传输层取消分派单元仅在确认存在服务端取消意图时发送携带请求标识的取消消息；服务端请求中止登记单元根据请求标识触发对应 AbortController；可恢复流缓冲单元继续保存 agent turn 已产生的响应分块；多观察者状态协调单元用于在刷新、重连或多标签页观察时对重放分块和实时分块进行去重合并。

通过上述划分，长时间 agent 对话中的“停止生成”不再由连接是否关闭、reader 是否取消或 React 组件是否卸载隐式推断，而由用户操作、应用业务逻辑或外部任务控制信号显式表达。这样可以同时满足两类相反需求：一方面，浏览器刷新、页面切换、短暂网络断开或观察者标签页关闭时，服务端 agent turn 仍可继续运行并等待后续恢复；另一方面，当用户明确点击停止或应用主动终止任务时，取消意图能够到达服务端并中止对应 turn 或工具续跑。

### 数据模型与状态模型

本方案采用统一的会话执行记录描述浏览器观察者、本地流、服务端 turn、工具运行和可恢复分块之间的关系。requestId 唯一标识一次服务端 agent turn 或一次续跑 turn，并作为取消、恢复和分块归属的主键；turnId 用于标识同一轮用户输入及其后续工具续跑形成的逻辑轮次，多个 requestId 可以关联到同一 turnId；runId 标识某个工具或子 agent 的 durable work；observerId 标识浏览器标签页或组件实例；connectionId 标识一次 WebSocket 连接；ownerTabId 标识当前对该 turn 具有默认停止权限的观察者；resumeToken 用于证明恢复请求与原会话、turn 或连接租约相关；chunkSeq 是同一 requestId 下单调递增的分块序号。

服务端维护两类登记表。controllerRegistry 以 requestId 或 runId 为键保存 AbortController 及 controllerState，controllerState 至少包括 active、abortRequested、aborted、released；streamRegistry 以 requestId 为键保存 streamState、active 标志、lastPersistedSeq、terminalSeq、doneReason、errorCode、bufferRetainUntil 和 checksum 摘要。controllerRegistry 用于实时中止正在运行的执行链路，turn 进入终止状态后即可释放；streamRegistry 用于恢复、去重和终止状态查询，即使 controller 已释放，也保留至缓冲过期或清理策略触发。

客户端维护 observerState 和 localStreamState。observerState 包括 observerId、connectionId、ownerTabId、permissionLevel、leaseExpireAt、lastAppliedSeq、lastReplaySeq、lastAckSeq、pendingCancelKey 和 activeRequestIds；localStreamState 包括 streamType、requestId、turnId、runId、streamState 和 cancelSource。streamType 用于区分普通提交流、恢复流、广播观察流和工具续跑流；streamState 至少包括 idle、submitting、awaitingResume、replaying、live、localClosed、terminal。上述字段使客户端能够判断某次关闭是本地观察关系结束，还是应升级为服务端取消。

### 本地流清理与服务端 turn 生命周期解耦

客户端侧执行取消语义分类时，以触发事件类型、streamType、requestId 是否已取得、observerId 是否为 ownerTabId、permissionLevel、应用策略、外部 AbortSignal 状态、当前 turn 状态和 pendingCancelKey 为输入，输出 localCleanup、serverCancel、deferUntilRequestId、ignore 或 retryCancel。localCleanup 表示只释放本地 ReadableStream、事件监听、resolver、定时器和 activeRequestIds；serverCancel 表示构造并发送服务端取消消息；deferUntilRequestId 表示工具续跑握手尚未得到请求标识，先记录停止意图；ignore 表示当前 turn 已处于终止状态或主体无权限；retryCancel 表示取消语义成立但连接暂不可用，需要在后续连接恢复后补发。

默认判定规则为：组件卸载、页面跳转、浏览器刷新、恢复流 reader cancel、广播观察流关闭和短暂 WebSocket 断开输出 localCleanup；普通提交流 reader cancel 仅在触发源被标记为用户停止或应用主动取消时输出 serverCancel，否则输出 localCleanup；用户点击停止、外部 AbortSignal abort、父级 turn 取消和系统策略终止输出 serverCancel；工具续跑握手前收到停止输出 deferUntilRequestId，握手完成并取得续跑 requestId 后转为 serverCancel；turn 已经 completed、failed、cancelled、expired 或 unrecoverable 时输出 ignore。多个事件同时出现时，显式取消优先于本地清理，但既有终止状态优先于后到取消。

分类结果作用于不同流类型的关闭处理函数。普通提交流在 localCleanup 下只关闭本地 reader 并保留服务端 requestId 的可恢复资格；恢复流的 cancel 固定按 localCleanup 处理，且不改变 streamRegistry 中的 active 状态；工具续跑流在 requestId 为空时仅释放本地等待状态并保留 deferUntilRequestId 标记，在获得 requestId 后若标记仍有效则发送服务端取消。由于 reader cancel 仅在分类结果为 serverCancel 时才调用服务端 AbortController，页面刷新、组件卸载和恢复订阅替换不会中止仍可恢复的 agent turn。

为保证服务端 turn 能在本地观察连接消失后继续推进，服务端在接收 chat 请求后以请求标识创建或取得对应的中止信号，并将该信号传入 agent 响应生成、消息保存和工具执行相关流程。只要未收到显式取消并且服务端执行环境仍允许继续运行，agent turn 的输出分块继续写入可恢复流缓冲区，并可广播给仍然在线的连接；没有在线观察者时，已产生分块仍可被后续恢复请求重放。

发送服务端取消前还应检查连接可用性、请求标识获取状态、权限状态、turn 当前状态和是否存在未确认的同一 pendingCancelKey。连接可用且无重复取消时立即发送；连接不可用但策略允许补发时，将 cancelSource、目标 requestId 或 runId、idempotencyKey 和时间戳写入本地待发送队列；若缺少请求标识且不是可延迟的工具续跑场景，则只执行本地清理并记录无法定位目标，避免向服务端发送范围不明的取消请求。

### 显式取消意图的传输与服务端执行

当分类结果为 serverCancel 时，客户端构造取消消息并通过当前 WebSocket 或恢复后的连接发送。取消消息至少包括 type、requestId 或 runId、turnId、cancelSource、observerId、ownerTabId、idempotencyKey、clientSeq 或 timestamp；其中 idempotencyKey 由目标标识和取消触发源生成，用于识别重复停止。服务端收到后先校验消息类型和目标标识，再读取 streamRegistry 中的 turn 状态和 controllerRegistry 中的 controllerState；若目标仍处于 pending、running、cancelRequested 或 cancelling，则进入取消处理；若目标已 completed、failed、cancelled、expired 或 unrecoverable，则返回或记录既有终止状态，不覆盖原状态。

服务端请求中止登记单元按如下流程工作：接收 chat 请求时，以 requestId 在 controllerRegistry 中延迟创建 AbortController，并将其 AbortSignal 传入 onChatMessage、响应流处理、保存消息和工具调用链路；若存在外部 AbortSignal，则通过链接函数把外部 abort 转换为同一 controller 的 abort；收到取消消息时，先把 streamRegistry 中的 streamState 从 running 迁移为 cancelRequested，再调用 controller.abort 并将 controllerState 标记为 abortRequested；执行链路感知 AbortSignal 后停止生成新分块，释放 controller 并保留 stream metadata 至 bufferRetainUntil。

响应分块采用统一结构持久化或广播，字段包括 requestId、chunkSeq、chunkType、payload、isTerminal、createdAt 和 checksum。文本 token、工具调用事件、replay 标记、done 分块和 error 分块在同一 requestId 下共享单调递增的 chunkSeq；done 或 error 作为 terminal 分块进入 streamRegistry，形成 terminalSeq、doneReason 或 errorCode。客户端按 requestId 与 chunkSeq 或 checksum 去重，只应用 chunkSeq 大于 lastAppliedSeq 的分块；若 terminal 分块先到达，则在所有不大于 terminalSeq 的已知分块处理后再关闭本地 streaming 状态。

对于工具调用、子 agent 或其他 durable work，服务端维护父 turn requestId、逻辑 turnId、工具 runId 和续跑 requestId 的映射表。父 turn 触发工具时创建 runId 并记录其父 requestId；工具结果需要继续生成时创建或登记续跑 requestId，并将其与同一 turnId 关联；父级取消默认级联至仍处于 running 的工具 run 和已建立的续跑 requestId，但可由策略关闭级联。不可中止工具若在取消后才返回结果，服务端只持久化工具运行的终止状态，不再把该结果驱动新的续跑 turn，除非策略明确允许把已完成且无副作用风险的结果作为已取消 turn 的附加记录。

### turn 状态机与竞态处理

服务端 turn 状态至少包括 pending、running、cancelRequested、cancelling、cancelled、completed、failed、expired 和 unrecoverable。pending 表示请求已登记但尚未开始生成；running 表示正在生成或等待工具结果；cancelRequested 表示已接收有效取消但执行链路尚未完全响应；cancelling 表示 AbortSignal 已触发并正在停止工具或响应流；cancelled、completed 和 failed 分别表示取消、正常完成和异常失败三类终止状态；expired 表示缓冲超过保留期；unrecoverable 表示因缓冲缺失、序号非法或服务端重启后无法重建而不能恢复。

允许的状态迁移为：pending 可进入 running、cancelRequested 或 failed；running 可进入 cancelRequested、completed 或 failed；cancelRequested 可进入 cancelling、cancelled、completed 或 failed，其中 completed 表示取消到达前服务端已经写入终止完成分块；cancelling 可进入 cancelled 或 failed；completed、failed、cancelled、expired 和 unrecoverable 为终止状态，不再迁移为其他终止状态。completed 或 failed 后收到取消消息时，不得改写为 cancelled；重复取消返回同一 terminal metadata 或取消确认；cancelRequested 后禁止生成新分块，但允许发送已经生成且 chunkSeq 不大于 terminalSeq 或 replayUntilSeq 的缓冲分块。

竞态处理遵循“终止状态优先、目标标识精确、取消幂等、序号有序”的规则。用户点击停止的同时服务端完成时，以先写入 streamRegistry 的 terminalSeq 和 doneReason 为准；取消消息延迟到达且 turn 已 completed 或 failed 时，仅记录重复取消或返回既有状态；两个标签页同时停止时，服务端根据 idempotencyKey 和 requestId 合并为一次 abort；父级 AbortSignal 与用户停止同时触发时，共用同一 controller，后到事件只补充 cancelSource，不重复中止。

### 恢复、工具续跑与多观察者协同

恢复流程的前置条件为：客户端消息处理器已经初始化，存在 conversationId、requestId 或 turnId 以及 resumeToken，客户端持有合法 lastReceivedSeq，服务端 streamRegistry 中对应缓冲尚未过期且未标记 unrecoverable。恢复请求可包含 type、conversationId、requestId 或 turnId、lastReceivedSeq、observerId、connectionId 和 resumeToken。服务端响应包含 streamState、active 标志、replayStartSeq、replayEndSeq、replayUntilSeq、doneReason 或 errorCode；若无活动流但存在 terminal metadata，则返回终止状态而不是让客户端无限等待。

服务端向某连接发送可恢复通知后，将该 connectionId 加入 pendingResumeConnections，并记录 replayUntilSeq。确认到达前，该连接不接收实时广播；确认超时或连接关闭时移出待确认集合，但不取消服务端 turn。确认到达后，服务端从 max(lastReceivedSeq+1, replayStartSeq) 开始重放缓冲分块，并在 replayUntilSeq 之后把该连接重新纳入实时广播。确认期间新产生的分块继续按 chunkSeq 写入缓冲并广播给其他非待确认连接；恢复连接随后通过序号水位线追上这些分块。

客户端处理 replay 与 live 交叉到达时，以 lastAppliedSeq 为唯一水位线，先按 requestId 分桶，再按 chunkSeq 排序应用；chunkSeq 小于或等于 lastAppliedSeq 的分块直接丢弃，checksum 不一致时将该 requestId 标记为 unrecoverable 并停止合并。若 done 或 error 分块先于部分 live 分块到达，客户端暂存 terminal 分块，直到所有不大于 terminalSeq 且已接收的分块处理完毕后再生效；因此恢复和实时广播交错时不会重复显示同一 token 或重复执行同一工具事件。

多标签页协同通过观察者注册表实现。观察者注册表记录 observerId、connectionId、ownerTabId、permissionLevel、leaseExpireAt、lastAckSeq 和 activeRequestIds。首次提交用户消息的标签页默认成为 ownerTabId；刷新后若携带同一 resumeToken 并在租约有效期内恢复，则继续保持拥有者身份；租约过期或拥有者明确释放时，服务端可按策略把拥有权转移给最近活跃且具有写权限的观察者。非拥有者关闭、切换页面或放弃 reader 只清理其 observerState；非拥有者点击停止时，若 permissionLevel 不允许服务端取消，则仅关闭本地观察，否则按同一 requestId 和 idempotencyKey 参与幂等取消。

工具续跑按“父 turn—工具 run—续跑 turn”三层映射处理。工具开始执行时，服务端写入 parentRequestId、turnId、runId、runState 和 cascadeCancel 标志；工具返回结果且需要继续生成时，创建 continuationRequestId，并把 continuationRequestId 与同一 turnId 和 runId 关联。握手前发生本地关闭只删除客户端等待 resolver；握手前发生用户停止则记录 deferUntilRequestId，并在 continuationRequestId 公告后立即取消；握手后用户停止直接按 continuationRequestId 取消。由于握手前仅清理本地等待、握手后才按续跑 requestId 中止，工具调用后的续跑不会因刷新或重连被误中断。

### 可配置策略与边界条件

本方案的默认策略为恢复优先：自动恢复开启；reader cancel、页面卸载、组件卸载和短暂 WebSocket 断开均仅触发 localCleanup；网络断开超过预设时长仍保持服务端 turn 运行，直到缓冲保留期届满或应用策略另行设置；非拥有标签页默认不能发起服务端取消；父 turn 取消默认级联至仍运行的工具 run 和已建立的续跑 requestId。应用可将页面卸载是否发送取消、断连超时后的处理、非拥有者取消权限、工具级联取消和缓冲保留时长作为配置项，但每个配置项均应映射到明确的分类输出和状态迁移。

异常边界按可定位、可恢复和可幂等三个维度处理。WebSocket 取消发送失败时，若仍存在 requestId 或 runId、turn 未终止且策略允许重试，则以同一 idempotencyKey 在后续连接补发；若服务端找不到 active controller 但 streamRegistry 中存在 terminal metadata，则返回既有 completed、failed、cancelled、expired 或 unrecoverable 状态；若 requestId 冲突或被重用，服务端以 conversationId、turnId、createdAt 和 resumeToken 共同校验，不通过校验的请求不得影响现有 turn。

缓冲区过期、溢出、服务端重启或客户端本地状态丢失时，服务端优先依据持久化的 stream metadata 和 terminal 分块判断是否仍可恢复。若 lastReceivedSeq 小于保留窗口起点、超过 lastPersistedSeq、与 checksum 不一致，或服务端重启后只能恢复 terminal metadata 而不能恢复中间分块，则恢复请求返回 expired 或 unrecoverable，并携带 lastPersistedSeq、terminalSeq、doneReason 或 errorCode。客户端据此停止自动重试，向上层暴露状态响应，而不是继续创建新的观察流。

上述状态通过 terminal event 或 status response 向上层输出，字段包括 requestId、turnId、streamState、controllerState、lastAppliedSeq、lastPersistedSeq、terminalSeq、doneReason、errorCode、recoverable 和 retryAfter。由于本地清理不触发 controllerRegistry 的 abort，刷新和 reader cancel 不会误杀 agent turn；由于显式停止按 requestId 或 runId 精确查找 controller，取消只作用于目标 turn 或工具；由于 replay 和 live 分块以 chunkSeq 与 lastAppliedSeq 去重，恢复期间不会重复显示；由于观察者关闭只删除 observerState，多标签页互不影响；由于工具续跑握手前后分别执行本地清理和按续跑 requestId 取消，工具续跑不会被重连误中断且仍可被用户显式停止。
