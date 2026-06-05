## 技术方案

本方案在 Mission Control 现有任务派发、agent 管理、会话追踪、token 计量和评测体系之上，引入统一的「Agent 执行记录」（Agent Execution Record，以下简称 AER）实体，将分散在 tasks、spawn_history、token_usage、eval_runs、mcp_call_log、activities、claude_sessions 等表中的执行相关数据碎片组织为一条可追溯、可查询、可评测的完整记录。

方案核心思路：以 agent 的一次完整执行（从触发启动到产出闭合）为粒度，建立 AER 作为聚合根实体，通过关联键（task_id、session_id、agent_name、spawn_id）将现有分散记录串联。AER 不替代现有表，而是在现有记录之上提供一层结构化聚合视图，同时补充触发溯源、产物摘要、评测附着和审计标签等现有体系缺失的字段。

方案同时设计 AER 的查询 API、Webhook 推送、批量导出能力，以及评测结果与执行记录的自动附着机制，为质量评测、排行榜、运行审计、成本归因和外部工具集成提供统一数据基础。

### 整体架构

AER 在 Mission Control 体系中的定位为执行聚合层，位于现有数据表之上、查询与集成接口之下。其与现有实体的关系如下：

- 触发源层：Task（任务派发触发）、Schedule（定期任务触发）、API/Jarvis Dispatch（外部调用触发）、Manual Spawn（人工启动触发）——AER 通过 trigger_type 和 trigger_ref 字段记录触发来源。
- 执行载体层：Agent（执行主体）+ Session（执行会话）+ Spawn History（启动记录）——AER 通过 agent_name、session_id、spawn_id 与之关联。
- 执行产物层：Activities（状态变更流）、MCP Call Log（工具调用明细）、Token Usage（资源消耗）、Claude Sessions（本地会话统计）——AER 聚合这些分散记录中的关键摘要信息。
- 评测层：Eval Runs（四层评测结果）——AER 通过 agent_name + session_id + task_id 组合键与评测记录关联，并在 AER 闭合后触发自动评测附着。
- 审计与安全层：Audit Log（管理员操作审计）、Security Events（安全事件）、Agent Trust Scores（信任评分）——AER 记录关联的审计标签，支持事后追溯。

### 数据结构设计

AER 采用新增数据库表 agent_execution_records 实现，与现有表通过外键引用关联，不修改现有表结构。核心字段设计如下：

- id：AER 唯一标识（UUID 或自增整数），作为聚合根主键。
- agent_name：执行 agent 名称，对应 agents 表，与 spawn_history.agent_name、token_usage.agent_name、eval_runs.agent_name 对齐。
- task_id：关联任务（可空，非任务触发的执行此项为空），对应 tasks.id。
- session_id：执行会话标识，对应 claude_sessions.session_id 或 gateway session，与 token_usage.session_id、spawn_history.session_id 对齐。
- spawn_id：启动记录引用（可空），对应 spawn_history.id。
- trigger_type：触发类型枚举，取值包括 task_dispatch、scheduled、api_dispatch、manual_spawn、retry。
- trigger_ref：触发源引用，JSON 格式存储触发上下文，如任务 ID、调度规则 ID、API 调用方标识等。
- status：执行状态枚举，取值包括 pending、running、completed、failed、cancelled、timeout。
- started_at / completed_at：执行起止时间戳，与 spawn_history.duration_ms 互相校验。
- outcome：执行结果摘要，JSON 格式，包含产物类型（代码/文档/分析报告等）、产物路径或摘要、关键指标。
- error_message：失败时的错误信息，与 tasks.error_message 对齐。
- token_summary：token 消耗摘要，JSON 格式，包含 input_tokens、output_tokens、total_tokens、cost_usd、model，从 token_usage 表按 session_id 聚合。
- tool_call_summary：工具调用摘要，JSON 格式，包含 total_calls、success_count、failure_count、mcp_servers 列表，从 mcp_call_log 表按 agent_name + session_id 聚合。
- eval_summary：评测摘要（可空），JSON 格式，包含各层评测分数和通过状态，在评测完成后回填。
- retry_count：重试次数，与 tasks.retry_count 对齐。
- audit_tags：审计标签数组，如 security、admin_action、scheduled，用于审计过滤。
- metadata：扩展元数据，JSON 格式，为排行榜、成本归因、外部工具集成预留。

### 生命周期管理

AER 的生命周期与 agent 执行过程同步推进，分为创建、更新、闭合三个阶段：

创建阶段——在 agent 启动时创建 AER 初始记录。具体挂载点位于 spawn API（src/app/api/spawn/route.ts）中 agent 进程成功启动后。此时写入 agent_name、task_id、session_id、spawn_id、trigger_type、trigger_ref、status='pending'、started_at，其余字段留空。对于通过 Jarvis Dispatch 或 Queue 触发的执行，trigger_type 分别标记为 api_dispatch 和 task_dispatch，trigger_ref 记录对应的 dispatch 请求体或队列认领记录的关键字段。

更新阶段——在 agent 运行过程中，通过以下事件驱动 AER 字段增量更新：(a) SSE event-bus 上的 agent.status 事件触发 status 字段更新，从 pending 转为 running，异常时转为 failed/timeout；(b) token_usage 表插入新行时，通过数据库触发器或应用层钩子按 session_id 增量聚合 token_summary；(c) mcp_call_log 插入时按 agent_name + session_id 增量更新 tool_call_summary；(d) activities 表中 agent 状态变更事件同步更新 AER 的 status 快照。

闭合阶段——在 agent 执行终止时完成 AER 最终回填。挂载点位于 spawn 进程退出回调或任务状态变更观测器中。闭合时写入 completed_at、outcome（从 tasks.outcome 或会话产物解析）、error_message（如有）、retry_count。闭合后触发异步评测附着流程（见评测附着机制章节），评测完成后回填 eval_summary。闭合状态的 AER 不再更新，作为不可变执行档案保留。

### 查询与集成接口

AER 提供三层查询与集成能力：REST API、SSE 事件推送和批量导出。

REST API——新增 /api/executions 端点：(a) GET /api/executions 列表查询，支持按 agent_name、task_id、session_id、trigger_type、status、时间范围过滤，支持分页和排序；(b) GET /api/executions/{id} 返回单条 AER 完整详情，包含关联的 token_summary、tool_call_summary、eval_summary 及原始 spawn_history 和 task 摘要；(c) GET /api/executions/by-agent/{agent_name} 按 agent 聚合执行统计（总次数、成功率、平均耗时、总成本），为排行榜提供数据源；(d) GET /api/executions/{id}/traces 返回该执行记录的完整追踪链路，包含从 trigger_ref → spawn → token_usage → mcp_call_log → eval_runs 的串联视图。

SSE 事件推送——在现有 event-bus 上新增 execution.* 事件类型：execution.created（AER 创建时）、execution.updated（状态变更时）、execution.closed（闭合时）、execution.evaluated（评测附着完成时）。外部系统和前端仪表盘可通过现有 /api/events 端点订阅，按 workspace 隔离。

批量导出——新增 /api/executions/export 端点，支持 JSON、CSV 两种格式，过滤条件与列表查询一致。导出数据包含 AER 全部字段及展开的关联数据，满足审计归档和离线分析需求。同时提供 Webhook 注册接口 /api/webhooks，外部系统可注册 execution.closed 事件的回调 URL，在 AER 闭合时自动推送摘要。

### 评测附着机制

评测附着机制解决如何将现有四层评测体系（Output、Trace、Component、Drift）的结果自动关联到对应 AER 的问题。

触发时机：AER 闭合时（status 变为 completed/failed/timeout），通过 SSE event-bus 发布 execution.closed 事件。评测调度器监听该事件，根据 agent_name + session_id + task_id 组合键查找该次执行对应的 eval_golden_sets（黄金评测集），自动触发四层评测运行。

关联方式：eval_runs 表（迁移038）已有 agent_name、eval_layer、score、passed、detail 字段，在此基础上新增 execution_id 外键字段指向 AER。评测运行时写入 execution_id，评测完成后汇总各层结果回填 AER 的 eval_summary 字段。回填内容包含各层分数、通过/失败状态、评测时间戳，以及 detail 中的关键发现摘要。

异步非阻塞设计：评测附着为异步流程，不阻塞 AER 闭合和后续执行。对于未配置黄金评测集的 agent，eval_summary 保持为空，不影响 AER 正常使用。评测失败或超时仅记录日志，不标记 AER 为异常。AER 查询接口在 eval_summary 为空时返回 pending_eval 状态，提示评测尚未完成。

### 扩展机制

AER 通过 metadata 字段和开放接口为以下扩展场景预留空间：

排行榜——基于 /api/executions/by-agent/{agent_name} 聚合数据，构建多维度排行榜：(a) 效率榜，按平均执行耗时和 token 消耗排序；(b) 质量榜，按 eval_summary 中各层分数加权排序；(c) 可靠榜，按成功率（completed 占比）和平均 retry_count 排序；(d) 成本榜，按 cost_usd 总和排序。排行榜数据由 AER 聚合查询实时计算，前端通过现有 32 面板仪表盘展示。

运行审计——audit_tags 字段标记敏感操作（如 security、admin_action），审计查询接口 /api/audit 可通过 execution_id 关联到完整 AER 追踪链路。结合现有 audit_log 和 security_events 表，实现从管理员操作 → AER → 执行细节的逐级下钻追溯。

外部工具集成——(a) Webhook：外部 CI/CD 或监控系统注册 execution.closed 回调，在 agent 执行完成时自动触发下游流程；(b) 导出接口：/api/executions/export 支持按时间范围批量导出 CSV，供外部数据仓库或 BI 工具消费；(c) metadata 扩展：外部系统可在 AER 创建时通过 API 参数传入自定义 metadata 键值对，如 external_ticket_id、pipeline_run_id，实现与外部系统的双向关联，无需修改 AER 核心表结构。
