## 技术方案

Mission Control 中每个 agent 的执行过程会产生多种维度的运行数据：spawn 生命周期记录、MCP 工具调用日志、token 消耗明细、质量审查结论、安全事件与信任评分、评估运行结果等。这些数据分散在独立的表和模块中，缺乏统一的运行记录契约将它们粘合成一个可查询、可追踪、可附着评估结果、可被外部工具访问的整体视图。本方案通过在现有分散记录之上建立统一的运行记录契约（Unified Run Record Contract），以 spawn_history 为主干记录每次 agent 运行的完整生命周期，以 task_id、session_id、agent_name、workspace_id 为关联键，将 MCP 调用审计、token 消耗、质量审查、安全事件和评估结果作为可附着（attachable）的记录片段挂载到主记录上，并通过事件总线、REST API 和数据库视图三个通道对外暴露统一的查询与订阅能力。

### 整体架构

统一运行记录契约以 spawn_history 表为主干运行记录，以 agent_name、task_id、session_id、workspace_id 为四维关联键，将 agent 每次执行的完整生命周期与周边记录关联为一个可查询的整体。

主干记录 spawn_history（migration 044）承载每次 agent spawn 的生命周期：agent_id、agent_name、spawn_type、session_id、trigger、status、exit_code、error、duration_ms、workspace_id、created_at、finished_at。一条 spawn_history 记录代表一次明确的 agent 执行，是契约中的一等实体。

可附着记录通过以下关联键与主干记录建立连接：mcp_call_log（migration 037）通过 agent_name + workspace_id 关联，记录每次 MCP 工具调用的 server、tool_name、success、duration_ms、error；token_usage（migration 018/025/039）通过 agent_name + task_id + session_id + workspace_id 四维关联，记录 model、input_tokens、output_tokens、total_tokens、cost_usd；quality_reviews（migration 002）通过 task_id 关联，记录 Aegis 审查的 reviewer_agent、decision、feedback、retry_count；security_events（migration 037）通过 agent_name + session_id 关联，记录 event_type、severity、source、trust_score_impact；eval_runs（migration 038）通过 agent_name + task_id 关联，记录四层评估结果与评分。

上述关联关系形成以 spawn_history 为中心的星型结构：一条 spawn 记录可关联多条 MCP 调用日志、多段 token 消耗明细、一次或多次质量审查、多条安全事件、以及一组评估运行结果。事件总线（event-bus，src/lib/event-bus.ts）在 spawn 开始/完成、任务状态变更、安全事件触发、审查决策时广播对应事件，使外部订阅者无需轮询数据库即可感知运行记录的变更。

现有 tasks 表已包含 metadata 字段（JSON），其中可存储 dispatch_session_id 和 dispatch_attempts，连接任务派发与 spawn 执行。spawn_history 的 session_id 字段进一步连接 claude_sessions 表（migration 020），使运行记录可追溯到具体的 Claude 会话实例。

### 运行记录的产生与附着

运行记录的产生贯穿 agent 执行全生命周期，由多个模块在关键节点写入对应表，最终通过关联键附着到主干 spawn_history 记录上。

第一阶段：任务派发与 spawn 触发。task-dispatch.ts 在任务创建后执行派发逻辑：根据任务类型和 agent 能力进行路由匹配，确定目标 agent 后通过 spawn 机制启动 agent 进程。spawn 启动时，spawn-history.ts 向 spawn_history 表写入一条 status='running' 的记录，记录 agent_name、task_id、session_id、spawn_type、trigger 和 workspace_id。task-dispatch.ts 同步更新 tasks 表的 metadata 字段，写入 dispatch_session_id 和 dispatch_attempts 供后续追溯。

第二阶段：执行中的增量记录。agent 运行期间，每次模型调用由 token 消耗记录模块写入 token_usage 表，携带 agent_name、task_id、session_id、workspace_id、model、input_tokens、output_tokens 和 cost_usd。每次 MCP 工具调用由 mcp-audit.ts 写入 mcp_call_log 表，记录 agent_name、mcp_server、tool_name、success、duration_ms 和 error。安全事件由 security-events.ts 实时写入 security_events 表，并在事件总线广播 security.event，同时更新 agent_trust_scores 表。

第三阶段：完成与审查。agent 执行完成后，spawn-history.ts 更新对应 spawn_history 记录的 status、exit_code、error、duration_ms 和 finished_at。task-dispatch.ts 中的 Aegis 质量审查机制随后触发：审查 agent 自动评估产出质量，向 quality_reviews 表写入 reviewer_agent、decision（approved/rejected）、feedback 和 retry_count。若 decision 为 rejected 且重试次数未达上限（默认 3 次），任务重新进入 dispatch 队列，生成新的 spawn 记录。

第四阶段：成本聚合。task-costs.ts 提供按 task_id、agent_name、project 维度的成本聚合查询。它读取 token_usage 表的 cost_usd 字段，结合 spawn_history 的 duration_ms 计算时间成本，按时间线（timeline）输出每次 spawn 的成本明细。聚合结果通过 API 层暴露，可附着到运行记录的统一查询视图中。

全链路中，task_id 是最稳定的关联键——从任务创建到最终完成贯穿始终；session_id 在每次 spawn 时可能变化（重试时产生新 session），通过 metadata.dispatch_session_id 保留历史映射；agent_name + workspace_id 作为补充关联维度，确保即使 task_id 缺失也能定位到对应记录。

### 评估结果的结构化附着

评估引擎（agent-evals.ts）提供四层评估能力，每层评估结果通过 eval_runs 表结构化持久化，并与 spawn_history 主干记录通过 agent_name + task_id 绑定，使每次 agent 运行都可附着量化的评估结论。

Layer 1 — 输出评估：评估 agent 任务完成率与输出正确性。对每次 agent 运行，系统将 agent 产出与 golden_sets（eval_golden_sets 表）中的参考答案进行比对，计算完成度分数和正确性分数。结果写入 eval_runs 表，字段包括 agent_name、task_id、eval_layer='layer1_output'、score、details（JSON 格式的逐项比对结果）。

Layer 2 — 追踪评估：评估 agent 推理过程的收敛性与一致性。系统从 eval_traces 表中读取 agent 执行期间的中间步骤记录（推理链），分析步骤间的逻辑跳跃度、重复推理次数和最终收敛步数，生成收敛性分数和一致性分数。结果同样写入 eval_runs 表，eval_layer='layer2_trace'。

Layer 3 — 组件评估：评估 agent 使用的工具链可靠性。系统汇总 mcp_call_log 中该次运行的 MCP 调用记录，统计各工具的 success_rate、平均 duration_ms、错误类型分布，生成组件可靠性分数。结果写入 eval_runs，eval_layer='layer3_component'。

Layer 4 — 漂移评估：将当前评估分数与历史滚动基线进行对比。系统从 eval_runs 表中读取该 agent 过去 N 次同层评估分数的移动平均值作为基线，计算当前分数与基线的偏差（drift），检测 agent 是否存在性能退化或异常波动。结果写入 eval_runs，eval_layer='layer4_drift'，details 中包含基线值、偏差方向和幅度。

四层评估通过统一的 eval_runs 表持久化，每条记录携带 agent_name 和 task_id 作为关联键，可直接 JOIN spawn_history 获取运行上下文。eval_runs 同时记录 evaluated_at 时间戳和 evaluator_version，支持评估结果的可审计性和可复现性。评估触发可由事件总线上的 agent.completed 事件自动驱动，也可通过 API 手动触发单层或全层评估。

### 可查询接口设计

统一运行记录契约通过三个通道对外暴露查询与订阅能力：数据库层 JOIN 查询、模块级导出函数、以及事件总线订阅。

数据库层统一查询。spawn_history 作为主表，通过 agent_name、task_id、session_id、workspace_id 四维关联键 LEFT JOIN 各附着表，可构建一次 agent 运行的完整画像。典型查询模式为：以 spawn_history 的 id 或 (agent_name, task_id) 组合为入口，JOIN mcp_call_log 获取工具调用明细，JOIN token_usage 获取 token 消耗与成本，JOIN quality_reviews 获取审查结论，JOIN security_events 获取安全事件，JOIN eval_runs 获取四层评估分数。所有 JOIN 条件均基于已有索引列（各表的 agent_name、task_id、workspace_id 列），查询性能可控。

模块级查询函数。spawn-history.ts 导出按 agent、task、session、时间范围查询 spawn 记录的函数；task-costs.ts 导出 getCostsByTask、getCostsByAgent、getCostTimeline 三个聚合查询函数，支持按任务/agent/项目维度汇总 token 成本和估算时间成本；agent-evals.ts 导出按 agent + task 查询评估结果的函数，支持按 eval_layer 过滤；mcp-audit.ts 导出按 agent + workspace 查询 MCP 调用日志的函数；sessions.ts 提供网关会话读取能力，可追溯 Claude 会话级信息；task-status.ts 提供任务状态归一化查询，将底层分散的任务状态字段映射为统一的状态枚举。

事件总线订阅。event-bus.ts 在关键节点广播事件：task.dispatched（任务派发时）、task.completed / task.failed（任务完成/失败时）、agent.spawned / agent.completed（agent spawn 生命周期）、security.event（安全事件触发时）、audit.security（审计事件）。外部系统可通过事件总线订阅这些事件，无需轮询数据库即可感知运行记录变更。事件 payload 中携带 task_id、agent_name、session_id 等关联键，订阅方可据此调用模块级查询函数获取完整上下文。

拓展方向：可在上述模块之上构建统一的 REST API 层，提供 GET /runs/:id 获取单次运行的完整聚合视图（含 spawn 信息、MCP 调用、token 消耗、审查结论、安全事件、评估分数），GET /runs 支持按 agent、task、时间范围、评估分数阈值等条件筛选，以及 GET /runs/:id/export 导出结构化运行报告。

### 外部工具可访问性

统一运行记录契约的设计目标之一是使运行数据可被外部工具（如监控面板、CI/CD 流水线、合规审计系统、自定义分析脚本）访问和消费，而不局限于 Mission Control 内部使用。

事件总线外部消费。event-bus.ts 的服务端事件（task.*、agent.*、security.event、audit.security）在进程内广播，外部工具可通过以下方式接入：在服务端注册事件监听器，将事件转发到外部 Webhook URL 或消息队列（如 Redis Pub/Sub、Kafka）；事件 payload 携带 task_id、agent_name、session_id 作为关联键，消费方可调用查询接口获取完整运行记录。security-events.ts 中的信任评分变更事件可被外部 SIEM 系统订阅，实现安全态势的跨系统感知。

审计日志的外部访问。audit_log 表（migration 007）记录所有关键操作的时间戳、操作者、操作类型和详情。该表独立于 spawn 生命周期，提供系统级的操作追踪能力。外部合规审计工具可通过数据库只读副本或定期导出机制访问审计日志，与 spawn_history 通过时间窗口和操作者关联。

结构化导出。exports 目录支持将运行记录、评估结果和成本统计导出为结构化格式。导出时以 spawn_history 为主记录，JOIN 各附着表构建完整运行画像后序列化输出。导出格式可支持 JSON（供程序消费）和 Markdown/HTML（供人工审阅）。导出内容包含：运行摘要（agent、任务、耗时、状态）、工具调用清单、token 消耗与成本、审查结论、安全事件摘要、四层评估分数，形成自包含的运行报告。

拓展方向：可基于 agent_trust_scores 表（migration 037）建立 agent 信任评分的对外查询 API，使外部调度系统在派发任务前查询 agent 的当前信任等级；可建立只读数据库视图（SQL VIEW）将 spawn_history 与各附着表的 JOIN 结果物化为 run_record_view，供外部 BI 工具直接查询；可支持 Webhook 注册机制，允许外部系统注册回调 URL 以接收特定 agent 或任务类型的运行完成通知。

### 技术效果

统一运行记录契约从以下维度产生可验证的技术效果：

可查询性：通过 spawn_history 主表与各附着表的 JOIN 查询，一次 agent 运行的完整画像（生命周期、工具调用、token 消耗、审查结论、安全事件、评估分数）可通过单次查询入口获取，无需跨模块多次调用。关联键（agent_name、task_id、session_id、workspace_id）在写入时即由各模块统一携带，查询时无需额外的映射转换。

可追踪性：从任务创建（tasks 表）→ 任务派发（task-dispatch.ts 写入 metadata.dispatch_session_id）→ agent spawn（spawn_history）→ 执行中增量记录（mcp_call_log、token_usage、security_events）→ 审查（quality_reviews）→ 评估（eval_runs），全链路通过 task_id 和 session_id 串联。任一节点的异常（如 MCP 调用失败、安全事件触发、审查拒绝、评估分数下降）均可沿链路反向追踪到具体的 spawn 记录和执行上下文。

可评估性：四层评估引擎的输出通过 eval_runs 表与 spawn_history 绑定，使每次运行的评估结果与运行上下文（耗时、工具使用、token 消耗）形成关联。Layer4 漂移评估持续跟踪 agent 性能变化趋势，可自动检测性能退化。

外部可访问性：事件总线使外部系统无需轮询即可感知运行状态变更；模块查询函数为 API 层提供统一数据访问入口；审计日志和结构化导出支持合规审计和离线分析。

系统可观测性提升：运行记录契约将分散的日志和指标统一为结构化记录，降低排查 agent 执行问题的定位时间。安全事件与信任评分的实时更新使系统具备主动防御能力——信任评分低于阈值的 agent 可被自动限制 spawn 频率或工具权限。

### 风险点与应对

风险一：关联键缺失导致记录断裂。部分记录（如 mcp_call_log）仅通过 agent_name + workspace_id 关联，缺少 task_id 字段时无法直接 JOIN 到具体任务。应对：优先在 mcp_call_log 写入时携带 task_id（若当前上下文可用）；查询时采用 agent_name + workspace_id + 时间窗口作为降级关联条件，通过 created_at 范围匹配最近的 spawn_history 记录。

风险二：重试场景下的记录一致性。Aegis 审查拒绝后任务重新派发，产生新的 spawn_history 记录和新的 session_id。旧记录的评估结果和安全事件需要通过 task_id 聚合而非 session_id，否则可能丢失重试前的上下文。应对：统一以 task_id 为聚合主键，spawn_history 以 id 区分每次尝试；在查询视图层按 task_id 聚合多条 spawn 记录，标注每次尝试的序号（通过 dispatch_attempts 字段）。

风险三：事件总线的进程内限制。当前 event-bus.ts 在 Node.js 进程内广播事件，分布式部署或多实例场景下，事件无法跨实例传播。应对：在进程内事件总线之上，增加可选的外部消息队列适配层（如 Redis Pub/Sub），使关键事件（task.completed、agent.completed、security.event）可跨实例广播；事件消费方通过幂等键（event_id + task_id）去重。

风险四：评估引擎的计算开销。四层评估中 Layer2 追踪评估和 Layer4 漂移评估涉及历史数据聚合和推理链分析，计算开销随 eval_traces 数据量增长。应对：Layer4 漂移评估采用滚动窗口（最近 N 次运行）限制数据量；Layer2 追踪评估可异步延迟执行，不阻塞 agent 完成后的审查流程；评估结果缓存于 eval_runs 表，避免重复计算。
