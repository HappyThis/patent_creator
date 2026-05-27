## 技术方案

本方案提出一种面向多 Agent 协作环境的统一执行记录契约机制，将 Agent 任务从调度、执行、完成到评估的全生命周期事件，以结构化、可查询、可追踪、可附着评估结果的形式持久沉淀为执行记录。该契约不是新增一张普通日志表，而是对系统中已有的 tasks、spawn_history、token_usage、quality_reviews、activities、audit_log 等分散执行数据建立统一关联模型，形成以任务（Task）为主轴、以会话（Session）为执行载体、以生成批次（Spawn）为最小执行单元的层级执行记录结构，并通过事件总线（ServerEventBus）向外部工具和前端 SSE 客户端实时广播类型化事件，使得任意下游系统均可通过统一接口访问完整的 Agent 执行轨迹与评估结论。

### 技术问题说明

在现有 Mission Control 系统中，Agent 执行过程产生的数据分散在多个独立的表与存储介质中：tasks 表记录任务状态与结果，spawn_history 表记录 Agent 进程的启停事件，token_usage 表记录模型调用成本，quality_reviews 表记录人工或自动审查结论，activities 表以通用活动日志形式记录各类操作。这些数据各自独立维护，彼此之间缺乏统一的关联标识和一致的查询入口。

这种分散架构带来三个核心问题：其一，缺乏统一的执行记录主键或关联标识，导致跨表查询——例如查找某个任务的全部 spawn 记录及其对应 token 消耗——需要多步 JOIN 且关联条件不完整（spawn_history 通过 agent_name+session_id 关联 agent 和 session，但未直接关联 task_id）；其二，评估结果（如 quality_reviews 的审查结论、agent-evals 的四层评估数据）与任务执行记录之间缺乏强制耦合，评估数据可能因关联断裂而无法回溯到具体执行上下文；其三，activities 和 audit_log 作为通用日志表，其 entity_type/entity_id 的松散设计使得按任务维度聚合执行轨迹时缺乏结构化的查询路径。

此外，现有的事件广播机制（ServerEventBus）虽已覆盖 task.*、agent.* 等类型化事件，但事件负载中并不携带完整的执行记录结构，外部消费者仍需回查多个数据源才能还原完整执行轨迹。因此，需要构建一个以任务为主轴、以会话和 spawn 为层级维度的统一执行记录契约，在不废弃现有表结构的前提下，通过关联模型、事件负载增强和查询聚合层实现执行数据的结构化沉淀与可追踪性。

### 核心技术方案

本方案的核心是建立以任务（Task）为主轴的三层执行记录模型：Task → Session → Spawn。每一层对应不同的执行粒度和生命周期范围，并通过明确的关联键逐层嵌套，形成完整的执行轨迹。

第一层为任务层（Task Layer），以 tasks 表为主记录。任务从 inbox 状态被 dispatch 至具体 Agent 时，在 tasks 表的 metadata 字段中写入 dispatch_session_id，建立 task→session 的单向关联。任务完成后，outcome（success/failed/partial/abandoned）、resolution、feedback_rating 和 completed_at 构成任务级执行结论。任务的重试通过 retry_count 和 dispatch_attempts 计数，每次重试可产生新的 dispatch_session_id，形成一条任务对应多次执行尝试的一对多关系。

第二层为会话层（Session Layer），以 session_id 为关联键统一 Gateway 会话（来自 OpenClaw 磁盘 sessions.json）、Claude Code 会话（claude_sessions 表）和 Hermes 会话（~/.hermes/state.db）。sessions 聚合 API（/api/sessions）已实现三源合并查询，返回统一的会话视图。会话层承载 Agent 执行期间的对话上下文、模型调用记录和 token 消耗数据——token_usage 表通过 session_id 与会话关联，并通过可选的 taskId 字段与任务关联，形成 task→session→token_usage 的完整成本归因链。

第三层为生成批次层（Spawn Layer），以 spawn_history 表为主记录。每次 Agent 进程的启停作为一个 spawn 事件，记录 agent_name、agent_id、spawn_type、session_id、trigger、status（started/completed/failed/terminated）、exit_code、error、duration_ms 和 workspace_id。spawn_history 通过 session_id 与会话层关联，通过 agent_name+agent_id 与 Agent 实体关联。在增强方案中，spawn_history 增加 task_id 字段，直接建立 spawn→task 关联，使每一次 Agent 进程级执行均可追溯到所属任务。

在以上三层模型基础上，方案通过统一的关联映射规则将各分散表接入执行记录契约：quality_reviews 通过 task_id→Task 关联，其 status（approved/rejected）和 notes 构成任务级质量结论；agent-evals 的四层评估（Output/Trace/Component/Drift）以 agent_name 和 workspace_id 为维度，通过 Agent→Spawn→Task 路径间接关联至任务执行上下文；activities 表在记录与任务相关操作时，其 entity_type 设为 'task'、entity_id 设为对应 task_id，从而纳入任务执行轨迹的时序轴中；audit_log 同理，当 target_type 为 'task' 时，其 action、actor 和 detail 构成任务审计链的一部分。

为实现外部工具对执行记录的实时消费，方案对 ServerEventBus 的事件负载进行增强。在 task 状态变更事件（task.assigned、task.in_progress、task.completed 等）中，除原有的 task_id 和 status 字段外，附加当前执行记录的摘要结构——包含关联的 session_id、最近的 spawn 记录摘要（spawn_type、status、duration_ms）以及 token 消耗累计值。外部 webhook 订阅者和 SSE 客户端在接收到事件后，无需回查数据库即可获得执行轨迹的关键节点信息；如需完整记录，可通过摘要中的关联键向聚合查询接口发起精确查询。

在查询侧，方案构建统一的执行记录聚合接口。该接口以 task_id 为入口，按三层模型逐层展开：先查询 tasks 表获取任务基本信息和执行结论；再通过 metadata.dispatch_session_id 和 spawn_history.task_id 反查关联的所有 session 和 spawn 记录；随后聚合 token_usage 的成本数据、quality_reviews 的审查结论、agent-evals 的评估结果以及 activities/audit_log 中 entity_type='task' 的时序活动记录。聚合结果以单一 JSON 结构返回，包含任务摘要、执行尝试列表（每次尝试对应一个 session+spawn 组合及其成本与评估结论）和完整活动时间线，下游系统只需一次查询即可获得任务的全量执行记录。

### 关键模块与处理流程

本方案涉及的关键模块分布在现有 Mission Control 系统的多个核心库中，并新增执行记录聚合模块以实现统一查询入口。各模块在任务生命周期的不同阶段介入，协同完成执行记录的结构化沉淀。

任务调度与关联模块（task-dispatch.ts）：该模块负责将 inbox 状态的任务自动路由至可用 Agent。在 dispatch 过程中，模块调用 Aegis 质量审查器对 review 状态任务进行自动评判，对过期任务（in_progress 超 10 分钟且 Agent 离线）执行重新入队。关键增强点在于：dispatch 成功时，将分配的 session_id 写入 tasks.metadata.dispatch_session_id，同时触发 recordSpawnStart 创建 spawn 记录，完成 Task→Session→Spawn 的初始关联建立。

Spawn 历史记录模块（spawn-history.ts）：提供 recordSpawnStart 和 recordSpawnFinish 两个核心函数。recordSpawnStart 在 Agent 进程启动时写入 spawn_history 记录（status='started'），携带 agent_name、session_id、spawn_type 和 trigger 信息；recordSpawnFinish 在进程结束时更新同一条记录，写入 status（completed/failed/terminated）、exit_code、error 和 duration_ms。在增强方案中，两个函数均接受可选的 task_id 参数，使得 spawn 记录直接归属到任务，消除此前仅通过 agent_name+session_id 间接关联的不确定性。

成本归因模块（task-costs.ts）：该模块从 token_usage 表读取模型调用记录，按 taskId、agent_name 和 workspace_id 三个维度聚合生成 TokenCostRecord 和 TokenStats。在执行记录契约中，token_usage 的 taskId 字段是连接成本数据与任务的关键纽带——每次模型调用在记录时携带当前任务的 taskId，使得成本报告可直接按任务维度汇总。同时，token_usage 的 session_id 与会话层关联，确保成本数据在 Session→Spawn→Task 的任一层级均可被检索和聚合。

评估集成模块（agent-evals.ts）：提供四层评估引擎——Output 层统计任务完成率与正确性，Trace 层分析 Agent 执行轨迹的收敛性与循环检测，Component 层评估各工具（tool）的调用可靠性（结合 mcp_call_log 表中 agent_name、tool_name、success 的统计数据），Drift 层基于滚动基线对比检测 Agent 行为的长期漂移。评估结果按 agent_name 和 workspace_id 维度存储，通过 Agent→Spawn→Task 路径与任务执行记录关联。评估结论可作为 quality_reviews 自动审查的输入参考，形成「执行→评估→审查」闭环。

事件广播模块（event-bus.ts）：ServerEventBus 单例维护 SSE 客户端连接池和 webhook 订阅注册表。在增强方案中，task 状态变更事件的负载被扩展为包含执行记录摘要结构——除 task_id 和新状态外，携带 dispatch_session_id、最近 spawn 记录摘要和累计 token 消耗。webhook 投递同样采用增强负载，并沿用现有的重试（指数退避）和熔断机制保障投递可靠性。

执行记录聚合模块（新增）：该模块提供以 task_id 为入口的统一查询接口，内部按三层模型执行聚合查询：第一步，查询 tasks 表获取任务基本信息和 outcome/resolution/feedback_rating 等执行结论；第二步，通过 metadata.dispatch_session_id 和 spawn_history.task_id 查询所有关联 session 和 spawn 记录，按时间排序形成执行尝试列表；第三步，聚合每个执行尝试的 token_usage 成本、quality_reviews 审查结论和 activities/audit_log 中的时序活动记录；第四步，以 agent_name 和 workspace_id 查询 agent-evals 评估结果并附加到对应执行尝试。聚合结果以单一 JSON 响应返回，包含任务摘要、执行尝试列表和完整活动时间线。

整体处理流程贯穿任务全生命周期：任务创建时，tasks 表写入初始记录（status='inbox'）；任务被 dispatch 时，task-dispatch 模块分配 Agent 和 session，将 dispatch_session_id 写入 metadata，同时 spawn-history 模块调用 recordSpawnStart 创建 spawn 记录（status='started'），ServerEventBus 广播 task.in_progress 事件（附带执行记录摘要）；Agent 执行期间，token_usage 持续记录模型调用成本（携带 taskId），mcp_call_log 记录工具调用（供 Component 评估使用）；执行结束时，spawn-history 调用 recordSpawnFinish 更新 spawn 状态和耗时，tasks 表更新 outcome 和 resolution；任务进入 review 状态后，Aegis 自动审查器结合 agent-evals 评估结果和 quality_reviews 历史记录做出 approve/reject/retry 决策；最终状态变更通过 ServerEventBus 广播，外部系统通过聚合接口获取完整执行记录。

### 技术效果说明

本方案通过构建统一执行记录契约，在不废弃现有数据表的前提下实现了以下技术效果：

第一，执行轨迹全链路可追踪。以 task_id 为入口，通过三层模型（Task→Session→Spawn）可沿时间轴还原任务的完整执行轨迹：何时被 dispatch、由哪个 Agent 承接、产生了几次 spawn、每次 spawn 的启停时间和退出状态、对应的会话上下文和 token 消耗、以及最终的质量审查结论。跨表关联不再依赖松散的条件拼接，而是通过 dispatch_session_id 和 spawn.task_id 形成明确的外键链路。

第二，成本精确归因。token_usage 通过 taskId 字段直接归属到任务，解决了此前仅通过 session_id 间接关联时可能出现的成本归属模糊问题。task-costs 模块可按任务、Agent 和项目三个维度生成精确的成本报告，每个任务的 token 消耗与对应的 spawn 执行过程一一对应。

第三，评估结果与执行上下文强制耦合。quality_reviews 的审查结论和 agent-evals 的四层评估结果通过 task_id 和 Agent→Spawn→Task 路径与具体执行记录关联，形成「执行→评估→审查」的完整证据链。当 Drift 层检测到 Agent 行为异常时，可直接定位到产生异常的具体任务和 spawn 批次，实现评估结果的可回溯。

第四，实时可观测性与外部集成。ServerEventBus 增强后的任务事件负载携带执行记录摘要，SSE 客户端和 webhook 订阅者可在事件到达时即时获知当前执行状态、关联会话和累计成本，无需主动轮询。外部 CI/CD、监控和审计系统可通过聚合接口一次查询获取完整执行记录，实现与现有工具链的无缝对接。

第五，向后兼容与渐进演进。方案不废弃或修改现有表的核心结构，spawn_history 增加的 task_id 字段为可选字段，现有调用无需立即修改。聚合接口作为新增查询层叠加在现有数据表之上，不改变已有 API 的行为。各模块的增强点（dispatch 时的关联写入、事件负载扩展、聚合查询逻辑）均为增量修改，可在不影响现有功能的前提下逐步接入。
