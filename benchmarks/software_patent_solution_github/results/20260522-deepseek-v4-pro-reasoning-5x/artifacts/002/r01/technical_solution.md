## 技术方案

本方案在 Mission Control 现有的 agent 编排和任务运行基础上，提出一种 agent 执行过程的统一运行记录（Run Record）机制。该机制将分散在多个存储层（spawn_history、token_usage、mcp_call_log、eval_runs、task 表的 outcome/resolution 字段等）中的执行信息整合为一份结构化、可查询、可追踪、可附着评估结果的运行记录契约，以支撑后续的质量评测、排行榜、运行审计和外部工具集成。

技术问题：在现有 Mission Control 中，agent 一次完整的执行过程涉及多条独立的数据记录——spawn_history 记录进程的启停和退出码，token_usage 记录模型调用消耗，mcp_call_log 记录工具调用成败，task 表记录最终产出（outcome、resolution、feedback_rating 等），eval_runs 和 eval_traces 记录评估结果。这些记录之间通过 agent_name、task_id、session_id 等字段隐式关联，但没有统一的主键将这些分片信息串联为一个可独立寻址、可整生命周期查询的运行单元。外部工具想要获取某次 agent 执行的完整画像时，需要跨多个 API 端点自行拼合，且无法确保拼合的一致性。

核心技术方案：设计一种三层结构的统一运行记录机制——Spine 索引层、关联键路由层、查询物化层。Spine 索引层为每次 agent 执行生成一条最小化索引记录，持有指向各数据分片的关联键；关联键路由层定义 Spine 到各现有表的标准查询路径；查询物化层在 API 请求时按路由协议动态聚合各分片数据，组装为完整的 Run Record 对象返回给调用方。该设计避免了对现有存储结构的大规模重构，同时提供了统一的外部访问契约。

### Spine 索引层设计

Spine 索引记录（run_records）是运行记录的最小化持久化单元。每条记录包含以下关键字段：唯一运行标识 run_id，锚定 spawn_history 的 spawn_id（1:1 关系），关联 task 表的 task_id（N:1，同一 task 多次重试产生多条 run），关联 token_usage 和 sessions 的 session_id，关联同工作空间数据的 workspace_id，以及记录生命周期状态的 status（created → running → completed/failed → finalized）和 phase（dispatch → spawn → execute → evaluate → complete）。

Spine 仅存储索引级元数据（状态、阶段、时间戳），不存储 token 用量、工具调用明细或评估结果等体量较大的执行明细。这些明细通过关联键在查询时从各现有表中按需拉取。Spine 的状态流是单向的——一旦从 running 进入 completed 或 failed，不可回退；重试场景下，每次 task-dispatch 创建新的 spawn_history 记录，对应生成新的 Spine 记录，通过共同的 task_id 串联形成执行链。

### 关联键路由层设计

关联键路由层定义了 Spine 索引记录到各现有数据表的标准化查询路径，形成运行记录的组装协议。

- session_id → spawn_history（取 exit_code、duration_ms、error）、token_usage（聚合 input_tokens、output_tokens、cost_usd）、sessions 磁盘存储（取 model、chatType、channel 等会话元数据）
- task_id → task 表（取 outcome、error_message、resolution、feedback_rating、feedback_notes、dispatch_attempts）、token_usage（按 task_id 过滤）、eval_traces（取收敛分析数据）
- agent_name + task_id + workspace_id → mcp_call_log（聚合工具调用次数、成功率、耗时分布）、eval_runs（取最新四层评估结果）、eval_traces（取收敛分数和追踪步骤）
- workspace_id → 所有关联查询的前置过滤条件，保证多租户数据隔离

评估结果的附着采用延迟绑定策略。eval_runs 和 eval_traces 的写入不触发 Spine 记录的更新；当外部通过 API 请求完整 Run Record 时，系统以 (agent_name, task_id, workspace_id) 三元组实时 JOIN 最新的 eval_runs 记录，按 output、trace、component、drift 四层组装评估子对象。若某层评估尚未完成，该层标记为 "pending"，整体 evaluation_status 返回 "partial"。评估完成时通过 event-bus 广播 eval.completed 事件，但不强制 Spine 感知。

### 查询物化层设计

查询物化层在 API 请求时动态执行，将 Spine 索引与各关联表的数据按路由协议组装为完整 Run Record 对象。Run Record 是一个 TypeScript interface，不是持久化存储单元，其在查询时由以下组件合成：

- header：run_id、status、phase、created_at、finalized_at（来自 Spine）
- spawn：spawn_type、exit_code、duration_ms、error（来自 spawn_history）
- execution：按 model 聚合的 token 统计（total_tokens、cost_usd）、按 tool_name 聚合的工具调用统计（count、success_rate、avg_duration_ms）（来自 token_usage 和 mcp_call_log）
- outcome：task 的 outcome、resolution、feedback_rating、retry_count（来自 task 表）
- evaluation：四层评估结果及漂移检测指标、整体 evaluation_status（来自 eval_runs 和 drift 计算）
- session：model、chatType、channel 等会话上下文（来自 sessions 磁盘存储或 claude_sessions 表）

### 运行记录生命周期与处理流程

运行记录的生命周期与 task-dispatch 和 agent 执行流程深度耦合，分为六个阶段：

1. 创建（created）：task-dispatch 模块调用 recordSpawnStart 写入 spawn_history 后，立即在 Spine 中插入一条 run_records，status=created、phase=dispatch，并写入 spawn_id、task_id、agent_name、workspace_id。
2. 启动（running/spawn）：adapter 协议（POST /api/adapters，action=register/heartbeat）确认 agent 上线后，Spine 更新 status=running、phase=spawn。
3. 执行（running/execute）：agent 执行过程中，token_usage 和 mcp_call_log 持续写入各自的数据表。Spine 更新 phase=execute，但不存储执行明细。此阶段 token_usage 的 session_id 和 task_id 字段确保后续可按 run 维度精确聚合。
4. 完成（completed/failed）：agent 执行结束，spawn_history 通过 recordSpawnFinish 写入 exit_code、duration_ms、error。Spine 同步更新 status（completed 或 failed）、phase=evaluate。task 表的 outcome、resolution、completed_at 也在此阶段由 task-dispatch 写入。
5. 评估（evaluate 阶段）：评估引擎（agent-evals 的四层模型）按需或定时产出 eval_runs 和 eval_traces 记录。Spine 不感知评估进度；Run Record 查询时动态附着最新评估结果。
6. 最终化（finalized）：当 token 成本聚合（task-costs）和评估结果均已就绪，Spine 更新 status=finalized、phase=complete、finalized_at 时间戳。event-bus 广播 run.finalized 事件，webhook 可据此触发下游集成动作。

### 外部访问与工具集成

运行记录通过 REST API、事件总线和 Webhook 三种通道向外部暴露。

- REST API：GET /api/runs 提供分页列表查询（仅查 Spine，支持按 agent_name、task_id、status、workspace_id、时间范围过滤）；GET /api/runs/:run_id 返回完整物化的 Run Record（含 execution、outcome、evaluation 子对象）；GET /api/runs/:run_id/evaluations 和 GET /api/runs/:run_id/costs 提供评估和成本的独立子资源端点。
- 事件总线：在现有 event-bus（基于 Node.js EventEmitter）上新增 run.created、run.status_changed、run.finalized 事件类型，与现有的 task.* 和 agent.* 事件并行广播。SSE（/api/events）和 WebSocket 客户端可直接订阅。
- Webhook：在现有 webhooks 系统的订阅事件白名单中增加 run.* 事件族。外部系统（如 CI/CD、数据仓库、排行榜服务）可通过 POST /api/webhooks 注册 webhook，指定订阅 run.finalized 事件，在运行记录最终化时自动接收完整 Run Record 的摘要 payload。

此外，外部工具可通过 task_id 查询某任务的所有重试运行记录链（同一 task_id 下的多条 run，按 created_at 排序），实现横向对比不同 dispatch_attempt 的执行效果和成本差异，支撑 agent 优化决策。

### 技术效果

本方案相比现有分散式日志记录方式，具有以下技术效果：

- 可查询性提升：一次 API 调用即可获取某次 agent 执行的完整画像（启动信息、token 消耗、工具调用、执行结果、评估结论），无需跨多个端点手动拼合。此前需要分别查询 spawn_history、token_usage、mcp_call_log、eval_runs 和 task 表，且缺乏统一主键保证拼合一致性。
- 可追踪性增强：通过 run_id 可沿生命周期阶段（dispatch → spawn → execute → evaluate → complete）追溯每次状态变更；通过 task_id 可串联同一任务的所有重试运行记录，形成完整的执行链视图。
- 评估附着标准化：评估结果与运行记录之间建立基于 (agent_name, task_id, workspace_id) 的延迟绑定契约，评估引擎产出的四层分析数据（输出质量、推理收敛性、工具可靠性、性能漂移）自动挂载到对应 Run Record，无需人工关联。
- 外部集成友好：REST API、SSE/WebSocket 事件、Webhook 三种通道覆盖了同步查询、实时订阅和异步通知三种集成模式。外部排行榜系统可通过 webhook 订阅 run.finalized 自动收集评估数据；审计系统可通过 run.* 事件流实现全量运行审计。
- 扩展性：Spine 索引层的最小化设计使其对存储压力低（每条记录约百字节级别），不会随 token_usage 或 mcp_call_log 的增长而膨胀。新增评估维度（如安全扫描结果、合规检查结果）只需在路由协议中增加新的关联路径，无需修改 Spine 结构。
- 复用现有基础设施：方案复用已有的 spawn_history、token_usage、mcp_call_log、eval_runs、event-bus、webhooks 和 workspace 隔离机制，不需要推翻或重建现有数据模型，仅在它们之上增加一层统一的索引和查询协议。

### 风险与待确认问题

- mcp_call_log 当前缺少 session_id 字段（仅有 agent_name、mcp_server、tool_name、success、duration_ms、workspace_id、created_at），在按 run 维度精确聚合工具调用数据时只能通过 agent_name + 时间窗口近似关联，需评估是否补加 session_id 字段以实现精确关联。
- spawn_history 的记录写入（recordSpawnStart / recordSpawnFinish）当前仅在部分代码路径中被调用，spawn API 路由（POST /api/spawn）生成的 spawn 事件并未通过这两个函数写入 spawn_history 表，需统一所有 agent 启动路径的 spawn 记录逻辑。
- 非 task 触发的 agent 执行（如手动 spawn、cron 定时触发、pipeline 步骤触发）是否纳入统一运行记录体系。这些场景下 task_id 为空，Spine 记录的 task_id 字段需允许 NULL，但运行记录的完整性会降低。需根据业务场景决定是否要求所有 agent 执行都关联一个 task。
- Spine 索引表的落盘方案：作为物理 SQLite 表（与现有 agents、tasks 同库）可获得完整的 SQL 查询、分页和 JOIN 能力；作为 JSON 索引文件（类似 sessions.json 的磁盘存储）则更轻量但查询能力受限。基于现有架构的 SQLite 技术栈，推荐使用物理表方案。
- 评估结果延迟绑定的时效性：如果 eval 引擎执行耗时较长，Run Record 可能在 finalized 之前被查询，此时 evaluation_status 为 partial。是否需要在 finalized 之前阻止外部查询，或允许返回部分结果——需要根据使用场景定义契约。
