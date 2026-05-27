## 技术方案

本方案在 Think agent 现有架构基础上，增加一个「外部任务提交层」（External Task Submission Layer），使 webhook、RPC 调用方或其他 Worker 能够以非阻塞方式向 Think agent 提交对话任务，并在获取即时确认后，异步查询、取消或清理该任务。方案完全复用 Think 现有的会话（Session）存储、消息持久化、TurnQueue 串行调度、AbortRegistry 取消机制、ResumableStream 流式缓冲和 Fiber 持久执行等基础设施，不重写普通聊天消息的保存路径。

### 整体架构

外部任务提交层引入一张新的持久化表 `cf_external_tasks`，用于记录从外部系统接收到的每一个提交请求的全生命周期状态。该表与 Think 现有的 `assistant_messages`（会话消息表）、`think_config`（配置表）、`cf_ai_chat_stream_metadata`（流元数据表）和 `cf_ai_chat_stream_chunks`（流数据块表）协同工作，但完全独立于普通 WebSocket 聊天路径的消息写入逻辑。

外部调用方通过 Think agent 的 Durable Object RPC 接口（或新增的 HTTP handler）调用 `submitTask` 方法。该方法在持久化任务记录后立即返回包含 `taskId` 和状态 `received` 的确认响应。随后，系统通过 Think 现有的 `saveMessages` 程序化接口将用户消息注入 Session，触发模型推理循环。推理过程中的流式输出通过 `ResumableStream` 缓冲，外部调用方可随时通过 `getTaskStatus` 查询执行进度和结果。

### 任务提交与即时确认

外部调用方通过 `submitTask(input, options)` 方法提交任务。该方法接受以下参数：（1）`input`：用户消息内容，可以是字符串或 `UIMessage` 对象；（2）`options.idempotencyKey`：由调用方生成的幂等键，用于去重；（3）`options.signal`：可选的 AbortSignal 用于外部取消。方法执行分为两个阶段：持久化阶段和确认阶段。在持久化阶段，系统以 `idempotencyKey` 为主键执行原子插入——若该键已存在且关联任务未处于终态，则直接返回已有 `taskId`；若不存在，则在 `cf_external_tasks` 表中写入一条状态为 `received` 的新记录，同时将消息载荷序列化存储。确认阶段在持久化完成后立即执行：系统返回包含 `taskId`、`status: "received"` 和 `createdAt` 时间戳的响应。整个确认路径不涉及模型推理、TurnQueue 排队或消息写入 Session，因此可以在毫秒级完成。

### 幂等提交与重复请求去重

幂等去重机制依赖于 `cf_external_tasks` 表中 `idempotency_key` 列上的 UNIQUE 约束。当外部系统因超时或网络问题重试同一请求时，`submitTask` 在持久化阶段执行 `INSERT OR IGNORE`（或等效的原子检查-插入操作），发现幂等键已存在，则查询已有关联记录的 `task_id` 和当前状态并返回，不产生新的消息写入或推理执行。

幂等键的保留窗口由 `created_at` 时间戳和可配置的 TTL（默认 24 小时）共同决定。超过保留窗口的历史任务记录通过 `ResumableStream` 已有的定时清理机制（`CLEANUP_INTERVAL_MS`，默认 10 分钟执行一次）统一回收，清理条件为：任务处于终态（completed/aborted/error/failed）且 `created_at` 早于当前时间减去 TTL。

对于幂等键已过期被清理后再次提交相同键的极端情况，系统将其视为全新提交，生成新的 `taskId` 并正常执行。此时旧任务的结果已经不可达，调用方应自行处理重复执行的情况。这种设计在「避免无限期存储开销」和「保证恰好一次语义」之间选择了实用折中。

### 持久状态边界与崩溃恢复

本方案的核心设计之一是「快速确认接收」和「模型推理最终完成」之间的持久状态边界。`cf_external_tasks` 表中的 `status` 列是这一边界的形式化表达，其状态转换如下：

- received：任务已持久化接收，消息载荷已存储，尚未开始调度执行。这是 `submitTask` 返回值中的状态。
- dispatching：任务已从 `received` 状态被任务调度器拾取，正在将消息写入 Session 并入队 TurnQueue。
- running：消息已成功写入 Session，TurnQueue 已接受该请求，模型推理循环正在执行。
- completed：推理正常完成，assistant 消息已持久化，结果可从 `ResumableStream` 查询。
- aborted：任务被外部调用方通过 `cancelTask` 取消。
- error：推理过程中发生不可恢复错误，错误信息存储在 `error_message` 列中。

持久状态边界的关键保证在于：`received → dispatching` 的转换是幂等且可恢复的。系统在 Think 的 `onStart` 生命周期中增加一个恢复步骤：扫描 `cf_external_tasks` 表中所有状态为 `received` 或 `dispatching` 的记录，对每条记录执行基于 `runFiber` 的持久执行调度。这意味着即使在 `submitTask` 持久化完成后、调度器拾取前发生 DO 休眠或崩溃，重启后也能自动恢复未完成的任务。

具体而言，恢复流程为：（1）查询 `WHERE status IN ('received', 'dispatching')` 获取所有未完成任务；（2）对每条任务，以 `task_id` 为 Fiber 名称调用 `runFiber` 执行调度逻辑；（3）在 Fiber 内部，先将状态原子更新为 `dispatching`，再调用 `saveMessages` 将存储的消息载荷注入 Session——从此刻起，该任务复用 Think 现有的全部执行、流式输出和持久化路径；（4）若 `saveMessages` 返回 `status: "skipped"`（因 TurnQueue 代际失效），任务重新进入 `received` 状态等待下一次调度拾取。

### 执行调度与生命周期

任务调度器是 Think 内部的一个轻量组件，负责将 `received` 状态的任务安全地转移到执行管道。它不替换 TurnQueue，而是在 TurnQueue 之前增加一个「领取」环节。

调度器在以下时机触发：（1）`submitTask` 持久化完成后，通过 `this.schedule(0, '_dispatchExternalTask', { taskId })` 注册一个零延迟调度回调，该回调利用 Agent 基类的 `schedule` 机制，天然支持 DO 休眠后的 alarm 唤醒；（2）`onStart` 恢复扫描触发（已在上节描述）。两种触发路径最终汇聚到同一个 `_dispatchExternalTask` 方法，该方法执行以下步骤：

1. 以 `taskId` 为参数查询 `cf_external_tasks`，若当前状态不是 `received` 则直接返回（防止重复调度）；
2. 使用 `UPDATE ... SET status = 'dispatching' WHERE task_id = ? AND status = 'received'` 进行条件更新，利用 SQLite 的行级原子性保证只有一个调度实例能成功领取该任务；
3. 若条件更新影响行数为 0（任务已被其他调度实例领取或状态已变更），直接返回；
4. 调用 `saveMessages` 将任务载荷中的消息写入 Session，并获取返回的 `requestId`；
5. 将 `requestId` 写回 `cf_external_tasks` 表，并将状态更新为 `running`。

进入 `running` 状态后，任务完全由 Think 现有的推理循环接管：TurnQueue 保证串行执行，AbortRegistry 管理取消信号，ResumableStream 缓冲流式输出。推理完成后，`onChatResponse` 生命周期钩子（Think 现有机制）将最终状态（completed/aborted/error）和 assistant 消息 ID 写回 `cf_external_tasks` 表。这一设计确保外部任务提交路径在消息成功写入 Session 之后，与普通 WebSocket 聊天路径完全重合。

### 状态查询、取消与清理

外部调用方通过三个核心方法获取任务状态和控制任务生命周期。

`getTaskStatus(taskId)`：查询 `cf_external_tasks` 表返回任务当前状态、创建时间、完成时间、错误信息（如有），以及可选的流式结果摘要。当任务处于 `running` 或 `completed` 状态时，额外返回 `streamId` 和 `requestId`，调用方可据此通过 `getTaskChunks(taskId, { afterSequence })` 方法获取增量流式输出块。该方法复用 `ResumableStream.getStreamChunks` 的现有实现，以 `chunk_index` 为游标支持断点续传。

`cancelTask(taskId, reason?)`：取消指定任务。实现逻辑为：（1）查询任务状态，若已处于终态则返回；（2）若任务处于 `received` 或 `dispatching` 状态（尚未入队 TurnQueue 或正在入队），直接将状态原子更新为 `aborted`；（3）若任务处于 `running` 状态，通过 `AbortRegistry` 的 `cancel(requestId, reason)` 方法取消对应的推理请求——该机制与 WebSocket 客户端发送 `cf_agent_chat_request_cancel` 消息完全等效；`saveMessages` 返回的 `status: "aborted"` 将在 `onChatResponse` 钩子中被捕获并写回 `cf_external_tasks`。

`deleteTask(taskId)`：清理指定任务及其关联数据。该方法执行级联清理：（1）若任务处于非终态，先调用 `cancelTask` 确保推理停止；（2）删除 `cf_external_tasks` 中的记录；（3）可选地通过 `ResumableStream` 的清理机制移除关联的流数据块；（4）释放占用的幂等键。注意该方法不删除 Session 中已持久化的消息——这些消息属于会话历史的一部分，由 Session 的统一生命周期管理。

此外，提供 `listTasks(options?)` 方法用于批量查询，支持按状态过滤（如仅查询 `running` 或 `received` 状态的任务）和基于 `created_at` 的游标分页，便于外部管理面进行监控和运维。

### 与现有路径的兼容性

本方案的核心设计原则是不重写 Think 现有的任何消息保存或推理执行路径。具体兼容性保证如下：

- 消息保存路径不变：外部任务调度器通过 `saveMessages` 方法将消息注入 Session，与 WebSocket 路径（`_handleChatRequest`）和 RPC 子 agent 路径（`chat()`）使用完全相同的消息持久化入口。Session 的 `appendMessage`、`updateMessage` 和 `getHistory` 方法无任何修改。
- TurnQueue 串行调度不变：外部任务通过 `saveMessages` → `_turnQueue.enqueue` 进入与普通聊天请求相同的串行队列，共享同一个代际失效（generation-based invalidation）机制。当用户通过 WebSocket 执行 `clear` 操作时，TurnQueue 的 `reset()` 会使所有未执行的外部任务一并失效（返回 `status: "skipped"`）。
- AbortRegistry 取消机制不变：外部任务的取消通过现有 `AbortRegistry.cancel(requestId)` 实现，与 WebSocket 客户端的取消路径完全一致。`linkExternal` 方法已支持将外部 AbortSignal 链接到注册表，`submitTask` 直接复用该能力。
- ResumableStream 流式缓冲不变：外部任务执行期间的流式输出由 `ResumableStream` 统一管理，外部调用方的增量查询通过 `getStreamChunks` 实现，与 WebSocket 断线重连时的 chunk 重放共享同一套存储和索引结构。
- Fiber 持久执行不变：外部任务的调度执行使用 `runFiber` 包裹，与 `saveMessages` 和 `_handleChatRequest` 中的 `chatRecovery` 路径保持一致。DO 休眠或崩溃后的恢复由 Fiber 的快照机制自动处理。

唯一的增量是 `cf_external_tasks` 表及围绕它的提交、调度和查询逻辑，这些组件完全独立于 Think 的核心推理循环和消息管理路径。

### 技术效果

本方案带来的技术效果如下：

- 快速确认与异步解耦：外部调用方在提交任务后毫秒级获得确认，不必等待模型推理完成（可能持续数秒到数分钟）。确认路径仅涉及单表插入操作，不经过 TurnQueue 或模型调用。
- 可靠去重：基于幂等键的原子插入机制保证网络重试不会产生重复消息或重复推理，且去重逻辑在消息写入 Session 之前执行，避免了「已确认但消息重复」的中间状态。
- 崩溃恢复全覆盖：`received → dispatching` 的状态转换通过 SQLite 条件更新实现原子领取；未完成任务通过 `onStart` 恢复扫描和 Fiber 持久执行机制在 DO 重启后自动恢复，不丢失任何已接收但未执行的任务。
- 状态可观察：外部调用方可通过 `getTaskStatus` / `listTasks` 随时查询任务生命周期状态和流式输出，实现完整的管理面可观察性。增量 chunk 查询支持游标断点续传。
- 取消与清理可控制：外部调用方可取消任意非终态任务，取消信号通过现有 AbortRegistry 传播到模型推理循环，部分结果仍被持久化。任务清理级联移除关联流数据但不影响会话历史。
- 零路径重写：方案完全在现有 Think 架构之上叠加，不修改 Session 存储、TurnQueue 调度、AbortRegistry 取消、ResumableStream 缓冲或 Fiber 恢复的任何代码路径。

### 关键数据表设计

`cf_external_tasks` 表是方案的核心新增数据结构，其 DDL 如下：

CREATE TABLE IF NOT EXISTS cf_external_tasks (task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'received', request_id TEXT, stream_id TEXT, input_json TEXT NOT NULL, assistant_message_id TEXT, error_message TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, completed_at INTEGER); CREATE INDEX IF NOT EXISTS idx_external_tasks_status ON cf_external_tasks(status, created_at); CREATE INDEX IF NOT EXISTS idx_external_tasks_idempotency ON cf_external_tasks(idempotency_key);

其中 `input_json` 存储序列化的用户消息载荷，避免依赖 Session 表来恢复未调度任务的消息内容。`request_id` 和 `stream_id` 分别关联 TurnQueue 中的请求标识和 ResumableStream 中的流标识，在任务进入 `running` 状态后写入。`assistant_message_id` 在推理完成后写入，关联 Session 中的 assistant 消息。所有时间戳使用毫秒级 Unix 时间。

### 风险与待确认问题

以下为当前设计中的风险和待确认点：

- 并发调度竞态：`_dispatchExternalTask` 依赖 SQLite 的 `UPDATE ... WHERE status = 'received'` 实现原子领取。在 Cloudflare Durable Objects 的单线程模型下，同一 DO 实例不存在真正的并发写入竞争，但 `onStart` 恢复扫描和 `schedule` 回调可能在同一次事件循环中先后触发同一个 taskId。当前方案通过在 `_dispatchExternalTask` 入口处先检查状态、再执行条件更新来防御，但需要测试验证这两种触发路径的时序交错安全性。
- 幂等键 TTL 与任务执行时间的不匹配：当前设计中幂等键的保留窗口（默认 24 小时）是全局配置。如果某个任务执行时间超过该窗口（例如长时间运行的 agent 循环），任务仍在执行中但幂等键可能被清理。建议将 TTL 的起算点从 `created_at` 改为 `completed_at`，确保任务在执行期间幂等键始终有效。
- Fiber 恢复的幂等性：`onStart` 恢复扫描对每个 `received`/`dispatching` 任务启动一个 `runFiber`。如果同一个 taskId 的 Fiber 在崩溃前已部分执行（如已写入消息到 Session 但尚未更新状态），恢复后的 Fiber 会重新调用 `saveMessages`，导致同一条用户消息被写入两次。需要通过检查 `request_id` 是否已存在来避免重复执行——若 `request_id` 非空，则跳过 `saveMessages` 调用，直接进入状态查询和恢复。
- 与现有 clear 操作的交互：当 WebSocket 客户端发送 `cf_agent_chat_clear` 时，TurnQueue 的 `reset()` 会使所有未执行的外部任务返回 `status: "skipped"`。这些任务的 `cf_external_tasks` 记录需要被同步更新为 `aborted`，否则调用方将看到永远处于 `dispatching` 状态的任务。建议在 `resetTurnState` 方法中增加对 `cf_external_tasks` 中 `received`/`dispatching` 记录的批量状态更新。
