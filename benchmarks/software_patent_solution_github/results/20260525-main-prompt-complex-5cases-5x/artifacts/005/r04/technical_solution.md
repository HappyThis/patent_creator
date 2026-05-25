## 技术方案

本方案在 Think agent 现有会话管理、消息持久化、流式推理、取消和恢复语义的基础之上，增加一个面向外部系统（webhook、RPC 调用方、Worker）的任务提交层，使外部调用方可以可靠地提交一次对话任务、快速获得「已接收」确认、异步查询执行状态、必要时取消或清理任务，而无需等待模型推理完成。方案复用 Think 现有的 Session、TurnQueue、AbortRegistry、ResumableStream 和 Fiber 恢复机制，不重写普通聊天消息的保存路径、WebSocket 协议或流式输出流程。

### 整体架构

方案在 Think 类中新增一个任务提交子系统，由以下核心组件构成：(1) 任务持久化表 cf_agent_task_submissions，用于在 SQLite 中记录每条外部提交的全生命周期状态；(2) 幂等键生成与去重检查逻辑，防止外部系统重试导致重复插入用户消息或重复执行推理；(3) 异步执行领取机制，在提交确认后通过 TurnQueue 排队执行推理，不阻塞提交响应的返回；(4) 状态查询、取消和清理接口，基于任务记录和现有 AbortRegistry 实现外部可控的生命周期管理；(5) 崩溃恢复逻辑，利用现有 runFiber 持久执行框架和 onStart 恢复扫描，确保 DO 重启后「已接收但尚未完成」的任务能够被继续执行或被标记为可查询的终态。

### 任务数据模型

新增 SQLite 表 cf_agent_task_submissions，作为任务提交的持久化状态存储。表结构包含以下字段：task_id（主键，外部调用方提供的幂等键或系统生成的唯一标识）、idempotency_key（基于提交内容的确定性哈希，用于跨重试去重）、session_id（关联的 Think Session 标识，一个任务可绑定到已有会话或创建新会话）、request_id（内部生成的请求标识，对应 TurnQueue 中的请求 ID 和 AbortRegistry 中的控制器键）、status（枚举值：accepted / running / completed / error / aborted / orphaned）、input_json（原始提交内容的 JSON 序列化）、output_json（推理完成后助手消息的 JSON 序列化）、summary（可选的摘要字段，供外部快速了解结果概要）、error_message（失败或取消时的错误描述）、created_at / started_at / completed_at（毫秒级时间戳）。

该表通过 DO 的 SQLite 存储确保在进程崩溃或 DO 休眠/驱逐后状态不丢失。索引建立在 idempotency_key 和 status 上，以支持快速的去重查询和待恢复任务扫描。表与现有的 assistant_messages（Session 消息存储）、cf_ai_chat_stream_metadata（流元数据）和 cf_agents_runs（Fiber 执行记录）之间通过 request_id 关联，不修改这些已有表的结构。

### 提交与幂等去重

外部调用方通过 submitTask(input, options) 方法提交任务。方法内部执行以下流程：

第一步：生成幂等键。对 input 内容进行规范化处理后计算哈希值（例如 SHA-256 摘要），并与可选的 options.sessionId 组合为完整的 idempotency_key。调用方也可以显式提供 options.idempotencyKey 覆盖自动生成的键。

第二步：去重检查。以 idempotency_key 查询 cf_agent_task_submissions 表。若命中已存在的记录，且该记录处于非终态（accepted 或 running），则直接返回已有记录的 task_id 和当前 status，不插入新行、不追加用户消息、不触发新的推理。若命中记录的 status 为终态（completed、error、aborted），且 options.allowRetry 为 true，则允许创建新任务（使用新的 task_id）；否则返回已有结果。若未命中，则继续执行插入。

第三步：原子插入。使用 INSERT 语句将新任务行写入 cf_agent_task_submissions，status 设为 accepted，同时记录 idempotency_key。插入在 DO 的单线程 SQLite 事务中完成，保证原子性。

第四步：快速返回。插入成功后立即返回包含 task_id、status: "accepted" 和 created_at 的确认响应。外部调用方此时可以从 HTTP 连接中释放，不必保持长连接等待推理完成。

第五步：异步触发执行。在当前请求上下文中，通过 ctx.waitUntil 调度后续处理逻辑，或直接在当前 isolate 中排队——利用 think 已有的 saveMessages 调用路径，将 input 转换为用户消息注入 Session，然后通过 TurnQueue 排队执行推理。这一步从提交路径中异步分离，不阻塞第四步的快速返回。

### 异步执行与状态转换

提交确认后的异步执行通过以下状态机驱动，状态转换记录在 cf_agent_task_submissions 表中：

accepted → running：当 TurnQueue 中轮到该任务时，在调用 saveMessages / _runInferenceLoop 之前，将任务行的 status 更新为 running，并写入 started_at 时间戳。这一更新发生在消息注入 Session 之后、推理开始之前，确保即使推理尚未产生首个 token，外部也能观察到任务已进入执行阶段。

running → completed：推理正常完成、助手消息已通过 _persistAssistantMessage 持久化后，更新任务行的 status 为 completed，写入 output_json（助手消息的序列化结果）、summary 和 completed_at。同时保留 request_id 以支持后续的流式回放查询。

running → error：推理过程中发生不可恢复的错误（例如模型调用失败且重试耗尽），将 status 更新为 error，写入 error_message 和 completed_at。此时 Session 中的用户消息和已持久化的部分助手消息保留，供调试查询。

running → aborted：外部通过 cancelTask(taskId) 触发了 AbortRegistry 中对应 request_id 的取消，或外部 AbortSignal 被触发。推理循环检测到信号终止后，将 status 更新为 aborted，记录取消原因。已流式输出的部分助手消息仍被持久化（与 Think 现有取消语义一致）。

running → orphaned：DO 在推理执行期间因休眠/驱逐而丢失了活跃的 LLM 流读取器（isLive = false）。在 onStart 恢复扫描中检测到该状态后，将 status 更新为 orphaned，并记录 error_message 说明任务中断原因。外部调用方可通过查询感知到此状态，并选择重新提交。

所有状态更新均通过 SQLite UPDATE 执行，利用 WHERE status = 'previous_status' 条件进行乐观并发控制，确保在 DO 单线程模型下的状态转换串行正确性。

### 崩溃恢复

方案利用 Think 现有的 Fiber（runFiber）持久执行框架和 ResumableStream 恢复机制来处理崩溃/驱逐场景。核心策略是区分消息写入前后的两个关键崩溃窗口：

崩溃窗口一：任务已写入 accepted 但尚未将用户消息注入 Session。在 onStart 恢复扫描中，查询所有 status = 'accepted' 且 created_at 早于当前时间的任务行。对于这些任务，重新执行消息注入和推理调度（等价于重新走提交流程的第五步），但跳过幂等键检查和快速返回——因为任务行已存在。这确保了即使在 accepted → 消息注入之间崩溃，任务也不会丢失。

崩溃窗口二：用户消息已注入 Session 且推理正在执行中。此时有两种保护路径：(a) 若推理包装在 runFiber 中（chatRecovery = true，为默认行为），Fiber 的行写入 cf_agents_runs 表发生在推理开始前。DO 重启后 _checkRunFibers 扫描到残留 Fiber 行，触发 _handleInternalFiberRecovery，进而调用 onChatRecovery 钩子，将部分流式输出回放并持久化为助手消息，然后通过 schedule(0, ...) 触发 continueLastTurn。在此恢复路径中，更新对应 cf_agent_task_submissions 的状态。(b) 若 chatRecovery 为 false，则 ResumableStream.restore() 在 onStart 时恢复活跃流元数据并标记 _isLive = false（孤儿流）。恢复扫描检测到 running 状态任务对应的流已变为孤儿，将其标记为 orphaned 终态。

崩溃窗口三：推理已完成、助手消息已持久化，但任务状态尚未从 running 更新为 completed。在 onStart 恢复扫描中，检查 running 状态的任务对应的 request_id 是否已在 cf_ai_chat_stream_metadata 中存在 completed 状态的流，若存在则补写 completed 状态。

恢复扫描在 onStart 中通过 _reconcileTaskSubmissions 方法执行，使用 status IN ('accepted', 'running') 查询批量处理。恢复逻辑不阻塞 onStart 的其他初始化流程，而是通过 Promise.all 并发执行。

### 状态查询

外部调用方通过 getTask(taskId) 或 listTasks(criteria) 方法查询任务状态。

getTask(taskId) 直接以 task_id 查询 cf_agent_task_submissions 表，返回包含 task_id、status、created_at、started_at、completed_at、summary、error_message 的结构化信息。当 status 为 completed 时，可选地返回 output_json 或通过 outputPreview 字段返回截断的输出预览。本方法为纯读操作，不触发任何副作用。

listTasks(criteria) 支持按 status、createdBefore / createdAfter 时间范围和 limit / cursor 分页查询，复用 Agent 基类中已有的 Workflow 分页模式（keyset pagination，基于 created_at + task_id 的游标编码）。分页游标使用 Base64 编码的 JSON 对象（包含时间戳和任务 ID），与 cf_agents_workflows 的分页实现保持一致。

此外，当推理在流式执行过程中，外部调用方可以通过 getTaskStream(taskId) 获取已持久化的流式输出块（复用 ResumableStream.getStreamChunks），从而在无需 WebSocket 连接的情况下实现轮询式的渐进输出消费。

### 取消与清理

cancelTask(taskId, reason?) 方法取消一个正在进行中的任务。流程如下：

(1) 查询 cf_agent_task_submissions 获取任务当前的 status 和 request_id。若 status 已经是终态（completed / error / aborted / orphaned），直接返回 false 表示无需取消。若 status 为 accepted，直接将其更新为 aborted 并写入取消原因——此时推理尚未开始，无需操作 AbortRegistry。若 status 为 running，进入下一步。

(2) 通过 request_id 调用 AbortRegistry.cancel(requestId, reason)，触发对应 AbortController 的 abort()。推理循环中通过 abortSignal.aborted 检测到取消后，在 _streamResult 中停止迭代、将已累积的部分内容持久化为助手消息，并通过 finally 块将任务状态更新为 aborted。这与 Think 现有的 cancel 协议路径完全共用。

(3) 同时，对外部 AbortSignal 的支持也通过 AbortRegistry.linkExternal 方法实现。外部调用方在 submitTask 时传入 options.signal，若该 signal 在推理执行期间被触发，行为等价于调用 cancelTask。

deleteTask(taskId) 方法删除任务记录及其关联资源。对于非终态任务，先调用 cancelTask 确保推理被取消，再删除 cf_agent_task_submissions 中的行。可选地通过 options.cleanupSession 参数触发关联 Session 的 clearMessages()，以释放存储空间。对于已完成任务，仅删除任务元数据行，不影响 Session 中的对话历史（外部调用方仍可通过普通聊天路径访问历史消息）。

定时清理机制：在 ResumableStream 的 _maybeCleanupOldStreams 基础上，增加对 completed、error、aborted、orphaned 状态任务的定期清理（默认保留 24 小时），通过检查 completed_at 字段判断清理条件，与流数据清理共用触发时机。

### 与现有路径的兼容

方案严格保持在 Think 现有架构的扩展点上，不重写现有路径：

消息保存路径：提交任务的消息注入完全复用 session.appendMessage(msg) → _broadcastMessages() → TurnQueue.enqueue() → _runInferenceLoop() → _streamResult() → _persistAssistantMessage() 的现有调用链。不引入新的消息存储表，不修改 assistant_messages 的树形消息结构。

流式输出路径：推理的流式输出仍通过 ResumableStream.storeChunk / broadcast 和 MSG_CHAT_RESPONSE 协议帧发送。外部查询 getTaskStream 走的是 ResumableStream.getStreamChunks 的只读路径，不影响广播行为。

取消语义：cancelTask 通过 AbortRegistry 取消推理，与 WebSocket 客户端发送 cancel 协议消息走完全相同的控制器路径。推理循环中通过 AbortSignal 检测取消的逻辑无需修改。

恢复语义：崩溃恢复通过 runFiber 的 _checkRunFibers → _handleInternalFiberRecovery → onChatRecovery 路径完成。新增的 _reconcileTaskSubmissions 在此恢复链路之后执行，作为补充的状态对齐步骤，不替代或修改 Fiber 恢复的既有行为。

并发控制：TurnQueue 的串行执行保证（单线程 DO + 生成代际失效）天然确保同一 Agent 实例中多个提交任务不会并发执行推理。messageConcurrency 策略（queue/drop/latest/merge/debounce）对提交任务同样生效。

会话隔离：每个提交任务可指定 options.sessionId 绑定到已有 Session，或省略此参数以使用 Agent 的默认会话。多会话支持通过 Session.forSession() 在提交路径中指定，遵循与 WebSocket 多会话相同的 Session 创建和配置路径。

### 技术效果

本方案的核心技术效果体现在以下几个方面：

(1) 快速确认与异步解耦：外部调用方在提交任务后仅等待一次 SQLite 写入和幂等检查的耗时（通常 < 10ms），即可获得 accepted 确认并释放连接，不需要维持长连接等待模型推理（后者通常需要数秒到数十秒）。这显著降低了调用方的连接占用和超时风险。

(2) 精确一次执行语义：通过基于内容哈希的幂等键和 INSERT 级去重检查，外部系统的网络重试不会导致用户消息的重复插入或同一任务的重复推理。即使崩溃发生在 accepted 写入之后、消息注入之前，恢复扫描也能确保任务最终被执行一次且仅一次。

(3) 全生命周期可观察：从 accepted → running → completed/error/aborted/orphaned 的每个状态转换都有持久化记录和时间戳，外部调用方可以通过轮询 getTask 或 listTasks 获取精确的当前状态，而不依赖 WebSocket 推送或事件回调。

(4) 取消可控：通过复用 AbortRegistry，取消操作可以精确中止正在进行的 LLM 推理，并且已生成的部分内容被持久化保留（而非丢弃）。取消可以被外部 AbortSignal 或显式的 cancelTask 调用触发，两者走相同的底层路径。

(5) 崩溃安全：利用 DO 的持久 SQLite 存储和 Fiber 框架，在任何执行阶段的进程崩溃都不会导致任务丢失。三个崩溃窗口分别有对应的恢复策略，确保任务最终收敛到可查询的终态。

(6) 架构零侵入：方案以纯扩展方式构建——新增一张 SQLite 表和一个方法族，不修改 assistant_messages、cf_agents_runs、cf_ai_chat_stream_chunks 等核心表的模式，不修改 _runInferenceLoop、_streamResult、TurnQueue、AbortRegistry 等核心方法的内部逻辑，不改变已有 WebSocket 客户端和普通聊天用户的行为。

### 风险与待确认问题

以下风险点基于当前项目环境（Cloudflare Agents SDK / Think agent）识别，建议在实际实施中确认：

(1) 幂等键保留窗口：去重检查依赖 idempotency_key 的唯一性约束。当任务完成并被定时清理删除后，相同内容的重新提交将创建新任务（而非返回旧结果）。是否需要支持「已完成任务永久保留幂等键」的配置选项，取决于业务场景对重放攻击/意外重复提交的容忍度。

(2) DO 单线程模型的吞吐量限制：每个 Agent（DO）实例在同一时刻只能执行一个推理任务（TurnQueue 串行）。高并发外部提交场景下，若单个 Agent 实例承载大量提交任务，accepted 任务在队列中的等待时间会线性增长。方案可考虑通过 Agent 分片（多个同名 Agent 实例）来水平扩展提交吞吐量，但分片策略需要额外的路由逻辑。

(3) 输出体积：当模型输出非常大时，将完整 output_json 存入 SQLite 行可能接近 SQLite 的默认行大小限制。当前 ResumableStream 已对单 chunk 做 1.8MB 截断保护，任务输出也需要类似的截断或外溢存储策略（例如仅存储前 N 个字符的预览、完整输出通过 Workspace 文件系统读写）。

(4) orphaned 状态的语义精确性：当 DO 在推理期间被驱逐且 chatRecovery 为 false 时，running 任务被标记为 orphaned。此状态下 Session 中的用户消息已持久化但助手消息不存在或不完整，可能导致后续查询会话历史时出现「有问无答」的情况。是否需要自动注入一条说明性系统消息或自动清理幽灵用户消息，需要根据产品语义确认。

(5) 与原有 Agent-Workflow 集成的关系：当前 Agent 已有 runWorkflow / getWorkflow 等持久化工作流跟踪表 cf_agents_workflows。本方案新增的 cf_agent_task_submissions 表在功能上有一定重叠（均为异步任务跟踪），但服务的目标调用模式不同（Workflow 面向 Cloudflare Workflows 的长时间执行，任务提交面向单次 Agent 推理的快速确认）。两者可在 API 层面保持独立，或在后续迭代中统一为通用的异步执行跟踪层。
