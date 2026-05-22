## 技术方案

本方案提出一种面向 Think agent 的外部任务提交与生命周期管理机制。该机制在 Think 现有的会话（Session）、消息持久化、流式执行、恢复和取消语义之上，增加任务（Task）抽象层，使 webhook、RPC 调用方或其他 Worker 能以可靠、可追踪、可控制的方式向 Think agent 提交单次对话任务，同时不改变普通聊天交互的消息保存路径。

### 要解决的技术问题

Think agent 当前的 chat 交互路径包括两条：WebSocket 路径（浏览器客户端通过 cf_agent_chat_* 协议发送消息）和 RPC 路径（父 agent 通过 chat() 方法驱动子 agent 推理）。这两条路径的共同特征是调用方需要在整个模型推理周期内保持连接或等待：WebSocket 路径需要维持长连接以接收流式响应，RPC 路径则需要调用方等待 chat() 方法返回。当外部系统（如 webhook 接收端点、定时任务 Worker、第三方 RPC 服务）希望向 Think agent 提交对话任务时，这种同步等待模式面临以下问题：

第一，同步等待成本高。模型推理可能耗时数十秒至数分钟，外部调用方在此期间需维持连接或阻塞等待，增加资源占用和超时风险。

- 外部调用方提交任务后需同步等待模型推理完成，期间连接断开将丢失结果；
- 外部系统因超时或网络波动重试同一请求时，现有 INSERT OR IGNORE 仅能防止消息重复插入，但无法防止重复触发模型推理；
- 外部调用方提交后缺乏查询任务执行状态的机制，无法获知任务是排队中、执行中、已完成还是已失败；
- 外部调用方无法主动取消已提交但尚未完成的任务；
- 在「快速确认接收」与「模型推理最终完成」之间的持久状态边界不清晰，DO 休眠/崩溃后可能丢失任务记录或导致重复执行。

### 核心技术方案

本方案在 Think 现有架构之上引入任务（Task）抽象层。核心思路是将外部提交的一次对话任务封装为持久化的任务记录，在「接收确认」和「推理完成」之间建立明确的状态边界。整体架构如下：

外部调用方通过 Task API 提交一次对话任务时，系统在任务持久化完成后立即返回「已接收」确认（包含任务标识 taskId），模型推理随后异步执行。任务记录在 SQLite 中维护完整的状态机（pending → running → completed/failed/cancelled），由 Think 所在的 Durable Object 实例统一管理。调用方可通过 taskId 查询状态、取消任务或清理任务记录。

### 任务提交与快速确认流程

任务提交入口为一组新增的 RPC 方法，挂载在 Think 基类上。提交流程如下：

1. 外部调用方通过 SubmitTask 方法提交任务，参数至少包含：用户消息内容（UIMessage）、可选系统提示覆盖、可选工具集、以及用于去重的幂等键（idempotencyKey）。
2. 系统首先以幂等键查询任务表（think_tasks），若已存在相同幂等键的任务记录，则直接返回已有任务的 taskId 和当前状态，不插入消息也不触发新推理。
3. 若幂等键不存在，系统在同一个 SQLite 事务中完成：(a) 在 think_tasks 表中插入任务记录，状态置为 pending；(b) 通过 Session.appendMessage 将用户消息写入 assistant_messages 表（INSERT OR IGNORE 保证消息级幂等）。
4. 提交方法在事务提交后立即返回 { taskId, status: 'pending' }，不等待模型推理。
5. 任务提交后，通过 schedule(0) 或 TurnQueue 机制触发异步推理执行，将任务状态更新为 running，然后进入 onChatMessage → streamText → 持久化 assistant 消息的标准推理流水线。
6. 推理完成后，任务状态更新为 completed 并记录完成时间；若推理失败，状态更新为 failed 并记录错误信息。

### 任务状态持久化与生命周期

任务记录存储在 Think Durable Object 实例的 SQLite 中，新增 think_tasks 表：

think_tasks 表包含以下关键字段：task_id（主键，任务唯一标识）、idempotency_key（唯一约束，外部幂等键）、status（枚举值：pending/running/completed/failed/cancelled）、user_message_id（关联 assistant_messages 表中用户消息的外键）、assistant_message_id（关联 assistant_messages 表中助手回复消息的外键）、error_message（失败时的错误描述）、created_at、updated_at、completed_at。

任务状态机包含以下转换路径：(a) pending → running：任务被调度执行；(b) running → completed：推理正常完成且 assistant 消息已持久化；(c) running → failed：推理过程中出现不可恢复的错误；(d) pending/running → cancelled：外部调用方主动取消任务。状态转换通过 TurnQueue 的串行化语义保证原子性——同一 DO 实例内不存在并发状态冲突。

### 重复提交去重机制

去重机制在两个层面实现：消息层面和任务层面。

消息层面沿用 Session.appendMessage 的 INSERT OR IGNORE 语义——同一 messageId 的消息不会被重复写入 assistant_messages 表。这保证了即使外部调用方在重试时生成了相同的消息 ID，用户消息也只会出现一次。

任务层面通过 idempotency_key 唯一约束实现去重。当 SubmitTask 收到重复的幂等键时：(a) 若已有任务处于 pending 或 running，直接返回已有 taskId，不触发新推理；(b) 若已有任务处于 completed，返回已完成任务的 taskId 和结果摘要；(c) 若已有任务处于 failed，根据配置可选择返回原失败任务、或清理旧任务并创建新任务。幂等键唯一约束在 SQLite 中通过 UNIQUE 索引强制执行，与事务原子性一起保证「恰好一次」提交语义。

### 消息写入前后崩溃恢复

本方案定义了「快速确认接收」与「模型推理最终完成」之间的持久状态边界，利用 Think 已有的 Fiber 机制和新增的任务记录协同保证崩溃恢复的正确性。

关键恢复场景包括以下四种：(1) 任务记录已写入但用户消息未写入——DO 崩溃。恢复时 think_tasks 中存在 pending 状态记录但没有关联的 user_message_id，系统将该任务标记为 failed 并记录「消息写入失败」。外部调用方重试时使用相同幂等键，系统识别 failed 状态并创建新任务。(2) 任务记录和用户消息均已写入但推理未开始——DO 崩溃。恢复时检测到 pending 任务，通过 schedule(0) 重新触发执行。(3) 推理执行中 DO 崩溃——利用 Think 已有的 chatRecovery 和 runFiber 机制。任务状态在 Fiber 启动时更新为 running，Fiber 恢复时通过 onChatRecovery 钩子重建部分响应、通过 continueLastTurn 继续推理。(4) 推理完成但任务状态未更新——消息已持久化到 assistant_messages，通过对比消息时间戳和任务记录修复不一致状态。

### 执行领取与 Fiber 恢复

任务执行恢复利用 Think 已有的 Fiber 基础设施。

Think 的 chatRecovery 属性（默认为 true）将每个推理回话包裹在 runFiber() 中执行，Fiber 状态持久化在 cf_agents_runs 表中。当 DO 因休眠或崩溃被驱逐后重新激活时，_handleInternalFiberRecovery 覆盖方法检测中断的 CHAT_FIBER_NAME 前缀 Fiber，调用 onChatRecovery 钩子重建 ChatRecoveryContext（包含从 Session.getHistory() 获取的当前消息、从 ResumableStream 存储的流式数据块重建的部分响应文本、以及通过 stash() 保存的自定义恢复数据）。默认 onChatRecovery 实现持久化已生成的部分 assistant 消息，然后通过 schedule(0) 触发 _chatRecoveryContinue 调度器：先调用 waitUntilStable(10s) 等待客户端工具交互静止，再通过 continueLastTurn() 从断点继续推理。整个流程中，任务状态始终维持在 running，直至推理最终完成或失败。

### 状态查询

外部调用方通过 GetTaskStatus(taskId) 方法查询任务状态。该方法从 think_tasks 表读取当前状态并返回包含以下信息的结构：taskId、status、createdAt、updatedAt、completedAt（已完成时）、errorMessage（失败时）。调用方可基于状态决定后续行为——若为 pending/running，轮询等待；若为 completed，可通过 GetTaskResult(taskId) 获取 assistant 消息内容；若为 failed，可检查错误信息后重试。

### 取消与清理

外部调用方通过 CancelTask(taskId) 方法取消任务。取消逻辑如下：(1) 若任务状态为 pending——直接将状态更新为 cancelled，任务永远不会被调度执行。(2) 若任务状态为 running——通过 AbortRegistry 触发对应 requestId 的 abort signal，推理循环中的 streamText 检测到 abort 后停止调用模型，已生成的部分 assistant 消息按现有逻辑持久化，任务状态更新为 cancelled。(3) 若任务状态为 completed 或 failed——取消操作无效果，返回当前状态。

DeleteTask(taskId) 方法用于清理任务记录。清理时可选是否同时清除关联的消息——若只删除任务记录而保留消息，对话历史仍可通过 Session 正常访问；若同时删除消息，则通过 Session.deleteMessages 移除关联消息。任务记录采用软删除（设置 deleted_at 时间戳），避免在推理进行中硬删除导致状态丢失。

### 与现有消息保存路径的兼容

本方案的核心设计原则之一是：任务机制是 Think 现有消息保存路径的外层封装，不修改内部消息持久化逻辑。

具体兼容策略如下：(1) 普通聊天路径（WebSocket cf_agent_chat_* 协议和 RPC chat() 方法）完全不经过任务表，消息直接通过 Session.appendMessage 写入 assistant_messages 表，行为与引入任务机制前完全一致。(2) 外部任务提交路径通过 SubmitTask 进入，内部复用了 saveMessages 的消息持久化和推理执行管线——用户消息通过 Session.appendMessage 写入、推理通过 TurnQueue.enqueue 序列化执行、结果通过 _streamResult 流式输出并持久化。区别仅在于外部路径在消息持久化前增加了任务记录创建和幂等检查，并在推理完成后更新任务状态。(3) Session 的树形消息存储（assistant_messages 表）不知道任务的存在——无论消息来自普通聊天还是外部任务，在消息层面统一管理，享受相同的上下文组装、压缩、分支和全文搜索能力。

### 技术效果

本方案在 Think 现有架构基础上引入任务抽象层，带来以下技术效果：

1. 快速确认与异步解耦。外部调用方提交任务后在毫秒级获得确认，无需等待完整模型推理周期。提交和推理在时间上解耦，外部系统可断开连接、释放资源。
2. 可靠去重。通过幂等键的唯一约束和消息级 INSERT OR IGNORE 双重保障，即使外部系统因超时重试多次提交，也不会产生重复用户消息或重复模型推理。
3. 崩溃恢复边界清晰。任务记录的持久化时刻构成「已接收」的持久边界——任务记录一旦写入即不会丢失；借助 Fiber 机制，推理中断后可从断点恢复，任务状态最终收敛到 completed 或 failed。
4. 可观测与可控。外部调用方可查询任务状态、获取执行结果，必要时取消或清理任务，形成完整的任务生命周期闭环。
5. 零侵入兼容。普通聊天交互的消息保存路径完全不受影响，任务机制作为外层封装复用现有 Session、TurnQueue、Fiber 和 ResumableStream 等基础设施，不需要重写任何现有代码路径。

### 与项目环境的对应关系

本方案基于 @cloudflare/think 包（v0.1.2，packages/think/src/think.ts）的现有架构设计，与以下已有基础设施对应：

- Session（packages/agents/src/experimental/memory/session）：提供树形消息存储（assistant_messages 表，含 parent_id）、idempotent appendMessage、全文搜索和压缩；任务的消息关联通过 message_id 外键实现。
- TurnQueue（agents/chat 共享层）：序列化推理执行，generation 计数器防止过期任务执行；任务调度复用 TurnQueue.enqueue 的串行化语义。
- AbortRegistry（agents/chat 共享层）：管理每个 requestId 的 AbortController；任务取消通过 AbortRegistry.getSignal 触发 abort。
- runFiber / onChatRecovery（Agent 基类 + Think 覆盖）：Fiber 状态持久化在 cf_agents_runs 表；任务执行的崩溃恢复复用 CHAT_FIBER_NAME Fiber 和 _chatRecoveryContinue 调度器。
- ResumableStream（agents/chat 共享层）：流式数据块缓冲在 cf_ai_chat_stream_chunks 表；任务推理中断后通过 ResumableStream.replayChunks 重建部分响应。
- saveMessages / continueLastTurn（Think）：任务执行的内部实际调用 saveMessages 写入用户消息并触发推理、continueLastTurn 用于 Fiber 恢复后的续写。
- think_config 表：Think 私有配置存储（_think_config、lastClientTools、lastBody）；新增 think_tasks 表遵循相同的 SQLite 表管理模式。

### 风险与待确认问题

以下问题需要在详细设计阶段进一步确认：

1. 任务表与消息表的清理策略：当 conversation 被 clear 时，是否同步清理 think_tasks 表中已完成/已取消的任务记录，还是仅依赖软删除 + TTL 机制。
2. RPC 方法暴露面：SubmitTask、GetTaskStatus、CancelTask、DeleteTask 是否全部作为 @callable 方法对外暴露，还是仅暴露 SubmitTask 和 GetTaskStatus，将取消和清理限制为内部管理接口。
3. 幂等键命名空间：idempotency_key 是否需要与调用方身份关联（如 per-caller namespace），以防止不同调用方的幂等键冲突。
4. 轮询效率：GetTaskStatus 可能被高频轮询，需评估对 DO 单线程执行的影响——是否需要引入 cache 或通过 WebSocket 推送状态变更事件。
5. 任务超时：处于 pending 或 running 状态超长时间的任务是否需要自动超时机制，超时后的处理策略（标记为 failed、自动取消还是仅告警）。
6. 多任务并发：同一 Think DO 实例中多个 pending 任务的执行顺序和优先级——当前 TurnQueue 的 FIFO 语义是否满足需求。
