## 技术方案

本技术方案在智能工作台已有的任务派发、agent 管理、会话记录和事件推送体系之上，引入"执行运行记录"（Execution Run）作为第一类实体，对每一次 agent 执行过程进行结构化沉淀。执行运行记录将触发源、执行主体、关联任务或会话、执行步骤、产出结果、资源消耗和评估结论统一组织为一条可溯源、可查询、可复用的记录，解决当前系统中 agent 执行信息分散在多个表中、难以回答"一次 agent 工作从何处触发、经过哪些步骤、消耗多少、是否经过质量评估"等问题。

### 核心技术构思

方案的核心是在不破坏现有任务（task）、agent、会话（session）、事件总线和审核日志体系的前提下，新增一个"执行运行记录"实体。该实体作为 agent 每一次完整执行周期的统一载体，包含以下关键设计原则：（1）一次执行一条记录——无论执行是由任务派发、定时调度、人工指令还是 API 调用触发，均生成一条独立且不可变的运行记录；（2）记录即溯源链——每条运行记录携带触发上下文、执行主体标识、关联任务与会话引用、时间区间和状态变迁序列，形成完整的可追溯链条；（3）消耗与评估附着——运行记录聚合本次执行中的 token 消耗、工具调用统计和执行后评估结论，使成本核算和质量评测可直接定位到单次执行；（4）工具化可查询——运行记录通过 RESTful 查询接口和事件推送对外暴露，支持按 agent、任务、时间范围、状态、评估结果等多维度检索，并支持外部系统通过标准格式导出和复用。

### 整体架构

执行运行记录系统位于现有任务调度层和 agent 执行层之间，作为横切关注点嵌入已有的执行路径中。整体架构包含四个层次：（1）记录采集层——在任务派发、agent 队列领取、agent 心跳上报、会话创建与关闭、质量审核完成等关键节点，自动生成或更新运行记录，不要求 agent 或网关做额外改造；（2）记录存储层——在现有 SQLite 数据库中新增 execution_runs 主表及关联的 execution_steps、execution_evaluations 附表，复用已有的 WAL 模式、busy_timeout 和 workspace 隔离机制；（3）关联绑定层——通过 execution_run_id 外键将已有 token_usage 表、mcp_call_log 表和 quality_reviews 表中的记录与特定运行记录绑定，实现分散数据的统一溯源；（4）查询服务层——新增 RESTful API 端点 /api/execution-runs，提供列表查询、详情获取、按维度聚合统计和结构化导出能力，同时通过已有 SSE 事件总线推送运行记录的生命周期事件。

### 执行运行记录数据结构

执行运行记录主表 execution_runs 包含以下核心字段：唯一标识 run_id；触发类型 trigger_type（包括 task_dispatch、cron_schedule、direct_command、api_call、webhook、manual_review 等枚举值）；触发源标识 trigger_source（指向触发该执行的任务 ID、定时任务 ID 或 API 请求标识）；执行主体 agent_id 和 agent_name；关联会话 session_id；关联任务 task_id（可选，非任务触发的执行可为空）；运行状态 run_status（包括 pending、running、completed、failed、cancelled、timeout）；状态变迁时间戳 started_at、completed_at；执行时长 duration_ms；入口指令或提示词 input_prompt（摘要或哈希）；产出结果摘要 result_summary 和结果详情引用 result_ref；token 消耗聚合 input_tokens、output_tokens、total_tokens 和估算成本 cost_estimate；工具调用聚合 tool_call_count、tool_success_count；工作空间标识 workspace_id。

除主表外，方案包含两张附表以支持细粒度追踪。execution_steps 表记录执行过程中的关键步骤事件，每条步骤包含 step_id、所属 run_id、步骤类型（如 task_claimed、model_routing、gateway_dispatch、tool_call、review_submitted、result_parsed）、步骤状态、开始与结束时间戳、关联的 token_usage 记录引用和 mcp_call_log 记录引用。execution_evaluations 表存储附着于运行记录的评估结果，每条评估包含 eval_id、所属 run_id、评估层级（output / trace / component / drift）、评分值、是否通过、评估详情和评估时间。两张附表均通过 run_id 外键与主表关联，支持按运行记录快速检索完整的执行轨迹和评估历史。

### 触发溯源机制

触发溯源机制通过在现有执行入口点植入运行记录的创建逻辑来实现。系统识别以下触发路径并分别建立溯源链：（1）任务派发触发——当调度器的 dispatchAssignedTasks 或 agent 通过队列 API 领取任务时，系统在将任务状态变更为 in_progress 的同时创建一条 execution_run，trigger_type 设为 task_dispatch，trigger_source 记录任务 ID，agent_id 指向领取任务的 agent；（2）定时调度触发——当 cron 调度器 spawnRecurringTasks 生成任务实例并派发时，创建的运行记录 trigger_type 为 cron_schedule，trigger_source 记录 cron 任务 ID；（3）直接指令触发——当通过 CLI、MCP 工具或 REST API 向 agent 直接发送指令（而非通过任务队列）时，trigger_type 为 direct_command 或 api_call，trigger_source 记录指令来源标识；（4）质量审核触发——Aegis 审核流程中每一次审核执行也生成运行记录，trigger_type 为 manual_review，trigger_source 指向对应的 quality_review 记录。所有触发路径均复用现有的 eventBus.broadcast 机制，在运行记录创建和状态变更时发出 execution_run.created 和 execution_run.status_changed 事件。

### 全链路关联机制

全链路关联机制解决当前 token_usage、mcp_call_log 和 quality_reviews 等表与具体执行之间关联松散的问题。方案采用"后向关联 + 执行上下文传播"的双重策略。（1）后向关联：在 execution_runs 表创建后，其 run_id 作为上下文标识通过执行路径向下传播。任务派发时，dispatchAssignedTasks 函数在创建运行记录后将 run_id 写入任务调度的上下文对象；agent 通过网关执行任务时，网关返回的 session_id 与 run_id 一同写入 token_usage 表的对应记录中，使每笔 token 消耗可追溯到具体执行。（2）执行上下文的写入时机：token_usage 记录写入时，除已有 session_id 和 task_id 外，增加 execution_run_id 字段；mcp_call_log 记录写入时，除已有 agent_name 外，增加 execution_run_id 字段。（3）quality_reviews 表已有 task_id 字段，方案通过 task_id 将审核记录间接关联到对应的执行运行记录（同一任务可能有多轮执行-审核循环，通过时间区间匹配确定对应关系）。通过上述关联，系统可以回答"某次执行消耗了多少 token、调用了哪些工具、每个工具的成功率、是否经过了质量审核以及审核结论如何"等溯源问题。

### 评估附着机制

评估附着机制将已有的四层评估引擎（输出层、轨迹层、组件层、漂移层）与执行运行记录绑定，使评估结论从"针对 agent 的阶段性统计"下沉为"针对单次执行的判定"。（1）评估触发时机：当一次执行运行记录的状态变更为 completed 或 failed 时，系统自动触发对应 agent 的四层评估流程。评估引擎读取本次执行关联的 token_usage 和 mcp_call_log 数据，计算输出完成率、工具可靠性、收敛性等指标。（2）评估结果存储：每层评估结果作为一条 execution_evaluations 记录写入，包含评分、是否通过和详细说明。运行记录主表的 evaluation_status 字段汇总整体评估状态（pending / passed / warning / failed）。（3）排行榜与审计扩展：execution_evaluations 表支持按 agent、时间段、评估层级聚合查询，为排行榜展示和运行审计提供数据基础。外部系统可通过查询接口获取特定 agent 在特定时间段内的评估趋势和单次执行评分，无需理解内部评估逻辑。

### 工具化查询与导出接口

工具化查询与导出接口通过新增 /api/execution-runs 端点实现，复用现有认证（requireRole）、限流（readLimiter / mutationLimiter）和工作空间隔离机制。接口提供以下能力：（1）列表查询——支持按 agent_id、task_id、trigger_type、run_status、时间范围（since / until）、workspace_id 等参数组合过滤，返回分页结果，每条记录包含主表字段及聚合的 token 消耗和工具调用统计；（2）单条详情——通过 /api/execution-runs/[run_id] 返回完整运行记录，包含 execution_steps 步骤列表和 execution_evaluations 评估结果；（3）聚合统计——通过 /api/execution-runs/stats 端点支持按 agent、时间段、触发类型等维度聚合查询，返回执行次数、成功率、平均耗时、总 token 消耗、总成本、平均评分等统计指标；（4）结构化导出——通过 /api/execution-runs/export 端点支持 JSON 和 CSV 格式导出，导出内容包含运行记录主字段及关联的评估结果，便于外部系统如 CI/CD 流水线、数据仓库或自定义分析工具消费。

### 与现有系统的复用与集成

方案最大化复用已有系统组件，避免引入新的外部依赖或执行框架。（1）数据库复用：新增表建立在现有 SQLite 数据库之上，复用已有的 WAL 模式、busy_timeout 并发控制、workspace 隔离和自动备份机制。表的创建通过现有迁移框架（migrations.ts）以增量迁移方式添加；（2）事件总线复用：运行记录的生命周期事件（创建、状态变更、评估完成）通过已有 ServerEventBus 的 broadcast 方法推送，复用 /api/events 的 SSE 通道向客户端实时推送，无需新增推送通道；（3）调度器复用：运行记录的自动创建嵌入已有 scheduler.ts 的任务派发流程（dispatchAssignedTasks、runAegisReviews、autoRouteInboxTasks 等函数），在关键状态变更点插入记录写入逻辑，不改变原有调度时序；（4）搜索复用：在已有 /api/search 全局搜索中增加 execution_run 实体类型，使用户可通过统一搜索框检索运行记录；（5）MCP 工具复用：在已有 mc-mcp-server.cjs 的 MCP 工具集中增加 execution-runs 相关工具，使外部 agent 可通过 MCP 协议查询运行记录。以上复用策略确保运行记录系统不是一套与现有执行体系割裂的独立日志，而是对现有体系的自然补充和增强。

### 执行运行记录的生命周期与数据流

执行运行记录的生命周期与现有任务状态流协同运转。以一次典型的任务派发执行为例：（1）调度器调用 dispatchAssignedTasks 选取待派发任务，在将任务状态变更为 in_progress 前，创建一条 run_status 为 pending 的 execution_run 记录，填入 trigger_type=task_dispatch、task_id、agent_id 和 workspace_id；（2）调度器完成模型路由选择（classifyTaskModel）后，将选定的模型标识和 gateway 派发参数写入 execution_steps 表，步骤类型为 model_routing；（3）通过网关向 agent 发送任务提示词后，将步骤类型 gateway_dispatch 写入 execution_steps，run_status 更新为 running，记录 started_at 时间戳；（4）agent 执行期间，网关通过心跳上报 token 消耗和工具调用信息——心跳处理逻辑在写入 token_usage 和 mcp_call_log 时携带当前执行上下文的 execution_run_id；（5）agent 返回执行结果后，调度器解析响应并将 run_status 更新为 completed，写入 completed_at、result_summary 和聚合的 token / tool_call 统计；（6）run_status 变更为 completed 触发评估流程，四层评估结果写入 execution_evaluations 表，主表的 evaluation_status 随之更新。若执行超时、agent 返回错误或达到最大重试次数，run_status 更新为 failed 或 timeout，同样触发评估流程但标记为异常执行。

### 执行上下文传播机制

执行上下文传播是实现全链路关联的关键技术手段。方案采用"请求级上下文对象"模式，在调度器和 API 处理函数中维护当前执行的上下文标识。（1）上下文初始化：在 dispatchAssignedTasks、runAegisReviews 和直接指令 API 处理器中，创建 execution_run 记录后立即将 run_id 存入一个请求级上下文对象（如 AsyncLocalStorage 或显式参数传递）；（2）上下文传递：上下文对象沿调用链向下传递——从调度器到网关调用函数（callOpenClawGateway），再到响应解析函数（parseAgentResponse），再到心跳处理逻辑和 token/mcp 日志写入函数；（3）写入时绑定：token_usage 插入、mcp_call_log 插入和 quality_reviews 插入函数在已有参数基础上接受可选的 execution_run_id 参数，当上下文中有活跃的 run_id 时自动绑定；（4）上下文清理：执行完成或失败后，上下文对象被清理，确保不会将后续无关操作错误关联到已结束的运行记录。该机制对已有函数签名的影响最小——仅需在相关写入函数中增加一个可选参数，不改变已有调用方的行为（未传 run_id 时保持现有行为不变），从而实现向前兼容。

### 扩展空间设计

方案在设计上预留了面向质量评测、排行榜、运行审计和外部工具集成的扩展空间。（1）排行榜扩展：execution_runs 和 execution_evaluations 表已包含 agent_id、评分、耗时、成本等排行所需的基础字段。可通过聚合查询按 agent 维度计算平均评分、总执行次数、成功率和平均 token 效率，支撑排行榜面板的数据展示。（2）运行审计扩展：每条运行记录的不可变性和完整的溯源链（触发源→执行主体→步骤序列→消耗→评估）天然支持审计需求。audit_log 表可通过 execution_run_id 引用运行记录，使审计事件能定位到具体执行。（3）外部工具集成：/api/execution-runs/export 端点输出的结构化 JSON 和 CSV 格式可直接被 CI/CD 流水线、数据仓库 ETL、监控告警系统或自定义分析仪表盘消费。MCP 工具集中的 execution-runs 工具使外部 AI agent 能通过标准协议查询运行记录，实现 agent 对自身历史执行的自我检查和跨 agent 执行经验复用。（4）可配置的保留策略：运行记录纳入已有调度器的数据清理（runCleanup）范围，通过 settings 表中的 execution_runs_retention_days 配置保留天数，支持按 workspace 独立设置。
