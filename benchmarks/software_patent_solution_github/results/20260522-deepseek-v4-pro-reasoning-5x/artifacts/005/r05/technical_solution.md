## 技术方案

本技术方案在 Think agent 现有架构基础上，引入一套面向外部系统（如 webhook、RPC 调用方、其他 Worker）的任务提交、追踪与控制机制。核心思路是将一次外部触发的 agent 对话任务拆分为两个阶段：(1) 接收阶段——外部调用方提交任务后快速获得“已接收”确认，不等模型推理完成；(2) 执行阶段——系统异步调度推理 turn，将最终结果写入持久化存储。方案通过引入任务状态持久化表、外部幂等键去重、与现有 TurnQueue 和 Durable Object 持久化恢复机制的深度集成，在不大幅重写普通聊天消息保存路径的前提下，实现可靠的任务提交、状态查询、取消、清理以及崩溃后自动恢复。

### 核心方案：两阶段任务提交模型

当前 Think agent 通过 saveMessages() 或 chat() RPC 接收外部程序化调用时，调用方必须同步等待整个推理 turn 完成才能获得返回结果。对于 webhook 或其他 Worker 触发场景，推理可能持续数十秒甚至数分钟，这导致调用方连接超时、资源占用过高。本方案将外部提交与推理执行解耦为两个独立阶段。

第一阶段（接收确认）：外部调用方通过新增的 submitTask RPC 接口提交任务，携带客户端生成的幂等键（idempotency_key）和任务载荷（如用户消息文本）。系统在 Durable Object 的 SQLite 事务中执行幂等检查——若 idempotency_key 已存在则直接返回已有任务标识；否则原子性地插入一条状态为 accepted 的任务记录，并立即向调用方返回包含 task_id 的确认响应。整个过程在数毫秒内完成，不触发任何模型推理。

第二阶段（异步执行）：确认响应返回后，系统在同一个 Durable Object 实例内将任务排入 TurnQueue。TurnQueue 按 FIFO 顺序取出任务，将其状态更新为 running，通过已有 saveMessages 路径将载荷消息持久化到 Session 消息树中，然后触发标准推理循环。推理完成后，最终状态（completed/failed/aborted）及结果被回写到任务记录中。两阶段之间由 Durable Object 的 SQLite 持久化存储作为状态边界——即使 DO 实例在确认返回后、推理完成前发生休眠或重启，任务记录也不会丢失。

### 任务状态持久化与幂等去重

方案的持久化核心是一张新增的任务状态记录表 task_runs，其设计与 Think 现有的 cf_agent_tool_child_runs 表模式一致。表结构关键列包括：task_id（主键，服务端生成的 UUID）、idempotency_key（UNIQUE 约束，客户端提供的幂等键）、status（任务状态枚举值）、request_id（关联到 TurnQueue 请求的标识）、epoch（关联的 TurnQueue 世代号）、payload（序列化的任务载荷）、result（推理完成后的结果摘要）、error_message（失败原因）、created_at、started_at、completed_at。

状态机定义如下：accepted（已接收，尚未排入执行队列）→ queued（已排入 TurnQueue 等待执行）→ running（推理进行中）→ completed（成功完成）| failed（执行异常）| cancelled（外部取消）。其中 accepted 是快速确认后的初始状态，queued 是 TurnQueue.enqueue 调用成功后的过渡状态。

幂等去重依赖 idempotency_key 列的 UNIQUE 约束。外部调用方在提交任务时生成一个稳定的幂等键（例如基于业务事件 ID 的哈希值）。submitTask 接口在 SQLite 事务中首先执行 SELECT 查询检查该键是否已存在：若存在且对应任务已完成或正在执行，则直接返回已有的 task_id 和当前状态；若不存在，则在同事务中 INSERT 新行并返回新 task_id。这一机制确保外部系统因超时或网络问题重试同一请求时，不会在消息树中重复插入用户消息，也不会触发重复的模型推理。该幂等键独立于消息 ID 的 INSERT OR IGNORE 去重，工作在任务提交层面而非消息存储层面。

payload 列存储外部调用方提交的原始任务内容（如用户消息文本、结构化输入参数等）。为避免 SQLite 行大小限制影响可靠性，payload 在写入前进行大小校验，超限时拒绝提交并返回错误。对于大体积输入，调用方可使用外部存储引用替代内联传递。

### 异步推理执行与 TurnQueue 集成

任务从 accepted 状态到最终完成的执行路径完全复用 Think 现有的 TurnQueue 序列化机制和推理管道，避免引入新的并发控制模型。TaskExecutionBridge（推理桥接层）是连接任务记录与推理 turn 的关键组件。

调度流程：submitTask 在 SQLite 事务提交后，调用 TurnQueue.enqueue() 将任务加入序列化队列。enqueue 调用时记录当前 TurnQueue 的 generation 值作为任务的 epoch。任务到达队列前端后：(1) 检查 TurnQueue 当前 generation 是否与任务记录的 epoch 一致——不一致说明会话已被清空或重置，任务标记为 failed(expired) 并跳过执行；(2) 更新 task_runs 状态为 running，记录 request_id；(3) 通过 AbortRegistry 为该 request_id 分配 AbortController，并通过 linkExternal 链接调用方可选的 AbortSignal；(4) 调用 Session.appendMessage 将载荷消息持久化到消息树——该步骤复用消息级 INSERT OR IGNORE 幂等；(5) 触发推理循环（streamText），运行完整的 agentic loop。

推理完成后的回调处理：推理成功（status=completed）时将结果摘要写入 result 列；推理异常（status=failed）时将错误信息写入 error_message 列；外部取消（status=cancelled）时保留已产生的部分结果（如有）。所有完成路径均执行 AbortRegistry.remove() 清理控制器资源，并写入 completed_at 时间戳。

并发控制：任务 turn 与普通聊天 turn 共享同一个 TurnQueue 实例，天然保证 FIFO 顺序执行。当 messageConcurrency 配置为非 queue 策略（如 drop、debounce）时，任务 turn 始终按 queue 策略处理——即 submitTask 路径不受 SubmitConcurrencyController 的 decide() 裁决影响。这确保了外部提交的任务不会被丢弃或合并，同时普通聊天的交互体验不受任务执行干扰。

### 外部控制接口：状态查询、取消与清理

方案通过新增三个 RPC 接口暴露外部控制能力，均通过 Durable Object 的 RPC 通道（与现有 chat() 子 agent 调用路径一致）提供服务。

getTaskStatus(task_id)：读取 task_runs 表中对应 task_id 的 status、error_message、started_at、completed_at 等字段并返回。该接口是只读查询，不修改任何状态。外部调用方可通过轮询此接口获知任务是否完成，也可在获得 completed 状态后进一步通过已有的 getMessages() 接口获取完整对话结果。

cancelTask(task_id)：取消一个已提交但尚未完成的任务。根据任务当前状态采取不同动作：(1) 状态为 accepted 或 queued——直接将状态标记为 cancelled，对应 TurnQueue 的 enqueue 回调在执行前检查到 cancelled 状态后跳过推理；(2) 状态为 running——通过 AbortRegistry.cancel(request_id) 触发推理中止，该中止信号沿现有路径传播至 streamText 的 abortSignal，使正在进行的 LLM 调用和工具执行安全终止。cancelTask 不删除任务记录和已持久化的消息，保留审计追溯能力。

cleanupTask(task_id)：清理已完成（completed、failed、cancelled）任务的大体积数据。将 payload 和 result 列置空，释放 SQLite 存储空间，但保留 task_id、idempotency_key、status 和时间戳等元数据用于去重和审计。对于仍在执行中的任务，该接口返回错误并拒绝清理。

### 崩溃恢复与持久化保障

Durable Object 在无请求时会休眠（hibernate），内存状态丢失，仅 SQLite 数据保留。本方案利用 Think 已有的 runFiber 持久化执行机制和 ResumableStream 流式缓冲，保证外部提交的任务在 DO 休眠、重启或意外崩溃后仍能继续执行或正确标记终态。

休眠唤醒后的恢复流程：DO 实例在 onStart 生命周期中扫描 task_runs 表中 status=running 的所有行。对每一行：(1) 通过 request_id 检查 AbortRegistry 中是否存在对应的 AbortController——DO 休眠后内存中的 AbortController 已丢失，因此不存在；(2) 通过 epoch 值校验 TurnQueue 当前 generation——若 generation 已变更（用户清空了会话），则标记任务为 failed(expired)；(3) 若 generation 匹配，则启动 runFiber 重新包裹该任务的推理执行。runFiber 内部首先从 ResumableStream 的 SQLite chunk 表中检查是否存在已缓冲的流式输出——若有，说明推理在流式输出阶段中断，可通过 continueLastTurn 从断点继续；若无，说明中断发生在推理开始前，则重新触发完整的推理 turn。

消息写入前后的崩溃保护：在第一阶段（确认接收），submitTask 在单个 SQLite 事务中完成幂等检查和 INSERT——事务的原子性保证不会出现“确认返回但任务记录未写入”或“任务记录已写入但确认未返回”的不一致。在第二阶段（推理执行），任务记录的状态从 queued 到 running 的更新发生在 TurnQueue.enqueue 回调内部——若更新 running 后、推理开始前崩溃，DO 唤醒后扫描到 running 状态即触发恢复；若推理进行中崩溃，runFiber 的 checkpoint（通过 ctx.stash）和 ResumableStream 的 chunk 缓冲共同保证可从最近断点恢复。

与普通聊天的恢复路径隔离：任务恢复仅扫描 task_runs 表中 status=running 的行，不影响通过 chatRecovery 路径恢复的普通聊天 turn（后者通过 cf_agents_runs 表中的 fiber 记录恢复）。两条恢复路径独立运作，互不干扰。

### 与普通聊天路径的兼容设计

方案的核心设计原则之一是：不重写、不侵入普通聊天的消息保存路径。普通聊天用户通过 WebSocket 发送消息、通过 useAgentChat 交互的流程完全不变。

具体兼容措施：(1) submitTask 路径内部调用 Session.appendMessage 写入载荷消息——这是普通聊天路径和 WebSocket 路径也使用的同一方法，消息以相同的 INSERT OR IGNORE 语义写入 assistant_messages 表，与普通聊天消息共用同一棵树形结构和 FTS5 索引；(2) 任务触发的推理 turn 与普通聊天 turn 共享同一个 TurnQueue，通过 generation-based invalidation 统一管理；(3) 任务推理完成后，assistant 响应消息的持久化走相同的 sanitizeMessage、enforceRowSizeLimit、StreamAccumulator 管道，与 WebSocket 触发的 turn 完全一致；(4) task_runs 表是独立新增的表，不修改现有 assistant_messages、assistant_compactions、assistant_fts、think_config 等表的 schema；(5) 现有的 clearMessages() 操作在清空消息树时会同时递增 TurnQueue generation，导致所有未执行的任务因 epoch 不匹配而标记为 failed(expired)，逻辑自洽。

多入口统一：无论是通过 WebSocket 的普通用户消息、通过 chat() RPC 的子 agent 调用，还是通过 submitTask 的外部任务提交，最终都汇入同一推理管道——区别仅在于入口处是否经过 task_runs 的两阶段包装。这种设计使得外部任务提交能力作为现有系统的增量扩展而非替代，避免了为支持外部触发而引入独立工作流引擎或重写整个消息系统的工程风险。

### 技术效果

本方案在不大幅重写现有系统的前提下，带来以下技术效果：

(1) 快速确认与异步解耦：外部调用方在数毫秒内获得提交确认，不再受模型推理耗时（数十秒至数分钟）阻塞。调用方连接可立即释放，避免了长连接超时和资源占用问题。

(2) 可靠去重：通过 idempotency_key 的 UNIQUE 约束 + SQLite 事务原子性，在任务提交层面实现精确一次语义。外部系统重试同一请求时，不会产生重复消息或重复推理，解决了仅依赖消息 ID 去重无法覆盖跨请求重试场景的局限。

(3) 崩溃安全：任务状态记录在 SQLite 中持久化，结合 TurnQueue generation 校验和 runFiber 恢复机制，保证 DO 休眠、重启或意外崩溃后未完成任务能被扫描并恢复执行或正确标记终态。

(4) 统一调度与兼容：任务 turn 与普通聊天 turn 共享同一个 TurnQueue 和推理管道，无需引入独立调度器。消息持久化、流式传输、取消语义均沿现有路径，不产生两份代码的分叉维护负担。

(5) 完整的生命周期管理：外部调用方可通过 RPC 接口查询任务状态、取消未完成任务、清理已完成的冗余数据，形成从提交到最终清理的闭环。

### 风险与待确认问题

(1) idempotency_key 的过期策略：长期保留所有幂等键会使 task_runs 表无限增长。可考虑为 completed/failed/cancelled 状态的任务记录设置 TTL（如 30 天），超期后由后台清理任务自动移除，但 idempotency_key 的去重窗口也随之关闭。需根据业务场景选择固定保留窗口或可配置 TTL。

(2) 单实例幂等范围：idempotency_key 的 UNIQUE 约束仅在单个 Durable Object 实例内有效。若多个 DO 实例各自独立接收任务（如按用户 ID 路由），则同一幂等键在不同实例间不会冲突。若业务需要全局幂等，需在路由层以上引入额外协调机制。当前设计接受实例级幂等语义。

(3) 任务取消的语义粒度：cancelTask 通过 AbortRegistry 触发中止信号，该信号沿 streamText → LLM provider 路径传播。若 LLM 调用已返回但工具执行仍在进行，中止行为取决于 AI SDK 的具体实现。需要在实际集成中验证 cancel 对处于工具执行阶段的任务是否完全生效。

(4) payload 大小与 SQLite 行限制：Durable Object SQLite 有约 2MB 的行大小上限。本方案参照 enforceRowSizeLimit 的做法，在写入前校验 payload 大小。超过阈值的任务提交被拒绝，调用方需改用外部存储引用（如 R2 key）传递大体积输入。阈值建议设为 1MB，保留充足余量。

(5) 任务堆积与调度公平性：外部系统可能短时间内提交大量任务，全部进入 TurnQueue 后可能阻塞普通聊天用户的交互。可考虑引入任务专用队列与普通聊天队列分离，或在 TurnQueue.enqueue 时对任务类提交施加速率限制。当前方案先采用共享队列设计，待实际负载数据确认后再优化。
