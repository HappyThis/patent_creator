## 技术方案

### 技术问题

Think agent 当前支持两种对话模式：WebSocket 实时流式对话和 RPC chat() 子代理调用。这两种模式都要求调用方在整个模型推理期间维持连接或等待回调完成。当外部系统（如 webhook、RPC 调用方、后台 Worker）需要向 Think agent 提交一次对话任务时，调用方期望快速获得“已接收”确认并断开连接，随后通过独立接口查询任务执行状态、获取结果、取消或清理任务。同时，外部系统因超时或网络问题重试同一请求时，系统不应重复插入用户消息或重复执行推理。

### 核心技术方案

本方案在 Think agent 现有架构基础上引入“异步任务”抽象层。核心思路是：在 Durable Object SQLite 中新增任务状态持久化表，为每次外部任务提交分配幂等键和任务标识，将“接收确认”与“推理执行”解耦为独立步骤。接收阶段完成幂等检查、消息持久化和任务记录创建后即可返回；推理阶段异步执行，通过 TurnQueue 保证串行化，执行完成后更新任务状态。整个过程利用 Durable Object 的单线程语义和 SQLite 的事务性，保证快速确认与最终完成之间状态的一致性。

### 任务数据模型与持久化

在 Think 已有的 SQLite 表之外，新增任务状态持久化表 think_tasks，记录从外部提交到推理完成的全生命周期状态。关键字段包括：idempotency_key（TEXT PRIMARY KEY，外部调用方生成的幂等键）、task_id（TEXT NOT NULL UNIQUE，系统生成的全局唯一任务标识）、status（TEXT NOT NULL，取值为 pending/running/completed/failed/cancelled）、user_message（TEXT NOT NULL，JSON 序列化的 UIMessage 格式用户消息体）、result_message（TEXT，status=completed 时写入的助手回复）、error_message（TEXT，status=failed 时的错误描述）、created_at/started_at/completed_at 时间戳、以及 generation（INTEGER，会话代际计数器，用于 clear 操作时批量失效）。用户消息同时通过现有的 INSERT OR IGNORE 语义写入 assistant_messages 表，保证普通聊天路径和外部提交路径的消息存储统一。

### 快速确认接收与幂等去重

外部调用方通过 Think agent 新增的 submitTask RPC 方法提交任务，传入 idempotency_key 和用户消息。方法执行以下步骤：

1. 以 idempotency_key 为 PRIMARY KEY 查询 think_tasks 表：若命中且 status 为 pending 或 running，直接返回已有 task_id 和状态，不再重复创建（幂等去重）；若命中且 status 为 completed/failed/cancelled，返回已有结果（重复查询也可得到相同结果）。
2. 若未命中：在同一 SQLite 事务中执行三项操作：（a）将用户消息通过 INSERT OR IGNORE 写入 assistant_messages 表（利用消息 ID 的幂等性，兼容现有消息存储）；（b）在 think_tasks 表中插入一条 status=pending 的新记录；（c）将任务排入 TurnQueue 等待异步执行。
3. 返回 { taskId, status: 'accepted' } 给调用方，调用方可立即断开连接。

由于 submitTask 在首个数据库写入完成即返回，调用方获得确认的延迟仅取决于一次 SQLite 写入的耗时（通常小于 1ms），与模型推理耗时（秒级到分钟级）完全解耦。

### 异步执行与状态转换

任务进入 TurnQueue 后，复用 Think 现有的串行执行机制。执行流程如下：

1. TurnQueue 调度到该任务时，将 think_tasks 中对应记录的 status 更新为 running，记录 started_at 时间戳。
2. 从 think_tasks.user_message 中反序列化用户消息，构造 ChatMessageOptions，传入 session.appendMessage() 写入消息（实际已在上一步写入，此处利用 Session 的 idempotent append 语义）。
3. 调用 onChatMessage() 执行 agentic loop —— 与普通 WebSocket 对话完全相同的推理路径：assembleContext() 组装上下文 → streamText() 调用 LLM → StreamAccumulator 累积结果 → sanitizeMessage() 清理元数据 → enforceRowSizeLimit() 截断超长内容。
4. 推理完成后，将组装好的助手消息持久化到 assistant_messages 表（INSERT ON CONFLICT UPDATE），同时将结果写入 think_tasks.result_message，更新 status 为 completed 和 completed_at 时间戳。
5. 若执行过程中发生错误：将错误信息写入 think_tasks.error_message，更新 status 为 failed；部分生成的消息仍按 Think 现有错误处理策略持久化到 assistant_messages（保留上下文不丢失）。

整个执行过程复用 Think 现有的 TurnQueue 串行化保证：同一 DO 实例的多个任务不会并发执行模型推理，避免消息列表竞争。generation 计数器在 clear 操作时递增，任务执行前会校验 generation 是否匹配——不匹配则跳过执行。

### 消息写入前后的崩溃恢复

Durable Object 在推理期间可能因休眠（hibernation）或异常导致实例被驱逐。方案针对不同时间点的崩溃场景设计恢复策略：

1. 消息写入前崩溃（任务记录已创建但用户消息未写入）：DO 重启后，在 onStart 中扫描 think_tasks 表中 status=pending 且 created_at 距离当前时间超过阈值的记录。对这类孤儿任务，直接标记 status=failed（错误信息：提交未完成），不尝试恢复——调用方重试时使用相同的 idempotency_key 即可安全重新提交。
2. 消息写入后、推理执行前崩溃（用户消息已持久化，任务 status=pending/running）：DO 重启后，在 onStart 中调用 recoverPendingTasks()。该方法重新加载消息列表、将匹配的 pending/running 任务重新排入 TurnQueue。由于用户消息已通过 INSERT OR IGNORE 持久化，重新入队不会导致重复消息。
3. 推理执行中崩溃（任务 status=running，部分流式结果可能已通过 ResumableStream 缓冲）：DO 重启后，若 ResumableStream 检测到孤儿流（stream metadata 存在但无活跃 reader），从缓冲区块重建部分助手消息并持久化；然后将任务标记为 failed 并记录“推理中断”错误信息。调用方查询状态后可以决定重试。
4. 推理完成后、结果写入前崩溃：消息已持久化到 assistant_messages 但 think_tasks 未更新。恢复时通过对比 assistant_messages 中是否存在与任务 user_message 对应的助手回复（按时间顺序匹配）来自动补全 think_tasks 状态。

恢复逻辑的核心原则是：用户消息和助手消息的持久化始终以 assistant_messages 表为准（与普通聊天路径一致），think_tasks 表作为状态索引可通过对消息表的补偿查询进行修复。

### 执行领取机制

任务执行采用“领取”模式避免重复执行。TurnQueue 调度器从队列中取出任务时执行以下原子步骤：

1. 在 SQLite 事务中执行 UPDATE think_tasks SET status='running', started_at=? WHERE task_id=? AND status='pending'。
2. 检查 affected rows：若为 0，说明任务已被其他路径领取或状态已变更，跳过执行。若为 1，说明成功领取，继续执行。
3. 执行完成后再次以 task_id 为条件更新状态（UPDATE WHERE task_id=? AND status='running'），防止覆盖已被取消的任务状态。

该机制利用 SQLite 的行级事务保证：即使多个 Worker 或并发路径试图执行同一任务（例如 DO 恢复时重新入队与原始 TurnQueue 条目竞争），也只有一个路径能成功将状态从 pending 改为 running。DO 单线程模型在大多数场景下消除竞争，领取机制为边界条件（如 onStart 恢复逻辑与尚未清除的 TurnQueue 条目之间）提供额外保障。

### 状态查询、取消与清理

方案为外部调用方提供三个 RPC 方法用于任务生命周期管理：

- getTaskStatus(taskId)：查询 think_tasks 表返回 { taskId, status, result?, error?, createdAt, startedAt, completedAt }。status 为 completed 时附带 result_message 中的助手回复；status 为 failed 时附带 error_message。幂等键也可用于查询（先通过 idempotency_key 查找 task_id 再查询状态）。
- cancelTask(taskId)：通过 SQLite 事务执行 UPDATE think_tasks SET status='cancelled' WHERE task_id=? AND status IN ('pending','running')。若 affected rows 为 0（任务已完成或不存在），返回相应错误。若任务正在执行中（status=running），同时调用 Think 现有的 AbortController.abort() 中断推理流，并复用现有的部分消息持久化策略保存已生成内容。
- deleteTask(taskId)：软删除任务记录和关联消息。将 think_tasks 中对应行标记 deleted_at 时间戳，同时从 assistant_messages 表中删除关联的用户消息和助手消息。使用软删除而非物理删除，保留审计追溯能力。

状态查询接口使外部系统可以实现轮询或 webhook 回调模式：调用方提交任务后定期查询状态，或在 completed 后拉取结果。取消和删除操作与 Think 现有的 cf_agent_chat_clear 和 cf_agent_chat_request_cancel 消息处理逻辑在底层共享 abort 和消息清理路径。

### 普通聊天消息保存路径兼容

整个方案刻意避免重写 Think 现有的消息保存路径。具体措施包括：用户消息统一通过 Session.appendMessage()（底层为 INSERT OR IGNORE）写入 assistant_messages 表，与普通 WebSocket 聊天和 RPC chat() 使用相同的存储表和写入语义；助手消息持久化复用 sanitizeMessage() → enforceRowSizeLimit() → INSERT ON CONFLICT UPDATE 的完整流水线；TurnQueue 串行化同时保护外部任务和普通对话，避免两者在消息列表上产生竞争；generation 计数器在 clear 操作时递增，外部任务和普通对话均受此保护；任务状态表 think_tasks 是 assistant_messages 的索引辅助结构，不替代消息主表，不影响现有消息广播（cf_agent_chat_messages）和流式推送（cf_agent_use_chat_response）逻辑。

### 关键处理流程

外部任务提交与执行的完整处理流程分为五个阶段。第一阶段“提交与确认”：外部调用方调用 submitTask(idempotencyKey, message)，系统在 SQLite 事务中查询幂等键、写入用户消息到 assistant_messages、创建 think_tasks 记录（status=pending）、入队 TurnQueue，返回 taskId。第二阶段“异步执行”：TurnQueue 调度任务，执行领取（UPDATE status=running），调用 onChatMessage() 执行 agentic loop，通过 StreamAccumulator 累积结果，完成后持久化助手消息并更新 think_tasks 状态。第三阶段“状态查询”：外部调用方调用 getTaskStatus(taskId) 查询 think_tasks 表，获取当前状态和结果。第四阶段“取消”：调用 cancelTask(taskId)，在事务中将 pending/running 状态改为 cancelled，若正在运行则触发 AbortController.abort()。第五阶段“恢复”：DO 重启时 onStart 扫描 think_tasks 中的 pending/running 任务，执行领取恢复或标记失败。

### 技术效果

相比现有方式，本方案带来以下技术效果：第一，接收与执行解耦——调用方在消息持久化完成后（毫秒级）即获得确认，无需等待模型推理完成（秒级到分钟级），显著降低外部系统连接占用和超时风险。第二，精确一次的提交语义——通过幂等键 PRIMARY KEY 约束和 INSERT OR IGNORE 消息写入，同一请求无论重试多少次，最多创建一条用户消息并执行一次推理，消除重复提交和重复推理问题。第三，可观测可控制——外部系统可随时查询任务状态、获取结果、取消执行或清理记录，任务生命周期完全透明。第四，崩溃安全——利用 Durable Object SQLite 的持久性，任务状态和消息在 DO 休眠/驱逐后不丢失，恢复逻辑自动补全或标记异常任务。第五，架构兼容——完全复用 Think 现有的消息存储、TurnQueue 串行化、StreamAccumulator、AbortController 和 generation 计数器，不引入新的消息格式或存储路径，不重写普通聊天逻辑。

### 与项目环境的对应关系

本方案直接基于当前项目环境中 Think agent 的现有架构设计。新增的 think_tasks 表遵循 assistant_messages 和 think_config 的 SQLite 表设计模式。submitTask RPC 方法遵循 chat() 和 configure() 的 @callable 接口模式。任务执行复用 onChatMessage()、assembleContext()、streamText() 的 agentic loop 路径。消息持久化复用 Session.appendMessage()（底层 INSERT OR IGNORE）和 sanitizeMessage()/enforceRowSizeLimit() 流水线。TurnQueue 串行化直接使用 agents/chat/turn-queue.ts 的 TurnQueue 类。AbortController 管理复用 agents/chat 的 AbortRegistry。generation 计数器复用 TurnQueue.generation 属性。崩溃恢复利用 Durable Object 的 onStart() 生命周期和 SQLite 持久性。

### 风险与待确认问题

以下为待确认的风险点和设计决策项。第一，幂等键语义边界：当前设计以 idempotency_key 的 PRIMARY KEY 约束实现去重，同一幂等键的所有重试请求返回同一 taskId。需确认外部调用方是否接受“幂等键一旦使用即永久绑定该 taskId”的语义（即使任务已 completed，用同一幂等键也无法创建新任务）。若需支持“相同幂等键在任务完成后可重新提交”，需引入幂等键版本号或状态依赖的过期策略。第二，任务结果保留策略：completed/failed 任务的结果消息和状态记录是否设置 TTL 自动清理？当前设计为永久保留（与 assistant_messages 生命周期一致），若需自动清理需引入定时 GC 机制。第三，WebSocket 客户端可见性：外部提交的任务产生的消息和回复是否应对 WebSocket 客户端可见？当前设计统一写入 assistant_messages 表，符合广播语义；若需隔离，需引入消息来源标记。第四，与 Chats 多会话目录的集成：若 Think agent 实例作为 Chats 父代理的子代理运行，任务管理接口需要从父代理路由到子代理，需确认路由方案。
