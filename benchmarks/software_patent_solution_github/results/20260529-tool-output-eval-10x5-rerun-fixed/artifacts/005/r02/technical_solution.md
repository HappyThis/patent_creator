## 技术方案

### 1. 方案要解决的问题

Think agent 现有的对话入口——WebSocket 协议路径（_handleChatRequest）、RPC chat() 方法和 saveMessages() 方法——均为同步等待模型：外部调用方发起请求后必须保持连接或等待 Promise，直到 LLM 推理、流式输出和消息持久化全部完成后才能得到最终结果。当 webhook、定时任务、外部 RPC Worker 或消息队列消费者需要向 Think agent 提交一次对话任务时，调用方不希望阻塞等待模型推理完成，也未必持有长期 WebSocket 连接。此外，外部系统在超时或网络故障后可能重试同一请求，系统需要保证不重复插入用户消息、不重复执行同一项工作。

### 2. 总体方案概述

本方案在 Think agent 现有架构基础上增加一条“外部任务提交与异步执行”路径，核心设计包括：引入独立的任务持久化表（think_external_tasks）作为“接收确认”与“推理完成”之间的持久状态边界；为每次外部提交分配任务标识并支持幂等键去重；任务状态从“已接受”经“运行中”到“已完成/已失败/已取消”实现完整生命周期管理；执行路径复用现有 TurnQueue 串行调度、AbortRegistry 取消信号、Session 消息持久化及 chatRecovery / runFiber 持久执行机制；同时提供状态查询、取消和清理接口供外部调用方使用。现有 WebSocket 聊天路径、chat() RPC 路径和 saveMessages() 路径不做任何修改。

### 3. 任务持久化表与状态边界

系统新增一张 SQLite 持久化表 think_external_tasks，作为外部任务的生命周期记录。该表位于 Think agent 所属 Durable Object 的 SQLite 存储中，与 Session 使用的 assistant_messages 表共享同一数据库实例。

表结构包含以下关键字段：task_id（任务唯一标识，主键）、idempotency_key（调用方提供的幂等键，唯一约束）、session_id（所属会话标识）、user_message（用户消息的 JSON 序列化内容）、status（任务状态，取值 accepted / running / completed / failed / cancelled）、request_id（关联的内部 Think 请求 ID，用于取消操作）、result_summary（完成后的结果摘要）、error_message（失败时的错误信息）、created_at 和 updated_at（时间戳）。

该表的设计满足以下约束：idempotency_key 的唯一索引保证同一外部请求无论重试多少次，系统中只存在一条任务记录；status 字段实现“快速确认接收”与“模型推理最终完成”之间的持久状态边界——外部调用方在提交后立即得到 accepted 状态和 task_id，而实际推理异步进行；request_id 字段记录 AbortRegistry 中对应的内部请求标识，使得外部取消操作可精确映射到正在执行的推理任务。

### 4. 任务提交流程与幂等去重

外部调用方通过新增的 submitTask() 方法提交任务。该方法是面向 webhook / RPC / Worker 场景的异步入口，签名设计为：接收用户消息、可选的幂等键和会话标识，返回包含 task_id 和 status 的承诺对象。

submitTask() 的执行流程如下：(1) 若调用方提供了 idempotency_key，先以该键查询 think_external_tasks 表，若命中则直接返回已有记录的 task_id 和当前 status，不做任何重复写入。这解决了外部重试导致消息重复插入的问题。(2) 若是新请求，生成 task_id（允许调用方自行指定或系统生成 UUID），向 think_external_tasks 表插入一条 status = 'accepted' 的记录，同时将用户消息通过 Session.appendMessage() 写入 assistant_messages 表——该方法内部先 SELECT 检查消息 ID 是否已存在，已存在则跳过，天然支持消息级幂等。(3) 插入成功后立即向调用方返回 { task_id, status: 'accepted' }，此时模型推理尚未开始，外部调用方已获得“已接收”的快速确认。(4) 随后，通过 TurnQueue.enqueue() 将推理任务排入串行队列，异步执行，不阻塞 submitTask() 的返回。

消息写入与任务记录插入在同一个 SQLite 事务边界内完成，保证崩溃恢复时状态一致：若在消息写入后、任务记录插入前崩溃，外部重试时幂等键未命中，会重新创建任务记录；若在任务记录插入后、返回确认前崩溃，外部重试时幂等键命中，直接返回已有 task_id 和 accepted 状态，任务已在队列中等待执行。

### 5. 任务执行、持久恢复与取消

任务从 TurnQueue 出队后进入执行阶段。系统先将 think_external_tasks 中对应记录的 status 更新为 'running'，更新 updated_at 时间戳，并记录内部 request_id（从 AbortRegistry.getSignal() 获取）。之后调用 Think 现有的 _runInferenceLoop() 和 _streamResult() 完成 LLM 推理与流式输出，整个执行路径与 WebSocket 聊天路径共享同一套推理管线。

执行完成时，系统将状态更新为 'completed'，写入 result_summary（如模型响应的文本摘要或结构化输出）。执行过程中若发生异常，状态更新为 'failed'，写入 error_message。若外部调用方通过 cancelTask() 请求取消，系统通过 request_id 调用 AbortRegistry.cancel() 触发推理中止，状态更新为 'cancelled'。所有状态更新操作均直接写入 SQLite，保证在 Durable Object 休眠或意外退出后状态不丢失。

持久执行与崩溃恢复：Think agent 已支持 chatRecovery 模式——将每个对话轮次包装在 Agent 基类的 runFiber() 中执行，runFiber 在 SQLite 的 cf_agents_runs 表中持久化任务记录，并在 DO 被驱逐后重新激活时调用 onFiberRecovered() 恢复执行。外部提交的任务在 TurnQueue 出队后同样经过 chatRecovery 路径，因此底层 DO 的驱逐（因空闲超时、代码更新或资源限制）不会导致任务丢失。恢复时系统从 think_external_tasks 表中读取当前状态——若状态为 'running' 但对应的 fiber 已不存在，则判定为异常中断，可重新入队执行或标记为 'failed'。若状态为 'accepted' 且尚未出队，TurnQueue 的 generation 检查机制确保清理会话后的过期任务被自动跳过。

StreamAccumulator 在推理过程中逐步构建助手消息，ResumableStream 将流式数据块缓冲到 SQLite，支持在 DO 休眠后重连时回放。这两项机制对外部任务执行同样生效——即使外部调用方不订阅流式输出，系统也能在推理完成后将完整的助手消息持久化到 Session 中。

### 6. 状态查询、取消与清理接口

系统对外暴露三个管理面接口：

(1) getTask(taskId)：查询单任务状态。从 think_external_tasks 表中按 task_id 读取记录，返回 { task_id, status, result_summary, error_message, created_at, updated_at }等字段。调用方通过轮询或回调触发该接口即可追踪任务执行进度。

(2) cancelTask(taskId)：取消任务。先查询任务记录获取 request_id，若状态为 'accepted' 或 'running'，则调用 AbortRegistry.cancel(request_id) 触发推理中止，并将状态更新为 'cancelled'。若任务已处于终态（completed / failed / cancelled），返回当前状态不变。

(3) deleteTask(taskId)：清理任务。若任务仍在运行中则先执行取消逻辑，然后可选地清理 Session 中关联的消息（通过 Session.deleteMessages() 按消息 ID 删除）并删除 think_external_tasks 中的记录。该接口用于外部系统在任务完成后释放存储空间。

上述接口均支持通过 Durable Object RPC 调用，也可通过 HTTP 端点暴露给 webhook 回调方。所有查询和状态变更操作直接读写 SQLite，不依赖内存缓存，天然兼容 Durable Object 的休眠/激活生命周期。

### 7. 与现有架构的兼容性

本方案采取“新增路径、不改旧路”的设计原则，确保与现有 Think 架构的完全兼容：

(1) 消息持久化路径不变：外部任务提交后，用户消息仍然通过 Session.appendMessage() 写入 assistant_messages 表，与 WebSocket 聊天和 chat() RPC 路径共享同一会话消息存储。该方法的先查后插（SELECT then INSERT）策略对三条路径一视同仁，保证消息级去重对普通聊天同样生效但无副作用。

(2) 推理执行管线不变：外部任务的推理执行复用 _runInferenceLoop() → _streamResult() 管线，与 WebSocket 路径共享 StreamAccumulator、ResumableStream、AbortRegistry 和 TurnQueue。唯一的区别是外部任务路径不需要向 WebSocket 连接广播流式数据块——执行结果仅最终持久化到 Session 和 think_external_tasks 表中。

(3) TurnQueue 串行调度不变：外部任务与普通聊天请求在同一 TurnQueue 中排队，按提交顺序串行执行。现有的 generation 机制在清空会话时统一失效所有排队任务（包括外部任务），保证语义一致。

(4) chatRecovery / runFiber 覆盖不变：chatRecovery 标志控制是否将 TurnQueue 出队后的执行体包装在 runFiber 中，外部任务与普通聊天请求均遵循同一标志。用户无需为外部任务单独配置持久执行策略。

(5) Session 多会话支持不变：think_external_tasks 表通过 session_id 字段关联到 Session，每条任务明确归属一个会话，与 SessionManager 的多会话管理机制无缝对接。

### 8. 技术效果

本方案在 Think agent 现有架构之上以最小侵入方式实现外部任务提交、追踪和控制能力，带来以下技术效果：

首先，通过独立的 think_external_tasks 持久化表和 accepted → running → completed/failed/cancelled 状态机，将“接收确认”与“推理完成”解耦为两个独立的持久化事件。外部调用方在数十毫秒内即可获得任务已接受的确认，不必等待长达数十秒甚至数分钟的模型推理，大大降低了 webhook 和 RPC 调用的超时风险。

其次，基于 idempotency_key 的唯一索引和 Session.appendMessage() 的消息级去重，双重保障了重试安全。外部系统因超时或网络故障重试同一请求时，系统不会重复插入用户消息，也不会创建重复任务记录或重复执行推理，同时仍然正确返回原有任务的 ID 和最新状态。

第三，任务状态全程持久化在 SQLite 中，结合 Durable Object 的 runFiber 持久执行机制，在 DO 因空闲超时被驱逐、代码更新触发重启、或底层资源调度导致迁移等场景下，任务状态和消息数据均不丢失。恢复后系统能从 think_external_tasks 表中重建任务上下文并继续或重新执行。

第四，通过复用 TurnQueue、AbortRegistry、Session、StreamAccumulator 等现有模块，方案在不重写普通聊天消息保存路径的前提下获得了异步执行、状态追踪、取消和清理的完整能力。普通 WebSocket 聊天用户感知不到任何行为变化。

### 9. 风险与待确认事项

以下问题需要在实际开发中进一步确认和细化：

(1) 事务边界：submitTask() 中消息写入（Session.appendMessage）与任务记录插入（INSERT INTO think_external_tasks）需要在同一 SQLite 事务中完成，以保证崩溃一致性。当前 Session.appendMessage() 由 AgentSessionProvider 实现，其内部使用了独立的 SQL 执行，需确认是否可与外部调用方的 SQL 语句包裹在同一事务中，或调整为两阶段写入+补偿逻辑。

(2) 流式输出传递：当前方案中外部分任务路径不向调用方实时推送流式推理块。若未来需要支持流式输出（如通过 Webhook 回调逐步推送推理结果），需在 _streamResult 中增加一个可选的 StreamCallback 参数，将推理块异步转发给外部回调端点。这涉及 ResumableStream 的块缓冲与外部推送之间的协调。

(3) 任务保留策略：think_external_tasks 表随时间增长，需定义清理策略——如按时间窗口自动清理已完成任务，或提供按 session_id 批量清理接口。可与 Session 的 compaction 机制协同设计，避免任务记录无限增长。

(4) 并发执行策略：当前 TurnQueue 为串行队列，外部任务与普通聊天请求共享同一队列。若外部任务量大且执行耗时长，可能阻塞普通聊天用户的交互体验。未来可考虑为外部任务引入独立的优先级队列或并发槽位。

(5) 与现有 saveMessages 的关系：saveMessages() 已支持外部 AbortSignal 传入和异步执行返回 { requestId, status }，但其仍然等待推理完成后才 resolve。可考虑让 saveMessages 复用 think_external_tasks 的持久状态边界，或保持两个接口独立。
