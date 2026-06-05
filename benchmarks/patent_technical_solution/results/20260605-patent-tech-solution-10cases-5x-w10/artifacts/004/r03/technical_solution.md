## 技术方案

本方案提出一种面向多 agent 协作工作台的 agent 执行过程结构化沉淀方法。该方法在现有任务分派、agent 管理、会话追踪、令牌统计、质量审核和事件总线的基础上，引入以"运行记录"（Run Record）为核心的统一执行溯源框架。该框架通过运行标识符（run_id）贯穿 agent 执行的完整生命周期，在任务状态变更、令牌消耗、工具调用和会话更新等现有钩子处自动采集执行上下文，将每次 agent 执行沉淀为包含触发源、执行主体、关联任务与会话、资源消耗、工具调用序列、执行结果和评估分数的结构化记录。以下从整体架构、数据模型、采集机制、溯源关联、评估附着、工具化查询和扩展空间七个方面进行阐述。

### 整体架构

本方案在现有智能工作台的 agent 任务派发、执行、结果展示及部分执行历史记录的基础上，构建一套结构化运行记录（Run Record）框架。该框架以"运行记录"为核心数据单元，在 agent 执行链路的关键节点自动捕获执行上下文，将一次 agent 执行过程沉淀为可溯源、可评估、可查询的结构化记录，同时复用现有任务、agent、会话、事件及工具入口，不引入与现有执行体系割裂的独立日志系统。

运行记录框架由以下核心组件构成：运行记录采集层、运行记录存储模型、溯源关联引擎、评估附着接口和工具化查询接口。采集层通过复用现有任务状态变更事件、agent 心跳与会话更新、令牌消耗记录写入、MCP 工具调用日志及质量审核事件等已有钩子，在 agent 执行链路的关键节点自动触发运行记录的创建与更新，无需 agent 或任务系统做侵入式改造。

每条运行记录以唯一的运行标识符（run_id）贯穿 agent 执行的完整生命周期：从触发源识别、agent 指派、会话绑定，到执行过程中的令牌消耗累积、工具调用序列记录，再到执行结束后的结果捕获、评估打分和外部通知。该运行标识符在任务分派时生成，并随事件总线广播至会话记录、令牌记录、工具调用日志等子系统，各子系统在写入自身数据时附带该标识符，实现跨表关联。

### 运行记录的数据模型

运行记录的数据模型以 agent_runs 表为核心，每条记录对应一次 agent 执行的完整实例。其关键字段设计如下：

- run_id：运行唯一标识符，在任务状态从待分派转为执行中时由系统生成，作为贯穿全链路的关联键
- agent_id / agent_name：执行该次运行的 agent 标识，复用现有 agents 表的主键和名称
- task_id：关联的任务标识，复用现有 tasks 表。一个任务可对应多条运行记录（重试、重新指派场景），运行记录与任务为多对一关系
- session_id / session_key：绑定的会话标识，可关联网关会话（OpenClaw gateway session）或本地会话（Claude Code session），复用现有会话发现机制
- trigger_source：触发来源，包括手动分派、定时任务（cron）、流水线步骤、webhook 回调、API 调用、其他 agent 委托等
- trigger_context：触发上下文，以 JSON 存储触发时的参数快照，如 cron 表达式、webhook 载荷摘要、上游任务标识等
- status：运行状态，取值包括 pending、running、completed、failed、cancelled
- started_at / completed_at：运行起止时间戳
- duration_ms：运行耗时（毫秒）
- model_used：本次运行使用的主要模型标识（如 claude-sonnet-4-20250514）
- token_usage：令牌消耗汇总，包含 input_tokens、output_tokens、total_tokens，通过在运行结束时聚合 token_usage 表中属于同一 run_id 的记录计算
- estimated_cost_usd：估算成本，基于模型定价和令牌消耗计算
- tool_calls_count / tool_calls_summary：工具调用次数及摘要（按工具名称分组的调用统计），从 mcp_call_log 表按 run_id 聚合
- outcome：运行结果，取值 success、failed、partial
- error_info：错误信息，以 JSON 存储错误类型、错误消息和堆栈摘要
- artifacts：产出物引用列表，以 JSON 存储关键产出物的路径或标识（如生成的文件、报告链接等）
- eval_attached / eval_score：是否已附着评估结果及综合评估分数，当评估引擎对本次运行完成打分后更新
- workspace_id / tenant_id：所属工作空间和租户标识，继承自关联的 task 和 agent，确保多租户隔离

除 agent_runs 主表外，方案还引入 run_event_log 表用于记录运行过程中的细粒度事件序列，每条事件包含 run_id、event_type（如 run_started、tool_called、token_consumed、checkpoint_reached、error_encountered、run_completed）、timestamp、event_data（JSON 格式的附加数据）。该表支持按时间轴重建一次运行的完整执行轨迹，为溯源和审计提供事件级精度。同时，对已有表（token_usage、mcp_call_log、eval_runs、quality_reviews）增加可空的 run_id 外键字段，以最小侵入方式建立与运行记录的关联，不影响现有非运行场景下的独立使用。

### 运行记录的自动采集机制

运行记录的采集不依赖 agent 或任务执行体的主动上报，而是通过在系统现有事件总线和 API 路由中植入采集钩子实现自动化。采集机制覆盖运行生命周期的三个阶段：启动采集、过程累积和结束归集。

1. 启动采集：当任务状态变更为 in_progress 且存在明确的 assigned_to agent 时，或当定时任务/流水线步骤触发 agent 执行时，系统自动创建一条 agent_runs 记录，生成 run_id，确定 trigger_source 和 trigger_context，并将 run_id 通过事件总线广播。同时向 run_event_log 写入 run_started 事件。
2. 过程累积：在 agent 执行期间，系统利用已有钩子自动归集运行数据。具体包括：(a) 令牌消耗归集——当 token_usage 表写入新记录时，若当前存在活跃的 run_id（通过 agent_name 和 task_id 匹配），将 run_id 写入该令牌记录；(b) 工具调用记录——当 mcp_call_log 表写入新记录时，以同样方式关联 run_id；(c) 会话关联——当网关会话或 Claude 会话更新时，通过 agent_name 和时间窗口匹配确定本次运行使用的会话，将 session_id 写入运行记录。上述关联不要求 agent 显式传递 run_id，而是由采集层通过 agent 标识和时间窗口自动匹配。
3. 结束归集：当任务状态变更为 done 或 agent 会话进入非活跃状态超过阈值时，采集层触发运行记录归集。系统聚合同一 run_id 下的所有令牌消耗记录，计算 total_tokens 和 estimated_cost_usd；统计 mcp_call_log 中同一 run_id 的工具调用次数和成功/失败分布；从任务表读取 outcome、error_message、resolution 等字段；计算 duration_ms，将 status 更新为 completed 或 failed，并向 run_event_log 写入 run_completed 事件。

对于重试场景：当同一任务被重新指派或 agent 执行失败后重试时，系统创建新的运行记录（新的 run_id），而非覆盖原有记录。原有运行记录保留其最终 status 和 outcome，新运行记录从头开始采集。这样保证了每次执行尝试都有独立、完整的运行轨迹，支持后续的失败原因对比分析和重试优化。对于 agent 同时执行多个任务的并发场景，采集层通过 task_id 精确区分不同运行，各运行记录独立归集各自的令牌消耗和工具调用，互不干扰。

### 溯源链与关联模型

溯源链的核心是回答以下问题：一次 agent 工作从何处触发、由谁执行、关联了哪个任务和会话、消耗了多少资源、产出了什么结果。本方案通过 run_id 作为统一关联键，将分散在多个子系统中的执行数据串联为可追溯的完整链路。

- 触发溯源：通过 trigger_source 和 trigger_context 字段，可追溯运行的上游触发原因。对于定时任务触发，可定位到具体的 cron 模板及其表达式；对于流水线触发，可定位到 pipeline_run 的步骤编号和触发人；对于 webhook 触发，可定位到 webhook_deliveries 中的来源载荷。每条运行记录均可沿触发链路回溯至最初的人机或系统操作。
- 执行溯源：通过 agent_name/session_id 关联，可定位执行该次运行的 agent 身份及其使用的会话实例。结合 run_event_log 中的事件序列，可还原 agent 在本次运行中的完整行为轨迹：何时启动、调用了哪些工具、每次工具调用的参数和结果、何时遇到错误、何时完成。
- 资源溯源：通过 run_id 关联 token_usage 表，可精确统计本次运行的令牌消耗（按模型分组的输入/输出分布）和成本。通过 mcp_call_log 关联，可统计工具调用的次数、成功率和耗时分布。支持按运行粒度的精细化成本归因。
- 结果溯源：通过 task_id 关联任务表，可获取本次运行对应的任务完整信息（标题、描述、优先级、标签、项目归属），以及运行结束后的 outcome、error_message、resolution。当存在质量审核时，可通过 task_id 关联 quality_reviews 表获取审核结论。

上述关联模型的实现依赖于 run_id 在事件总线中的传播。当采集层创建运行记录并生成 run_id 后，该标识符随 run_started 事件广播。token_usage 写入点、mcp_call_log 写入点和会话管理模块订阅该事件，将当前活跃的 run_id 缓存在内存中（以 agent_name + task_id 为键），后续写入操作自动附加 run_id。运行结束后，采集层广播 run_completed 事件，各订阅模块清除对应的缓存条目。该机制确保 run_id 的关联是自动且低延迟的，不需要上游系统改造。

### 评估附着机制

运行记录为质量评测提供了精确的附着目标。现有评估引擎（四层评估框架：输出层、追踪层、组件层、漂移层）原本以 agent 或任务为评估对象，本方案将其评估粒度细化到单次运行。

- 运行级输出评估：eval_runs 表新增 run_id 字段，评估引擎在执行输出层评估时，不再仅按 agent_name 和时间窗口聚合任务的完成率与正确率，而是直接关联到具体 run_id，计算该次运行对应的任务完成情况和反馈评分。评估结果同时更新 agent_runs 表中的 eval_attached 和 eval_score 字段。
- 运行级追踪评估：eval_traces 表新增 run_id 字段。当 agent 执行过程中产生工具调用序列时，该序列作为 trace 数据存入 eval_traces。评估引擎对 trace 进行收敛性分析（工具调用总次数与唯一工具数的比值），评估结果附着到对应 run_id，反映该次运行的推理效率。
- 运行级组件评估：基于 mcp_call_log 中同一 run_id 的工具调用记录，评估引擎统计工具调用的成功率和平均耗时，生成组件可靠性分数并附着到 run_id。
- 运行级漂移检测：通过对同一 agent 多次运行记录的评估分数序列进行基线比较，检测 agent 性能是否发生显著漂移。漂移检测结果可触发告警规则（复用现有 alert_rules 机制）。

### 工具化查询与外部集成

运行记录通过 REST API、事件订阅（webhook/SSE）和搜索接口对外暴露，使外部工具和系统可以查询、订阅和分析 agent 执行历史。

- REST 查询接口：新增 /api/runs 端点，支持按 agent、task、status、trigger_source、时间范围、成本范围、评估分数范围等多维度筛选。查询结果包含运行记录摘要列表（run_id、agent_name、task_title、status、duration_ms、estimated_cost_usd、eval_score），支持分页和排序。新增 /api/runs/[run_id] 端点返回单条运行的完整详情，包括关联的令牌消耗明细、工具调用序列、事件时间线和评估结果。
- 搜索集成：复用现有 /api/search 端点的多实体搜索架构，将 agent_runs 作为新的可搜索实体类型（type=run），支持按关键字搜索运行记录的任务标题、agent 名称和触发源描述。
- 事件订阅与 Webhook 推送：在现有事件总线中新增 run.started、run.progress、run.completed、run.failed 事件类型。当运行记录状态变更时，事件总线广播对应事件，SSE 客户端可实时接收。Webhook 订阅（复用现有 webhooks 表）支持选择 run.* 事件类型，将运行记录摘要推送到外部系统（如通知平台、数据仓库、审计系统）。
- 排行榜支撑：通过对 agent_runs 表按 agent_name 和时间窗口聚合，可计算每个 agent 的运行成功率、平均耗时、平均成本、评估分数均值、吞吐量（单位时间运行数）等指标。这些指标通过 /api/runs/leaderboard 端点暴露，支持按不同指标排序，为 agent 排行榜提供数据基础。
- 审计支撑：运行记录的 run_event_log 提供事件级审计轨迹。结合现有 audit_log 表中与 run_id 关联的记录，外部审计系统可通过 /api/runs/[run_id]/audit 端点获取一次运行从触发到完成的所有系统级操作记录（谁在何时执行了何种操作），满足合规审计需求。

### 扩展空间

运行记录框架在核心功能之外预留了明确的扩展空间，以满足未来可能的高级需求。

- 自定义评估维度：agent_runs 表的 metadata 字段（JSON 格式）允许外部评估系统写入自定义评分维度和评估结果，无需修改核心表结构。评估引擎的插件机制（复用现有 plugin-loader 架构）支持注册自定义评估器，在运行结束时自动执行并写入 metadata。
- 外部工具集成：运行记录的 webhook 推送机制使外部数据平台（如 BI 工具、数据湖、实验追踪系统）可以自动接收运行数据，无需轮询 API。运行记录导出端点（/api/runs/export）支持 CSV/JSON 格式导出，便于离线分析。
- 运行对比与 A/B 分析：基于同一任务多次运行的记录（不同 agent、不同模型或不同配置参数），可构建对比视图，分析不同执行策略的效果差异。扩展端点 /api/runs/compare 接收多个 run_id，返回并排对比结果。
- 运行回放：基于 run_event_log 的事件序列，可以按时间轴回放一次运行的工具调用过程和状态变化。该能力可用于调试 agent 行为、教学演示或合规审查。回放端点 /api/runs/[run_id]/replay 按时间顺序返回事件流。
- 数据生命周期管理：运行记录支持可配置的保留策略。通过运行记录的 created_at 时间戳和 workspace_id，系统可定期清理超过保留期的旧记录，同时支持在清理前将数据导出到外部长期存储。该策略复用现有 cleanup 调度机制。
