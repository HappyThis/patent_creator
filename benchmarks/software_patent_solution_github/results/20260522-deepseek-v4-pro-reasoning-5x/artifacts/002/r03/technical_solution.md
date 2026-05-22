## 技术方案

本技术方案针对多 agent 协作平台中的执行过程记录、追踪与评估能力不足的问题，提出一种统一的 agent 执行记录模型及其支撑系统。该方案在现有任务派发、agent 编排、会话管理、成本统计和事件总线的基础上，构建一套可查询、可追踪、可附着评估结果、可被外部工具访问的结构化执行记录契约，而不是新增一张普通日志表。

### 核心技术方案：统一执行记录模型

系统在现有任务表（tasks）、spawn 历史表（spawn_history）、会话管理（sessions/token_usage/claude_sessions）、MCP 工具调用日志（mcp_call_log）、质量审查表（quality_reviews）、评估运行表（eval_runs）和安全事件表（security_events）的基础上，建立一套以任务执行为核心的统一记录关联模型。每条执行记录以一次 agent 的任务处置过程为锚点，通过外键或会话标识串联其 spawn 信息、会话上下文、token 消耗、工具调用明细、审查结果和评估得分。

### 执行记录的关联路径与数据模型

统一执行记录的核心关联路径为：任务（task）→ spawn 记录（spawn_history）→ 会话标识（dispatch_session_id / session_id）→ token 用量（token_usage）→ MCP 工具调用（mcp_call_log）→ 质量审查（quality_reviews）→ 评估结果（eval_runs）。上述关联通过以下关键字段实现：

- tasks 表通过 metadata 字段中的 dispatch_session_id 指向本次执行的会话标识，与该会话的 token_usage 和 mcp_call_log 按 session_id 关联；
- spawn_history 表通过 session_id 和 agent_name 与任务和会话建立双向追溯；
- quality_reviews 表通过 task_id 外键与任务关联，记录审查结论和审查意见；
- eval_runs 表通过 agent_name 维度聚合同一 agent 的多次执行评估，drift 检测通过 token_usage 和 mcp_call_log 的滚动窗口计算基线；
- task-costs 模块通过 token_usage 的 task_id 字段实现 token 消耗到任务的归属，支持按任务、agent、项目维度的成本归因报表。

### 执行全生命周期追踪流程

系统在 agent 执行的全生命周期中持续记录结构化数据，形成完整的执行追踪链。该流程覆盖从任务派发到最终评估的五个阶段：

1. 派发与 spawn 记录：scheduler 的 task_dispatch 定时任务将 assigned 状态的任务通过网关或直接 API 派发给 agent，同步在 spawn_history 表中写入 spawn 记录（含 agent_name、spawn_type、session_id、trigger 和 status=started），并更新任务状态为 in_progress；
2. 执行与会话关联：agent 执行完成后，系统将返回的 sessionId 写入任务 metadata.dispatch_session_id，agent 响应内容写入 tasks.resolution，任务状态流转至 review；同时 token_usage 和 mcp_call_log（如有）按 session_id 持续累计；
3. 质量审查：Aegis 审查任务（runAegisReviews）读取 resolution 内容生成审查结论，审查结果写入 quality_reviews 表；通过则任务进入 done 并设置 outcome=success，驳回则回退并附加反馈意见至 comments 表，支持最多 3 次重试；
4. 结果与评估记录：任务完成后，outcome 跟踪接口（/api/tasks/outcomes）提供按时间窗口、agent、优先级维度的成功率和耗时统计；agent-evals 模块的四层评估（output/trace/component/drift）按需运行并将评分持久化到 eval_runs 表；
5. 成本归因：token_usage 表通过 task_id 字段将每次 LLM 调用的 token 消耗归属到具体任务，task-costs 模块据此生成按任务、agent、项目和模型的成本报表（TaskCostReport），支持时间线趋势分析。

### 事件驱动的实时追踪与外部访问

所有执行状态变更通过统一的 ServerEventBus（单例模式）广播。事件类型覆盖 task.created、task.updated、task.status_changed、agent.status_changed、activity.created、notification.created、audit.security 等。前端通过 SSE（Server-Sent Events）端点 /api/events 订阅事件流，Zustand store 根据事件类型自动更新本地状态，无需轮询。SSE 连接支持指数退避重连（上限 20 次，最大延迟 30 秒）。

Webhook 子系统在事件总线上进一步扩展外部集成能力：webhook_deliveries 表记录每次投递的状态码、响应体、耗时和错误信息；支持指数退避重试（attempt 字段递增）、断路器保护（consecutive_failures 跟踪）和 HMAC-SHA256 签名验证。此外，MCP Server（35 个工具）和 REST API（101 个端点）为外部工具和脚本提供结构化查询入口，使执行记录可被第三方质量平台、审计系统或排行榜服务直接消费。

### 关键模块与职责划分

上述统一执行记录模型依赖以下核心模块协同工作，各模块职责明确、数据关联清晰：

- spawn-history：记录每次 agent spawn 的启动/完成状态、spawn 类型（claude-code/codex-cli/hermes）、触发来源、退出码、耗时和错误信息，提供按 agent 的统计聚合（总数/成功数/失败数/平均耗时）；
- task-dispatch：负责任务自动路由（autoRouteInboxTasks，基于角色亲和度和 agent 容量打分分配）、任务派发（dispatchAssignedTasks，支持网关模式、直接 API 模式和指定 session 续接模式）、过期任务重新入队（requeueStaleTasks）；
- task-costs：基于 token_usage 记录构建 TaskCostReport，提供按任务、agent、项目和模型的成本分解，含时间线趋势和未归属消耗统计；
- agent-evals：四层评估引擎——输出层评估任务完成率和正确性评分，追踪层检测工具调用收敛性和循环行为，组件层评估工具可靠性（含 P50/P95/P99 延迟），漂移层基于 4 周滚动基线检测 token 消耗、工具成功率和任务完成率的异常偏离（阈值 10%）；
- sessions：从网关 agent 会话存储文件中读取会话元数据（模型、token 用量、活跃状态），派生活跃/空闲/离线状态判定，支持会话 TTL 缓存和过期清理；
- event-bus + webhooks：事件总线广播所有状态变更，WebSocket/SSE 推送至前端，webhook_deliveries 提供投递可观测性和自动重试；
- MCP Server / REST API：对外暴露 agents、tasks、sessions、memory、skills、tokens、evals、cron、status 等 35 个 MCP 工具和 101 个 REST 端点，外部系统可通过标准化接口查询执行记录和统计数据。

### 技术效果

本方案通过构建结构化的统一执行记录契约，相比简单的日志记录方式产生以下技术效果：

- 可查询性：通过 task → spawn → session → token → tool_call → review → eval 的关联链路，任意一次 agent 执行都可按时间窗口、agent、项目、状态等多维度组合查询，无需日志解析或全文搜索；
- 可追踪性：spawn_history 记录每次执行的触发来源和生命周期状态，任务 metadata 中的 dispatch_session_id 将任务与会话精确绑定，形成从任务创建到最终评估的完整溯源链；
- 可评估性：四层评估框架的输出自动持久化到 eval_runs，drift 检测基于滚动窗口基线自动判断异常偏离，评估结果与执行记录在 agent_name 维度上自然关联，支持排行榜和质量趋势分析；
- 可外部访问性：MCP Server（35 个工具）和 REST API（101 个端点）为外部质量评测系统、审计平台或 CI/CD 流水线提供结构化数据消费接口；webhook 投递机制支持将执行记录变更实时推送至第三方系统；
- 可复用性：执行记录中的 resolution 内容、cost 数据、eval 评分为后续同类任务提供参考基线，recurring tasks 的 template-clone 模式基于历史执行记录生成子任务；
- 与现有系统的自然集成：方案复用已有的 tasks、agents、sessions、spawn_history、token_usage、mcp_call_log 等数据表，通过外键和会话标识建立关联，而非引入独立的日志表或外部存储，避免数据割裂。

### 风险与待确认问题

以下为当前方案基于项目环境分析后识别的待确认或需注意的风险点：

- mcp_call_log 表的 session_id 关联：当前 mcp_call_log 表主要通过 agent_name 维度查询，与具体 task 或 session 的直接关联较弱。如需将工具调用精确归属到某次任务执行，建议在 mcp_call_log 中增加 task_id 或 dispatch_session_id 字段；
- eval_runs 与 task 的关联粒度：当前 eval_runs 以 agent_name 维度聚合评估结果，而非以单次 task 执行维度。如需按任务粒度查询评估得分，建议在 eval_runs 中增加 task_id 可选字段；
- token_usage 的 task_id 回填时机：当前 task-costs 模块依赖 token_usage.task_id 进行成本归因，但 token_usage 记录可能在与 task 关联建立之前就已写入（如 agent 在非任务上下文中的 LLM 调用），未归属消耗归入 unattributed 统计。需确认是否需要更强的 task_id 回填机制；
- spawn_history 的 session_id 关联：spawn_history 通过 session_id 与会话建立关联，但并非所有 spawn 类型都会产生可追溯的 session_id（如 CLI 直接调用模式），此种情况下的执行记录完整性可能受限；
- 跨 workspace 的执行记录隔离：当前各数据表均已通过 workspace_id 实现多租户隔离，需确认统一执行记录查询接口是否需要支持跨 workspace 聚合视图（如超级管理员的全量审计场景）。
