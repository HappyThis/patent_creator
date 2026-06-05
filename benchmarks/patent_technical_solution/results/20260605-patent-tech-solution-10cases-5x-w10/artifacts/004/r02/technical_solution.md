## 技术方案

本技术方案提出一种面向智能工作台的 agent 执行过程结构化沉淀与溯源查询方法，通过在现有 agent 编排体系中引入“执行运行记录”（Execution Run Record）作为一等实体，自动捕获每次 agent 执行的完整生命周期信息，并对外提供统一的工具化查询接口，从而实现对 agent 执行过程的精确溯源、质量评估附着和外部系统复用。

### 1. 执行运行记录的数据模型

系统在数据库层新增一张 execution_runs 表，作为 agent 每次执行过程的统一结构化记录。每条记录包含以下核心字段组：（1）溯源标识组：唯一运行标识 run_id、触发来源 trigger_source（区分任务派发、定时调度、流水线步骤、手动触发、外部回调等）、触发实体标识 trigger_entity_id 及类型、关联会话标识 session_id；（2）执行主体组：执行 agent 标识 agent_id/agent_name、agent 所属工作空间 workspace_id；（3）执行上下文组：关联任务标识 task_id、流水线运行标识 pipeline_run_id（当执行来自流水线步骤时）；（4）执行结果组：运行状态 run_status（排队中/执行中/成功/失败/超时/被取消）、结果摘要 result_summary、错误信息 error_detail；（5）资源消耗组：输入 token 数、输出 token 数、调用模型标识、执行耗时 duration_ms、估算成本 cost_usd；（6）评估附着组：评估是否完成 eval_completed、评估结果摘要 eval_summary、各评估层得分 eval_scores（JSON 结构）、信任评分快照 trust_score_snapshot；（7）时间戳组：创建时间、开始执行时间、完成时间。

### 2. 执行生命周期的自动捕获机制

该数据模型的设计要点在于：（1）以 run_id 为唯一主键，贯穿从触发到评估完成的全链路，使任何下游查询都能通过 run_id 串联全部相关数据；（2）trigger_source 和 trigger_entity_id 的组合使得执行记录可以反向追溯到任意触发场景——无论是用户通过任务面板手动创建的任务、cron 定时器基于模板自动生成的重复任务、流水线中某一步骤派发的子任务，还是通过 REST API 或 MCP 工具从外部系统调起的执行；（3）将与执行直接相关的 token_usage 表通过 run_id 关联，而非仅通过 session_id 间接关联，实现更精确的资源归因；（4）eval_scores 字段以结构化 JSON 直接嵌入记录，使得评估结果与执行记录形成原子绑定，避免跨表关联导致的不一致。

执行运行记录的创建和更新由系统在 agent 执行生命周期的关键节点自动触发，无需 agent 开发者或使用者额外编写日志代码。具体机制如下：

（a）执行发起阶段：当系统检测到 agent 开始处理一个任务（任务状态从 assigned 变为 in_progress），或外部通过调度器/流水线/pipeline 触发一个新的 agent 调用时，创建一条 execution_runs 记录，状态置为 running，记录 trigger_source、agent_id、task_id 或 pipeline_run_id、session_id 及 created_at 时间戳。

（b）执行进行阶段：agent 的心跳上报（heartbeat）携带当前 token 消耗快照和会话状态，系统据此更新 execution_runs 记录中的 input_tokens、output_tokens、model 等资源消耗字段，实现增量归集。同时，agent 通过网关产生的 token_usage 行在写入时自动关联当前活跃的 run_id，使细粒度的按模型、按时间窗口的成本分析可以精确归因到具体执行。

（c）执行完成阶段：当 agent 将任务状态更新为 done 或报告执行结果时，系统将 run_status 更新为最终状态（success/failed/timeout），写入 result_summary 和 error_detail，记录 completed_at 时间戳，并计算执行总耗时 duration_ms。完成时自动触发评估附着流程（见第 4 节）。

### 3. 多维溯源与关联查询

溯源能力通过 execution_runs 记录中嵌入的多维关联标识实现。每条记录在创建时即与以下实体建立外键或逻辑关联：（1）agent：通过 agent_id/agent_name 关联到 agents 表；（2）task：通过 task_id 关联到 tasks 表，从而进一步关联到 task 所属的 project、workspace；（3）session：通过 session_id 关联到网关会话记录（claude_sessions 或网关 session store）；（4）pipeline：当触发来源为流水线时，通过 pipeline_run_id 关联到 pipeline_runs 表，从而追溯到具体流水线定义 workflow_pipelines 和对应步骤；（5）trigger_source 字段标识触发类型，结合 trigger_entity_id 指回触发实体。

溯源查询支持的正向和反向路径包括：从任务出发，查询该任务历史上所有 agent 执行尝试（含重试），并比较各次执行的结果和消耗；从 agent 出发，查询该 agent 在指定时间窗口内执行的所有运行记录及其任务归属；从 session 出发，查询该会话对应了哪些执行运行，以及每次执行产生了多少 token 消耗；从流水线出发，查询流水线每次运行的每一步分别触发了哪些 agent 执行及各自的结果状态；从触发来源出发，按 trigger_source 类型聚合分析不同触发渠道的执行成功率和平均成本。查询结果以统一的活动流（activity stream）格式返回，与现有的 activities 表和 SSE 事件流保持一致的数据结构，前端无需适配新协议。

### 4. 评估附着与质量闭环

评估附着机制将 agent 质量评估结果与执行运行记录进行结构化绑定。系统在每次 execution_run 进入终态（success/failed）时自动触发评估流程，或通过外部工具显式触发。评估结果写入 execution_runs 记录的 eval_completed、eval_summary 和 eval_scores 字段。

评估分为四个维度层：（1）输出层（output）：基于任务完成状态、结果正确性和反馈评分，计算任务完成率和正确性得分；（2）追踪层（trace）：分析执行轨迹中的推理步数与最优步数的收敛比，评估推理效率；（3）组件层（component）：基于 MCP 工具调用日志统计工具可靠性和调用成功率；（4）漂移层（drift）：将当前执行指标与 agent 历史基线对比，检测性能漂移。每一层的评估结果以（score, passed, detail）三元组存入 eval_scores 字段，支持按层查询和跨执行对比。

评估附着带来的质量闭环包括：（1）排行榜生成：按 agent 维度聚合 eval_scores 中各层得分，加权计算综合排名，支持按时间窗口滚动更新；（2）质量门禁：在执行完成时检查评估结果，若 output 层 score 低于预设阈值（如 0.6）或 drift 层检测到显著漂移，自动将任务状态回退为 quality_review 并通知管理员；（3）信任评分联动：将 eval_scores 的输出层得分和组件层得分作为 agent_trust_scores 表中 trust_score 的动态调整因子，连续多次低分执行将降低 agent 信任评分，影响后续任务派发优先级。

### 5. 工具化查询接口与外部复用

执行运行记录的查询和复用通过三个统一的工具化接口层暴露，使外部系统和工具可以按结构化的方式消费执行数据：

（a）REST API 层：新增 /api/runs 端点，支持按 agent、task、session、pipeline_run、trigger_source、status、时间范围等多维过滤查询，返回分页的执行运行记录列表；新增 /api/runs/[id] 端点返回单条记录的完整详情，包含关联的评估结果、token 消耗明细和审计事件。查询参数沿用现有 API 风格（limit/offset、since/until、workspace 隔离），与现有 token、evals、sessions 等端点保持一致的调用范式。

（b）MCP 工具层：在现有 MCP Server（35 个工具）中新增工具：mc_list_runs（按过滤条件查询执行运行记录列表）、mc_get_run（查询单条执行详情）、mc_run_evals（对指定运行触发评估）、mc_agent_leaderboard（查询 agent 排行榜）。这些工具使外部 AI agent（如 Claude Code 代理）可以直接在工具调用中查询执行历史、触发评估和获取排行数据，无需离开其执行上下文。

（c）Webhook 与导出层：在现有 webhook 事件类型中新增 run.completed、run.failed、run.eval_completed 事件。外部系统可以订阅这些事件，在 agent 执行完成或评估完成时自动接收结构化 payload（包含 run_id、agent_name、task_id、run_status、eval_scores 等关键字段），实现与 CI/CD 系统、监控平台、数据仓库等外部工具的自动化集成。同时，/api/runs 端点支持 CSV/JSON 导出，满足审计归档和离线分析需求。

### 6. 扩展架构设计

本方案在 execution_runs 数据模型中预留了扩展字段和扩展点，以支持未来需求而不破坏已有记录结构：（1）metadata 字段（JSON 类型）：存储任意扩展键值对，外部系统可通过此字段附加自定义标签、业务上下文或外部系统标识；（2）custom_metrics 字段（JSON 类型）：存储自定义评估指标或业务度量，排行榜和查询接口可按自定义指标排序和过滤；（3）hook 机制：在 execution_run 状态变更（创建、开始、完成、失败）时，触发可配置的钩子函数，允许在现有 webhook 之外执行自定义逻辑（如向外部排行榜服务推送数据、写入审计日志到专用存储等）。这些扩展点使得本方案可以在不修改核心数据模型的前提下，适配质量评测平台集成、多租户排行榜、合规审计系统和第三方工具消费等多样化场景。

### 7. 方案总结与关键区别于现有技术

本方案的核心创新在于将 agent 每次执行过程提升为“执行运行记录”这一一等实体，通过自动捕获、多维关联、评估绑定和工具化暴露四个机制形成完整的技术闭环。与现有方案的关键区别在于，本方案不是简单地将日志文本结构化，而是在现有 agent 编排体系（任务、agent、会话、事件、流水线）的每个关键节点植入自动捕获逻辑，使执行记录天然成为执行体系的有机组成部分，而不是人工维护的外部日志。

本方案产生的技术效果包括：（1）可精确回答“某次 agent 工作从哪触发、由谁执行、关联哪个任务/会话、产出什么结果、消耗多少、是否经过评估”等溯源问题；（2）评估结果与执行记录原子绑定，使得排行榜、质量门禁和信任评分都基于同一数据源，避免数据不一致；（3）REST API、MCP 工具和 Webhook 三层接口使外部系统可以按结构化方式查询和复用执行数据，无需理解内部存储格式；（4）扩展字段和 hook 机制为质量评测平台、排行榜、运行审计和第三方工具集成预留了标准化扩展空间。整体方案复用了现有任务、agent、会话、事件、审计和流水线基础设施，不引入新的外部依赖，以 SQLite 单数据库承载全部执行记录的写入和查询。
