## 技术方案

本方案描述在 Mission Control 现有任务编排和 agent 运行基础设施之上，构建一套统一、可查询、可追踪、可附着评估结果、可被外部工具访问的 agent 执行过程结构化记录机制——即「统一运行记录契约」（Unified Run Record）。该契约将 agent 的每一次执行过程（从 spawn 启动、会话建立、任务执行、工具调用、成本消耗到质量评估）串联为一条可追溯的完整记录，而不是零散分布在多张独立日志表中。

### 需解决的技术问题

Mission Control 已在多张数据库表中记录了 agent 执行的各个环节：spawn_history 记录 spawn 启停事件，tasks 记录任务状态流转与结果，token_usage 记录每次模型调用的 token 消耗与成本，mcp_call_log 记录每次工具调用的成败与耗时，quality_reviews 记录 Aegis 审查结论，eval_runs 记录四层评估得分。但这些记录以各自的主键独立存储，彼此之间仅通过 agent_name、task_id、session_id 等字段做弱关联，缺少一条贯穿全过程的统一标识和结构化查询入口。

这导致一系列技术问题：无法通过单一标识查询某次 agent 执行的完整生命周期；无法将 spawn 事件、任务结果、token 成本和评估得分串联为一条审计轨迹；外部工具需要通过多次跨表 JOIN 才能还原一次执行的完整上下文；评估结果缺少与具体执行实例的稳定绑定，难以支撑排行榜、回归对比和趋势分析。

### 核心技术方案：统一运行记录契约

本方案的核心是在现有数据模型之上引入一个轻量的「运行记录（Run Record）」概念，作为串联 agent 每次执行全过程的统一标识和聚合载体。Run Record 不是一张新增的普通日志表，而是一个以 run_id 为主键的结构化契约，通过外键关联将 spawn_history、tasks、token_usage、mcp_call_log、quality_reviews、eval_runs 等已有表中的分散记录收敛到同一条执行轨迹上。

Run Record 的结构化契约包含以下核心维度，每个维度指向现有数据模型中的对应实体，而非重复存储：

- run_id：全局唯一的执行标识，作为该次 agent 执行的主键，在 spawn 启动时由系统生成
- agent 标识：agent_name + agent_id，关联 agents 表
- 会话标识：session_id / session_key，关联 spawn_history.session_id 及 gateway session 存储中的会话记录
- 任务上下文：task_id（可选，非所有执行都与任务绑定），关联 tasks 表的 outcome、resolution、error_message 等字段
- 时间窗口：started_at / finished_at / duration_ms，从 spawn_history 继承
- 执行状态：status（started / running / completed / failed / terminated），与 spawn_history.status 保持一致
- 成本摘要：total_tokens + estimated_cost_usd，从 token_usage 按 run_id 聚合计算
- 工具调用摘要：total_tool_calls + failed_tool_calls + unique_tools，从 mcp_call_log 聚合
- 质量评估：review_status + review_score（来自 quality_reviews），eval_scores（来自 eval_runs 的各层得分）
- 扩展元数据：JSON 字段用于附着标签、排行榜排名、外部工具自定义数据

Run Record 与现有数据实体的关联采用「主记录 + 外键回填」策略。首先在 spawn_history 表中增加 run_id 字段：spawn 启动时，recordSpawnStart 同时生成 run_id 并写入。spawn_history 是 agent 执行的自然起点——每次 agent 被启动（无论是通过 task dispatch、手动触发还是定时任务）都会调用 recordSpawnStart，因此以此为 run_id 诞生点最为合适。

其次在 token_usage、mcp_call_log、quality_reviews、eval_runs 中分别增加 run_id 字段。每次记录 token 用量、工具调用、审查结论或评估得分时，同时写入当前活跃的 run_id。这使得后续可按 run_id 聚合出该次执行的完整画像。对于未绑定 task_id 的独立执行（如用户直接与 agent 对话），run_id 通过 spawn_history → session_id → token_usage 的链路仍可串联完整会话过程，不依赖任务系统。

### 关键处理流程

run_id 的生命周期与 agent 执行过程同步，由系统中已存在的两条主要执行路径分别驱动。

路径一——任务派发执行：当 scheduler 定时调用 dispatchAssignedTasks 时，系统将任务状态从 assigned 更新为 in_progress，随后通过 recordSpawnStart 启动 agent spawn 并生成 run_id。agent 执行期间，所有 token_usage 写入和 mcp_call_log 写入均携带该 run_id。执行完成后，Aegis 审查（runAegisReviews）生成 quality_reviews 记录，评估引擎（POST /api/agents/evals）生成 eval_runs 记录，均写入同一 run_id。最终 run_id 关联了 spawn → task → session → cost → tools → review → eval 的完整链路。

路径二——直接会话执行：当用户通过 WebSocket 或 chat.send 直接与 agent 对话（不经过任务系统），spawn 仍然触发 recordSpawnStart 并分配 run_id。会话期间的 token_usage 和 mcp_call_log 写入同一 run_id。此路径下 task_id 为空，但 run_id 仍将 spawn、session、成本和工具调用串联为完整记录。

run_id 的传递机制：在 spawn 启动时，recordSpawnStart 返回 run_id，调用方将该 run_id 注入当前执行上下文（如通过环境变量或进程级全局变量）。token_usage 写入点和 mcp_call_log 写入点从执行上下文中读取当前 run_id。此设计不需要修改 agent 内部逻辑，只需在 Mission Control 侧的记录写入点增加 run_id 参数即可。

### 对外查询与工具集成

Run Record 的对外查询通过统一的 REST API 和事件流两种方式暴露，确保外部工具、排行榜系统、审计面板和 CI/CD 流水线可以标准化访问。

REST 查询接口：提供 GET /api/runs/:run_id 获取单次执行的完整聚合视图，包括 spawn 信息、关联任务、token 成本摘要、工具调用统计、审查结论和各层评估得分。支持 ?include=tokens,tools,reviews,evals 参数按需展开明细。提供 GET /api/runs 列表接口，支持按 agent_name、workspace_id、时间范围、status 等条件筛选和排序。

聚合统计接口：提供 GET /api/runs/stats 返回按 agent、按时间段聚合的执行统计（总次数、成功率、平均耗时、平均成本、评估得分趋势），直接支撑排行榜和 dashboard 面板。

事件流推送：利用现有 eventBus 和 WebSocket 基础设施，在 run 生命周期关键节点（run.started、run.completed、run.failed、run.evaluated）广播事件。外部系统可通过 WebSocket 订阅或通过 webhook 回调接收，无需轮询。事件 payload 中包含 run_id 和当前已知的摘要数据。

与现有 API 的关系：上述接口不替代现有的 /api/tasks/outcomes、/api/agents/evals 等专用端点，而是在其上提供以 run_id 为维度的统一聚合视图。现有端点继续服务于各自专用场景。

### 评估与审计能力增强

Run Record 作为统一执行标识，使现有的四层评估引擎（agent-evals.ts）从「按 agent + 时间窗口统计」升级为「按 run 实例评估」，显著增强评估的精确性和可对比性。

实例级评估：eval_runs 中的每条评估记录通过 run_id 绑定到具体执行。同一 agent 的不同 run 之间可做横向对比（如对比两次执行同一类型任务时的工具调用收敛性差异），支撑排行榜排名和回归检测。评估引擎的 runDriftCheck 可以从「按 agent 全局基线对比」升级为「按同类型 run 的基线对比」，降低不同任务类型混合导致的漂移误报。

审计追踪：通过 run_id 可完整回溯某次 agent 工作的产生过程：谁触发、何时启动、分配到哪个 agent、使用了哪个模型、调用了哪些工具、每个工具是否成功、token 成本是多少、Aegis 审查是批准还是驳回、各层评估得分如何。activities 表中的活动日志通过 entity_type='run' + entity_id=run_id 关联，形成带时间戳的审计事件序列。

排行榜支撑：GET /api/runs/stats 返回的按 agent 聚合数据（成功率、平均耗时、平均成本、评估得分）可直接驱动排行榜 UI。排行榜可按不同 eval_layer（output/trace/component/drift）分别排名，也可按综合得分排名。排行榜数据可按时间窗口（日/周/月）切片，支持趋势展示。

### 技术效果

本方案带来的技术效果体现在以下几个方面：

可查询性：外部系统和运维人员通过单一 run_id 即可获取某次 agent 执行的完整视图，无需跨 6 张以上表做多次 JOIN。REST API 和 WebSocket 事件流提供标准化的程序化访问入口。

可追踪性：从 spawn 启动到最终评估的每个环节均附着 run_id，形成不可断链的审计轨迹。任何执行异常都可以沿 run_id 回溯到具体环节（是 spawn 失败、工具调用失败、审查驳回还是评估异常），精确定位问题。

可评估性：评估引擎从 agent 粒度升级到 run 粒度，使评估结果与具体执行上下文绑定。同类任务的 run 之间可横向对比，支撑排行榜、A/B 测试和 agent 优化迭代的量化反馈。

可扩展性：run_id 作为稳定标识，为后续质量评测体系、排行榜系统、运行审计面板、外部 CI/CD 集成和数据分析管道提供了统一的锚点。扩展元数据 JSON 字段允许外部工具附着自定义标签和排名数据，不污染核心数据模型。

与现有架构的兼容性：本方案不改变现有表结构的主键和外键约束，不修改 agent 内部逻辑，仅在 Mission Control 侧增加 run_id 字段和写入逻辑。现有 API 端点、scheduler 定时任务、eventBus 事件广播均继续正常工作。

### 风险与待确认问题

以下为需要后续确认的技术风险与待定设计决策：

- run_id 的传递机制：当前设计通过执行上下文（环境变量或全局变量）传递 run_id。需确认在异步并发场景下（多个 agent 同时执行）run_id 不会混淆。替代方案是让每个写入点通过 session_id 反查 run_id，但会增加一次数据库查询
- token_usage 的归属精度：当前 token_usage 在 callClaudeDirectly 中写入，时机为单次 API 调用完成时。如果一次 run 包含多次模型调用（多轮对话），需确认所有调用都能正确携带 run_id
- mcp_call_log 的 run_id 写入时机：mcp_call_log 当前由 agent 框架侧写入，Mission Control 通过扫描读取。需确认 agent 框架是否暴露了注入 run_id 的钩子，否则需要在 Mission Control 侧做后置关联
- eval_runs 的 run_id 绑定：评估引擎当前按 agent_name + 时间窗口查询，升级为按 run_id 后需要 run_id 已在 eval_runs 写入时可用。需确认评估触发时机（手动触发或定时触发）能否获取到正确的 run_id
- 已有历史数据的迁移：spawn_history、token_usage、mcp_call_log 等表中已有的历史记录没有 run_id。需要制定回填策略（如通过 agent_name + 时间窗口近似匹配），但回填的 run_id 可能不精确
