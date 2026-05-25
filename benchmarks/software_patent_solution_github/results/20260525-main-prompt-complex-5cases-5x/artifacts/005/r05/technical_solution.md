## 技术方案

本方案提出一种面向 Think agent 的异步任务提交与生命周期管理系统，使 webhook、RPC 调用方或外部 Worker 能够可靠地向 Think agent 提交对话任务、快速获得接收确认，并在后续追踪、控制该任务的执行全过程。方案在 Think 现有 Durable Object 会话、消息持久化、流式执行、AbortRegistry 取消和 runFiber 崩溃恢复机制之上，新增一层持久化的任务记录层和异步调度层，无需重写普通聊天路径。

### 整体架构

方案在 Think agent 内部新增一个异步任务管理层（Async Task Layer），位于外部 HTTP/RPC 入口与 Think 现有消息处理、推理循环之间。外部调用方通过一个专用的提交端点（如 POST /tasks）发起任务，系统在持久化任务记录后立即返回任务标识（task_id）和状态为“已接收”的响应。随后由 Think agent 内部的调度器在条件满足时将任务转入执行，复用现有的会话管理（Session）、消息追加（appendMessage）、流式推理（streamText）、取消（AbortRegistry）和崩溃恢复（runFiber）机制完成实际对话任务。

### 任务状态机与持久化边界

系统为每个异步提交的任务维护一条持久化的任务记录，存储于 Think agent 的 SQLite 数据库中。任务在其生命周期中经历以下状态：

- RECEIVED：任务记录已创建并持久化，用户消息尚未写入 Think 会话。外部调用方已收到 task_id 和确认响应。
- QUEUED：任务已经过合法性校验和去重检查，等待调度执行。可配置并发限制，超过限制的任务在 QUEUED 状态排队。
- RUNNING：调度器已领取该任务，用户消息已写入 Think 会话（通过 Session.appendMessage），推理循环正在执行中。该状态下任务锁定了 Think 的 TurnQueue 执行槽位。
- COMPLETED：推理循环正常结束，assistant 消息已持久化到会话中。任务记录标记为完成，保留结果摘要。
- FAILED：推理过程中发生不可恢复的错误（如模型调用持续失败、超时）。任务记录包含错误信息，用户消息和部分 assistant 响应已持久化。
- CANCELLED：外部调用方在任务完成前请求取消。若任务处于 RECEIVED 或 QUEUED 状态，直接标记为 CANCELLED；若处于 RUNNING 状态，通过 AbortRegistry 发出取消信号后等待推理循环终止，再标记为 CANCELLED。
- DELETED：任务被外部调用方请求清理。系统删除或软删除任务记录，并可选择清理关联的会话消息。

### 异步任务提交与快速确认

外部调用方通过 POST /tasks 端点提交任务，请求体中包含：会话标识（session_id，可选，不提供时系统自动创建新会话）、用户消息内容（messages）、调用方生成的幂等键（idempotency_key）、以及可选的回调通知地址。系统在接收请求后执行以下步骤：

1. 幂等检查：以 idempotency_key 为键查询 SQLite 中的任务记录表（cf_async_tasks）。若存在匹配记录且在其保留窗口内（默认 24 小时），直接返回已有任务的 task_id 和当前状态，不重复处理。
2. 创建任务记录：INSERT 一条新记录到 cf_async_tasks 表，状态为 RECEIVED，同时记录 idempotency_key、session_id、创建时间戳、请求体摘要等信息。该写入使用 SQLite 的事务保证原子性。
3. 立即响应：返回 HTTP 202 Accepted，响应体包含 task_id、状态 RECEIVED，以及用于后续查询的状态端点 URL。整个接收路径耗时仅需一次 SQLite INSERT，可在毫秒级完成，无需等待模型推理。

此设计的关键在于“快速确认接收”与“模型推理最终完成”之间的持久状态边界：任务记录一旦写入 SQLite，即使 Think agent 所在的 Durable Object 在此后被休眠或意外终止，任务状态也不会丢失。外部调用方在收到 202 响应后即可认为任务已被可靠接收，断开连接不会影响任务执行。

### 幂等提交与重复去重

外部系统因网络超时、连接中断等原因可能重试同一提交请求。方案通过以下机制保证幂等性：

- idempotency_key 唯一约束：cf_async_tasks 表在 idempotency_key 列上建立 UNIQUE 索引。重复提交时 INSERT 触发冲突，系统转而执行 SELECT 查询已有记录并返回。
- 保留窗口：idempotency_key 的有效期由 created_at 和可配置的 retention_seconds（默认 86400 秒）决定。超过保留窗口的旧记录可被清理，释放 idempotency_key 供新任务使用。
- 状态复用：若重复提交对应的已有任务处于终态（COMPLETED、FAILED、CANCELLED），直接返回最终结果而不重新执行。若处于非终态（RECEIVED、QUEUED、RUNNING），返回当前状态供调用方继续轮询。
- 与 Think 现有消息幂等的协同：Think 已有的 INSERT OR IGNORE 消息插入机制（基于消息 ID 去重）与本方案的 idempotency_key 机制相互独立且互补。前者防止同一会话内消息重复插入，后者防止同一外部提交被多次创建为独立任务。即使任务记录层的幂等检查因边界条件失效，会话层的消息去重仍可防止重复插入用户消息，提供双重保护。

### 执行调度与崩溃恢复

任务的异步执行调度和崩溃恢复是方案的核心机制，直接决定了“快速确认接收”与“模型推理最终完成”之间持久状态边界的可靠性。

调度与领取。系统在任务记录创建后触发调度。调度器通过 SQL 查询状态为 RECEIVED 的任务，按创建时间排序，使用原子条件更新将状态从 RECEIVED 改为 RUNNING（UPDATE cf_async_tasks SET status='RUNNING' WHERE id=? AND status='RECEIVED'），确保并发场景下同一任务不会被多个执行实例重复领取。领取成功后，调度器将用户消息通过 Session.appendMessage 写入对应会话，然后调用 Think 现有的 _runInferenceLoop 执行推理。

消息写入与崩溃恢复的边界处理。消息写入和推理执行之间存在关键的崩溃窗口：（1）若在消息写入前崩溃，任务状态仍为 RECEIVED，下次调度器扫描时将重新领取；（2）若在消息写入后、推理执行前崩溃，会话中已存在该用户消息。恢复时，调度器检测到任务处于 RUNNING 状态且对应会话中已存在匹配消息，则跳过消息写入步骤直接进入推理恢复；（3）若在推理执行中崩溃，方案复用 Think 的 runFiber 机制：任务执行体包装在 runFiber 调用中，推理过程中的中间状态通过 stash() 存入 SQLite。Durable Object 重新激活时，onFiberRecovered 钩子根据已保存的快照恢复执行上下文，继续推理循环。

与 Think 现有 TurnQueue 的衔接。任务调度器将任务执行体通过 TurnQueue.enqueue 提交，自动获得 Think 的串行执行保证和 generation 校验。若用户在任务执行期间通过普通聊天路径清空了会话（触发 TurnQueue.reset），当前任务的 generation 校验失败，任务标记为 SKIPPED 并通知调用方。

### 状态查询、取消与清理

方案提供三个管理接口，供外部调用方追踪和控制已提交的任务：

状态查询（GET /tasks/:task_id）。调用方使用提交时返回的 task_id 查询任务当前状态。系统从 cf_async_tasks 表读取记录，返回状态（RECEIVED/ QUEUED/ RUNNING/ COMPLETED/ FAILED/ CANCELLED）、进度信息（如推理步骤数、已生成 token 数）、创建时间和最后更新时间。对于已完成的任务，可选择性返回结果摘要或会话中 assistant 消息的引用。

取消（POST /tasks/:task_id/cancel）。调用方请求取消一个尚未到达终态的任务。若任务处于 RECEIVED 或 QUEUED，系统直接将其状态原子更新为 CANCELLED，任务不会被调度执行。若任务处于 RUNNING，系统通过 AbortRegistry.cancel 发出取消信号——该机制与现有 WebSocket 路径的 cf_agent_chat_request_cancel 共享同一 AbortRegistry 实例，因此取消语义完全一致。推理循环收到 abort 信号后终止，任务标记为 CANCELLED。若任务已处于终态（COMPLETED、FAILED、已 CANCELLED、DELETED），取消请求返回当前状态，不产生副作用。

清理与删除（DELETE /tasks/:task_id）。调用方请求清理任务及其关联资源。系统将任务记录标记为 DELETED（软删除），保留一段可配置的宽限期后由后台清理任务真正删除记录。对于关联的会话和消息，方案采用可配置策略：可仅删除任务记录而保留会话（适用于会话仍需被普通聊天路径使用的场景），或级联清理会话及其全部消息（适用于一次性外部触发任务）。

### 与 Think 现有系统的兼容性

方案的关键设计原则是不重写 Think 现有模块。异步任务层以下列方式与现有路径并行共存：

- 会话管理复用：任务执行直接调用 Session.appendMessage 和 Session.getHistory，与普通 WebSocket 聊天使用完全相同的会话存储和消息树结构。会话的 FTS5 搜索、compaction、context block 等功能对异步任务透明可用。
- 消息保存路径不变：异步任务路径的用户消息写入和 assistant 消息持久化通过相同的 saveMessages 或 _runInferenceLoop → _streamResult 路径完成，不引入独立的消息存储表或序列化格式。已存在的 INSERT OR IGNORE 去重、sanitizeMessage 清洗、enforceRowSizeLimit 截断、maxPersistedMessages 淘汰等逻辑对异步任务自动生效。
- 取消语义统一：异步任务的取消复用 AbortRegistry，与 WebSocket 的 cf_agent_chat_request_cancel 和 RPC 的 AbortSignal 取消共享同一控制器注册表，取消行为一致。
- 流式执行不受影响：任务执行期间的流式输出通过 ResumableStream 缓冲和广播，连接的 WebSocket 客户端可实时观察到异步任务的推理进度。
- 崩溃恢复复用：任务执行体通过 runFiber 包装，与 Think 的 chatRecovery 模式共享 onFiberRecovered 钩子和 stash() 检查点机制。
- 普通聊天路径零影响：普通 WebSocket 聊天的消息接收、推理、流式输出路径完全不经过 cf_async_tasks 表或调度器。仅当请求通过 /tasks 端点进入时才创建任务记录，因此普通聊天的延迟和资源开销不受方案影响。

### 技术效果

本方案相比现有方式带来以下技术效果：

- 请求-执行解耦：外部调用方无需维持长连接等待模型推理完成，提交后即可断开连接，通过轮询或回调获取结果。这消除了 webhook 场景中常见的 HTTP 超时问题，允许 agent 处理耗时远超 HTTP 超时限制的复杂对话任务。
- 可靠提交保证：基于 SQLite 持久化任务记录和 idempotency_key 去重，确保即使在网络重试、Durable Object 休眠/唤醒、代码更新重启等边界条件下，同一外部请求不会被重复执行，也不会丢失。
- 崩溃恢复无数据丢失：任务执行的每个关键阶段（任务创建、消息写入、推理过程中间状态）均有持久化检查点。DO 意外终止后，恢复路径根据任务状态和会话内已有消息自动判断恢复起点，无需人工干预。
- 并发安全：通过 SQLite 条件更新（CAS）实现任务领取的原子性，配合 TurnQueue 的串行执行保证，确保同一会话在任何时刻只有一项任务在执行，避免竞态条件导致的重复推理或消息错序。
- 生命周期可观测：调用方可通过标准 REST 接口查询任意任务的当前状态、执行进度和最终结果，支持构建仪表盘、告警和自动重试等上层运维能力。
- 渐进式集成：方案不替代现有任何模块，仅新增一层任务管理层。已有 agent 子类无需修改即可同时支持普通 WebSocket 聊天和异步任务提交两种模式，两者共享同一 DO 实例的存储和计算资源。

### 风险与待确认问题

本方案在实施中需关注以下风险点与待确认问题：

- 并发调度器的性能上限：当前方案依赖 Think DO 的单实例串行 TurnQueue。若异步任务提交量大，RECEIVED 状态任务可能在队列中积压。方案可通过配置最大并发任务数（如单 DO 实例同时仅允许 1 个 RUNNING 任务）来控制内存和计算资源消耗。更高吞吐场景可考虑将调度器与执行 DO 分离为独立的调度 DO。
- idempotency_key 保留窗口与存储增长：idempotency_key 的唯一索引随任务累积持续增长。需实现定期清理机制，删除超过保留窗口且已处于终态的任务记录，同时回收 idempotency_key 索引空间。清理策略需与外部调用方的重试窗口协调。
- 会话级取消与任务级取消的交互：若用户在普通聊天路径通过 clear 操作重置会话，所有排队的异步任务将被 TurnQueue 的 generation 机制跳过（标记为 SKIPPED）。需明确此行为是预期设计还是需要保护异步任务不受会话重置影响。
- 任务结果的回调通知：当前方案以轮询方式提供状态查询。若外部调用方需要推送式通知（如 webhook 回调），需额外设计回调注册、重试和失败处理机制，这不在本方案范围内但可作为后续扩展。
- runFiber 恢复的完整性依赖：方案将推理过程包装在 runFiber 中，恢复的正确性依赖于 onFiberRecovered 钩子对推理中间状态（已生成的 token 流、工具调用结果等）的准确重建。该重建逻辑与 Think 现有的 chat recovery 路径一致，已在 Think 的 chat-recovery e2e 测试中验证，但异步任务场景需补充针对任务状态转换边界的专项测试。
