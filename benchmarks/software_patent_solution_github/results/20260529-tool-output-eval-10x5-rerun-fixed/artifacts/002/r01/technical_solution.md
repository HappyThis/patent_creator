## 技术方案

本方案在 Mission Control 多 agent 编排平台现有数据基础设施之上，引入统一运行记录（Unified Run Record，以下简称 RunRecord）契约，将 agent 执行过程中分散在多张表和多模块中的 spawn 生命周期、token 消耗、评估结果、活动事件、质量审查结论等数据，聚合为以单次任务执行为主键的结构化运行记录。RunRecord 不是一张新表，而是一个逻辑聚合层：它定义统一的索引契约、生命周期事件和外部访问接口，将已有的 spawn_history、token_usage、eval_runs、tasks、activities、quality_reviews 等数据按 run_id（复用 task_id）关联，形成可查询、可追踪、可附着评估结果、可被外部工具消费的完整执行档案。

### 统一运行记录契约设计

RunRecord 以 task_id 作为主键（run_id 复用 task_id），将一次 agent 任务执行的全生命周期数据聚合为一个结构化档案。其契约定义如下：

- run_id：复用 tasks 表的 id，作为跨表关联的主键
- task_summary：从 tasks 表提取的任务摘要，包括状态（inbox→assigned→in_progress→review→quality_review→done）、outcome（success/failed/partial/abandoned）、error_message、feedback_rating、dispatch_attempts
- spawn_records：从 spawn_history 表按 task_id 查询的 spawn 生命周期列表，每条记录包含 agent_name、trigger、started_at、completed_at、exit_code、duration_ms
- token_usage：从 token_usage 表按 task_id 聚合的 token 消耗明细和汇总，包括 input_tokens、output_tokens、context_tokens、cost_usd，按 agent_name 和 session_id 分组
- eval_results：从 eval_runs 表按 agent_name 查询的四层评估结果（Layer1 Output/Layer2 Trace/Layer3 Component/Layer4 Drift），每条包含 layer、score、passed、detail
- activity_timeline：从 activities 表按关联实体（task_id、agent 等）查询的活动事件时间线，按 timestamp 排序
- quality_reviews：从 quality_reviews 表关联的 Aegis 质量审查记录，包含自动审批/驳回结论和重试历史

RunRecord 不引入新的持久化表，而是作为查询时的逻辑聚合视图。当外部请求某次运行的完整档案时，系统以 run_id 为锚点，并发查询上述各表，在内存中组装为 RunRecord 对象后返回。这种设计避免了数据冗余和同步问题，所有权威数据仍由各自的源表维护。

### 与现有数据基础设施的关联机制

RunRecord 契约需要稳定、高效地从六类现有数据源中提取和聚合信息。系统通过以下关联机制实现对分散数据的统一索引：

- tasks 表作为主锚点：tasks 表的 id 即为 run_id。tasks 表已有的状态机（inbox→assigned→in_progress→review→quality_review→done）和 outcome/error_message/feedback_rating/retry_count 字段直接映射为 RunRecord.task_summary 的内容，无需额外转换
- spawn_history 通过 task_id 关联：spawn_history 表在迁移 044 中已包含 task_id 列。当 RunRecord 查询 spawn_records 时，以 run_id = spawn_history.task_id 为条件，按 started_at 排序返回该任务下所有 agent spawn 的生命周期记录
- token_usage 三向关联链：token_usage 表（迁移 018/025/039）通过 task_id、agent_name、session_id 三列形成完整归因链。RunRecord 查询以 task_id 为主条件，按 agent_name 分组聚合，得到每个 agent 在该任务中的 token 消耗和成本；同时利用 session_id 关联 sessions 模块从磁盘 sessions.json 读取的上下文 token 信息，补充 context_tokens 字段
- eval_runs 通过 agent_name 关联：eval_runs 表（迁移 038）按 agent_name 和 layer 存储评估结果。RunRecord 查询时，从 spawn_records 中提取本次任务涉及的所有 agent_name，以 agent_name IN (...) 为条件查询 eval_runs，按 layer 分组返回四层评估得分和详情
- activities 通过关联实体查询：activities 表支持按 entity_type 和 entity_id 查询。RunRecord 查询时，以 entity_type='task' AND entity_id=run_id 为主条件，同时扩展查询 entity_type='agent' 且 entity_id 在本次 spawn 涉及的 agent 列表中的活动记录，合并为 activity_timeline
- quality_reviews 通过 task_id 关联：quality_reviews 表记录 Aegis 质量审查的自动审批/驳回结论和重试上限。RunRecord 查询时以 task_id = run_id 为条件，返回该任务的审查历史和最终结论

上述关联机制的核心原则是：每类数据保持其独立存储和写入路径不变，RunRecord 只在读取侧建立聚合逻辑。写入路径仍由各模块独立负责——spawn 模块写入 spawn_history、成本统计模块写入 token_usage、评估引擎写入 eval_runs、事件总线广播写入 activities——RunRecord 不拦截也不复制这些写入操作。

### 运行记录生命周期与事件驱动

RunRecord 的生命周期由底层任务状态机驱动，通过事件总线（eventBus）在关键节点发布 run.* 事件，使外部系统能够实时感知运行记录的演进：

- run.created：当任务被 Gateway 调度并首次 spawn agent 时，spawn_history 写入 started 记录，eventBus 发布 run.created 事件，携带 run_id、agent_name、trigger 信息
- run.token_update：当 token_usage 表写入新记录（每次 LLM 调用完成后），成本统计模块在持久化后发布 run.token_update 事件，携带 run_id、agent_name、本次消耗的 input_tokens/output_tokens/cost_usd 和累计值
- run.spawn_completed：当 spawn 进程退出时，spawn_history 写入 completed_at/exit_code/duration_ms，eventBus 发布 run.spawn_completed 事件
- run.eval_available：当四层评估引擎完成任一层评估并写入 eval_runs 后，发布 run.eval_available 事件，携带 run_id、agent_name、layer、score、passed
- run.quality_reviewed：当 Aegis 质量审查完成自动审批或驳回后，发布 run.quality_reviewed 事件，携带 run_id、approved、reason
- run.closed：当任务进入终态（done 且 outcome 已确定）时，系统发布 run.closed 事件，标记该 RunRecord 不再追加新数据

所有 run.* 事件复用现有 eventBus 单例（event-bus.ts），与已有的 task.*、agent.*、activity.created 事件共享同一广播通道。SSE 客户端和 webhook 订阅者可以通过事件类型过滤，只订阅 run.* 事件来追踪运行记录的演进。事件负载中始终携带 run_id，外部系统可以以此为键在本地构建或更新 RunRecord 缓存。

### 外部可访问接口

RunRecord 通过三种互补的外部访问通道，满足不同场景的消费需求：

- REST API 全量查询：新增 GET /api/runs/:run_id/full 接口，接收 run_id 参数，并发查询 tasks、spawn_history、token_usage、eval_runs、activities、quality_reviews 六类数据源，在内存中组装为 RunRecord 对象后返回 JSON。接口支持可选的 ?fields= 参数按需裁剪返回字段，以及 ?include_evals=true 控制是否包含评估结果
- SSE 增量订阅：复用现有 /api/events SSE 端点，客户端通过 eventType=run.* 过滤器订阅运行记录事件流。当 run.token_update、run.spawn_completed、run.eval_available、run.quality_reviewed、run.closed 事件发布时，SSE 连接实时推送事件负载，客户端可增量拼接完整的运行记录视图
- Webhook 异步推送：在现有 webhook 系统中注册 run.* 事件类型。外部 CI/CD 系统、监控平台或审计系统可通过 webhook URL 接收运行记录事件，在自身系统中构建运行档案或触发后续流程（如自动重试、告警、成本归因）

三种通道中，REST API 提供的是运行记录的快照视图（point-in-time），SSE 提供实时增量流，Webhook 提供异步推送。三者共享同一 RunRecord 组装逻辑，确保数据一致性。此外，RunRecord 的 JSON 结构设计遵循 OpenAPI 3.0 规范，外部工具可以通过 /api/openapi.json 自动发现 RunRecord schema，无需人工对接。

### 技术效果

RunRecord 统一运行记录契约在现有分散数据基础上，通过逻辑聚合层实现了以下技术效果：

- 可查询：以 run_id 为单一主键即可获取一次 agent 执行的完整档案，无需跨多张表手动 JOIN。GET /api/runs/:id/full 接口提供 O(1) 键查找的查询体验，内部并发查询六类数据源后一次返回
- 可追踪：RunRecord 将 spawn 生命周期、token 消耗时间线和活动事件按时间排序，形成从任务创建到终态的完整因果链。当某次执行出现异常时，可以从 RunRecord 中定位出问题的具体 spawn、token 消耗突变点或评估失败层
- 可附着评估结果：四层评估引擎（Layer1 任务完成率、Layer2 收敛性/循环检测、Layer3 工具可靠性、Layer4 滚动基线漂移对比）的结果通过 run.eval_available 事件自动附着到 RunRecord，外部系统无需单独查询 eval_runs 表即可获取评估结论
- 外部工具可消费：通过 REST API（快照）、SSE（增量流）、Webhook（异步推送）三种通道和 OpenAPI schema 自动发现，外部 CI/CD、监控、审计系统可以零对接成本地消费 RunRecord 数据
- 写入路径解耦：每类数据由各自模块独立写入源表，RunRecord 只在读取侧聚合。这种设计避免了中心化写入瓶颈和跨表同步问题，各模块的写入延迟和可靠性不受 RunRecord 影响
