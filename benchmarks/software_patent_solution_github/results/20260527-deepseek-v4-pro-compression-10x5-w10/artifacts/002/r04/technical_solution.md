## 技术方案

### 整体架构

本方案在 Mission Control 现有数据基础设施之上，构建一套以 agent 单次执行为粒度的统一运行记录（RunRecord）契约。该契约将分散在 spawn_history、token_usage、mcp_call_log、eval_runs、quality_reviews、audit_log 等多个表中的执行痕迹，按 agent spawn 事件作为生命周期锚点进行归集，形成结构化、可查询、可追踪、可附着评估结果、可被外部工具访问的统一记录单元。

系统整体分为四层：（1）采集层——在 agent 执行路径的各个关键节点（spawn、工具调用、token 消耗、审查、评估）以事件驱动方式写入对应持久化表；（2）归集层——以 spawn_history 记录的 run_id 为主键，将各表记录通过 agent_name + session_id + 时间窗口进行关联归集，构建完整的 RunRecord 视图；（3）评估层——四层评估引擎（output/trace/component/drift）将评估结果附着到对应的 RunRecord 上；（4）访问层——通过 SSE 实时流、REST 查询/导出接口、审计 API 向外部系统暴露运行记录。

### 运行记录数据契约（RunRecord）

RunRecord 定义为 agent 单次执行（spawn）的完整运行记录，包含以下结构化字段，各字段来源于系统已有的持久化表，通过 run_id 进行关联：

- run_id：对应 spawn_history 表的主键，作为运行记录的唯一标识
- agent_name：执行 agent 名称，关联 agents 表
- spawn_type：触发类型（manual/scheduled/webhook/auto-routing），来源于 spawn_history.spawn_type
- session_id：关联的 OpenClaw 会话标识，用于检索会话级 token 统计和对话历史
- status：执行终态（completed/failed/cancelled/timeout），来源于 spawn_history.status
- exit_code：进程退出码，来源于 spawn_history.exit_code
- duration_ms：执行耗时，来源于 spawn_history.duration_ms
- token_usage：Token 消耗明细（input/output/total tokens 及 cost_usd），来源于 token_usage 表按 agent_name + task_id 聚合
- tool_calls：工具调用序列，来源于 mcp_call_log 表按时间排序，含工具名、成功/失败状态、耗时
- quality_review：Aegis 质量审查结果（approved/rejected/re-dispatch/failed），来源于 quality_reviews 表
- eval_results：四层评估结果（output/trace/component/drift），来源于 eval_runs 表
- task_context：关联的任务上下文（task_id、标题、优先级），来源于 tasks 表
- created_at / completed_at：记录创建和完成时间戳

### 与现有系统的关联点

RunRecord 并非新建独立存储，而是在现有持久化表之上构建的归集视图。各字段与现有系统的关联关系如下：

- spawn_history 表（migration 044）：提供 run_id、agent_name、spawn_type、session_id、status、exit_code、error、duration_ms，构成 RunRecord 的生命周期骨架
- token_usage 表（migration 018/025/039）：通过 agent_name + task_id 关联，聚合单次执行的 input/output/total tokens 和 cost_usd，提供 Token 成本归因
- mcp_call_log 表（migration 037）：通过 session_id 或 agent_name + 时间窗口关联，提供工具调用序列（tool_name、success、duration_ms、error）
- quality_reviews 表（migration 002）：通过 task_id 关联 Aegis 审查结果（review_status、review_summary、reviewed_at），反映执行质量
- eval_runs / eval_golden_sets / eval_traces 表（migration 038）：通过 agent_name 关联四层评估结果，支持历史对比和 drift 检测
- tasks 表：通过 task_id 关联任务上下文，获取标题、优先级、outcome、feedback_rating 等任务级元数据
- audit_log 表（migration 007）：通过 actor + 时间窗口关联实例级审计事件，补充安全合规维度

### 记录生命周期

RunRecord 的生命周期与 agent spawn 事件严格绑定，分为四个阶段：

一、创建阶段。当 task-dispatch.ts 或外部触发源发起 agent spawn 时，spawn-history.ts 在 spawn_history 表中写入一条初始记录（status=pending），生成 run_id。同时 event-bus 广播 agent.spawned 事件，SSE 实时推送运行记录创建通知。

二、执行阶段。agent 运行期间，以下数据以 run_id 为关联键持续写入：（a）mcp_call_log 记录每次工具调用的成功/失败和耗时；（b）token_usage 在每次 LLM 调用后累加写入 token 消耗和成本；（c）task-dispatch.ts 中的 Aegis 审查流程将 quality_reviews 结果写入。event-bus 广播 chat.message 和 task.status_changed 事件，外部系统可通过 SSE 实时追踪执行进度。

三、完成阶段。agent 进程退出时，spawn-history.ts 更新 spawn_history 记录（status=completed/failed，写入 exit_code、error、duration_ms）。event-bus 广播 agent.status_changed 事件。RunRecord 进入终态，不可再追加工具调用或 token 消耗记录，但评估结果可后续附着。

四、评估阶段。RunRecord 进入终态后，agent-evals.ts 的四层评估引擎可异步触发评估运行：output 层比对 agent 输出与 golden set；trace 层分析工具调用路径；component 层评估各模块表现；drift 层检测与历史基线的偏离。评估结果通过 eval_runs 表持久化，并与 run_id 关联。

### 评估附着机制

评估附着机制是 RunRecord 区别于普通日志的关键特征。系统通过以下方式将评估结果结构化附着到运行记录上：

一、评估触发。评估运行可由以下事件触发：（a）agent 执行完成（agent.status_changed → completed）自动触发；（b）通过 /api/agents/evals 接口手动触发；（c）golden set 更新后触发已有 RunRecord 的重新评估。触发逻辑在 agent-evals.ts 中实现，每次评估生成唯一的 eval_run_id。

二、四层评估附着。（1）OutputEval：将 agent 输出与 eval_golden_sets 中的预期输出比对，生成相似度分数和差异摘要；（2）TraceEval：分析 mcp_call_log 中的工具调用序列，与预期调用路径比对，检测异常调用或遗漏；（3）ComponentEval：按 agent 内部模块（路由、派发、审查）分别评估性能指标；（4）DriftEval：将本次 RunRecord 的 output/trace/component 指标与历史基线（同一 agent 的历史评估均值）对比，检测性能漂移。各层评估结果以 eval_run_id 为主键写入 eval_runs 表，同时通过 run_id 关联到 RunRecord。

三、评估索引。eval_runs 表按 agent_name + run_id + eval_type 建立联合索引，支持按 agent 查询历史评估趋势、按 run_id 查询单次执行的完整评估快照、按 eval_type 横向对比同类评估。

### 外部访问接口

RunRecord 通过三条路径向外部系统暴露，形成统一的访问契约：

一、实时事件流（SSE）。/api/events 路由基于 event-bus 单例，按 workspace 隔离推送事件。与 RunRecord 相关的事件包括：agent.spawned（记录创建）、agent.status_changed（记录终态更新）、task.status_changed（任务状态关联变更）、chat.message（执行过程消息）。外部系统通过订阅 SSE 流获取运行记录的实时生命周期事件，无需轮询。

二、REST 查询与导出。（1）/api/agents/evals 按 agent_name 查询评估结果和 drift 时间线，返回以 run_id 为维度的评估快照；（2）/api/export 支持 admin 按时间范围和过滤条件导出 RunRecord 数据（含 spawn_history、token_usage、mcp_call_log、eval_results）为 CSV 或 JSON 格式；（3）/api/audit 按 action/actor/time 查询 audit_log，补充运行记录的安全合规维度。各接口均通过 workspace 隔离和 admin 鉴权保护。

三、程序化访问。spawn-history.ts 提供 getSpawnHistoryByAgent() 和 getSpawnStats() 函数，task-costs.ts 提供 getTaskCosts() 和 getCostReport() 函数，agent-evals.ts 提供 getEvalRunsByAgent() 和 getDriftTimeline() 函数。外部工具可直接导入这些模块函数进行程序化查询和二次分析。

### 技术效果

本方案通过构建统一的 RunRecord 契约，在现有系统基础上取得以下技术效果：

- 可追踪性：以 run_id 为主线将分散在 7 张表中的执行痕迹串联为完整执行链路，外部系统可通过单一标识符检索 agent 从 spawn 到评估的全生命周期数据
- 可评估性：四层评估结果以 run_id 为锚点附着到运行记录上，支持按 agent、按时间、按评估维度进行纵向对比和 drift 检测，使 agent 性能退化可被自动发现
- 实时可观察性：通过 event-bus + SSE 将运行记录的关键生命周期事件实时推送，外部监控系统无需轮询即可感知 agent 执行状态变更
- 外部可访问性：通过 SSE 流、REST API、导出接口和程序化模块函数四条路径，RunRecord 可被外部 CI/CD 流水线、监控面板、合规审计系统等工具消费，无需直接访问数据库
- 成本归因：token_usage 的 cost_usd 字段通过 task_id 和 agent_name 关联到 RunRecord，实现单次 agent 执行的精确成本核算，支持按项目、按 agent、按时间线的成本聚合分析
- 复用现有基础设施：方案不引入新的存储引擎或独立服务，完全基于现有 SQLite 表、event-bus 单例、SSE 路由和 API 路由构建，降低系统复杂度和维护成本
