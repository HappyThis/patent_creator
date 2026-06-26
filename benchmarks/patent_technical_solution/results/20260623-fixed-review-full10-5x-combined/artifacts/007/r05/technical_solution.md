## 技术方案

本方案面向基于浏览器连接的 agent chat 场景，在浏览器侧接收服务端 agent 的流式响应，并允许对同一 agent turn 在刷新、短暂断网、页面切换、组件卸载或多标签页切换后继续恢复观察。方案的核心不是简单延长连接超时时间，而是把“本地流消费者退出”和“服务端 agent turn 取消”拆分为不同语义：前者仅表示某个浏览器连接或某个 reader 不再消费当前响应，后者才表示用户或应用要求终止服务端正在运行的生成、工具调用或续跑流程。

### 总体构思与系统组成

在一种实现中，系统包括浏览器侧 chat 传输组件、服务端 agent 会话组件、turn 运行队列、取消控制注册表、可恢复流存储组件以及连接观察状态组件。浏览器侧 chat 传输组件为每次用户提交或续跑请求生成请求标识，将服务端返回的 chunk 转换为前端消息流；服务端 agent 会话组件根据请求标识执行对应的 agent turn，并将模型输出、工具调用结果、错误或完成事件写入可恢复流。turn 运行队列用于保证同一会话中的多个 turn 按序执行；取消控制注册表以请求标识为索引管理可取消信号；可恢复流存储组件将已输出 chunk 与流元数据持久化；连接观察状态组件维护当前连接是拥有该流、恢复该流还是仅观察该流。

上述组件以 requestId 作为贯穿前后端的主关联键。浏览器提交消息时携带 requestId 与幂等键；服务端在同一会话内校验其唯一性，创建 turn 记录、取得取消信号、分配 streamId 并启动可恢复流。恢复请求、显式停止请求、工具结果续跑请求和广播数据均通过 requestId、streamId、continuationId 与 connectionId 的组合进行匹配。若 WebSocket 连接或前端 `ReadableStream` 实例被销毁，服务端仍以持久化记录识别该 turn 的持续运行身份；若相同 requestId 再次到达且请求体一致，则返回已有 turn 或恢复入口，而不是重复启动 agent turn。

### 标识、记录与协议模型

| 标识 | 生成方及唯一性 | 绑定关系 | 重复请求处理 |
| --- | --- | --- | --- |
| requestId / turnId | 浏览器提交新消息时生成或由服务端确认；在同一会话内唯一 | 标识一次用户请求及其 agent turn，服务端以其索引取消信号和队列项 | 若已有同会话同 requestId，按幂等重试或恢复处理；若请求体不一致则拒绝新建 |
| streamId | 服务端启动流式响应时生成；在会话内唯一 | 与一个 requestId 绑定，用于索引 chunk 序列和恢复流元数据 | 重复 streamId 不创建新流，按已有流状态返回 active、completed、expired 或 error |
| continuationId | 服务端在工具结果、审批通过或自动续跑阶段生成 | 从属于原 requestId，可绑定新的 streamId 或复用原流的后续阶段 | 重复 continuationId 只确认已接收的工具结果或续跑状态，不重复启动模型生成 |
| connectionId | 服务端在浏览器连接建立时分配 | 标识拥有、恢复或观察该流的连接，不决定 turn 是否存在 | 连接重建产生新 connectionId，旧连接关闭仅清理观察关系 |
| activeRequestId | 服务端运行队列在 turn 开始时设置 | 指向当前正在执行或续跑的 requestId | 完成、错误、取消或过期后释放，避免后续取消误作用到新 turn |

服务端持久化至少三类记录。turn 记录保存 requestId、sessionId、status、stateVersion、activeConnectionId、queuedAt、startedAt、updatedAt、expireAt、cancelOrigin、cancelReason、errorCode 和 errorMessage；每次状态迁移均以 stateVersion 递增写入。流记录保存 streamId、requestId、status、lastPersistedIndex、lastBroadcastIndex、completedAt、expireAt、gapRanges、orphaned 标记和最后错误信息。连接观察记录保存 connectionId、streamId、role、resumeCursor、lastAckedIndex、pendingResumeSince 和 replayDeadline，其中 role 取 owner、resumer 或 observer；该记录只表示连接与流的观察关系，不作为取消服务端 turn 的依据。

协议消息采用控制帧与数据帧分离的模型。提交帧携带 requestId、sessionId、messages、clientTools 和 idempotencyKey；取消帧携带 frameType、requestId、origin、reason、timestamp、clientId 或 sessionId、stateVersion 或 idempotencyKey；恢复请求帧携带 requestId、可选 streamId、客户端最后连续接收的 chunkIndex、最后确认的 replayComplete 标记以及连接标识；恢复确认帧携带 streamId、requestId 和 resumeCursor；数据帧携带 streamId、requestId、chunkIndex、body、replay、continuation、done 或 error 标记；无可恢复流帧携带 requestId、status 和可选 errorCode。通过字段化控制帧，服务端能够对会话归属、重复控制请求和迟到控制请求作出确定处理。

### 统一状态机与竞争处理规则

服务端将 queued、running、detached-but-resumable、pending-resume、replaying、waiting-tool、waiting-approval、continuing、cancel-requested、completed、error 和 expired 纳入统一状态机。queued 表示请求已入队但尚未执行；running 表示正在生成或读取模型流；waiting-tool 和 waiting-approval 表示模型已发出客户端工具调用但尚未取得工具结果或审批；continuing 表示工具结果或审批通过后正在执行续跑；detached-but-resumable 表示无活动消费者但后台任务仍可继续；pending-resume 与 replaying 是连接级恢复子状态，不改变 turn 是否运行；completed、error、expired 和 cancelled 为终态。

状态转换按事件和版本号原子写入：提交新请求进入 queued，队列取得执行权后转 running 并设置 activeRequestId；模型发出客户端工具调用时转 waiting-tool 或 waiting-approval；工具结果或审批通过后转 continuing；连接全部脱离且未收到取消时，running、waiting-tool、waiting-approval 或 continuing 可附加 detached-but-resumable 标记；收到合法取消帧时转 cancel-requested 并触发取消信号，随后写入 cancelled 终态；生成完成写入 completed，异常写入 error，超过资源或时间边界写入 expired。pending-resume 和 replaying 只记录在连接观察记录中，恢复完成后连接转 observer 或 owner。

终态采用 compare-and-set 规则写入：只有当前状态仍为非终态且 stateVersion 与读取时一致时，completed、error、cancelled 或 expired 才能落库；终态一旦写入，后续 chunk、done、error、取消帧、工具结果或续跑请求均不得覆盖该终态。若 done 已持久化并转 completed，迟到取消仅返回已完成或无须取消；若 cancel-requested 已持久化，后续模型 chunk 和工具结果不再广播，必要时仅写入审计记录；若 error 与 cancel 同时发生，以先成功写入终态的事件为准，另一事件作为附加原因记录。该规则避免多标签页或异步回调互相覆盖最终结果。

### 取消语义分层与控制帧

浏览器侧将本地清理事件划分为至少两类。第一类为消费端脱离事件，包括浏览器刷新、路由切换、组件卸载、WebSocket 临时关闭、前端 reader cancel 以及因重新渲染导致的本地流关闭；该类事件只释放本地监听器、reader、回调和连接级状态，不自动发送服务端取消控制帧。第二类为明确取消事件，包括用户点击停止按钮、应用业务逻辑调用主动取消接口、超出业务策略后确认放弃该 turn 等；该类事件才生成可被服务端识别的取消意图。

对明确取消事件，浏览器侧通过独立于数据 chunk 的取消控制通道发送取消帧。取消帧包括 frameType、requestId 或 turnId、origin、reason、timestamp、clientId 或 sessionId，以及 stateVersion 或 idempotencyKey。服务端先校验连接身份、会话归属和 origin 白名单，确认该 requestId 属于当前会话且处于 queued、running、detached-but-resumable、waiting-tool、waiting-approval 或 continuing 等可取消状态后，才触发对应 AbortController；单纯连接关闭、reader cancel 或观察者退出不生成该帧，因此不会误触发服务端取消。

取消帧按 requestId 与 idempotencyKey 幂等处理。重复取消同一非终态 turn 时，服务端只返回同一取消受理结果，不重复触发多个取消信号；取消已 completed、error、expired 或 cancelled 的 turn 时，服务端返回当前终态并不覆盖既有结果；取消不存在、跨会话或已过期清理的 requestId 时，服务端返回 notFound、expired 或 forbidden 类状态。若取消帧与恢复请求同时到达，服务端先按状态版本号写入 cancel-requested，再让恢复请求读取该状态并重放已持久化内容及取消终态，从而确保显式停止优先于新的观察附着。

浏览器侧传输组件还可以在本地流关闭路径中执行“只分离不取消”的处理：停止向已关闭的 stream controller 入队、移除该连接上的响应监听器、将该连接的 active request 关系标记为 detached，并保留 requestId 与恢复策略信息。若之后同一页面实例或新的页面实例重新建立连接，则通过恢复请求重新声明希望接收该 requestId 关联的流，而不是重新提交用户消息或重新启动 agent turn。

### 可恢复流与重连恢复机制

服务端开始流式响应时，为 requestId 创建 streamId 和流记录，并按“先持久化后广播”的顺序处理每个数据帧。服务端先在同一 streamId 下分配单调递增且唯一的 chunkIndex，将 body、frameKind、createdAt 和校验信息写入 chunk 记录，再更新流记录的 lastPersistedIndex；持久化成功后才向已就绪连接广播并更新 lastBroadcastIndex。done、error 和缺失标记也作为有序帧进入同一序列，以保证恢复端可按一个连续序列重放。先持久化后广播使断线发生在广播之后时仍能按游标补发，从而减少丢失。

若持久化失败，服务端不广播该 chunk，并根据失败类型重试、写入 error 终态或触发策略性取消，避免客户端收到无法恢复的片段；若广播失败但持久化成功，服务端仅清理失败 connectionId 的观察关系，turn 继续运行，后续该连接可按 lastPersistedIndex 发起恢复；若出现重复 chunk 或重试写入，服务端以 streamId 与 chunkIndex 的唯一约束去重，已存在且内容一致时视为成功，内容不一致时写入 error 并停止该流。若 chunkIndex 分配失败，服务端不得降级为无序广播，而应暂停读取或写入 error，以保持重放顺序可验证。

恢复请求由客户端携带 requestId、可选 streamId、最后连续接收的 chunkIndex 和 replayComplete 确认状态发起。服务端先校验会话归属和流记录是否存在，再确定重放起点：客户端游标缺失时从最早可用 chunk 开始；游标小于 lastPersistedIndex 时从游标后一项开始；游标等于 lastPersistedIndex 时只发送 replayComplete 并切换实时接收；游标大于 lastPersistedIndex 时返回 cursor-ahead 状态并要求客户端回退到服务端最新序号；游标落在已清理区间或 gapRanges 内时返回 expired 或 gap 标记，提示前端不能保证完整恢复。

通过校验后，服务端将该 connectionId 写入 pending-resume 集合并发送恢复通知，等待恢复确认帧。确认到达后，连接进入 replaying，服务端按 chunkIndex 连续发送缺失 chunk；重放期间新产生的实时 chunk 仍先持久化并广播给其他已就绪连接，但排除处于 pending-resume 或 replaying 的连接。重放完成时，服务端比较 lastReplayedIndex 与当前 lastPersistedIndex；若相等，则发送 replayComplete 并将连接切换为 observer 或 owner；若不相等，则继续重放新增区间，直到 replayComplete 严格位于所有已持久化历史片段之后。该排除集合和排序规则避免 replay 与 live 交叉、重复或乱序。

恢复异常按确定分支处理：无流记录返回 notFound；流已过 expireAt 返回 expired，并可附带最后可用终态；仅存在持久化记录而无运行中的 reader 或工具上下文时，将 orphaned 标记写入流记录，重放已保存 chunk 后发送 error 或 completed 类终态，并明确不再实时生成；恢复确认超时或重放中再次断开时，服务端清理该 connectionId 的 pending-resume 或 replaying 记录，并保留 resumeCursor 以便下次继续；重复恢复请求到达时，若来自同一 connectionId 则刷新游标和期限，若来自不同连接则各自独立重放并分别排除实时广播，互不抢占。

### 服务端 turn 状态机与运行控制

服务端以 agent turn 而非连接作为运行控制对象，并采用统一状态集合描述 turn、工具续跑和恢复观察。queued 表示请求已排队；running 表示正在读取模型输出；waiting-tool 与 waiting-approval 表示等待客户端工具结果或审批；continuing 表示工具结果或审批后继续生成；detached-but-resumable 表示没有活动消费者但后台任务仍继续并持久化；cancel-requested 表示已接收合法取消；completed、error、cancelled 和 expired 为终态。pending-resume 与 replaying 是连接观察状态，不改变 turn 终态。

状态转换由服务端统一判定：收到新的用户消息并进入执行队列时创建 running 状态；连接关闭或本地 reader cancel 时，如果未收到明确取消控制帧，则仅移除该连接的观察关系，并在仍有未完成输出时进入或保持 detached-but-resumable 状态；收到取消控制帧或受信外部取消信号时，将状态切换为 cancel-requested 并触发取消注册表中的 AbortController；模型流、工具续跑和响应发送逻辑在检测到该取消信号后取消底层 reader 或终止后续读取，并将已产生片段保留在可恢复流中。

turn 运行队列按会话串行执行 queued 项。服务端在入队时记录 requestId、idempotencyKey、queuedAt 和 queueState；队列取得执行权时设置 activeRequestId，完成、错误、取消、跳过或过期后释放该标识并继续下一个 queued 项。针对尚未开始的 requestId 收到合法取消时，队列项转 cancelled 或 skipped，并向相关连接发送带 requestId 的取消结果帧，后续 turn 不受影响。同一会话中恢复旧 turn 与提交新 turn 并发时，恢复请求只读取既有 stream 记录，不进入生成队列；新提交 turn 仍按队列顺序等待，从而避免恢复动作触发重复生成。

服务端重启后，内存中的 AbortController、模型 reader、工具执行上下文和 activeRequestId 可能已经丢失。启动恢复时，服务端扫描状态为 running、detached-but-resumable、waiting-tool、waiting-approval 或 continuing 且未达终态的流记录：若存在可继续的外部执行上下文，则重新建立取消注册表并保持可恢复；若只剩持久化 chunk 而无可继续执行进程，则将流标记为 orphaned，并按已保存内容生成 completed、error 或 expired 终态。客户端恢复该类流时只接收已保存片段和终态说明，不再收到 live chunk，避免误以为后台仍在实时生成。

### 工具调用、续跑与多标签页协同

对于客户端工具调用、工具审批或工具结果回传，服务端为原 requestId 建立 continuationId，并记录 continuationId、parentRequestId、toolCallId、toolResultId、approvalState、status 和关联 streamId。若采用同一 requestId 模式，continuationId 作为原 turn 的阶段号，用于在同一流中追加有序帧；若采用绑定续跑标识模式，continuationId 生成新的 streamId，但仍从属于 parentRequestId。工具结果回传时，服务端以 toolCallId 与 toolResultId 去重；重复提交且内容一致时返回已接收，内容不一致时拒绝并写入错误原因，避免重复启动模型续跑。

工具 continuation 的异常按状态决定输出。审批超时或用户拒绝时，waiting-approval 转 cancelled 或 error，并写入有序终态帧；工具执行失败时，waiting-tool 或 continuing 转 error，错误帧占用 chunkIndex 以便恢复端重放；不可中断工具在 turn 已取消后返回结果时，服务端不得继续模型续跑，也不得把成功结果作为正常 chunk 广播，只能记录审计信息或发送已取消终态；若外部工具已经产生不可回滚副作用，则将副作用结果与取消状态分开记录，避免把副作用完成误解释为 agent turn 成功完成。

在多标签页场景中，每个连接的观察状态在 owner、resumer、observer 和 detached 之间迁移。owner 表示该连接发起或当前拥有直接消费流；resumer 表示该连接正在等待确认或重放；observer 表示该连接接收实时广播但不拥有取消语义；detached 表示连接已退出。三种在线角色互斥，但多个连接可同时为 observer 或 resumer；owner 退出时服务端不必重新选主，turn 继续以 requestId 为中心运行，其他连接可按游标恢复或观察。连接状态变化只影响广播目标，不改变 turn 是否取消。

浏览器侧按 streamId 与 chunkIndex 合并消息。对 replay 帧，前端从本地已保存的最大连续 chunkIndex 之后开始插入；对 live 帧，若 chunkIndex 已存在则丢弃，若等于当前最大连续序号加一则追加，若出现跳号则标记本地落后并发起恢复请求。replayComplete 到达后，前端只在已收到服务端声明的重放区间末尾时切换到实时接收；done、error、cancelled 或 expired 终态按 stateVersion 覆盖本地临时 UI 状态，但不得被其他标签页的旧状态回写。该合并算法使多个标签页各自独立重放，同时以服务端终态为准。

### 可配置策略与边界条件

本方案允许应用配置取消与恢复策略，但核心路径按确定流程执行。默认策略为：本地 reader cancel、组件卸载和短暂 WebSocket 断开只使连接转 detached，不发送取消帧；只有用户显式停止、应用主动取消或受信外部取消信号才进入 cancel-requested。外部取消信号须来自同一会话、授权管理端或预先登记的父级任务控制器，并通过 origin 与 reason 策略表校验；不受信来源被拒绝或忽略，不影响服务端 turn。对于需要强资源约束的部署，可以配置断开超过阈值后触发策略性取消，但该策略产生的 origin 与用户显式停止区分记录。

资源边界按状态化流程处置。超过最大保留时长且 turn 无可继续执行上下文时，服务端将流转为 expired，并保留已生成 chunk 供只读恢复；超过最大 chunk 数或最大字节数时，服务端根据策略选择写入 gap 标记后停止持久化、保留尾部窗口并标记不可完整恢复，或触发策略性取消并写入 cancelled 终态；每个会话活动流数量超过上限时，优先拒绝新 turn 或使最旧 detached 流 expired。单个 chunk 超过可持久化上限时，不应静默只广播而不记录；服务端写入 gapRanges 与缺失标记，客户端恢复时据此显示恢复不完整或转为重新请求。

该方案与既有流式响应协议保持兼容：提交、取消、恢复请求、恢复确认、数据 chunk、replayComplete、done、error、gap 和无可恢复流均作为可识别消息传递。关闭恢复策略时，服务端仍保持取消帧与本地清理事件的语义区分，但可以在连接关闭后立即将本地流结束；开启恢复策略时，连接关闭只影响 connectionId 观察记录。通过这种兼容设计，已有流式响应可以逐步引入 requestId 幂等、游标重放和终态不可覆盖规则，而不要求一次性替换全部业务逻辑。

通过上述配置和边界控制，方案能够在不改变 agent 业务逻辑主体的情况下，使长时间运行的对话、模型生成和工具执行从浏览器连接生命周期中解耦出来；同时又保留用户显式停止时的强取消能力。由此，刷新页面、组件卸载或短暂网络抖动主要表现为观察端重新附着和流片段重放，而不会误触发服务端中止；明确取消则通过 requestId 精确作用于目标 turn，减少误取消、重复提交、重复生成以及多标签页之间相互干扰的风险。
