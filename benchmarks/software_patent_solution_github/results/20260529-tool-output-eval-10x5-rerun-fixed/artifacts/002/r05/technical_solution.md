## 技术方案

### 核心技术问题与解决思路

Mission Control 作为多 agent 任务编排平台，在 agent 执行过程中已产生多种独立的运行记录：spawn_history 记录 agent 会话的启动与终止，token_usage 记录每次模型调用的 token 消耗与成本，eval_runs 存储评估运行的得分与元数据，activities 流记录平台级业务事件，tasks 表记录任务状态与最终结果。然而这些数据源各自独立维护，缺少统一的关联契约，导致以下问题：（1）无法从一次 agent 执行出发，串联其完整的 token 消耗、成本、评估结论和业务事件；（2）评估引擎的运行结果与 spawn 记录之间缺乏稳定引用，无法按执行批次做对比分析；（3）activities 流中的事件与 spawn/token/eval 记录之间缺少结构化的 traverse 路径，查询效率低且容易遗漏。

本技术方案不引入新的核心存储表，而是在现有数据模型基础上，以 spawn 事件为运行周期锚点，定义一套统一运行记录契约（Unified Run Record Contract）。该契约通过 spawn_id → session_id → task_id → eval_run_id 的多级关联链路，将 spawn_history、token_usage、activities、eval_runs、tasks 五类数据源编织为可查询、可追踪、可附着评估结果的统一视图。

### 统一运行记录数据契约设计

统一运行记录契约定义了一条 agent 执行运行的完整数据画像，由以下层级组成，每层对应一个现有数据源，并以 spawn_id 为根锚点穿透关联。

第一层——运行元数据（Run Metadata）：取自 spawn_history 表。以 spawn_id 为主键，包含 agent_id、agent_name、spawn_type（manual/auto/scheduled）、trigger（触发来源标识）、session_id、status（running/completed/failed/cancelled）、exit_code、error、duration_ms、workspace_id、created_at、finished_at。该层提供运行的起止时间、退出状态和执行耗时，是所有下游关联的根节点。

第二层——Token 消耗与成本（Token & Cost）：取自 token_usage 表。以 spawn_history.session_id 为关联键，聚合该会话下所有 token_usage 记录，得到 input_tokens 总和、output_tokens 总和、cost_usd 总和，以及按 model 分组的消耗明细。同时通过 token_usage.task_id 关联到具体任务，支持计算单任务的 token 成本。该层提供运行的经济成本画像。

第三层——评估结果附着（Eval Attachment）：取自 eval_runs 表。以 agent_name 和 workspace_id 为关联键，将评估运行（output/trace/component/drift 四层评估）的得分、结论和元数据附着到运行记录上。当同一 agent 在 spawn 时间窗口内存在多次评估运行时，取最近一次 completed 状态的评估结果。该层提供运行的质量评估画像。

第四层——业务活动流（Activity Stream）：取自 activities 表。以 spawn 运行时间窗口 [created_at, finished_at] 为过滤范围，按 entity_type 和 entity_id 筛选与该次运行相关的业务事件（task_created、task_updated、agent_status_change 等），按时间序排列。当 spawn 尚未结束时，窗口上限取当前时间。该层提供运行过程中可审计的业务操作轨迹。

第五层——任务结果（Task Outcome）：取自 tasks 表。通过 token_usage.task_id 反向查找本次运行涉及的所有任务，收集每个任务的 status、priority、outcome（success/failure）和 tags。该层提供运行所处理任务的业务结果汇总，为评估引擎的 output 维度（任务完成率与正确性）提供直接数据来源。

五层数据通过以下关联链路编织：spawn_id → session_id → token_usage 记录（按 session_id 聚合，同时带出 task_id）→ tasks 记录（按 task_id 反查）→ activities 记录（按时间窗口 + entity_id 过滤）→ eval_runs 记录（按 agent_name + workspace_id + 时间窗口匹配）。所有层级均受 workspace_id 多租户隔离约束，查询时以 workspace_id 为首要过滤条件。

设计原则：（1）增量构建——不新增核心存储表，利用现有 spawn_history、token_usage、eval_runs、activities、tasks 五表完成数据编织；（2）spawn 为锚——每次 agent spawn 作为一次不可分割的运行单元，所有下游数据以 spawn 为粒度聚合；（3）时间窗口对齐——activities 和 eval_runs 的匹配以 spawn 的 created_at 和 finished_at 为窗口边界；（4）可评估——每条统一运行记录可附着零个或一个评估结果，评估维度覆盖 output、trace、component、drift 四层；（5）可查询——支持按 agent、task、workspace、时间范围和评估得分区间等多维度查询和排序。

### 记录生成链路

统一运行记录的数据生成贯穿 agent 执行的完整生命周期。以下按时间线描述各层数据的产生时机和写入路径。

阶段一：Spawn 启动。当外部调用 POST /api/spawn 或内部调度触发 agent spawn 时，spawn/route.ts 首先校验 agent 可用性，然后调用 recordSpawnStart（位于 lib/db/spawn-history.ts）向 spawn_history 表插入一条 status='running' 的记录，同时通过 logAuditEvent 写入审计日志。此时 spawn_id 和 session_id 已确定，后续所有数据均以此为锚。SSE 事件 agent.status_changed 通过 ServerEventBus 广播至客户端。

阶段二：执行中——Token 记录。agent 执行期间，每次通过 OpenClaw Gateway 或直接 Claude API 调用模型时，task-dispatch.ts 中的 dispatchToAgent 或 dispatchDirect 在获取模型响应后，将 input_tokens、output_tokens、model、session_id、task_id、cost_usd、agent_name、workspace_id 写入 token_usage 表。此写入与模型调用同步完成，保证即使 spawn 异常退出，已消耗的 token 数据不丢失。

阶段三：执行中——Activity 事件。任务状态变更（创建、更新、完成）和 agent 状态变更时，相关处理逻辑通过 activities 表和 ServerEventBus 双重通道记录和广播。activities 记录携带 entity_type、entity_id、type、actor、detail 等字段，通过 api/activities/route.ts 提供按类型、执行者、实体和时间戳的组合查询。

阶段四：Spawn 终止。agent 执行结束（正常退出、异常退出或超时取消）时，调用 recordSpawnFinish（位于 lib/db/spawn-history.ts）更新 spawn_history 记录的 status、exit_code、error、duration_ms 和 finished_at。此时该 spawn 的时间窗口闭合，可作为 activities 和 eval_runs 匹配的精确时间边界。

阶段五：评估触发与附着。评估可通过 POST /api/agents/evals 手动触发，也可在 Aegis 质量审查流程（task-dispatch.ts 中 runAegisReviews）中间接触发。评估引擎（lib/agent-evals.ts）的四层评估（output/trace/component/drift）各自产生得分与结论，写入 eval_runs 表。评估结果通过 agent_name + workspace_id 关联到 agent，再通过 agent 与 spawn_history 的关联（agent_name ↔ spawn_history.agent_name）附着到具体运行记录。查询统一运行记录时，在 spawn 时间窗口内匹配最近一次 completed 状态的 eval_run。

### 评估引擎与运行记录的集成

Mission Control 的评估引擎（lib/agent-evals.ts）定义四层评估维度，每层与统一运行记录的不同数据层对接，实现从运行数据到质量结论的闭环。

Output 评估（任务完成率与正确性）：直接消费统一运行记录的第五层（Task Outcome）。引擎遍历本次 spawn 关联的所有 task 记录，读取每个 task 的 outcome 字段（success/failure），计算任务完成率（success 任务数 / 总任务数）和正确性得分。评估结果通过 agent_name + workspace_id + 时间窗口附着回 spawn 运行记录。

Trace 评估（收敛性分析与推理连贯性）：消费统一运行记录的第四层（Activity Stream）和第二层（Token & Cost）。引擎分析 activities 流中 agent 处理任务的步骤数量、状态变更频率和回退次数，结合 token_usage 的消耗趋势，判断 agent 推理是否收敛（步骤数趋于稳定）以及是否存在异常波动。该评估需要至少 5 次 spawn 运行的历史数据作为基线。

Component 评估（MCP 工具可靠性）：消费统一运行记录的第一层（Run Metadata）和 activities 流中与工具调用相关的事件。引擎统计 agent 在 spawn 运行中调用各 MCP 工具的成功/失败次数，按 spawn 聚合为工具可靠性得分。该评估结果与具体 spawn 运行直接绑定，因为工具调用的上下文（模型版本、参数、workspace 状态）与 spawn 粒度的执行环境强相关。

Drift 评估（滚动基线漂移检测）：消费统一运行记录的第二层（Token & Cost）和第三层（Eval Attachment）的历史数据。引擎对同一 agent 的最近 N 次 spawn 运行（默认 N=10）的 token 消耗、output 得分、trace 得分做滚动统计，当最新运行的指标偏离滚动均值超过阈值（如 2 个标准差）时标记为漂移。该评估依赖统一运行记录提供按 agent + workspace 维度的有序运行历史。

集成方式：评估引擎产生的每条 eval_run 记录包含 agent_name、workspace_id、eval_type（output/trace/component/drift）、score、status、created_at 等字段。统一运行记录查询时，给定一条 spawn 记录，以其 agent_name 和 workspace_id 在 eval_runs 表中查找 created_at 落在 [spawn.created_at, spawn.finished_at] 窗口内且 status='completed' 的评估记录，作为该次运行的评估附着。若窗口内有多条同类型评估，取 created_at 最近的一条；若无匹配，评估附着为空，表示该次运行尚未被评估。

### 查询与扩展接口

统一运行记录的对外接口基于现有 API 路由模式（App Router Route Handler）设计，提供面向运行记录的聚合查询能力，与已有的 spawn、activities、evals 接口互补而非替代。

运行记录列表查询（GET /api/runs）：支持以下查询参数——agent_name（按 agent 过滤）、workspace_id（多租户隔离，必填）、status（running/completed/failed/cancelled）、spawn_type（manual/auto/scheduled）、created_after / created_before（时间范围）、min_output_score / max_output_score（按 output 评估得分区间过滤）、has_eval（布尔，仅返回有/无评估附着的记录）、sort（按 created_at/cost_usd/duration_ms 排序）、limit/offset（分页）。返回每条运行记录的精简摘要：spawn_id、agent_name、status、duration_ms、total_cost_usd、task_count、latest_eval_score（output 维度）。

运行记录详情查询（GET /api/runs/[spawn_id]）：以 spawn_id 为路径参数，返回该次运行的完整五层展开数据。响应体结构：run_meta（spawn_history 全部字段）、token_summary（总 input_tokens、output_tokens、cost_usd，以及按 model 分组明细）、eval_attachment（各维度评估得分与结论，无评估时为空对象）、activity_stream（时间序的活动事件列表，带 entity_type/entity_id/type/actor/created_at）、task_outcomes（关联任务列表，含 id/status/outcome/priority）。所有数据在服务端按前述关联链路一次性编织后返回，客户端无需多次请求。

与现有接口的关系：（1）GET /api/spawn 返回 spawn 原始记录，GET /api/runs 在其基础上聚合了 token、eval、activities、tasks 数据，是 spawn 接口的上层封装；（2）GET /api/activities 按全局时间线查询，GET /api/runs/[spawn_id] 中的 activity_stream 按 spawn 窗口裁剪，是 activities 接口的上下文子集；（3）POST /api/agents/evals 触发评估，评估结果通过 GET /api/runs/[spawn_id] 的 eval_attachment 字段可查询。三个现有接口与新接口形成互补：现有接口面向独立数据源的增删改查，新接口面向跨数据源的聚合读取。

### 与现有系统的关联映射

以下逐一列出 Mission Control 现有核心模块和数据库表在统一运行记录契约中的角色与映射关系。

spawn_history 表（lib/db/spawn-history.ts，migration 044）：作为统一运行记录的根数据源，提供 spawn_id、agent_name、session_id、status、duration_ms 等运行元数据。recordSpawnStart 和 recordSpawnFinish 分别标记运行起止边界。getSpawnHistory 和 getSpawnStats 为列表查询和统计提供基础。

token_usage 表（lib/db/task-costs.ts，migration 018/039/025）：作为统一运行记录的成本层数据源，通过 session_id 关联到 spawn，通过 task_id 关联到 tasks。buildTaskCostReport 提供按 task/agent/project 三维度的成本聚合，是运行记录 token_summary 的计算基础。

eval_runs 表（lib/agent-evals.ts）：作为统一运行记录的评估附着数据源，四层评估结果通过 agent_name + workspace_id + 时间窗口匹配到具体 spawn 运行。提供 getAgentEvals 查询历史和 triggerEvalRun 触发新评估。

activities 表（lib/db/activities.ts + lib/event-bus.ts）：作为统一运行记录的 activity_stream 数据源，通过 entity_type + entity_id 和 spawn 时间窗口过滤。ServerEventBus 的 SSE 广播通道可在运行记录查询之上叠加实时推送，使客户端在查看历史运行记录的同时接收进行中运行的实时事件。

tasks 表（schema.sql）：作为统一运行记录的 task_outcomes 数据源，提供 outcome 字段用于 output 评估的任务完成率和正确性计算。task-dispatch.ts 中的 dispatchToAgent/dispatchDirect 是 token_usage 写入的执行点，也是 spawn 与 task 之间关联的建立点。sessions.ts 中的 GatewaySession（从 OpenClaw 磁盘 store 读取）通过 sessionId 与 spawn_history.session_id 对应，为运行记录补充模型名称和活跃状态信息。

spawn/route.ts（api/spawn/route.ts）：作为 spawn 启动的 HTTP 入口，调用 recordSpawnStart 和 logAuditEvent 建立运行记录的根节点和审计轨迹。agents/evals/route.ts（api/agents/evals/route.ts）：作为评估触发和历史查询的 HTTP 入口，其 GET 和 POST 端点与统一运行记录的 eval_attachment 层互补——前者管理评估运行本身，后者将评估结果附着到具体 spawn 上下文中呈现。

### 技术效果

本技术方案通过统一运行记录契约，在不引入新核心存储表的前提下，实现以下技术效果。

（1）端到端可追踪：从一次 spawn 出发，可沿 spawn_id → session_id → task_id 链路，完整追溯该次运行的 token 消耗明细、成本、处理的任务及其结果、业务活动事件和评估结论，消除现有碎片化数据源之间的查询盲区。

（2）评估结果稳定附着：评估引擎的四层评估结果通过 agent_name + workspace_id + 时间窗口匹配机制，与具体 spawn 运行建立确定性关联，同一 spawn 在重复查询时返回一致的评估附着，支持按运行批次做质量趋势分析。

（3）成本可视化与归因：每次 spawn 运行的总 token 消耗和美元成本可精确计算，并可按 model、task、agent 维度下钻。结合 task_outcomes 层，可进一步计算单任务的投入产出比（任务成本 / 任务结果）。

（4）多运行横向对比：统一运行记录以 agent + workspace 为维度组织有序历史，支持对同一 agent 的多次 spawn 运行做 token 消耗趋势、评估得分变化、执行耗时分布等统计分析，为 drift 检测和模型选型决策提供数据基础。

（5）可审计：activities 流在 spawn 窗口内的裁剪视图提供 agent 执行过程的业务操作轨迹，结合 logAuditEvent 记录的 spawn 启动审计事件，构成从启动到终止的完整审计链，满足安全合规和问题回溯需求。

（6）增量构建与低侵入：方案不新增核心存储表，不修改现有 spawn_history、token_usage、eval_runs、activities、tasks 五表的 schema，仅在查询层通过关联链路编织数据。新接口 GET /api/runs 和 GET /api/runs/[spawn_id] 作为现有接口的上层聚合，不影响已有 API 的兼容性和独立使用。

### 风险与待确认点

以下列出本方案在实施中需要关注的风险和待确认的技术细节。

（1）session_id 关联的可靠性：spawn_history.session_id 与 token_usage.session_id 之间为字符串值匹配，无数据库级外键约束。若两侧 session_id 的生成来源不一致（如 OpenClaw 内部生成的 session ID 格式与 Mission Control 侧记录的不同），关联可能断裂。需确认 token_usage 记录的 session_id 与 spawn_history.session_id 在现有代码路径中是否同源。

（2）评估时间窗口匹配精度：eval_runs 的 created_at 与 spawn 的 [created_at, finished_at] 窗口匹配存在固有延迟——评估通常在 spawn 结束之后才触发，此时 eval_runs.created_at 必然晚于 spawn.finished_at。方案中建议匹配窗口扩展为 [spawn.created_at, spawn.finished_at + 评估延迟容忍窗口]（如 +5 分钟），但需在实施时根据实际评估触发延迟调整。

（3）大查询性能：GET /api/runs/[spawn_id] 的完整五层展开涉及 spawn_history、token_usage、eval_runs、activities、tasks 五表联查，当 activities 表中单次 spawn 窗口内的事件数量过大（如高频 agent 产生数千条事件）时，响应体积和查询延迟可能超出可接受范围。建议在 activity_stream 中设置返回条数上限（如 500 条），超出时标记 truncated 并提供分页游标。

（4）activities 与 spawn 的间接关联：activities 表没有直接的 spawn_id 外键，仅能通过 entity_type + entity_id 与 spawn 时间窗口间接匹配。若同一 entity（如 task）在短时间内被多次 spawn 处理，时间窗口匹配可能将其他 spawn 产生的事件误归入当前 spawn。需在实施中验证时间窗口裁剪的准确性，或考虑在 activities 表中增加 spawn_id 字段作为可选优化。

（5）GatewaySession 缓存延迟：sessions.ts 中的 getSession 使用 30 秒 TTL 缓存，可能导致统一运行记录中引用的 session 状态（如 active、totalTokens）与实际值存在最多 30 秒的滞后。对于已完成 spawn 的历史查询影响较小，但对于进行中 spawn 的实时查询，需明确标注数据时效性。

（6）logAuditEvent 实现细节待确认：spawn/route.ts 中调用了 logAuditEvent 记录 agent_spawn 动作，但当前数据库 schema 中未发现独立的 audit_events 表。该函数可能将审计事件写入 activities 表或独立的审计存储。在统一运行记录的审计链路设计中，需确认其实际写入目标后再确定集成方式。
