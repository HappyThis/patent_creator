## 技术方案

本方案提出一种面向多智能体编排系统的统一运行记录机制，通过在现有任务派发、会话管理、成本统计、工具调用审计、质量评估等子系统之上引入运行记录标识（Run ID）作为跨子系统关联契约，实现对每次智能体执行全过程的结构化沉淀、可追溯查询和可评估验证。

### 要解决的技术问题

在多智能体编排系统中，一次智能体执行涉及多个独立子系统：任务派发系统记录任务状态和结果、会话系统记录对话历史和令牌用量、工具调用审计系统记录每次工具调用、评估系统记录质量评分。这些子系统各自独立记录数据，缺乏统一的运行标识将这些分散记录关联为一次完整的执行过程。这导致三个问题：第一，无法从一个入口查询某次执行从派发到完成的全链路信息；第二，成本统计难以精确归属到具体执行而非笼统的智能体维度；第三，评估结果无法与具体执行实例绑定，导致无法对单次执行的质量进行追溯判断。现有的 spawn_history 表仅记录启动和结束事件，缺少与任务、会话、令牌消耗、工具调用、评估结果的系统化关联。

### 核心机制：Run ID 驱动的统一运行记录契约

本方案的核心是在现有各子系统的记录中引入统一的运行标识 Run ID 作为关联键，并建立运行记录聚合层。Run ID 在智能体执行启动时生成，贯穿整个执行生命周期，被注入到任务元数据、派发会话标识、令牌消耗记录、工具调用日志和评估记录中。运行记录聚合层通过 Run ID 从各子系统收集关联记录，组装为一次执行的完整视图。

### Run ID 生成与生命周期

Run ID 的生成时机分为三种场景：（1）任务派发执行：当调度器从任务队列中取出状态为 assigned 的任务并准备派发给智能体时，生成 Run ID 并写入任务的 metadata 字段（JSON 格式，键为 run_id）；（2）手动触发执行：当用户通过 spawn API 或 pipeline 启动智能体执行时，在 spawn 记录插入前生成 Run ID；（3）会话内持续执行：当智能体在已有会话中接收新指令时，可生成新的 Run ID 标记该次指令执行边界。

Run ID 的格式采用「前缀-时间戳-随机串」三段结构，例如 run-1717500000-a1b2c3d4，兼顾可读性和全局唯一性。Run ID 的生命周期状态包括：pending（已生成、等待执行）、running（执行中）、completed（执行完成）、failed（执行失败）、cancelled（被取消）。状态转换规则为：pending → running（执行开始）、running → completed/failed/cancelled（执行结束）。已完成状态的 Run ID 不可再次转换。

### 与现有子系统的关联机制

Run ID 通过以下方式与各现有子系统建立双向关联：

- 任务系统：任务的 metadata JSON 字段中持久化 run_id 和 dispatch_session_id。当调度器将任务状态从 assigned 转为 in_progress 时，在 metadata 中写入当前 Run ID。任务完成时，任务记录的 resolution 字段保存智能体响应正文，outcome 字段记录执行结论（success/failed/partial），两者均可通过 Run ID 关联到本次执行。
- 派发/执行系统：spawn_history 表中新增 run_id 列，使每次 spawn 记录与其所属运行关联。recordSpawnStart 在插入时接收 Run ID 参数，recordSpawnFinish 通过 Run ID 关联更新终态。这使得 spawn 的启动时间、结束时间、退出码、错误信息、执行时长均归属到具体运行。
- 会话系统：任务 dispatch 过程中产生的 sessionId 同时写入 metadata.dispatch_session_id 和运行记录的 session_id 字段。claude_sessions 表可通过 session_id 与 Run ID 建立间接关联，从而追溯该次执行使用的模型、令牌消耗和对话上下文。
- 令牌消耗系统：token_usage 表新增 run_id 列。在 dispatchAssignedTasks 和 callClaudeDirectly 中写入 token_usage 记录时同时写入当前 Run ID。这使得每次 API 调用的输入/输出令牌量和成本可精确归属到具体运行，而非仅按 task_id 或 agent 维度的粗略统计。
- 工具调用审计系统：mcp_call_log 表新增 run_id 列。logMcpCall 函数接收 Run ID 参数，将每次工具调用（工具名、MCP 服务器、成功/失败、耗时、错误详情）关联到特定运行。这使得可以回答“某次执行中调用了哪些工具、哪些失败、各耗时多少”等细粒度问题。
- 评估系统：eval_runs 表新增 run_id 列。当对某次运行执行四层评估（输出层、追踪层、组件层、漂移层）时，评估记录通过 Run ID 关联到具体执行实例，而非仅按 agent_name 维度存储。eval_traces 表同理新增 run_id 列，保存每次评估的详细追踪信息。
- 流水线系统：pipeline_runs 表新增 run_id 列。当流水线步骤触发智能体执行时，该步骤对应的 Run ID 写入 steps_snapshot JSON 中每个步骤的 run_id 字段，以及 pipeline_runs 的 run_id 列，使得流水线执行与智能体执行形成层级可追溯关系。

### 运行记录聚合层

运行记录聚合层负责以 Run ID 为键，从多个子系统表中收集关联数据，组装为一次执行的完整运行记录。聚合查询采用按需组装而非预计算写入的策略，避免数据冗余和不一致。

聚合流程如下：第一步，以 Run ID 查询 spawn_history 表获取启动时间、结束时间、执行状态、退出码、错误信息等基础信息。第二步，以 Run ID 关联的 agent_name 和 session_id 查询 claude_sessions 表获取模型、令牌总量、对话轮次；同时查询 token_usage 表获取按模型和请求粒度的时间序列令牌消耗。第三步，以 Run ID 查询 mcp_call_log 表获取工具调用明细（工具名、成功/失败、耗时）。第四步，以 Run ID 关联的 task_id 查询 tasks 表获取任务标题、优先级、结果摘要（outcome）、智能体响应正文（resolution）。第五步，以 Run ID 查询 eval_runs 表获取四层评估结果（分数、通过/未通过、详情）。第六步，若 Run ID 关联到 pipeline_run，则查询 pipeline_runs 表获取流水线上下文。以上六步查询结果合并为一个 RunRecord 对象返回。

RunRecord 对象的数据结构包含：run_id（运行标识）、status（运行状态）、agent_name（执行智能体）、task_id 和 task_title（关联任务）、session_id（会话标识）、spawn_type（启动方式：task_dispatch/manual/pipeline/cron）、timing（started_at、finished_at、duration_ms）、cost（total_tokens、total_cost、per_model 分模型统计）、tools（调用总数、成功数、失败数、per_tool 分工具统计）、eval（各层评估结果）、resolution_summary（结果摘要）。该结构是面向查询的扁平化视图，不增加新的持久化写入负担。

### 评估结果附着机制

评估结果的附着是本方案区别于简单日志的关键特征。现有 eval_runs 表按 agent_name 维度存储评估结果，同一智能体的多次执行评估混杂在一起，无法区分哪次评估对应哪次执行。引入 Run ID 后，评估流程变为：当执行完成后，评估引擎接收 Run ID 参数，从运行记录聚合层获取该次执行的完整数据（任务结果、工具调用明细、令牌消耗时间线），然后在四层评估中分别计算得分，将结果写入 eval_runs 表时携带 run_id。这使得后续查询可以回答“智能体 X 在第 N 次执行中的输出质量得分是多少、推理是否收敛、工具可靠性如何、相比基线是否漂移”等精准问题。

特别地，漂移检测层（Layer 4: Drift）通过 Run ID 的时间属性实现滚动基线对比：以当前 Run ID 的执行时间戳为窗口边界，将该次执行的令牌消耗、工具成功率、任务完成率与历史基线窗口（如前四周）的同指标进行 delta 计算，判断是否超出漂移阈值。每个 Run ID 关联的漂移检测结果独立存储，支持按时间序列绘制智能体行为趋势。

### 外部访问契约

方案为运行记录提供两种外部访问方式：REST API 查询接口和事件流推送。

REST API 路径设计为 GET /api/runs，支持以下查询参数：run_id（精确查询单次运行）、agent（按智能体名称过滤）、task_id（按任务过滤）、status（按状态过滤：running/completed/failed/cancelled）、since/until（时间范围过滤）、limit/offset（分页）。返回的 RunRecord 对象包含完整的六步聚合结果。此外提供 GET /api/runs/stats 聚合统计接口，返回指定时间窗口内的运行总数、成功率、平均耗时、平均令牌消耗、平均工具调用数等指标，支持按智能体和时间粒度分组。

事件流通过现有 eventBus 机制扩展。当 Run ID 状态转换时（pending→running→completed/failed/cancelled），eventBus 广播 run.status_changed 事件，事件数据包含 run_id、agent_name、status、task_id、timestamp。SSE 客户端可订阅此类事件实现运行状态的实时监控。当评估结果产生时，广播 run.eval_completed 事件包含 run_id、eval_layer、score、passed。外部工具或系统可通过订阅 SSE 端点 /api/events 获取运行事件的实时推送。

此外，方案在 audit_log 表中记录每次 Run ID 状态转换，action 字段为 run.created、run.started、run.completed、run.failed，detail 字段包含 run_id、agent_name、task_id 等上下文信息。这使得管理员可通过现有的 /api/audit 接口查询运行审计记录。

### 事件驱动集成流程

运行记录与现有事件系统的集成遵循最小侵入原则，仅在关键执行节点注入 Run ID 写入和事件广播。

任务派发场景的完整流程：调度器的 dispatchAssignedTasks 函数从任务队列取出任务后，首先生成 Run ID，将其写入任务的 metadata.run_id；调用 recordSpawnStart 时传入 Run ID；通过 OpenClaw gateway 或直接 API 调用智能体时，在 invokeParams 的 idempotencyKey 中包含 Run ID；智能体返回响应后，在写入 token_usage 记录时携带 Run ID；更新任务状态为 review 时，metadata 中保持 Run ID；任务进入 Aegis 质量审查时，审查结果通过 task_id 间接关联 Run ID；评估引擎通过 Run ID 查询运行数据进行四层评估。

手动触发场景的流程：POST /api/spawn 在生成 spawnId 的同时生成 Run ID；spawn 成功后将 Run ID 与 sessionId 一同返回给调用方；后续令牌消耗和工具调用通过 sessionId 回溯 Run ID。流水线场景的流程：pipeline run 启动时生成顶层 pipeline_run_id，每个步骤启动时生成步骤级 Run ID，steps_snapshot 中每个步骤记录其 run_id，形成 pipeline_run → step_run 的层级关系。

关键设计原则：Run ID 的生成和写入发生在执行启动的原子操作中，避免因系统崩溃导致部分记录有 Run ID 而部分没有。具体实现为：在 dispatchAssignedTasks 中，Run ID 在任务状态从 assigned 转换为 in_progress 的同一个数据库事务中生成并写入 metadata，确保 Run ID 的持久化与状态转换的原子性。

### 与现有方式的对比

与现有方式相比，本方案的区别在于不增加新的日志表，而是在现有分散记录之上建立 Run ID 关联层和聚合查询层。

现有 spawn_history 表仅记录启动/结束，不关联任务结果和令牌消耗。本方案通过 Run ID 将 spawn_history、tasks、token_usage、mcp_call_log、eval_runs 串联为一次完整执行。现有 token_usage 表虽有 task_id 列，但 task_id 对应的是任务而非单次执行——一个任务可能经历多次重试派发（dispatch_attempts），每次派发产生不同 Run ID，token_usage 的 task_id 无法区分哪次消耗属于哪次重试。引入 run_id 列后，每次重试的令牌消耗精确归属。现有 eval_runs 表仅按 agent_name 存储评估结果，无法区分同一智能体不同运行的质量差异。引入 run_id 后，评估结果精确绑定到执行实例。

相比新增一张大而全的「执行日志表」将所有字段冗余存储，本方案的聚合查询策略避免了数据重复写入、一致性问题以及表结构随需求变化的频繁迁移。各子系统的表结构只需增加一个 run_id 列（可为 NULL 以兼容历史数据），写入负担极小，而复杂聚合在查询时按需执行。

### 技术效果

本方案带来的技术效果包括：

- 全链路可追溯：从一次执行的启动（spawn 记录）、执行过程（会话和工具调用）、资源消耗（令牌和成本）、执行结果（任务 outcome 和 resolution）到质量评估（四层 eval 得分），所有信息通过 Run ID 串联为一条可查询的完整链路。
- 精确成本归属：令牌消耗从智能体/任务维度细化到执行实例维度，每次重试派发产生的令牌成本独立统计，支持按 Run ID 计算单次执行成本。
- 评估精准化：评估结果从智能体维度下沉到执行实例维度，可比较同一智能体不同执行的质量变化，支持细粒度排行榜和趋势分析。
- 复用现有基础设施：仅在各子系统中增加 run_id 列（最小侵入），不要求重构现有表结构。聚合查询复用现有的 SQLite 查询能力和索引，不需要引入新的存储引擎。
- 外部工具集成友好：REST API 和 SSE 事件流为外部系统（监控面板、数据分析工具、CI/CD 流水线）提供标准化的运行记录访问接口，支持按 Run ID、智能体、时间范围等多维度查询。
- 扩展空间：Run ID 的契约化设计为后续的质量评测排行榜（按智能体/时间段聚合 eval 得分）、运行审计（管理员审查每次执行的全链路信息）、成本优化（识别高成本运行模式）等场景提供了数据基础。

### 风险与待确认问题

以下为需要后续确认的风险点和待解决问题：

- 历史数据兼容：引入 run_id 列后，历史记录中 run_id 为 NULL。聚合查询需要处理 NULL 情况，统计类接口应提供「仅统计有 Run ID 的记录」或「包含历史记录」的选项。
- Run ID 的回填策略：对于正在执行中（in_progress）的任务，其 metadata 中可能没有 run_id。是否需要一个迁移脚本为这些任务补写 Run ID，以及如何处理无法确定执行边界的场景。
- 并发安全性：在任务重试派发场景中，同一任务可能在不同时间点产生多个 Run ID。token_usage 和 mcp_call_log 的写入需要确保写入的是正确的（最新的）Run ID，避免归属错误。
- 与外部会话系统的同步延迟：Claude Code 和 Hermes 等外部会话系统通过定时扫描发现，其令牌消耗记录的写入可能晚于 Run ID 的状态转换。聚合查询在运行刚完成时可能无法立即获取完整令牌数据，需要在接口层面标注数据完整性状态。
- 长期存储与清理策略：Run ID 关联的数据分散在多个表中，清理策略需要考虑级联关系——当按保留策略清理 spawn_history 时，是否同步清理关联的 eval_runs，以及如何保证 token_usage 的清理不影响成本报表。
