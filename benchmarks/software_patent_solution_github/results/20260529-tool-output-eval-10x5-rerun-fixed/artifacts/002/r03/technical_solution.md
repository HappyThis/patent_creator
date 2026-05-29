## 技术方案

本方案提出一种面向 Mission Control 多 agent 编排平台的 agent 执行运行记录（Agent Execution Run Record）统一契约与关联机制。该机制在现有任务派发、agent 注册、会话管理、spawn 历史、token 用量追踪、评估框架和安全审计等子系统之上，建立一个贯穿 agent 执行全过程的结构化运行记录标识与数据附着模型，使每次 agent 执行从派发到终态的完整过程信息可被查询、追踪、评估和外部工具访问，而非通过事后梳理分散日志来推断。

### 技术问题

Mission Control 已具备任务派发、agent 注册、spawn 历史、token 用量追踪、质量审查和四层评估框架等子系统，但这些子系统各自独立记录 agent 执行过程的不同维度数据，缺乏统一的执行标识将它们关联为一条可查询的完整记录。具体表现为：当需要复盘某次 agent 执行的全貌时，必须在 tasks、spawn_history、token_usage、quality_reviews、eval_runs、mcp_call_log 等多个表之间手动关联，查询路径分散且无法保证关联完整性；外部工具（排行榜、审计系统、CI/CD 流水线）无法通过标准化接口获取某次执行的结构化数据；重试场景下同一任务的多次执行之间缺乏显式的链式追溯关系。这些技术障碍使得 agent 执行的"可观测性"停留在各子系统内部的局部视角，无法形成统一的执行全景视图。

### 核心技术方案

本方案在现有 Mission Control 架构之上引入"运行记录"（Run Record）作为 agent 执行过程的第一公民实体。核心机制包括三个层面：（1）标识层——在任务派发时生成全局唯一的 run_id，该标识贯穿派发、spawn、会话、token 用量、质量审查、评估和审计全链路，并在重试场景中通过 parent_run_id 形成链式追溯；（2）存储层——引入轻量 runs 表存储运行记录的特有字段（状态、时间线、异常信息等），避免在各子系统间冗余存储已有明细数据，同时通过 run_id 将各子系统的记录编织为可 JOIN 聚合的统一视图；（3）接口层——提供标准化的 REST API（/api/runs）和 SSE 事件流（run.* 事件类型），使内部前端和外部工具以统一契约获取 agent 执行全貌。运行记录不替代 tasks、spawn_history、token_usage 等现有表，而是在其上建立一层轻量的统一查询与追踪面。

### 运行记录契约设计

运行记录的数据契约定义了一组标准字段，每个运行记录在创建时即获得这些字段的初始值，并在执行过程中逐步填充。核心字段分为两类：第一类为存储字段，写入轻量 runs 表（每条约 200 字节），包括 run_id（全局唯一标识，在 dispatch 时生成，格式为 run-{timestamp}-{random}）、task_id（关联的任务记录，对应 tasks 表主键）、agent_id 和 agent_name（执行该运行的 agent 标识）、trigger（触发来源，包括 manual_dispatch、cron_schedule、webhook_event、quality_review、retry 等）、model（实际使用的模型标识）、spawn_id（关联 spawn_history 表的 spawn 记录）、session_id（关联网关会话标识）、status（运行状态，包括 pending、dispatched、running、completed、failed、rejected、cancelled）、timeline（关键时间节点数组，包括 dispatched_at、spawn_started_at、spawn_finished_at、review_started_at、review_completed_at、completed_at）、outcome（终态结果，success/failed/partial/abandoned）、error_info（异常信息，包含 error_code、error_message、retry_count）、parent_run_id（重试场景中链接到原运行记录）。第二类为聚合字段，在查询时通过 run_id 从现有表实时 JOIN 计算，包括 cost_summary（聚合自 token_usage 表的 input_tokens、output_tokens、total_tokens、estimated_cost_usd）、eval_results（附着于该运行的评估结果快照，聚合自 eval_runs 表）、quality_review（质量审查结果，聚合自 quality_reviews 表的 verdict 和 notes）。

该契约的关键特征是不引入重型冗余存储。系统新增轻量 runs 表仅存储运行记录的特有字段（run_id、task_id、agent_id、trigger、status、timeline、error_info 等，每条约 200 字节），而 cost_summary、eval_results 和 quality_review 等可从现有表派生的数据，通过 run_id 在查询时利用 SQLite 的多表 JOIN 实时聚合。查询实现按 run_id 关联 tasks、spawn_history、token_usage、quality_reviews、eval_runs、security_events 等表数据，实时组装为完整的运行记录 JSON 响应。这样避免了将 token 用量明细、评估结果等已有数据在 runs 表中冗余存储，同时 runs 表的轻量设计使得百万级运行记录不会造成存储压力。

### 运行记录的生命周期

运行记录的生命周期与现有任务派发流程深度集成，在关键节点自动记录和更新。

生成阶段：当系统通过 task-dispatch 模块派发任务时（无论是手动派发、定时调度触发还是 webhook 触发），dispatch 函数首先生成 run_id，将其写入当前派发上下文，并随派发请求传递给 agent 网关。同时，运行记录的状态初始化为 pending，dispatched_at 时间戳被记录。

执行追踪阶段：agent 网关回调或 agent 自身通过 spawn 接口启动执行时，spawn_history 表的记录中携带该 run_id，使得 spawn 的启动、完成、失败、退出码、耗时等信息与运行记录自动关联。agent 执行过程中产生的 token 用量（token_usage 表）、MCP 工具调用日志（mcp_call_log 表）、安全事件（security_events 表）均通过 run_id 或 task_id 与运行记录建立关联。

审查与终态阶段：任务进入 quality_review 状态后，Aegis 质量审查的结果（quality_reviews 表）通过 task_id 关联到运行记录。审查通过后运行记录状态转为 completed；审查驳回后状态转为 rejected，并根据 dispatch_attempts 计数决定是否触发自动重试（生成新的 run_id，通过 parent_run_id 字段链接到原运行记录）。任务支持重试时，每次重试生成独立的 run_id，通过 parent_run_id 形成运行记录的链式追溯关系。

评估附着阶段：运行记录完成后，系统可触发评估流程（输出层、追踪层、组件层、漂移层四层评估）。评估结果写入 eval_runs 表并携带 run_id，使得评估分数和详情直接附着于运行记录之上。运行记录查询 API 返回的 eval_results 字段包含该运行关联的所有评估结果快照。

### 与现有子系统的关联机制

运行记录机制通过以下方式与 Mission Control 现有子系统建立稳定关联：

与任务系统（tasks）：run_id 写入 tasks 表的扩展字段，任务查询时可反向定位关联的运行记录。task_id 是运行记录的核心外键，任务的状态变更通过现有 event-bus 的事件（task.status_changed）广播，运行记录服务监听该事件并更新运行记录状态。

与 spawn 历史（spawn_history）：spawn_history 表新增 run_id 字段（可为空以兼容历史数据）。spawn 启动时，调用方传入 run_id，recordSpawnStart 函数将其写入记录；recordSpawnFinish 更新终态时同步更新运行记录的 spawn 阶段时间戳。

与成本统计（token_usage）：token_usage 表已具有 task_id 和 agent_name 字段，运行记录查询时通过 task_id 聚合其下所有 token_usage 记录，计算 input_tokens、output_tokens、total_tokens 和 estimated_cost_usd 的汇总值，作为运行记录的 cost_summary 字段。

与会话管理：spawn_history 中的 session_id 字段链接到网关会话，运行记录可据此关联 claude_sessions 表和网关 session store 中的会话元数据（模型、消息数、上下文 token 数等）。

与事件总线（event-bus）：运行记录的关键状态变更（run.dispatched、run.spawned、run.completed、run.failed、run.evaluated）作为新的事件类型注册到 ServerEventBus，通过 SSE 实时推送给前端和控制台，与现有的 task.* 和 agent.* 事件共存于同一事件流。

与 webhook 系统：运行记录事件可作为 webhook 的触发事件类型，webhooks 表的 events 字段中可配置 run.* 事件，使外部系统在运行记录状态变更时收到回调通知。

### 评估结果的附着与联动

运行记录与现有四层评估框架的集成方式如下：

评估触发：运行记录进入 completed 状态后，可通过两种方式触发评估：（1）自动触发——系统检测到运行记录完成，自动调用 runOutputEvals、evalReasoningCoherence、evalToolReliability、runDriftCheck 等现有评估函数；（2）手动触发——通过 POST /api/agents/evals 接口以 action=run 参数指定 agent 和 layer 触发评估，并传入 run_id 以限定评估范围。

评估结果附着：评估函数执行后，结果除写入现有 eval_runs 表外，同步生成评估快照并附着于运行记录。运行记录的 eval_results 字段为数组结构，每条评估结果包含 layer（output/trace/component/drift）、score、passed、detail 和 created_at，与 eval_runs 表结构一致但作为快照固化在运行记录视图内，确保历史查询时评估结果的不可变性。

评估聚合与信任评分联动：运行记录查询 API 提供 eval_summary 聚合字段，汇总四层评估的通过率和平均分。评估结果同时反馈到 agent_trust_scores 表，影响该 agent 的 trust_score 计算（综合考虑 auth_failures、injection_attempts、secret_exposures、successful_tasks、failed_tasks 和 eval 通过率），为后续任务派发时的 agent 选择提供参考。

### 外部查询与工具集成

运行记录通过标准化 API 对外暴露，支持内部前端和外部工具的统一查询。

查询 API：GET /api/runs 提供运行记录列表查询，支持按 agent_name、task_id、status、outcome、model、timeframe（day/week/month/all）等参数过滤，支持分页。GET /api/runs/{run_id} 返回单条运行记录的完整视图，包含所有关联子系统的聚合数据。GET /api/runs/{run_id}/timeline 返回该运行的完整时间线，按时间顺序列出 dispatched、spawn_started、spawn_finished、review_started、review_completed、evaluated 等事件节点。

事件流集成：运行记录的状态变更通过 /api/events 的 SSE 流实时推送，新增事件类型 run.dispatched、run.spawned、run.completed、run.failed、run.evaluated，与现有 task.* 和 agent.* 事件共用同一 SSE 通道，前端可通过 EventSource 订阅并按 workspace_id 过滤。

外部工具访问：运行记录的标准化 JSON 契约使得排行榜系统可直接查询 /api/runs 获取 agent 执行统计并计算排名；审计系统可遍历运行记录的时间线和评估结果进行合规检查；CI/CD 流水线可通过 webhook 订阅 run.completed 事件触发后续自动化步骤；MCP 工具可通过 API key 认证访问运行记录数据，实现 agent 执行的可观测性集成。

### 技术效果

本方案带来的技术效果包括：

可追溯性提升：通过 run_id 统一标识，每次 agent 执行从派发到终态的完整链路可被单次查询获取，无需跨多表手动关联。重试链通过 parent_run_id 形成有向追溯图，支持审计和问题复盘。

评估闭环：评估结果直接附着于运行记录而非游离在独立评估表中，使得每次执行的评估分数与执行上下文绑定，形成"派发-执行-审查-评估"的完整闭环，为质量评测和排行榜提供结构化数据基础。

外部可集成性：标准化 JSON 契约和 REST API 使外部工具无需理解 Mission Control 内部表结构即可获取运行记录全貌，降低了排行榜、审计、CI/CD 等系统的集成成本。SSE 事件流和 webhook 机制提供实时推送能力。

零冗余存储：运行记录不引入新的重型存储表，而是通过 run_id 在查询时实时聚合现有表数据，避免了数据同步维护的复杂性，同时利用 SQLite 的 JOIN 性能和现有索引保证查询效率。

渐进兼容：spawn_history 和 token_usage 等表的 run_id 字段设为可空，历史数据在无 run_id 时按原有逻辑工作，新派发的执行自动获得 run_id，渐进式覆盖，不破坏存量数据。

### 风险与待确认问题

以下设计边界已在方案中确定处理方式：

token_usage 表的关联策略——token_usage 表同时维护 task_id 和 run_id 两个关联字段，确保与现有成本统计面板的向后兼容。run_id 在 token 记录写入时由调用方同步填充；历史数据中 run_id 为空时，通过 task_id 查询 runs 表完成回填，回填为一次性批量操作。

spawn_history 的 run_id 对齐——spawn_history 表新增 run_id 字段（可空以兼容历史数据）。spawn 请求中携带 run_id，agent 网关在 spawn 回调中按原值回传，spawn_history 记录写入（recordSpawnStart）时同步存储该 run_id。

JOIN 查询性能——当 token_usage 和 mcp_call_log 等表数据量增长到百万级别时，引入物化视图 run_summary 作为查询加速层，定期（每分钟）从各源表增量刷新，将高频查询的聚合结果预先计算并缓存，避免每次请求都执行全表 JOIN。

评估快照的固化时机——runs 表维护 eval_status 字段（none / pending / partial / complete），标识评估就绪状态。查询时若 eval_status 为 pending，eval_results 返回空数组并附带状态标识；评估完成后通过 SSE 事件 run.evaluated 推送通知，调用方可据此获取最新评估结果。
