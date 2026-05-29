## 技术方案

本方案提出一种面向多 agent 编排平台的 agent 执行过程统一记录与追踪机制。方案在 Mission Control 已有的任务管理、agent 编排、会话同步、token 统计和评估框架之上，构建一套以 agent 执行运行记录（Agent Run Record）为核心的结构化沉淀、可查询追踪和可评估的软件契约，使每一次 agent 执行从产生、运行、完成到评估的全过程信息形成闭环，并可被外部工具以统一接口查询和复用。

### 技术问题与设计目标

在现有 Mission Control 系统中，agent 执行过程的相关信息分散在多个独立的存储结构中：任务表（tasks）记录任务状态和结果，spawn_history 表记录 agent 启动事件，token_usage 表记录 token 消耗，eval_runs 表记录评估结果，quality_reviews 表记录质量审核，activities 表记录活动流，mcp_call_log 表记录工具调用。这些表虽然在数据层面通过外键（如 task_id、agent_name、session_id）存在局部关联，但缺少一个统一的执行维度将它们组织为可查询的完整运行视图。外部工具或下游系统无法通过单一查询接口获取“某次 agent 执行的完整过程信息”，包括谁发起的、执行了什么任务、消耗了多少 token、产生了多少成本、执行结果如何、是否经过了评估和质量审核。

本方案的设计目标是：在不引入额外外部依赖、不破坏现有数据表结构的前提下，构建一个以 agent 执行运行记录（Agent Run Record）为统一维度的软件契约。该契约满足以下要求：(1) 可查询——通过单一接口可按 agent、任务、时间范围、状态、评估结果等维度查询；(2) 可追踪——能从运行记录回溯到触发来源（spawn）、关联会话（session）、关联任务（task）；(3) 可附着评估结果——评估引擎输出的分层评估数据可挂载到运行记录上；(4) 可被外部工具访问——提供稳定的 HTTP API 契约，支持排行榜、审计、成本分析等下游集成。

### 统一运行记录契约

本方案的核心是定义一种虚拟运行记录结构（Agent Run Record），它并非新增一张物理日志表，而是在现有关系型数据之上构建的逻辑视图。运行记录通过稳定的关联键将分散在多个表中的执行信息组织为统一的查询单元。

运行记录由以下关联键串联各数据源：run_id 为每次 agent 执行的唯一标识，可直接复用 spawn 阶段生成的 spawnId（格式如 spawn-{timestamp}-{random}），也可在首次记录时生成新的稳定标识；task_id 关联到 tasks 表，标识本次执行对应的任务；agent_name 关联到 agents 表，标识执行主体；session_id 关联到 spawn_history 和 token_usage 表，标识与 agent 网关的会话；workspace_id 关联到多租户工作空间。这些关联键均为现有表中已有字段，方案不需要新增外键列。

运行记录的数据组成分为四个维度：(1) 标识与生命周期——run_id、task_id、agent_name、spawn_type、trigger、status（started/completed/failed/terminated）、created_at、finished_at、duration_ms，数据来源为 spawn_history 表与 tasks 表 JOIN；(2) 执行成本——按 model 分组的 input_tokens、output_tokens、total_tokens 和 cost_usd，以及汇总的 estimated_cost，数据来源为 token_usage 表按 session_id 和 task_id 聚合；(3) 执行结果——来源于 tasks 表的 outcome（success/failed/partial/abandoned）、error_message、resolution、retry_count、feedback_rating，以及 quality_reviews 表的审核状态；(4) 评估附着——来源于 eval_runs 表按 agent_name 关联的最新分层评估结果（output/trace/component/drift 四层），以及 eval_traces 表中的收敛分数和步骤统计。

### 统一查询接口与 API 契约

方案通过新增一个统一的 HTTP API 端点将运行记录对外暴露为可查询契约。该端点基于 Next.js API Route 实现，复用现有认证中间件（requireRole）和限流中间件（readLimiter），通过 SQL 跨表 JOIN 查询实时组装运行记录，无需物化视图或预计算缓存。

列表查询 GET /api/agent-runs 支持以下过滤参数：agent（按 agent 名称过滤）、task_id（按任务过滤）、status（按运行状态过滤：started/completed/failed/terminated）、outcome（按任务结果过滤：success/failed/partial/abandoned）、since/until（时间范围）、has_evaluation（是否已有评估结果，布尔值）、workspace_id（多租户隔离）、limit/offset（分页）。查询以 spawn_history 为主表，LEFT JOIN tasks、token_usage（聚合子查询）、eval_runs（子查询取每层最新记录）和 quality_reviews 表。返回结果包含每条运行的汇总信息：run_id、agent_name、task 摘要、状态、时长、token 总量、成本估计、评估结论。

详情查询 GET /api/agent-runs/[run_id] 返回单条运行的完整信息，包含四维数据的全部字段：完整的 spawn_history 记录、关联的 task 详情（含 outcome、error_message、resolution、retry_count、feedback_rating）、按模型拆分的 token 消耗与成本明细、质量审核记录（quality_reviews）、以及四层评估结果（eval_runs）和收敛追踪（eval_traces），MCP 工具调用日志（mcp_call_log）。该接口可作为外部工具获取单次执行完整审计信息的唯一入口。

### 与现有基础设施的集成关系

本方案的关键优势在于最大化复用 Mission Control 已有的数据基础设施，不引入新的存储结构或外部依赖。

与 spawn 流程的集成：当前 spawn API（POST /api/spawn）在生成 spawnId、调用 agent 网关创建会话后，已通过 logAuditEvent 写入审计日志。方案在此路径上增加调用 recordSpawnStart 写入 spawn_history 表，记录 agent_name、spawn_type、session_id、trigger 和 workspace_id。当 agent 执行完成或异常退出时，由心跳或回调路径调用 recordSpawnFinish 更新状态、退出码、错误信息和执行时长。这使得每次 spawn 自然成为一条运行记录的起点，无需额外的记录触发逻辑。

与 token 统计的集成：token_usage 表已包含 session_id、model、input_tokens、output_tokens、cost_usd、agent_name 和 task_id 字段。运行记录的“执行成本”维度直接通过按 run_id 关联的 session_id 和 task_id 对 token_usage 表聚合查询得到，无需修改 token 记录逻辑。与任务结果追踪的集成：tasks 表已通过迁移 026_task_outcome_tracking 增加了 outcome、error_message、resolution、feedback_rating、retry_count、completed_at 字段，运行记录的结果维度直接读取这些字段。

与评估框架的集成：现有 agent-evals 模块提供四层评估引擎（output/trace/component/drift），评估结果写入 eval_runs 和 eval_traces 表。运行记录通过 agent_name 关联查询最新评估结果。当通过 API 触发评估（POST /api/agents/evals { action: 'run' }）后，同一 agent 的运行记录即可自动携带评估结论，无需重复评估。与事件总线的集成：ServerEventBus 已支持 task.updated、agent.status_changed、activity.created 等事件类型。当运行记录的关键状态变化时（如 spawn 完成、任务 outcome 更新、评估完成），可扩展事件类型为 agent_run.started、agent_run.completed、agent_run.evaluated，通过 WebSocket/SSE 推送至前端面板和 webhook 订阅方。

### 运行记录生命周期与处理流程

一次完整的 agent 执行运行记录生命周期如下：

1. 记录创建：用户或调度器通过 POST /api/spawn 发起 agent 执行，系统生成 spawnId 作为 run_id，调用 recordSpawnStart 在 spawn_history 表中写入一条 status='started' 的记录，同时写入 agent_name、spawn_type、session_id、trigger 和 workspace_id。
2. 执行中追踪：agent 网关定期上报心跳，token 消耗持续写入 token_usage 表（按 session_id 和 task_id 关联）。期间 agent 的 MCP 工具调用写入 mcp_call_log 表，任务活动写入 activities 表。
3. 执行完成：agent 完成或异常退出时，系统调用 recordSpawnFinish 更新 spawn_history 的状态为 completed/failed/terminated，同时记录 exit_code、error 和 duration_ms。对应任务的状态和 outcome 通过任务 API 更新到 tasks 表。
4. 质量审核：若任务配置了 Aegis 质量门，审核结果写入 quality_reviews 表（status 为 approved/rejected）。该记录通过 task_id 关联到运行记录。
5. 评估附着：通过 POST /api/agents/evals 触发的分层评估结果写入 eval_runs 表，通过 agent_name 关联到该 agent 的所有运行记录。运行记录的汇总查询中自动携带最新的评估结论。
6. 查询与消费：外部系统通过 GET /api/agent-runs 或 GET /api/agent-runs/[run_id] 获取运行记录。数据由服务端通过 SQL 跨表 JOIN 实时组装，不依赖物化视图。事件总线在关键状态变化时推送 agent_run.* 事件，webhook 订阅方可异步消费。

### 技术效果与扩展空间

本方案相比现有方式带来的技术效果体现在以下几个方面。

信息闭环化：一次 agent 执行从触发来源、执行过程、资源消耗、执行结果到质量评估的全链路信息被组织为统一运行记录，消除了现有方式中需要手动跨表关联才能获取完整视图的碎片化问题。查询效率提升：通过单一 API 即可获取运行记录的完整四维数据，避免多次请求不同端点再手动拼接。外部工具可集成性：统一的 API 契约（含过滤、分页、详情查询）使得排行榜系统、审计工具、成本分析面板、CI/CD 流水线等外部系统可以直接以标准化方式消费运行数据。

评估可追溯性：评估结果直接附着在运行记录上，可追踪每次评估对应的具体执行上下文，支持评估结果与执行过程的因果关系分析。最小侵入性：方案不需要新增物理表（spawn_history、token_usage、eval_runs 等表已在现有迁移中定义），不需要修改现有数据写入路径的核心逻辑，仅在 spawn 流程和查询层面做轻量适配。扩展空间：运行记录契约可作为质量排行榜（按 agent 聚合成功率、平均成本、评估分数）、运行审计（按时间范围导出完整执行记录）、自动重试策略（基于 outcome 和 retry_count 的阈值判断）、以及流水线步骤间的运行依赖分析等高级功能的数据基础。

### 风险与待确认问题

以下为当前方案中需要后续确认或注意的技术风险点。

- run_id 稳定性：当前 spawnId 由 spawn API 在运行时生成（spawn-{timestamp}-{random}），若未来存在非 spawn 触发的 agent 执行路径（如直接通过 agent 网关创建会话），需考虑这些路径如何纳入运行记录体系并分配稳定 run_id。
- 查询性能：列表查询涉及 spawn_history、tasks、token_usage、eval_runs 四表 JOIN 和聚合子查询。在单工作空间数据量较大（如数万条运行记录）时，需关注 SQLite 的查询性能，必要时可对 token_usage 和 eval_runs 的关联字段增加复合索引。
- 评估时效性：eval_runs 通过 agent_name 关联（而非 run_id），这意味着查询某条运行记录时返回的是该 agent 的最新评估结果，而非该次执行当时的评估快照。若需要保留历史评估与具体运行的精确对应关系，可在 eval_runs 表增加 run_id 字段。
- 多 agent 网关适配：当前系统支持 OpenClaw、Claude Code、Hermes、Codex 等多种 agent 网关，各网关的会话标识格式和生命周期管理存在差异。方案以 session_id 为关联纽带，但需确认各网关的 session_id 均能在 spawn_history 和 token_usage 中可靠记录。
