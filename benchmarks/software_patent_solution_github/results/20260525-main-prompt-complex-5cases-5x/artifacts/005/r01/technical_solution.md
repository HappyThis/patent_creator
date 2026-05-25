## 技术方案

本方案在现有 Think agent 的会话、消息持久化、流式推理、恢复与取消语义之上，新增一层面向外部调用方的异步任务提交与追踪机制。外部系统（如 webhook、RPC 调用方或其他 Worker）可通过新增的任务提交接口向 Think agent 提交一次对话任务，并立即获得「已接收」确认，无需等待模型推理完成。外部调用方随后可通过任务标识符查询执行状态，必要时取消或清理该任务。系统在遭遇崩溃或 eviction 后可通过持久化状态恢复未完成的任务执行。

### 整体架构

本方案在 Think agent 现有架构之上引入三个新增组件：（1）任务记录表（task_records），用于持久化存储每次外部提交任务的元数据与状态；（2）任务提交接口，作为外部调用方的统一入口，完成接收、去重、快速确认与异步调度；（3）任务管理接口，提供状态查询、取消和清理能力。这三个组件复用 Think agent 已有的 Durable Object SQLite 存储、runFiber 持久化执行、AbortRegistry 取消控制、TurnQueue 串行化调度，以及会话消息的 INSERT OR IGNORE 幂等写入机制。

### 任务提交与快速确认机制

外部调用方通过任务提交接口（例如 Think agent 上新增的 submitTask RPC 方法或 HTTP 端点）提交一次对话任务。提交请求至少携带：待发送的用户消息内容、目标会话标识符（可选，不提供时自动创建新会话）、以及调用方生成的幂等键（idempotency key）。

提交处理流程分为两个阶段。第一阶段（同步持久化与快速确认）：系统首先检查 task_records 表中是否已存在相同幂等键的记录。若存在且任务尚未进入终态，直接返回已有任务记录的标识符与当前状态；若已处于终态（已完成、已失败、已取消），则根据调用方意愿决定返回已有结果或创建新任务。若不存在相同幂等键的记录，系统在同一 SQLite 事务中依次执行：在 task_records 表中插入一条状态为「received」的新任务行；调用 Session 的消息追加接口（底层为 INSERT OR IGNORE，天然幂等）将用户消息写入会话消息表。事务提交后，系统立即向调用方返回任务标识符和「received」状态，无需等待模型推理。此时任务记录的持久化已经完成——即使在此之后发生 DO eviction，任务记录和用户消息均已安全落盘。

第二阶段（异步调度与执行）：快速确认返回后，系统通过 runFiber 将任务执行包装为持久化纤程（fiber）。纤程内部调用 Think 现有的 onChatMessage 推理管线——组装上下文、调用 LLM、流式输出、持久化助手消息。任务状态在纤程生命周期中依次流转：received → executing → completed（或 failed）。纤程利用 ctx.stash() 在关键检查点（如消息已写入、推理已开始）持久化中间状态。若 DO 在执行期间被 eviction，系统在下次激活时通过 onFiberRecovered 钩子读取最后一次 stash 的快照，并基于快照决定继续执行或标记失败。恢复路径复用 Think 已有的 ResumableStream 机制，可将已缓冲的流式块重放给重连的观察者。

### 重复提交去重与幂等性

重复提交去重通过 task_records 表上的幂等键唯一约束实现。外部调用方在每次提交时携带一个由自身生成的幂等键（例如 UUID 或业务流水号）。task_records 表对幂等键建立 UNIQUE 索引。当调用方因超时或网络问题重试同一请求时，再次提交相同幂等键的请求会触发唯一约束冲突，系统捕获该冲突后查询已存在记录的当前状态并返回，而非重复插入用户消息或创建新的执行纤程。这保证了即使外部系统重试 N 次，会话中也仅出现一条用户消息，且仅执行一次模型推理。

幂等键的有效期与任务记录的保留策略关联。系统为幂等键设置保留窗口（如 24 小时），超过保留窗口的已完成任务记录可被清理，之后相同幂等键的提交将被视为新任务。对于仍在执行中或已取消但未过保留期的任务，幂等键继续生效。消息写入路径的去重由 Session 层的 INSERT OR IGNORE 提供第二道防线：即使用户消息以相同 ID 被重复写入，SQLite 也会忽略后续重复行。

### 崩溃恢复边界

消息写入与任务记录插入在同一 SQLite 事务中完成，保证原子性——要么用户消息和任务记录同时落盘，要么都不落盘。若系统在事务提交前崩溃，数据库回滚到事务前状态，外部调用方收到错误后可安全重试（携带相同幂等键）。若系统在事务提交后、纤程启动前崩溃：任务记录和用户消息均已持久化。DO 下次激活时，onStart 或 onFiberRecovered 逻辑扫描 task_records 表中状态为 received 的行，自动为其创建纤程并开始执行。若系统在纤程执行期间崩溃：纤程行（cf_agents_runs 表）和最后一次 stash 的快照均在 SQLite 中。DO 下次激活时，onFiberRecovered 被调用，快照中指出当前执行阶段（如「消息已写入，推理尚未开始」或「推理已开始，流式块已缓冲至第 N 块」），恢复逻辑据此决定是重启推理、从检查点继续，还是标记任务失败。

### 状态查询接口

外部调用方可通过任务标识符调用状态查询接口（例如 getTaskStatus RPC 方法）。查询接口读取 task_records 表中对应行的状态字段，返回：当前状态（received / executing / completed / failed / cancelled）、创建时间、完成时间（若已完成）、以及可选的执行摘要（如助手回复的前若干字符）。若任务正在执行中，查询接口还可通过 TurnQueue 和纤程状态判断当前是否活跃、已排队位置等信息。

### 取消与清理

外部调用方可通过 cancelTask 接口请求取消一个正在执行或尚未开始执行的任务。取消处理分两种情况：若任务尚在 TurnQueue 中排队（状态为 received），系统直接从队列中移除该任务，将状态更新为 cancelled，释放相关资源。若任务正在执行中（状态为 executing），系统通过 AbortRegistry 获取该任务对应的 AbortController 并触发 abort()——这与现有 WebSocket 客户端发送 cf_agent_chat_request_cancel 走相同的取消路径。abort 信号沿 AbortSignal 链传播至 LLM 提供商的 HTTP 请求，导致推理中断。Think 现有的错误处理管线将部分助手消息持久化后，将任务状态更新为 cancelled。

清理接口（deleteTask）允许外部调用方删除任务记录，并可选地清除关联的会话消息。清理操作复用 Think 现有的 clear 管线——递增 TurnQueue 的 generation 计数器以失效所有排队中的轮次、通过 AbortRegistry.destroyAll 中止所有进行中的请求、清除 ResumableStream 状态、删除会话消息。清理后 task_records 表中对应行被标记为 deleted 或物理删除。幂等键在清理后进入冷却期，冷却期内相同幂等键的提交被拒绝，防止因异步系统中延迟到达的重试请求导致误创建新任务。

### 与现有系统的兼容关系

本方案通过复用而非重写实现与现有系统的兼容。任务提交接口内部调用的是 Think 已有的消息持久化路径（Session.appendMessage → INSERT OR IGNORE）和推理管线（onChatMessage → streamText → 流式输出 → 助手消息持久化），不修改这些路径的任何逻辑。普通聊天用户通过 WebSocket 提交的消息完全不受影响——它们不经过 task_records 表，不走纤程包装，保持原有的同步请求-流式响应模式。取消路径通过 AbortRegistry.linkExternal 将外部调用方的 AbortSignal 链接到内部 AbortController，使用与 WebSocket 取消完全相同的底层机制。消息保存、流式块缓冲（ResumableStream）、轮次排队（TurnQueue）、并发控制（SubmitConcurrencyController）均保持原样。唯一的增量是 task_records 表和新接口方法，它们作为独立层叠加在现有架构之上。

### 技术效果

本方案带来的技术效果包括：（1）「提交-确认」分离：外部调用方提交任务后立即获得持久化确认，与模型推理耗时解耦，避免了长时间 HTTP 连接的超时风险和资源占用；（2）端到端幂等：幂等键 + SQLite 唯一约束 + 消息 INSERT OR IGNORE 三道防线，确保无论外部系统重试多少次，系统内部仅执行一次有效工作；（3）崩溃安全：任务记录与用户消息在同一事务中原子写入，纤程机制通过 stash 检查点 + onFiberRecovered 恢复钩子覆盖事务提交后到推理完成之间的所有崩溃窗口；（4）取消一致性：外部取消与 WebSocket 客户端取消共用 AbortRegistry 机制，取消后部分推理结果按现有逻辑持久化，不产生孤立状态；（5）架构兼容：不重写消息保存、流式推理、恢复、取消等现有路径，普通聊天用户的使用模式完全不受影响；（6）可观察性：外部调用方通过任务标识符可随时查询任务生命周期状态，支持构建上层编排逻辑（如重试策略、超时告警、结果回调等）。

### 任务记录表与状态机

task_records 表是方案的核心持久化结构，在 Think agent 已有的 SQLite 存储中创建，与 assistant_messages、think_config、cf_agents_runs 等表并存。表结构包含：task_id（主键，UUID）、idempotency_key（唯一索引，外部调用方提供）、session_id（关联的会话标识符）、status（枚举：received / executing / completed / failed / cancelled）、fiber_id（关联的纤程标识符）、request_payload（提交时的消息内容 JSON）、result_summary（完成后填充的摘要文本，可为空）、created_at、updated_at、completed_at。其中 idempotency_key 上的唯一索引是实现重复提交去重的数据库层保障。

任务状态机遵循严格的单向转换规则：received → executing（纤程开始执行时写入）；executing → completed（推理成功完成且助手消息持久化后写入）；executing → failed（推理抛出不可恢复异常时写入）；received → cancelled（排队期间被取消时写入）；executing → cancelled（执行期间被取消时写入，底层 abort 信号传播完成后写入）。completed 和 failed 为终态；cancelled 也为终态，但关联的幂等键在保留窗口内仍受保护。状态写入与纤程 stash 检查点之间保持因果一致：先 stash 当前阶段，再更新 task_records 状态，确保恢复时快照与状态不矛盾。

### 风险与待确认问题

以下方面有待进一步确认和细化：（1）幂等键保留窗口的具体时长需要根据业务场景确定，过短可能导致重复执行，过长会积累废弃记录；（2）并发提交场景——当多个外部调用方以不同幂等键同时向同一会话提交任务时，TurnQueue 的默认串行策略是否会成为瓶颈，是否需要引入会话级并发控制策略（如 queue / merge / drop）；（3）任务执行结果回调机制——本方案当前仅提供轮询式状态查询，是否需要新增 webhook 回调（任务完成时主动通知外部调用方）取决于上层业务需求；（4）多会话支持——当前 Think agent 基于 SessionManager 提供多会话能力，任务提交接口与会话生命周期的绑定关系需要进一步明确（例如是否允许向已归档或已 compact 的会话提交新任务）。
