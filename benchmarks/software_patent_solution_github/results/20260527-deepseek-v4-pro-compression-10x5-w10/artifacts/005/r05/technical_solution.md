## 技术方案

### 整体架构

本方案在 Think agent 现有架构基础上新增三个核心组件：(1) 持久任务记录表 task_records，存储外部触发任务的完整生命周期状态；(2) 请求去重层，基于外部调用方提供的幂等键实现提交级去重；(3) 任务管理 RPC 接口，提供状态查询与取消能力。方案沿用 Think 现有的 TurnQueue 序列化执行、AbortRegistry 取消控制、ResumableStream 流式缓冲、Session 消息持久化等基础设施，不重写普通聊天路径。

任务表 task_records 建在 Think 已有的 SQLite 数据库（由 AgentSessionProvider 管理）中，包含字段：idempotency_key（外部幂等键）、task_id（系统生成任务 ID）、status（received/queued/executing/completed/failed/cancelled）、request_id（关联的 TurnQueue requestId）、session_id（关联的会话 ID）、input_messages（待注入的消息载荷）、result_summary（完成后摘要）、created_at、updated_at。idempotency_key + session_id 建立唯一索引，实现提交级去重。

外部调用方通过增强后的 chat() RPC 方法提交任务，携带 idempotency_key 参数。Think agent 先写入 task_records 表（INSERT OR IGNORE 保证幂等），若为重复提交则直接返回已有任务状态；若为新任务，写入成功后立即返回 'accepted' 响应（包含 task_id），随后异步将任务排入 TurnQueue 执行。执行过程中任务状态随 TurnQueue 生命周期流转，执行结果通过独立的任务查询接口对外暴露。

### 持久任务状态机

任务状态机定义六个状态及其转换条件：received（已接收，任务记录已持久化，尚未排入 TurnQueue）、queued（已排入 TurnQueue，等待并发槽位或正在队列中排队）、executing（模型推理执行中，已获取 TurnQueue 执行权）、completed（推理成功完成，结果已落盘）、failed（推理异常失败）、cancelled（被外部调用方取消）。状态写入 task_records.status 字段，每次状态变更同时更新 updated_at 时间戳。

状态转换路径：received → queued，发生在 chat() 方法将任务排入 TurnQueue 成功后；queued → executing，发生在 TurnQueue 回调开始执行时，由任务处理器在进入推理循环前更新；executing → completed，发生在模型推理完成、assistant 消息落盘后；executing → failed，发生在推理异常或超时；received / queued / executing → cancelled，发生在外部调用方通过 cancelTask() 接口取消任务时。cancelled、completed、failed 为终态，不可再转换。

崩溃恢复时的状态判定规则：启动时扫描 status IN ('received', 'queued', 'executing') 的任务。received 状态的任务直接排入 TurnQueue（状态转为 queued）；queued 状态的任务重新排入 TurnQueue（队列在进程重启后已清空，需重建排队）；executing 状态的任务通过 ResumableStream 已有机制判断流是否已完成——若流已完成则标记 completed 或 failed，若流未完成则重新排入 TurnQueue 触发 continueLastTurn 语义恢复执行。

### 幂等接收与请求去重

外部调用方通过增强后的 chat() RPC 方法提交任务时，必须携带 idempotency_key 参数。该参数由外部调用方生成，对其业务域内唯一（如 UUID 或业务流水号），与 session_id 组合构成全局唯一的幂等键。Think agent 在 task_records 表上建立 UNIQUE(idempotency_key, session_id) 约束，使用 INSERT OR IGNORE 语义写入：若插入成功，说明是新任务，继续后续流程；若插入被忽略（冲突），说明是重复提交，直接查询已有记录并返回当前任务状态和 task_id。

去重粒度控制在提交层面而非消息层面。一次提交可携带多条输入消息（input_messages 字段，JSON 数组），这些消息在任务被 TurnQueue 拾取执行时，通过现有的 saveMessages() 路径批量注入到 Session。消息注入沿用现有的 INSERT OR IGNORE 按消息 ID 去重机制，与提交级去重形成两层防护：外层幂等键防止重复创建任务和重复注入消息，内层消息 ID 去重防止同一任务内部消息重复写入。

为兼容不带 idempotency_key 的普通聊天调用（如 WebSocket 客户端的实时对话），chat() 方法在未收到 idempotency_key 时保持原有行为：直接走 TurnQueue → 流式输出路径，不创建 task_records 记录，不进入任务状态机。这使得外部触发任务路径与普通聊天路径在入口处即分叉，互不干扰。

### 执行领取与崩溃恢复

任务排入 TurnQueue 后，由 TurnQueue 的序列化回调负责执行。回调函数在获取执行权后，先将任务状态从 queued 更新为 executing（写入 task_records），然后调用 saveMessages() 将 input_messages 批量注入 Session，再触发模型推理。这一过程中，任务记录的状态更新与 TurnQueue 的 generation 机制协同：若 TurnQueue 因 reset() 导致 generation 递增，旧回调变为 stale 自动丢弃；此时任务状态停留在 queued，后续由恢复机制重新排入。

崩溃恢复在 Think agent 启动时触发。启动流程新增 scanPendingTasks() 步骤：查询 task_records WHERE status IN ('received', 'queued', 'executing')。对 received 和 queued 的任务，调用 enqueueTask() 重新排入 TurnQueue。对 executing 状态的任务，先通过 ResumableStream 查询该 request_id 的流 chunk 是否已标记完成——若 cf_ai_chat_stream_metadata 中该流的 status 为 'completed' 或 'error'，则将任务对应标记为 completed 或 failed；若流状态为 'streaming'（未完成），则调用 continueLastTurn() 恢复推理执行。

恢复时消息去重的保证：由于 input_messages 在任务首次执行时已通过 saveMessages() 注入 Session，且消息级别有 INSERT OR IGNORE 保护，continueLastTurn() 恢复执行时不会重复注入用户消息。ResumableStream 在重放时也从已持久化的 chunk 断点续传，不会重复输出已完成的部分。这两层保证使得崩溃恢复后的任务执行与首次执行在数据层面等效。

### 状态查询与取消清理

状态查询通过新增的 getTaskStatus(task_id) RPC 方法暴露。该方法直接从 task_records 表按 task_id 查询，返回结构为 { taskId, status, idempotencyKey, sessionId, createdAt, updatedAt, resultSummary }。resultSummary 在任务进入 completed 或 failed 终态时写入，包含执行耗时、输出消息数量、错误信息（失败时）等摘要。查询接口为纯读操作，不修改任务状态，可被外部调用方轮询使用。

取消通过新增的 cancelTask(task_id) RPC 方法实现。该方法先检查任务状态：若已处于 completed、failed 或 cancelled 终态，直接返回当前状态不做操作；若处于 received、queued 或 executing，则执行取消流程——将 status 更新为 cancelled，同时调用 AbortRegistry 的 abort(request_id) 方法触发推理中断。AbortRegistry 沿现有的 AbortController 链路传播取消信号：中止模型推理、停止流式输出、使 TurnQueue 中该任务的回调变为已取消。

清理通过增强后的 _handleClear(session_id) 方法实现。在原有清理逻辑（重置 TurnQueue、销毁 AbortRegistry、清除 ResumableStream、删除消息）基础上，新增对 task_records 中该 session 关联任务的批量终态标记：将所有非终态任务标记为 cancelled，确保清理后状态查询返回一致的结果。Delete 操作将任务记录软删除或标记为 deleted 终态，保留审计追溯能力。清理完成后广播 clear 事件，通知所有连接的 WebSocket 客户端。

### 与现有聊天路径的兼容关系

本方案新增的外部触发任务路径与 Think 现有普通聊天路径在入口处通过 idempotency_key 参数的有无实现分叉。不带 idempotency_key 的调用（包括 WebSocket 客户端的实时对话和现有的程序化 saveMessages 调用）完全不受影响：chat() 方法检测到缺少 idempotency_key 时，跳过 task_records 写入和状态机逻辑，直接走原有的 TurnQueue 排队和 WebSocket 流式推送路径。消息保存路径（_handleChatRequest 中的 message reconciliation、Session 持久化）也不做任何修改。

带 idempotency_key 的外部触发路径与普通聊天路径共享 TurnQueue、AbortRegistry、ResumableStream、Session 等核心基础设施，但通过以下方式实现隔离：(1) 任务状态写入独立的 task_records 表，不侵入 Session 消息表结构；(2) 外部触发任务的流式输出默认不通过 WebSocket 推送，而是通过 ResumableStream 持久化后由状态查询接口暴露，避免干扰实时聊天客户端；(3) 取消操作在 AbortRegistry 层面统一处理，cancelTask() 与 WebSocket cancel 消息使用相同的 abort(request_id) 底层调用。

并发控制方面，外部触发任务与普通聊天共用 SubmitConcurrencyController 的并发槽位。外部任务排入 TurnQueue 时同样受 concurrency 策略（queue/latest/merge/drop/debounce）约束，由 beginEnqueue/release 追踪 pending 计数。这意味着在高并发场景下，外部任务和实时聊天请求在队列层面公平竞争，不会出现某一方饥饿的情况。

### 技术效果

本方案通过引入持久任务记录表和幂等键机制，在 Think agent 现有架构上实现了对外部系统触发任务的完整支持，达到以下技术效果：

(1) 快速确认接收：外部调用方提交任务后，仅需一次 SQLite INSERT 写入即可返回 accepted 响应，耗时在毫秒级，不受模型推理耗时影响，解决了长时间推理场景下调用方超时和连接占用问题。(2) 提交级幂等：通过 idempotency_key + session_id 唯一约束，保证同一请求重复提交不会创建重复任务、不会重复注入消息、不会重复触发推理，使外部调用方可以安全重试而不产生副作用。

(3) 崩溃恢复可靠：启动时扫描非终态任务，结合 ResumableStream 的流状态判断，自动恢复或终结中断的任务。消息层面的 INSERT OR IGNORE 和流层面的 chunk 断点续传保证恢复后数据一致性。(4) 可观察与可控：getTaskStatus 提供标准轮询接口，cancelTask 提供主动取消能力，清理操作保证终态一致性。(5) 架构兼容：方案不修改普通聊天路径的任何逻辑，复用 TurnQueue、Session、ResumableStream、AbortRegistry 等已有基础设施，增量代码集中在 task_records 表操作和少量 RPC 方法增强上，侵入性低。
