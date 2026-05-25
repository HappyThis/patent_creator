## 技术方案

### 技术问题概述

Think agent 当前通过 WebSocket 协议与浏览器客户端交互，或通过 RPC 子代理接口被父代理调用，这两种路径均假设调用方与 agent 维持长连接直至推理完成。当外部系统（如 webhook、RPC 调用方、Worker 定时任务）需要向 Think agent 提交对话任务时，面临以下技术问题：外部调用方无法在提交后立即获得确认并断开连接；网络超时或瞬时故障导致的重试可能造成用户消息重复写入或同一任务被多次执行；调用方缺少可靠的接口查询任务执行状态、取消或清理已提交的任务；agent 在「已接收确认」与「模型推理最终完成」两个时刻之间若发生崩溃或 DO 重启，调用方无法获知任务是否已被持久化接收。

### 整体架构

本方案在 Think agent 现有架构基础上引入外部任务（External Task）抽象层，在调用方与 agent 推理循环之间增加一个持久化的任务状态管理层。系统整体架构如下：

- 外部任务入口层：通过 HTTP 端点（如 POST /tasks）或 RPC 方法接收外部提交，生成任务记录并立即返回 taskId，不等待模型推理完成。
- 任务状态管理层：基于 SQLite 持久化表 cf_agent_external_tasks 记录每个外部任务的生命周期状态，包括幂等键、状态机、关联的 requestId、时间戳和错误信息。
- 任务执行调度层：复用现有 TurnQueue 序列化机制，在任务被确认接收后异步入队执行，执行期间将 requestId 回写到任务记录中供外部查询。
- 状态查询与控制面：提供任务状态查询接口（GET /tasks/:taskId）、取消接口（POST /tasks/:taskId/cancel）和清理接口（DELETE /tasks/:taskId），均通过现有 onRequest HTTP 路由机制实现。

### 外部任务提交与快速确认机制

外部调用方通过 HTTP POST 向 Think agent 的 /tasks 端点提交任务，请求体中携带用户消息内容（或消息数组）和幂等键（idempotency_key）。agent 在 onRequest 路由中识别该路径后，执行以下快速确认流程：

1. 检查幂等键：查询 cf_agent_external_tasks 表中是否存在相同 idempotency_key 且未过期的记录。若存在，直接返回已有记录的 task_id 和当前状态，不重复创建任务。
2. 创建任务记录：在 cf_agent_external_tasks 表中插入新记录，字段包括 task_id（系统生成的 UUID）、idempotency_key、status='accepted'、created_at 时间戳，以及序列化后的用户消息内容。
3. 写入用户消息：调用 session.appendMessage 将用户消息持久化到 Session 的消息存储中。由于 Session 和任务表位于同一 DO SQLite 实例中，此写入与任务记录创建在同一事务上下文中完成，保证原子性。
4. 立即返回确认：向调用方返回 HTTP 202 Accepted，响应体包含 task_id 和 status='accepted'。此时调用方可断开连接。
5. 异步入队执行：调用 saveMessages 的等效逻辑将任务入队到 TurnQueue。入队成功后将任务记录中的 status 更新为 'running'，并回写关联的 requestId。

### 幂等性保障与重复提交去重

幂等性通过 cf_agent_external_tasks 表中的 idempotency_key 字段实现，机制如下：

- 唯一约束：idempotency_key 列具有唯一索引（或在查询时进行存在性检查）。同一幂等键在同一保留窗口内只能对应一条任务记录。
- 保留窗口：每条任务记录在创建时记录 created_at 时间戳。系统维护一个可配置的幂等保留窗口（例如 24 小时），超过该窗口的历史 idempotency_key 视为过期。查询幂等键时附加 WHERE created_at > (now - retention_window) 条件，过期记录不参与去重匹配。
- 重复请求处理：当外部调用方因超时或网络错误重试同一请求时，系统在步骤 1 中命中已有幂等键，返回现有 task_id 和当前状态，不执行后续的消息写入和入队操作。这同时避免了重复插入用户消息和重复执行推理任务。
- 与崩溃恢复的协作：如果 agent 在「写入任务记录」之后、「写入用户消息」之前崩溃，DO 重启后查询到该幂等键对应的任务记录状态为 'accepted' 但消息实际未写入。系统在恢复时检测到此不一致状态，可选择补写消息后继续执行，或将任务标记为 'error'。详细恢复逻辑见「崩溃恢复」章节。

### 任务状态持久化与追踪

cf_agent_external_tasks 表是外部任务状态管理的核心持久化结构，其 DDL 如下：

CREATE TABLE IF NOT EXISTS cf_agent_external_tasks (task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('accepted','running','completed','error','aborted','cancelled')), request_id TEXT, user_message_json TEXT NOT NULL, error_message TEXT, created_at INTEGER NOT NULL, completed_at INTEGER); CREATE INDEX IF NOT EXISTS idx_ext_tasks_idempotency ON cf_agent_external_tasks(idempotency_key, created_at);

状态机转换规则如下：

- accepted：任务记录已创建且用户消息已持久化，尚未入队到 TurnQueue。这是外部调用方收到 202 响应时任务所处的状态。
- running：任务已被 TurnQueue 接收并开始执行推理。request_id 字段在此时回写，外部调用方可通过 requestId 关联到底层推理的执行。
- completed：模型推理正常完成，assistant 消息已持久化到 Session。completed_at 记录完成时间戳。
- error：推理过程中发生未恢复的错误。error_message 字段记录错误详情。
- aborted：任务被外部调用方通过取消接口主动取消，或通过 AbortSignal 机制被中止。
- cancelled：任务被外部调用方请求取消且尚未开始执行时直接转换到此状态（区别于 aborted：cancelled 表示从未进入 running，aborted 表示推理中途被中止）。

状态查询通过 GET /tasks/:taskId 端点实现。agent 的 onRequest 方法在识别该路径后，从 cf_agent_external_tasks 表读取记录，将 task_id、status、created_at、completed_at、error_message 等字段以 JSON 形式返回。若任务正在执行中（status='running'），额外返回关联的 requestId 以便调用方进一步追踪。

### 任务取消与清理

取消操作：外部调用方向 POST /tasks/:taskId/cancel 发送请求。系统从 cf_agent_external_tasks 表读取任务记录，根据当前状态执行不同策略：

- 若 status='accepted'（尚未入队）：直接将状态更新为 'cancelled'，设置 completed_at。由于任务尚未进入 TurnQueue，无需操作 AbortRegistry。
- 若 status='running'（推理进行中）：通过 requestId 关联到 AbortRegistry，调用 abortRequest(requestId) 取消正在执行的推理。AbortRegistry 的 cancel 方法触发 AbortController.abort()，推理循环中的 abortSignal 随之触发，导致 streamText 中止。推理循环的 finally 块将任务状态更新为 'aborted' 并设置 completed_at。
- 若 status 已是终态（completed/error/aborted/cancelled）：返回当前状态，不执行任何操作。

清理操作：外部调用方向 DELETE /tasks/:taskId 发送请求。系统从 cf_agent_external_tasks 表中删除对应记录。清理仅删除任务追踪元数据，不删除已持久化到 Session 中的对话消息，以保持消息历史的完整性。若任务当前处于 'running' 状态，先执行取消操作再删除记录。

### 崩溃恢复与持久状态边界

本方案的关键设计在于明确「快速确认接收」与「模型推理最终完成」之间的持久状态边界，确保在任意时刻发生崩溃或 DO 重启后，系统可以一致地恢复。

持久状态边界定义为：外部调用方收到 HTTP 202 响应的时刻，此时以下数据已同步写入 DO 的 SQLite 存储：(1) cf_agent_external_tasks 表中存在 status='accepted' 的任务记录；(2) 用户消息已通过 session.appendMessage 写入 Session 的消息表。两个写入操作共享同一 DO 存储事务边界，要么同时成功，要么同时失败。

崩溃恢复按崩溃发生的时刻分为以下场景：

- 崩溃发生在任务记录写入前：外部调用方收到 HTTP 错误（而非 202），不会产生孤儿任务。调用方可安全重试，幂等键尚未写入，重试将被视为新任务。
- 崩溃发生在任务记录写入后、用户消息写入前：DO 重启后，onStart 生命周期中执行恢复扫描：查询 status='accepted' 且无对应 Session 消息的任务记录。对于此类记录，检查对应的 user_message_json 字段中保存的消息内容，补写用户消息到 Session，然后将任务继续入队执行。若 user_message_json 也损坏，则将任务标记为 'error'。
- 崩溃发生在用户消息写入后、入队执行前：DO 重启后，任务记录 status 仍为 'accepted'，但用户消息已存在于 Session 中。恢复扫描检测到消息已存在，直接将任务入队到 TurnQueue 并更新状态为 'running'。
- 崩溃发生在推理执行中（status='running'）：利用 Think 现有的 chatRecovery 机制（基于 runFiber 的持久执行），当 fiber 恢复时，_handleInternalFiberRecovery 被触发。系统通过 requestId 关联回 task 记录，若推理最终完成，状态更新为 'completed'；若无法恢复，更新为 'error' 并记录原因。

恢复扫描在 onStart 中执行，与现有 Session 初始化、ResumableStream 重建等逻辑并行。恢复扫描仅处理 status 为 'accepted' 或 'running' 的非终态任务，已完成的终态任务不参与恢复。

### 与现有系统的兼容性

本方案在以下方面与 Think agent 现有架构保持兼容，不重写现有路径：

- 消息持久化路径不变：用户消息仍然通过 session.appendMessage 写入 Session，assistant 消息仍然通过 _persistAssistantMessage 写入。外部任务仅增加了一层任务元数据追踪，不修改 Session 的表结构或写入逻辑。
- TurnQueue 序列化语义复用：外部任务入队执行时使用与现有 saveMessages 和 WebSocket chat-request 相同的 TurnQueue.enqueue 机制，保证推理任务按提交顺序串行执行。messageConcurrency 配置同样适用于外部任务。
- AbortRegistry 取消机制复用：外部任务的取消通过现有 abortRequest(requestId) 方法实现，与 WebSocket chat-request-cancel 走相同的取消路径，使用相同的 AbortController 和 abortSignal 机制。
- chatRecovery 恢复机制复用：推理执行中的崩溃恢复利用现有 runFiber 和 _handleInternalFiberRecovery 机制，外部任务仅需将 fiber 恢复结果回写到 cf_agent_external_tasks 表。
- HTTP 路由复用：外部任务的所有 HTTP 端点（POST/DELETE /tasks、GET /tasks/:taskId 等）均通过现有 onRequest 方法的路由分支实现，与已有的 /get-messages 端点共存。
- 普通聊天路径不受影响：WebSocket 协议路径、sub-agent RPC chat() 调用、saveMessages 编程接口的行为保持不变。外部任务系统仅在被显式调用时介入，不改变任何现有调用路径的语义。

### 技术效果

本方案通过在 Think agent 现有架构上增加外部任务抽象层，取得以下技术效果：

- 快速确认与解耦：外部调用方提交任务后在毫秒级获得 taskId 确认，无需等待模型推理完成（可能耗时数十秒至数分钟），实现调用方与推理执行的完全解耦。
- 可靠的重试安全：基于幂等键的去重机制确保网络重试不会产生重复用户消息或重复推理任务，系统在数据库层面保证同一幂等键的任务唯一性。
- 明确的持久状态边界：任务记录创建与用户消息写入在同一 DO 存储事务中完成，外部调用方收到 202 响应的时刻即为持久化完成时刻。崩溃恢复时通过恢复扫描处理所有非终态任务，保证「已确认接收」的任务最终必然达到终态。
- 可观察的状态追踪：外部调用方通过 taskId 随时查询任务状态，了解任务是正在执行、已完成、已取消还是出错，无需维持长连接。
- 可控的生命周期管理：支持外部调用方主动取消尚未开始或正在执行的任务，以及清理已完成任务的状态追踪记录。
- 现有路径零侵入：消息持久化、TurnQueue 序列化、AbortRegistry 取消、chatRecovery 崩溃恢复等核心机制均被复用而非重写，普通聊天、WebSocket 交互和 RPC 子代理调用的行为不受影响。

### 风险与待确认事项

以下为当前方案中需要后续确认的风险点和技术决策项：

- 幂等保留窗口的默认值：建议默认 24 小时，需根据实际业务场景确认合适的窗口大小。窗口过大可能导致 cf_agent_external_tasks 表膨胀，需配合定期清理机制。
- 恢复扫描的性能影响：若 onStart 中积压大量 'accepted' 状态任务，恢复扫描可能延长 DO 启动时间。建议限制单次扫描处理的任务数量上限，超出部分通过调度后续 alarm 分批处理。
- 任务清理策略：DELETE /tasks/:taskId 仅删除任务元数据，不删除已持久化的对话消息。若业务需要同时清理关联消息，需额外设计消息级联清理逻辑。
- 并发提交的幂等键竞争：两个并发请求携带相同幂等键同时到达时，可能出现两者均未查到已有记录的情况。需通过 SQLite 的 INSERT OR IGNORE + 唯一索引，或通过行级锁确保只有一个请求成功创建任务记录。
- 与现有 startAgentToolRun 机制的关系：现有 cf_agent_tool_child_runs 表用于追踪 agent 工具子运行，其设计与 cf_agent_external_tasks 相似但语义不同。后续可考虑统一两者的状态追踪抽象，但不属于本方案范围。
