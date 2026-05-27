## 技术方案

本方案提出一种面向多 agent 编排系统的 agent 执行运行记录（Agent Execution Run Record）机制，在 Mission Control 已有的任务派发、agent 会话管理、spawn 追踪、token 成本统计、质量审查、MCP 工具调用日志和评估引擎等模块之上，构建一个统一的、可查询、可追踪、可附着评估结果且可被外部工具访问的执行记录契约。该记录将一次 agent 执行的完整生命周期——从触发、派发、执行到评估和归档——组织为一条结构化的"运行记录"，并通过统一的查询接口和事件流向外暴露。

### 技术问题

在现有的多 agent 编排系统中，agent 每次执行产生的结果信息分散在多个异构数据源中：任务表记录派发结果和最终状态，spawn 历史表记录进程级启停，token 用量表记录模型调用消耗，质量审查表记录人工或自动审查结论，MCP 调用日志记录工具执行明细，评估运行表记录评测分数。这些数据之间缺乏统一的关联标识和结构化的聚合视图。当需要回答"某次 agent 工作是怎么产生的、执行结果如何、成本是多少、是否经过评估"等问题时，必须跨多表联查、手工拼接，且缺乏稳定的外部访问契约。这使得下游工具（如质量排行榜、运行审计系统、成本看板、外部 CI/CD 流水线）无法以统一方式消费执行记录。

### 核心技术方案

本方案的核心是在现有数据模型之上引入"Agent 执行运行记录"（AgentRun）抽象层。AgentRun 不是一张独立的新日志表，而是一个关系聚合视图——它将一次 agent 执行的完整上下文组织为一条结构化记录，通过统一的 run_id 将分散在多张表中的相关行串联起来。

AgentRun 的生命周期起始于一个可追踪的触发事件：任务派发（task dispatch）、流水线步骤启动（pipeline step spawn）、定时任务触发（recurring task spawn）或手动调用。AgentRun 在创建时即获得全局唯一 run_id，该 run_id 被写入各相关数据表（任务记录的 metadata.run_id、spawn_history 的 run_id 列、token_usage 的 run_id 列、quality_reviews 的 run_id 列、mcp_call_log 的 run_id 列、eval_runs 的 run_id 列），形成以 run_id 为主键的跨表关联。

AgentRun 记录包含以下结构化维度：（1）触发上下文——触发类型（task/pipeline/manual/cron）、触发者标识、关联任务 ID 或流水线运行 ID；（2）执行主体——agent 名称与 ID、agent 角色与配置快照、使用的模型标识；（3）会话关联——gateway session key、claude session ID、hermes session ID 等异源会话标识的统一映射；（4）进程生命周期——spawn 开始时间、结束时间、退出码、错误信息、执行时长；（5）资源消耗——输入/输出 token 量、估算成本、各模型的 token 分布；（6）执行结果——任务 outcome（success/failed/partial/abandoned）、agent 返回的 resolution 文本；（7）质量评估——Aegis 审查结论（approved/rejected）与审查备注；（8）工具调用明细——MCP 工具名、调用次数、成功率；（9）评测结果——output/trace/component/drift 四层评估分数与通过状态。

### 运行记录生命周期与状态机

AgentRun 记录在系统中经历以下状态转换：CREATED（触发事件发生，run_id 生成并写入任务/流水线记录）→ DISPATCHED（agent 被调用，spawn 记录创建，session 关联建立）→ IN_FLIGHT（agent 执行中，token 用量和 MCP 调用持续写入，事件总线广播进度事件）→ COMPLETED（agent 返回结果，resolution 和 outcome 写入，进入 REVIEW 子状态）→ REVIEWED（Aegis 审查完成，审查结论写入）→ EVALUATED（评测引擎运行，评估分数写入 eval_runs）→ ARCHIVED（记录完整闭合，可供外部查询）。状态转换通过事件总线以 run.status_changed 事件广播，外部系统可通过 SSE 订阅或 webhook 接收。

### 数据模型关联改造

在现有数据表基础上，引入 run_id 作为跨表关联的统一标识。具体改造方式：（1）在 spawn_history 表增加 run_id 列及索引，spawn 记录在创建时即绑定 run_id；（2）在 token_usage 表已有 task_id 基础上增加 run_id 列，使 token 消耗可直接按 run 聚合；（3）在 quality_reviews 表增加 run_id 列，使审查结论直接关联到具体 run；（4）在 mcp_call_log 表增加 run_id 列，使工具调用可追溯到具体 run；（5）在 eval_runs 表增加 run_id 列，使评估结果与 run 绑定。以上改造均为非破坏性的列增加（ADD COLUMN），不影响既有数据写入路径；对于历史数据，run_id 留空，查询时按 run_id IS NOT NULL 过滤即可区分新老记录。

### 查询接口与外部访问

AgentRun 通过统一的查询接口暴露。查询接口支持以下访问模式：（1）按 run_id 精确获取单条记录的完整聚合视图；（2）按 agent_name 或 task_id 检索相关 run 列表，支持时间范围、状态、outcome 等过滤条件；（3）按 workspace_id 和 agent_name 聚合统计（总执行次数、成功率、平均耗时、总成本、平均评测分数），支撑排行榜和仪表盘；（4）导出模式——返回符合 OpenClaw JSON 格式的完整记录，供外部 CI/CD 工具和数据分析流水线消费。查询接口复用了现有的 EventBus 基础设施：在 AgentRun 各生命周期节点，系统通过 eventBus.broadcast('run.updated', runSnapshot) 推送增量更新，SSE 客户端和 webhook 订阅者可实时接收。

### 与现有任务派发流程的集成

AgentRun 的 run_id 在任务派发流程中的生成和传播路径如下：（1）dispatchAssignedTasks() 将任务状态从 assigned 更新为 in_progress 时，同时生成 run_id（格式：run-{task_id}-{timestamp}），写入任务的 metadata.run_id；（2）调用 callClaudeDirectly() 或 runOpenClaw() 执行 agent 前，调用 recordSpawnStart() 并传入 run_id——spawn_history 表新增 run_id 列接收该值；（3）agent 执行过程中的 token 用量写入 token_usage 时，从中继的 session 上下文提取 run_id；（4）agent 返回结果后，dispatchAssignedTasks() 将 resolution 写入任务表，此时 run_id 已存在于任务 metadata 中；（5）runAegisReviews() 执行审查时，从任务的 metadata.run_id 读取并写入 quality_reviews.run_id；（6）后续评测引擎运行时同样从任务 metadata.run_id 获取并写入 eval_runs.run_id。该路径确保 run_id 在现有流程中自然传播，无需重构现有派发逻辑的核心结构。

### 关键模块

（1）RunRecordManager：负责 run_id 的生成、生命周期状态管理和聚合查询。核心方法包括 createRun(trigger)、transitionRun(runId, newStatus)、getRun(runId) 和 queryRuns(filters)。（2）RunRecordAggregator：负责将分散在多表中的数据聚合为单一 AgentRun 视图。查询时通过 run_id 并行从 tasks、spawn_history、token_usage、quality_reviews、mcp_call_log、eval_runs 六张表获取分片数据，在应用层组装为完整记录。（3）RunEventPublisher：在 RunRecordManager 执行状态转换时，构建 runSnapshot 并通过 eventBus.broadcast('run.updated', runSnapshot) 发送事件。现有的 webhook 监听器（initWebhookListener）扩展对 'run.updated' 事件类型的订阅，将 run 数据推送到配置的外部 URL。（4）RunQueryAPI：新增 /api/runs 路由，支持 GET /api/runs（列表查询）、GET /api/runs/[run_id]（详情）、GET /api/runs/stats（聚合统计）。复用现有 requireRole 鉴权和 rate-limit 中间件。

### 技术效果

（1）可查询性：通过统一的 run_id 和聚合查询接口，一次请求即可获取某次 agent 执行的完整上下文——谁触发的、哪个 agent 执行的、在哪个 session 中、消耗了多少 token 和成本、返回了什么结果、审查是否通过、评测分数如何——无需跨多表手工联查。（2）可追踪性：run_id 在整个执行链路中贯穿任务、spawn、token、审查、评测各环节，形成端到端的执行溯源链；结合 activities 流可还原完整的时序事件序列。（3）可评估性：评测引擎的四层评估结果通过 run_id 直接附着到执行记录上，使每次 agent 执行的质量可量化、可比较、可追踪趋势（通过 drift 检测）。（4）外部可访问性：通过 REST API 的标准化 JSON 格式和 webhook/SSE 推送，外部 CI/CD 流水线、数据看板、审计系统可以直接消费 AgentRun 记录，无需理解内部多表结构。（5）可扩展性：AgentRun 抽象层不改变现有表的核心写入路径（仅增加列），新增的 run 事件类型可被现有 webhook 基础设施直接复用；未来引入新的评测维度或执行类型时，只需在聚合视图中增加对应数据源的关联即可。

### 风险与待确认点

（1）与现有任务、会话、spawn history、成本统计、事件和工具入口的关联稳定性：run_id 在各表中的传播依赖现有任务派发和审查流程在各步骤正确传递 run_id 上下文。如果某个中间步骤（如 token 用量记录）因异常提前返回而未写入 run_id，则该记录的 token 数据将留在"未归因"（unattributed）集合中。建议在各写入点增加防御性日志，并定期扫描未归因数据。（2）历史数据兼容：改造前已有的 spawn_history、token_usage、quality_reviews 等记录没有 run_id，查询时需通过 run_id IS NOT NULL 过滤。如需为历史数据回填 run_id，需要基于 task_id 和 session_id 的启发式匹配，可能存在一定的匹配误差。（3）大量 run 记录下的查询性能：聚合查询涉及最多六张表的并行读取，在 run 数量达到数十万级别时可能产生性能压力。建议在 run_id 列上建立索引，并对 stats 聚合查询引入缓存层（如基于现有 SESSION_CACHE_TTL_MS 模式）。（4）与 pipeline_run 的关系：pipeline 的一次运行（pipeline_run）包含多个步骤，每个步骤可能对应一个 AgentRun。两者的关系为 1:N，需在 pipeline_runs 的 steps_snapshot 中增加每个步骤的 run_id 引用，以支持从流水线视角下钻到单步执行详情。
