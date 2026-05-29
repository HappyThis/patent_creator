## 技术方案

本方案在 Think agent 现有架构之上增加一层「外部任务提交层」，使 webhook、RPC 调用方或其他 Worker 能够以 REST 风格向 Think agent 提交对话任务、快速获得接收确认，并在后续按需查询执行状态、取消或清理任务，而不必等待模型推理完成。

### 技术问题说明

现有 Think agent 的三种对话入口——WebSocket 协议（_handleChatRequest）、RPC 子代理调用（chat()）、程序化调用（saveMessages）——均将「接收用户消息」与「等待模型推理完成」耦合在同一个请求生命周期中。WebSocket 路径下，客户端需维持长连接直到推理结束；RPC 路径下，调用方需阻塞等待异步流式回调完成；saveMessages 虽为程序化入口，但同样在 TurnQueue 中排队并等待推理完成才返回。当调用方是 webhook、定时任务或其他短生命周期 Worker 时，这种耦合导致调用方必须实现复杂的超时重试和连接保持逻辑。

### 整体架构

本方案在 Think agent 的 HTTP 表面（onRequest 方法）新增三个 REST 端点：POST /submit 用于提交任务并立即返回任务标识；GET /status/:task_id 用于查询任务执行状态及结果；DELETE /tasks/:task_id 用于取消执行中的任务或清理已完成任务。任务状态独立存储在新增的 SQLite 表 cf_task_submissions 中，与现有 Session 消息存储、ResumableStream 流式块存储并列。任务执行仍通过现有的 saveMessages → TurnQueue → _runInferenceLoop 路径驱动，不重写推理管线。

### 任务状态机与持久化存储

系统定义以下任务状态及转换规则：

- accepted：任务已被接收并持久化，但尚未进入 TurnQueue。外部调用方在此状态下即可获得 task_id 和 HTTP 202 响应。
- queued：任务已调用 saveMessages 并将用户消息写入 Session，但 TurnQueue 中尚有前序 turn 未完成，当前任务处于排队等待状态。
- running：TurnQueue 已调度到该任务，_runInferenceLoop 正在执行模型推理。该状态由 TurnQueue.enqueue 的 fn 回调首次执行时写入。
- completed：模型推理正常结束，助理消息已持久化到 Session，结果可供查询。
- failed：推理过程抛出未捕获异常。错误信息持久化到任务记录中。
- cancelled：外部调用方通过 DELETE /tasks/:task_id 触发了取消。若任务尚在 queued 状态，则通过 TurnQueue 的代际失效机制跳过执行；若任务处于 running 状态，则通过 AbortRegistry 中断推理循环。

状态转换规则：accepted → queued（saveMessages 调用成功后写入）；queued → running（TurnQueue 调度到时写入）；running → completed / failed（推理完成或异常时写入）；accepted / queued / running → cancelled（取消请求到达时写入）。所有状态写入均通过 DO 的 SQLite 事务保证原子性。

新增 SQLite 表 cf_task_submissions 的核心字段包括：task_id（主键，由服务端在接收提交时生成）、idempotency_key（外部调用方提供的幂等键，建立唯一索引）、status（上述枚举之一）、request_id（关联到 saveMessages 返回的 requestId）、created_at、updated_at、error_message、result_message_id（完成后指向 Session 中的助理消息 ID）。idempotency_key 的唯一索引在数据库层面保证：同一幂等键的重复提交只会插入一次任务记录，后续提交通过查询已有记录直接返回状态。

### 提交流程——幂等提交与快速确认

POST /submit 端点接收 JSON 请求体，必须包含 messages（UIMessage 数组）和 idempotency_key（字符串），可选包含 clientTools、body 等配置字段。处理流程如下：

1. 解析请求体，校验 idempotency_key 和 messages 非空。
2. 在 DO SQLite 中开启事务：首先按 idempotency_key 查询 cf_task_submissions，若命中已有记录且状态非 cancelled，则直接返回已有 task_id 和当前状态（HTTP 200），不执行任何消息写入或推理触发——这是去重的关键机制。
3. 若未命中：生成 task_id，INSERT 一条 status='accepted' 的任务记录，同时将用户消息写入 Session（复用现有 session.appendMessage 路径），COMMIT 事务。消息写入与任务记录插入在同一 DO 事务中完成，保证「接收确认」与「消息持久化」的原子性。
4. 事务提交成功后，服务端立即向调用方返回 HTTP 202，响应体包含 task_id 和 status='accepted'。此时调用方可断开连接。
5. 服务端在返回响应后，异步调用 saveMessages 触发推理管线：saveMessages → TurnQueue.enqueue → _runInferenceLoop。在 saveMessages 调用前将状态更新为 queued；在 TurnQueue 实际开始执行 fn 回调时将状态更新为 running；推理完成后将状态更新为 completed 或 failed。

步骤 3 的事务原子性是关键设计：若消息写入 Session 成功但任务记录插入失败（如唯一约束冲突），整个事务回滚，不会产生孤儿消息；同理，若事务成功提交，则外部调用方必定能通过 task_id 查询到该任务，且用户消息已确定性地存在于 Session 中。

### 状态查询流程

GET /status/:task_id 端点查询指定任务的当前状态。实现直接读取 cf_task_submissions 表中对应 task_id 的行。响应体包含 task_id、status、created_at、updated_at。当 status 为 completed 时，额外返回 result_message_id，调用方可据此通过已有的 GET /get-messages 端点获取完整助理消息内容。当 status 为 failed 时，额外返回 error_message。当 status 为 cancelled 时，额外返回 cancelled_at。

### 取消与清理流程

DELETE /tasks/:task_id 支持两种语义，由查询参数 operation 区分：

- operation=cancel（默认）：取消正在执行或等待中的任务。如果任务处于 accepted 或 queued 状态，通过 TurnQueue.reset() 提升代际计数器使该任务变为 stale 跳过；同时将任务状态更新为 cancelled。如果任务处于 running 状态，通过 AbortRegistry.cancel(requestId) 中断正在执行的推理循环，部分已流式输出的内容仍按现有语义持久化到 Session（复用 onChatError 的「部分消息持久化」路径），然后更新状态为 cancelled。
- operation=cleanup：用于已完成（completed / failed / cancelled）的任务，清除任务记录及其关联的消息。具体操作为：删除 cf_task_submissions 中对应行，并通过 Session 的消息管理接口删除关联的用户消息和助理消息（若助理消息已生成）。为防止误删，cleanup 仅对终态任务有效；对 accepted / queued / running 状态的任务返回 409 Conflict。

取消操作的幂等性：对已处于终态（completed / failed / cancelled）的任务再次发送 cancel，直接返回当前状态（HTTP 200），不产生副作用。

### 崩溃恢复与边界处理

本方案需处理以下关键边界，确保「快速确认接收」与「模型推理最终完成」之间的持久状态在各种故障场景下保持一致：

场景一：提交事务成功后、saveMessages 调用前 DO 崩溃。DO 重新激活后，cf_task_submissions 中存在一条 status='accepted' 的记录，但对应的推理尚未触发。系统在 onStart 生命周期中（与现有 _restoreClientTools / _restoreBody 并列）增加 _restorePendingTasks 步骤：扫描 cf_task_submissions 中 status 为 accepted 或 queued 的行，对每条恢复调用 saveMessages（其中用户消息已持久化在 Session 中，故 saveMessages 的 messages 参数传空数组，仅触发推理），并将状态更新为 queued。saveMessages 内部的 TurnQueue 代际机制和 runFiber 恢复机制将接管后续执行。

场景二：推理执行中 DO 崩溃。这是现有 chatRecovery + runFiber 机制已覆盖的场景。runFiber 将推理回调包裹在持久化的 fiber 行中；DO 重新激活后，_runFiberRecoveryOnStartup 检测到孤儿 fiber 行，触发 onChatRecovery 钩子，持久化部分流式输出并调度 continueLastTurn。本方案新增的任务表中的 running 状态在恢复后保持不变——因为同一 requestId 关联的推理恢复由现有机制保证。

场景三：外部调用方因超时或网络问题重试同一 idempotency_key 的提交。由于 idempotency_key 唯一索引在数据库层面生效，第二次提交在事务中查到已有记录，直接返回已有 task_id 和当前状态，不会重复插入用户消息。若第一次提交的状态已推进到 completed，重试直接返回完成结果。

场景四：取消操作与推理完成的竞态。若 cancel 请求（通过 AbortRegistry.cancel）与推理完成几乎同时发生，最终状态取决于 DO 单线程模型下的执行顺序。若 cancel 先执行，abortSignal 触发，推理循环以 aborted 状态结束，任务状态更新为 cancelled；若推理先完成，助理消息和任务状态已分别写入 Session 和 cf_task_submissions，cancel 请求发现状态已为 completed，直接返回当前状态不产生副作用。

### 与现有路径的兼容性

本方案的兼容性设计遵循以下原则：

- 消息保存路径不重写：外部提交产生的用户消息仍通过 session.appendMessage 写入 Session，助理消息仍通过 _persistAssistantMessage 写入；这些路径与 WebSocket 聊天、RPC 子代理调用完全共享，不会产生两套消息存储逻辑。
- TurnQueue 复用不变：外部提交通过 saveMessages 接入 TurnQueue，与 WebSocket 的 _handleChatRequest 共享同一个 TurnQueue 实例。现有 concurrency 策略（queue / latest / merge / drop / debounce）对外部提交同样生效。
- AbortRegistry 复用不变：外部提交的 requestId 注册到同一个 AbortRegistry，cancel 操作通过 AbortRegistry.cancel(requestId) 执行，与 WebSocket 的 chat-request-cancel 走相同的取消路径。
- resumable stream 和 chatRecovery 不受影响：外部提交进入 saveMessages 后，其流式输出同样经过 _resumableStream.start / _streamResult 处理，受益于相同的流式块持久化和断线重放能力。runFiber 包裹的推理回调同样享受 fiber 级崩溃恢复。
- 新增表为纯增量：cf_task_submissions 表与现有 Session 消息表、stream_chunks 表、fiber 表互不依赖，增删任务记录不会破坏其他数据的完整性。

### 技术效果

本方案通过增加薄层 REST 接口和任务状态表，在尽量复用现有基础设施的前提下实现了以下技术效果：

- 接收与推理解耦：外部调用方提交任务后在毫秒级获得 task_id 确认，无需维持长连接等待模型推理（可能持续数十秒甚至数分钟）。调用方可自行选择轮询或回调方式获取结果。
- 精确一次语义：通过数据库层的 idempotency_key 唯一约束，同一幂等键的重复提交不会产生重复消息或重复推理，外部系统可安全重试而不必担心副作用。
- 崩溃恢复覆盖全链路：提交事务中消息持久化与任务记录写入原子完成；DO 重新激活后自动恢复 accepted / queued 状态的任务；running 状态的任务由现有 runFiber 恢复机制接管。三个状态阶段的恢复路径均被覆盖，无遗漏窗口。
- 取消语义完整：取消操作覆盖 accepted、queued、running 三个阶段，分别通过跳过排队和 AbortRegistry 中断推理实现。部分输出已持久化，与现有 WebSocket cancel 行为一致。
- 普通聊天路径零影响：新增端点仅在 onRequest 中匹配特定路径时生效；消息存储、TurnQueue、AbortRegistry、Session 均保持原有接口不变。现有 WebSocket 聊天和 RPC 子代理调用不受任何影响。
- 可观察性增强：外部系统可通过 GET /status 实时查询任务进展；任务表为后续增加 list（列举所有任务）、inspect（查看任务详情）等管理面接口提供了持久化基础。

### 风险与待确认问题

以下为当前方案中需要后续确认或进一步设计的技术点：

- idempotency_key 的保留窗口：当前设计不对幂等键设置过期时间。若外部系统持续产生新的幂等键，cf_task_submissions 表将无限增长。建议后续增加可配置的保留窗口（如 24 小时），定时清理超过窗口的已完成任务记录。相关的消息清理需要联动 Session 的消息删除接口。
- 轮询效率：GET /status 是纯轮询模式，高频率轮询会对 DO 产生不必要的读写压力。后续可考虑增加 Webhook 回调通知或 Server-Sent Events 推送，使外部系统无需轮询即可获知任务完成事件。
- 并发提交限制：当前方案未对同一 agent 实例的并发外部提交数量设置硬限制，依赖 TurnQueue 的序列化执行和 SubmitConcurrencyController 的策略控制。若外部系统以极高频率提交（如每秒数百次），可能造成 TurnQueue 堆积。建议后续基于 pendingEnqueueCount 增加背压信号（返回 HTTP 429）。
- 任务结果的长期存储：completed 任务的助理消息存储在 Session 中，受 Session 的 compaction 和消息截断策略影响。若外部调用方期望长期保留任务结果，需要在 cleanup 之前自行拉取并存储结果。
