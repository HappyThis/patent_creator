## 技术方案

本方案在 Think agent 现有会话管理、消息持久化、流式推理、取消和恢复语义的基础上，增加一个异步任务管理层，使 webhook、RPC 调用方或其他 Worker 能够以非阻塞方式向 Think agent 提交对话任务。

核心思路是：在 Think 已有的 saveMessages() 编程接口、TurnQueue 串行执行队列、AbortRegistry 取消管理和 runFiber 持久恢复机制之上，引入任务记录表与异步任务管理器，将“外部提交”与“模型推理”之间的生命周期解耦为可追踪、可恢复的持久状态机。外部调用方提交任务后，系统在一条同步 RPC 内完成任务登记并立即返回确认；推理执行、状态更新和结果持久化均在后台异步完成。普通聊天路径（WebSocket 协议、_handleChatRequest）不受任何影响。

### 任务提交与快速确认机制

系统在 Think 的 Chats 父 Agent（或独立的任务调度 DO）上新增一套 @callable RPC 方法，对外暴露异步任务提交接口。调用方通过 submitTask(externalId, targetSessionId, message) 提交一次对话任务。

快速确认的关键在于：submitTask 在单次 SQLite 事务中完成两步——(1) 以 INSERT OR IGNORE 将任务记录写入 async_tasks 表，若 external_id 已存在则直接返回已有任务状态；(2) 新任务登记为 "received" 状态后立即向调用方返回 {taskId, status: "received"}。上述操作在 DO 的同步 RPC 内完成，调用方在毫秒级获得接收确认，无需等待模型推理。

确认返回后，系统通过 runFiber 异步启动任务处理流程：将任务状态更新为 "enqueued"，调用 Think 已有的 saveMessages() 方法将用户消息注入目标 Session 并获取 requestId，随后由 TurnQueue 串行调度推理执行。这一设计使得“接收确认”与“推理完成”被解耦为两个独立且可分别恢复的持久阶段。

### 重复提交去重

重复提交去重基于 async_tasks 表的 external_id 字段实现。该字段由调用方生成（如 webhook 事件 ID、RPC 请求的幂等键），表上建立 UNIQUE 约束。提交时使用 INSERT OR IGNORE：新 external_id 正常插入并返回新任务；重复 external_id 被静默忽略，查询已有记录后返回当前任务状态和 taskId。

这一设计直接复用了 Think 已有的 Session 层消息去重模式——AgentSessionProvider.appendMessage 对相同 message.id 使用 INSERT OR IGNORE 实现幂等。任务层与消息层两级去重协同工作：任务层阻止重复 saveMessages 调用，消息层进一步保证即使任务被意外多次入队，同一 messageId 也不会在 Session 中产生重复消息。

对于外部系统因超时或网络问题重试同一请求的场景，submitTask 的 RPC 调用本身就是幂等的：首次调用创建任务并返回 received；重试调用命中已有 external_id 返回当前状态，可能是 received、running、completed 或 failed，调用方可据此决定是等待还是重新提交。

### 持久状态边界与状态机

系统定义了一条贯穿“快速确认”到“推理最终完成”的持久状态链，每一步状态迁移都伴随 SQLite 写入，使得 DO 在任意时间点休眠或崩溃后均可恢复。

状态机如下：received（任务记录已持久化，确认已返回）→ enqueued（已调用 saveMessages，消息已写入 Session，等待 TurnQueue 调度）→ running（TurnQueue 分配回合，推理循环正在执行）→ completed / failed / aborted（终态）。

关键边界设计：(1) received 到 enqueued 的迁移在 runFiber 内部完成——先 enqueued 写入，再调用 saveMessages。若 saveMessages 之前崩溃，DO 重启后扫描 status='received' 且 updated_at 超时的任务重新触发处理。(2) enqueued 到 running 的迁移依赖 saveMessages 返回的 requestId 和 TurnQueue 分配——requestId 写入任务记录后即可通过 AbortRegistry 取消。(3) running 期间若 DO 被驱逐，chatRecovery 模式下 runFiber 自动恢复推理，非 chatRecovery 模式下 saveMessages 内部 keepAliveWhile 阻止驱逐直到推理完成或流中断。推理正常结束后，assistant 消息通过 AgentSessionProvider.appendMessage 持久化到 Session，任务记录更新为 completed 并记入结果摘要。(4) 异常路径：推理抛出异常→任务标记 failed 并记录 error；外部调用 cancelTask→AbortRegistry.cancel(requestId)→推理 loop 感知 abort→任务标记 aborted。

所有状态迁移经由 TurnQueue 的 generation 校验：若在排队等待期间 Session 被清空导致 generation 递增，TurnQueue 返回 status: "stale"，任务标记为 aborted 并说明原因。

### 执行领取与崩溃恢复

执行领取机制基于 async_tasks 表的状态字段和 updated_at 时间戳实现，不依赖分布式锁。

正常路径：submitTask 同步返回后，在同一个 runFiber 内原子性地将 received 更新为 enqueued 并写入 updated_at，随后调用 saveMessages。由于 DO 单线程模型，不存在多个 Worker 争抢同一任务的问题——任务处理始终在拥有该 Session 的 DO 实例上执行。

崩溃恢复路径：DO 在 onStart 时扫描 async_tasks 表中 status IN ('received', 'enqueued', 'running') 且 updated_at 早于阈值（如当前时间减去 5 分钟）的记录。对于 received 状态的任务，重新触发 runFiber 处理。对于 enqueued 状态的任务——此时消息已写入 Session（saveMessages 中的 session.appendMessage 是持久操作），但推理可能未开始或未完成——检查对应 requestId 是否仍存在于 AbortRegistry：若不存在，重新调用 saveMessages 注入消息并触发推理。对于 running 状态且 chatRecovery 已启用的任务，runFiber 自动恢复；未启用 chatRecovery 的任务，由于 keepAliveWhile 的作用，running 任务通常不会因 DO 驱逐而丢失，超时的 running 任务标记为 failed 并写入 error。

该设计复用了 Think 现有的 saveMessages 幂等写入（消息层 INSERT OR IGNORE）、TurnQueue 的 generation 失效校验和 runFiber 的持久化 fiber 状态，无需引入额外的分布式协调组件。

### 状态查询

系统通过 Chats DO 上的 @callable 方法 getTaskStatus(taskId) 暴露任务状态查询接口。查询直接读取 async_tasks 表的当前状态、error 和 result_brief 字段，在一次 SQLite 查询内完成，不依赖推理是否在进行中。

返回结构包含：taskId、externalId、status、createdAt、updatedAt、error（如有）和 resultBrief（已完成任务的结果摘要）。对于 running 状态的任务，可额外通过 requestId 查询 AbortRegistry 确认推理是否仍在执行。调用方可通过轮询 getTaskStatus 或由系统在状态变更时回调通知（可选）获知任务完成。

完整推理结果（assistant 消息正文）通过 Session 的消息查询接口获取——任务表仅存储摘要，避免在任务表中冗余存储大段文本。如果是基于 SessionManager 的多 Session 场景，调用方可进一步通过 listTasks(sessionId) 列出指定 Session 下的所有任务。

### 取消机制

cancelTask(taskId) 通过 AbortRegistry 实现推理取消，与 Think 现有 WebSocket 取消路径（cf_agent_chat_request_cancel）使用同一取消原语。

处理流程：查询 async_tasks 表获取 requestId；若状态为 received 或 enqueued（尚未分配 requestId），直接将任务标记为 aborted——TurnQueue 的 generation 递增会使排队中的旧回合在到达队列前端时判定为 stale 并被跳过。若状态为 running 且 requestId 存在，调用 AbortRegistry.cancel(requestId)，推理循环内的 abortSignal 触发，streamText 中断，部分已生成的 assistant 消息按 Think 现有机制持久化（partial persistence），任务标记为 aborted。若状态已为终态（completed、failed、aborted），cancelTask 为幂等操作，直接返回当前状态。

取消的传递同样支持外部 AbortSignal：调用方在 submitTask 时传入 signal，系统通过 AbortRegistry.linkExternal 将外部信号链接到内部 requestId 的 AbortController，使得调用方无需知道内部 requestId 即可取消任务。

### 清理与删除

cleanupTask(taskId) 提供任务记录的清理能力。对于终态任务（completed、failed、aborted），cleanupTask 从 async_tasks 表中删除对应记录。对于非终态任务，先执行 cancelTask 逻辑再删除记录。清理操作不影响 Session 中已持久化的消息——消息仍可通过 Session 的消息查询接口访问。

系统同时支持基于时间的自动清理：可配置任务保留时长，DO 在 onStart 或定期调度中扫描 async_tasks 表中终态任务且 updated_at 超过保留阈值的记录并批量删除。这种自动清理与 Think 现有 maxPersistedMessages 机制解耦——消息历史的长度由 Session 的 compaction 策略管理，不受任务记录清理影响。

删除操作使用软删除标记，在确认无正在进行的推理后执行物理删除，避免硬删除在 DO 休眠恢复时产生竞态。

### 与普通聊天路径的兼容性

本方案的异步任务管理层与 Think 现有普通聊天路径完全解耦，不修改以下已有组件：(1) WebSocket 协议处理（_handleChatRequest 及 cf_agent_chat_* 消息类型）；(2) Session 的消息持久化（AgentSessionProvider 的 appendMessage、updateMessage、getHistory）；(3) 流式推理与广播（_streamResult、ResumableStream）；(4) 自动续接（auto-continuation）和客户端工具（client tools）。

异步任务提交仅通过 saveMessages() 编程接口与 Think 交互，这与 WebSocket 路径共享同一 TurnQueue、同一 AbortRegistry 和同一 Session 存储层，但入口完全独立。普通聊天用户通过浏览器 WebSocket 发送的消息不受任务排队影响——TurnQueue 的串行化保证两者按提交顺序依次执行。如果需要在同一 Session 上同时支持普通聊天和外部任务提交，TurnQueue 天然提供互斥，无需额外协调。

在存储层面，async_tasks 表独立于 Session 的 assistant_messages / assistant_compactions / assistant_fts 表族，不改变 Session 的表结构和索引。任务提交时向 Session 写入的用户消息和推理生成的 assistant 消息与普通聊天消息共享同一消息树（通过 parent_id 链接），查询时无感知差异。

### 处理流程

以下是一次完整异步任务提交与执行的全链路流程：

1. 外部调用方生成 external_id（如 webhook 事件 ID 或业务幂等键），调用 Chats DO 的 submitTask RPC 方法，传入 external_id、目标 session_id 和用户消息内容。
2. submitTask 在 SQLite 事务中执行 INSERT OR IGNORE INTO async_tasks。若 external_id 已存在，直接查询已有记录并返回 {taskId, status, duplicate: true}。
3. 新任务：写入 status='received'、created_at 和 updated_at，提交事务。方法立即向调用方返回 {taskId, status: 'received'}，调用方在毫秒级获得确认。
4. RPC 返回后，系统在 runFiber 中异步启动任务处理：将状态更新为 enqueued，构造 UIMessage 并调用 saveMessages(messages) 将消息注入目标 Session。saveMessages 内部执行 session.appendMessage（INSERT OR IGNORE），消息落盘。
5. saveMessages 返回 requestId。系统将 requestId 写入任务记录，状态更新为 running。TurnQueue 按序分配回合，推理循环（onChatMessage → streamText）开始执行。
6. 推理过程中的流式 chunk 通过 ResumableStream 缓冲以便客户端重连回放；对于异步任务场景，可选地通过回调或 WebSocket 向外部系统推送中间结果。
7. 推理正常结束：StreamAccumulator 组装 assistant 消息，sanitizeMessage 清理提供商元数据，enforceRowSizeLimit 保证行大小不超限，session.appendMessage/updateMessage 持久化。任务记录更新为 completed，result_brief 写入结果摘要。
8. 推理异常：catch 块记录 error，任务标记为 failed。推理被取消：abortSignal 触发，partial persistence 执行，任务标记为 aborted。
9. 调用方通过 getTaskStatus(taskId) 轮询或接收回调获知最终状态，通过 Session 消息查询接口获取完整 assistant 回复。

### 技术效果

本方案在 Think agent 现有架构上增量引入异步任务管理层，取得以下技术效果：

- 快速确认与解耦：外部调用方在同步 RPC 内（毫秒级）获得“已接收”确认，无需阻塞等待模型推理完成。任务提交与推理执行的生命周期完全解耦，各自独立可恢复。
- 可靠的重复提交去重：基于 external_id 唯一约束的 INSERT OR IGNORE 策略，结合 Session 层消息 ID 幂等写入，两级去重保证外部重试不产生重复消息和重复推理。
- 崩溃恢复全覆盖：received→enqueued→running 每一状态迁移都伴随持久写入。DO 重启后通过扫描非终态任务并结合 runFiber 自动恢复，不会丢失已接收但未执行的任务。
- 状态可追踪：从 received 到终态的完整状态链持久化在 async_tasks 表中，外部系统可随时查询当前进度，不再需要维护长连接等待推理完成。
- 取消可传播：cancelTask 通过 AbortRegistry 与 Think 现有取消原语对接，支持内部取消和外部 AbortSignal 链接，取消后部分生成内容按 Think 现有机制持久化保留。
- 普通聊天路径零影响：异步任务提交仅通过 saveMessages() 编程接口与 Think 交互，WebSocket 协议、Session 存储、流式推理和客户端工具均不受修改。TurnQueue 串行化保证异步任务与普通聊天消息在同一 Session 上互斥执行。
- 存储隔离与复用：async_tasks 表独立于 Session 消息表族，任务元数据与消息内容分离存储；任务产生的用户消息和 assistant 消息复用 Session 的树状消息结构，查询和 compaction 无差异。
