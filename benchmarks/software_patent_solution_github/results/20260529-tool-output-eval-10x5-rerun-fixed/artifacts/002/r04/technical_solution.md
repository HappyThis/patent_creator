## 技术方案

本方案提出一种面向多 Agent 协作平台的统一运行记录契约机制，通过将平台内既有的 spawn 启动追踪、token 用量记录、任务执行结果、多维度评估、活动流、安全审计日志等分散的数据表整合为一份具有稳定查询语义的运行记录契约，使任意 Agent 从被调度（dispatch）到执行完成、评估、可观测的全生命周期数据均可通过统一入口被查询、追踪和外部工具访问。方案不新增普通日志表，而是在现有表结构基础上，通过契约层定义字段映射、关联键规范和对外暴露接口，形成跨模块的稳定数据协议。

### 整体架构与数据模型

统一运行记录契约建立在以下既有数据表之上，每张表均含 workspace_id 实现多租户隔离，并通过 session_id、task_id、agent_name 等字段形成关联网络：spawn_history（Agent 启动追踪，含 spawn_type/status/exit_code/duration_ms）、token_usage（每次模型调用的 input/output token 和 cost_usd，含 task_id 归因）、tasks（任务执行结果，含 outcome/error_message/retry_count）、eval_runs（四层评估结果持久化，含 correctness_score/convergence_score/tool_reliability）、mcp_call_log（MCP 工具调用详情，含 success/duration_ms/error）、activities（实体级事件流，持久化）、audit_log（操作审计，含 action/actor/ip_address）。

契约层以 session_id 为主关联键串联 dispatch→spawn→token→outcome→eval 全链路，以 task_id 为聚合维度支持按任务维度的成本和结果报告，以 agent_name 为维度支持按 Agent 的行为画像和信任评估。各表之间不引入新的外键约束，而是在契约定义中明确字段映射规则和查询语义。

### 核心处理链路：从 Dispatch 到评估

Dispatch 阶段：当任务进入 dispatch 流程，classifyTaskModel 根据任务内容关键词和优先级将任务路由至 Haiku（Routine）、Sonnet（Moderate）或 Opus（Complex）三级模型，支持 agent.config.dispatchModel 覆盖。dispatch 成功后自动向 token_usage 表写入初始记录，关联 session_id 和 task_id，为后续全链路追踪奠定起点。dispatch 尝试记录写入 task_dispatch_attempts 表（migration 045），含模型选择理由和路由耗时。

Spawn 与执行阶段：Agent 进程启动时，recordSpawnStart 向 spawn_history 写入 started 状态，记录 agent_name、spawn_type、trigger、session_id。执行期间，每次模型调用通过 gateway JSON-RPC 或直接 Claude API 完成，token 用量由调用层自动写入 token_usage，含 model、input/output tokens、cost_usd、agent_name。MCP 工具调用经 command 子进程封装执行，调用结果（success/duration_ms/error）写入 mcp_call_log。

结果落盘与评估阶段：Agent 执行结束后，recordSpawnFinish 将 spawn_history 更新为 completed/failed/terminated，写入 exit_code、error、duration_ms。任务级 outcome（success/failed/partial/abandoned）写入 tasks 表，含 error_message 和 resolution。评估引擎随即触发：evalTaskCompletion 判定任务完成度，evalCorrectnessScore 计算正确性得分，evalToolReliability 基于 mcp_call_log 分析工具可靠性，runDriftCheck 检测行为漂移；四层结果持久化至 eval_runs，trace 路径写入 eval_traces。

### 可观测与对外查询层

Event Bus 广播层：ServerEventBus（单例）在 dispatch 触发、spawn 状态变更、activity 生成、安全事件发生时广播对应事件。事件类型覆盖 task/agent/activity/security 四类，payload 携带 session_id、task_id、agent_name 等关键关联字段。事件总线不持久化，仅负责进程内实时分发。

SSE 实时推送层：/api/events 端点基于事件总线订阅，将运行记录事件以 Server-Sent Events 形式推送到前端，按 workspace_id 隔离，30 秒心跳维持连接。前端可通过 SSE 实时观察 Agent 执行进度、token 消耗趋势、评估结果，无需轮询。

RESTful 查询 API 层：/api/activities 端点支持按 type、actor、entity_type 过滤查询持久化的活动流，含 entity detail 增强；spawn_history 通过 getSpawnHistory/getSpawnStats 暴露按 Agent/时间范围的启动统计；token_usage 通过 TaskCostReport 暴露按任务/Agent/项目维度的成本和用量聚合；/api/audit 端点（admin only）暴露审计日志。上述 API 共同构成对外部工具友好的稳定查询契约，外部系统可通过标准 HTTP 调用获取运行记录、评估结果和成本数据。

### 统一运行记录契约的三层约定

统一运行记录契约并非新建一张聚合表，而是通过以下三层约定形成稳定数据协议：第一层为字段映射契约——明确各表之间通过 session_id、task_id、agent_name 的关联语义。session_id 标识一次 Agent 会话的生命周期边界，是 spawn_history、token_usage、sessions 三表的主关联键；task_id 标识任务维度，是 tasks、token_usage、eval_runs 的聚合键；agent_name 标识执行主体，贯穿 spawn_history、token_usage、mcp_call_log、agent_trust_scores。

第二层为查询语义契约——对外暴露的查询接口（getSpawnHistory、TaskCostReport、eval_runs 查询、activities 过滤、audit 查询）承诺稳定的入参、出参结构和分页语义，外部工具可据此构建自动化流水线而无需感知底层表结构变化。第三层为生命周期契约——定义记录写入的时序约束：dispatch 记录先于 spawn 记录，spawn started 先于 token_usage，token_usage 先于 outcome 落盘，outcome 落盘后触发 eval，eval 完成后方可标记任务终态。该时序约束在契约中声明，由 dispatch 流程和 spawn 生命周期管理代码保证执行。

### 安全审计与信任评估

运行记录契约同时覆盖安全与信任维度。migration 037 引入的 agent_trust_scores 表跨维度记录每个 Agent 的信任评分，含 auth_failures（认证失败次数）、injection_attempts（注入尝试检测）、rate_limit_hits（速率限制触发）、secret_exposures（密钥泄露风险），各维度评分随每次 Agent 执行动态更新。audit_log 表记录所有管理操作的 action/actor/ip_address/user_agent/detail，提供不可擦除的操作轨迹。

信任评分与运行记录契约的集成方式：eval_runs 中的 component 层评估引用 mcp_call_log 中的工具调用记录作为输入，drift 层评估将当前行为轨迹与 eval_golden_sets 中的基准数据对比；评估结果可触发 agent_trust_scores 的自动下调。审计日志通过 /api/audit 端点暴露，与 activities 流中的 security 类事件形成互补——前者面向合规审计，后者面向实时安全态势感知。

### 技术效果与扩展点

本方案产生的技术效果包括：（1）可查询性——通过 session_id/task_id/agent_name 三维关联键，外部系统可单次查询获取 Agent 从启动到评估的完整运行记录；（2）可追踪性——spawn_history 与 token_usage 的时序关联使每次模型调用的成本和耗时可精确定位到具体 Agent 会话和任务；（3）可评估性——四层评估引擎的持久化结果与 mcp_call_log、token_usage 形成闭环，评估结论可追溯到原始工具调用和模型响应；（4）外部可访问性——RESTful API 和 SSE 端点提供标准 HTTP 访问，第三方 CI/CD 或监控系统可直接消费运行记录而不依赖平台内部 SDK。

扩展点包括：契约层支持通过 webhook 回调将运行记录事件推送到外部系统（当前事件总线已具备 task/agent/activity/security 事件类型，webhook 层仅需新增订阅配置和 HTTP 投递逻辑）；eval_golden_sets 支持用户自定义基准数据集以扩展 drift 检测的覆盖范围；agent_trust_scores 的维度可按需扩展新的信任指标；mcp_call_log 的工具调用记录可作为 component eval 的扩展输入源。

### 待确认点与风险

（1）契约稳定性依赖现有表结构——若 spawn_history、token_usage、eval_runs 等表发生字段级 breaking change，契约的字段映射规则需同步更新；建议通过迁移版本号与契约版本号关联，在 API 层做兼容转换。（2）生命周期时序约束当前由 dispatch 和 spawn 管理代码隐式保证，缺乏显式的状态机校验；若未来引入新的 Agent 启动路径绕开 dispatch，可能导致链路断裂。（3）eval 触发当前为同步紧耦合，大规模并发 Agent 执行时评估可能成为瓶颈——可通过异步评估队列解耦。（4）SSE 推送依赖单进程事件总线，多实例部署时需引入跨进程事件分发机制（如 Redis Pub/Sub）以保证所有实例的 SSE 客户端收到一致的事件流。
