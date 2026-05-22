## 技术方案

### 要解决的技术问题

在现有的 agent 编排系统中，agent 执行过程的记录分散在多个独立的数据表中：spawn_history 记录 agent 进程的启动与终止，token_usage 记录 token 消耗与成本，tasks 记录任务结果与反馈，mcp_call_log 记录工具调用详情，quality_reviews 记录质量审查结论，eval_runs 和 eval_traces 记录评估结果。这些表之间仅通过 agent_name、session_id 等字符串字段进行弱关联，缺乏统一的执行记录标识。

由此导致以下技术问题：第一，无法通过单一标识查询一次 agent 执行的完整证据链，需要跨多表进行字符串匹配 JOIN，查询效率低且结果不可靠；第二，大量 token 消耗记录因 task_id 可空而成为"无归属成本"，无法精确核算单次执行的实际开销；第三，工具调用日志（mcp_call_log）与任务和会话之间缺少直接关联，追踪具体工具调用属于哪次执行需要人工推断；第四，评估结果（eval_runs/eval_traces）挂载在 agent_name 维度而非执行维度上，无法对单次运行进行精细化的质量归因。

### 核心技术方案：统一运行记录契约

本方案的核心是引入运行记录标识（Run ID），在 agent 执行的完整生命周期中注入同一标识，将当前分散在 spawn_history、token_usage、tasks、mcp_call_log、quality_reviews、eval_runs、eval_traces 等表中的记录，收敛为以 Run 为原子单位的可追溯执行契约。方案不新增独立的日志表，而是在现有表结构上补强关联字段，使得一次 agent spawn 从启动到终止的全部过程信息（会话对话、任务流转、token 消耗、工具调用、质量审查、评估结论）均可通过 Run ID 一键串联。

Run 定义为一次 agent spawn 从 recordSpawnStart 到 recordSpawnFinish 的完整执行过程。一次 Run 内部可以包含：多轮 session 对话、一个或多个 task 的创建与流转、token 消耗记录、MCP 工具调用序列、Aegis 质量审查结论以及四层评估（output / trace / component / drift）结果。

### Run ID 的生成与注入链路

Run ID 的生成时机为 recordSpawnStart 被调用时，由系统生成全局唯一的标识符（UUID），作为 spawn_history 记录的主关联键。Run ID 随后通过显式参数传递方式注入到下游各入口，保持函数调用链的可追溯性。具体注入链路如下：

第一步，recordSpawnStart 生成 run_id 并写入 spawn_history 表的 run_id 字段，同时将 run_id 返回给调用方。第二步，调用方将 run_id 传递给 Gateway 会话初始化过程，写入 sessions.json 的 run_id 字段，使得该 session 内产生的 token_usage 记录在写入时携带同一 run_id。第三步，当 agent 在执行过程中创建或领取 task 时，将 run_id 写入 tasks 表的 run_id 字段，后续该 task 关联的 quality_reviews 即可通过 tasks.id 间接追溯到 run_id。第四步，MCP 工具调用拦截层在写入 mcp_call_log 时，同时写入 run_id 和当前 task_id，实现工具调用到执行和任务的双重关联。第五步，评估触发时，eval_runs 和 eval_traces 写入 run_id，使评估结论挂载在执行维度而非仅 agent 维度。

对于无法归属到特定 task 的 token 消耗（如 agent 启动时的系统提示消耗），标记为 run 级 unattributed，但仍可通过 run_id 追溯到所属执行，解决当前大量"无归属成本"无法归因的问题。

### 与现有系统的关联方式

Run ID 通过在各现有表中新增关联字段的方式与系统集成，不改变表名、不新增独立日志表。各表的改造方式如下：

- spawn_history：新增 run_id 字段（TEXT NOT NULL UNIQUE），作为运行记录的根节点。同时将 session_id 补强为外键约束，确保 spawn 与 session 的关联一致性。
- token_usage：新增 run_id 字段（TEXT），与已有的 task_id、agent_name、model、cost_usd 等字段并列，支持按 run_id 聚合成本。
- mcp_call_log：新增 run_id 字段和 task_id 字段，使工具调用记录同时归属到执行和任务两个维度。
- tasks：新增 run_id 字段，使任务直接关联到创建它的执行。quality_reviews 通过 tasks.id 间接获得 run_id 追溯能力。
- eval_runs / eval_traces：新增 run_id 字段，评估结果挂载到具体执行。
- sessions（磁盘 JSON）：在 sessions.json 的会话记录中增加 run_id 字段，使会话数据在文件层面即可追溯。

此外，event-bus 在发布 task.*、agent.*、security.* 等事件时，在事件 payload 中携带 run_id 字段，使得实时事件订阅方可以按 run_id 过滤和关联事件流。新增 run_id 字段为附加字段，不影响现有 SSE 和 WebSocket 订阅方的解析逻辑，保证向后兼容。

### 统一查询契约与外部工具访问

基于 Run ID 的统一关联，系统对外暴露结构化的运行记录查询契约。以 run_id 为入参，一次性返回该次执行的完整信息聚合，包含以下组成部分：

- spawn 基础信息：agent_name、spawn_type、status、exit_code、duration_ms、created_at、finished_at
- session 概要：session_id、对话轮次（messageCount）、模型信息
- token 成本明细：按 task 分组的 input_tokens / output_tokens / cost_usd，标注 unattributed 部分，提供 run 级汇总
- 任务链：该 run 内创建或关联的所有 task，包含 title、status、outcome、retry_count、feedback_rating
- 工具调用序列：来自 mcp_call_log 的 tool_name、success、duration_ms、error 列表
- 质量审查结果：关联的 quality_reviews 的 reviewer、status、notes
- 评估结论：四层评估（output / trace / component / drift）的 score、passed、detail

该查询契约通过 REST API 端点暴露（如 GET /api/runs/:run_id），返回结构化 JSON（camelCase 命名），同时可作为 MCP Tool 注册到外部工具链中，供质量评测系统、排行榜服务、运行审计工具和 CI/CD 流水线通过标准协议查询和复用。查询接口支持 workspace_id 级别的多租户隔离，确保不同工作区的运行记录相互独立。

### 关键处理流程

Run ID 贯穿 agent 执行的完整生命周期，各阶段处理流程如下：

启动阶段：当系统通过 POST /api/spawn 或内部调度器触发 agent 生成时，recordSpawnStart 被调用，生成 run_id 并写入 spawn_history（status='started'）。run_id 随返回值传递给调用方。调用方将 run_id 注入 Gateway 会话初始化参数，Gateway 在 sessions.json 中记录该 run_id。此后该 session 的 token_usage 写入操作自动携带 run_id。

执行阶段：agent 在 session 中执行任务。当 agent 通过 POST /api/tasks 创建任务或通过 GET /api/tasks/queue 领取任务时，tasks 表的新记录或更新记录中写入当前 run_id。MCP 工具调用拦截层在每次工具调用完成时写入 mcp_call_log，同时记录 run_id 和当前活跃的 task_id。event-bus 在发布 task.created、task.status_changed 等事件时，在 payload 中附加 run_id。

完成与审查阶段：agent 完成任务并将任务状态推进到 review 时，Aegis 质量审查系统对任务进行审批。quality_reviews 记录通过 tasks.id 与 run_id 间接关联。评估系统（agent-evals）在执行完成后触发，eval_runs 和 eval_traces 写入 run_id，实现对单次运行的精细化质量评估。

终止阶段：recordSpawnFinish 被调用，更新 spawn_history 的 status、exit_code、duration_ms、finished_at。此时该 run_id 对应的完整执行记录已形成闭环，可通过统一查询接口获取全量信息。

### 技术效果

统一运行记录契约相比现有分散记录方式，具有以下技术效果：

- 可查询性：通过单一 run_id 即可一键获取一次 agent 执行的完整证据链，包括 spawn 基础信息、session 对话、token 成本、任务链、工具调用、质量审查和评估结论，无需跨多表进行字符串匹配 JOIN。
- 可追踪性：spawn → session → task → cost → tool call → review → eval 全链路无断点，每个环节的记录均可通过 run_id 追溯，支持运行审计和问题定位。
- 成本归属：将当前大量 task_id 为空的 token_usage 记录（无归属成本）收敛到 run 级别，并进一步区分 task 级归属和 run 级 unattributed，使成本核算精确到单次执行。
- 评估附着：四层评估结果从 agent_name 维度提升到 run 维度，可对单次运行进行精细化的质量归因，为排行榜、模型对比、agent 优化提供可比较的评估数据。
- 事件过滤：event-bus 的 19 种实时事件均携带 run_id，SSE 和 WebSocket 订阅方可按 run_id 订阅细粒度事件流，实现针对特定执行的实时监控。
- 外部工具集成：统一查询接口可作为 REST API 和 MCP Tool 暴露，质量评测系统、排行榜服务、运行审计工具和 CI/CD 流水线均可通过标准协议查询和复用运行记录。
- 向后兼容：方案在现有表上新增字段而非新增独立表，event-bus 事件 payload 中 run_id 为附加字段，不影响现有订阅方的解析逻辑。

### 风险与待确认问题

以下为当前方案中需要后续确认的技术风险点：

- Session 与 Run 的对应关系：当前设计中假设一次 session 归属于一次 run，但实际场景中可能存在 session 跨多次 spawn 复用的情况。若 session 可复用，run_id 在 sessions.json 中需改为数组类型，需进一步确认 Gateway 的 session 生命周期策略。
- 嵌套 Spawn 的处理：agent 在执行过程中可能通过 spawn 启动子 agent（嵌套 spawn），子 spawn 应分配独立的 run_id，并通过 parent_run_id 字段建立父子关系。需确认 spawn 调用链中是否已存在嵌套场景及频率。
- eval_traces 的 trace JSON 与 transcript 格式对齐：当前 transcript-parser 解析的 JSONL 消息格式与 eval_traces 表中 trace 字段的 JSON schema 不完全对应。需设计统一的中间表示，使评估系统可直接引用会话消息作为评估输入。
- 存量数据迁移：历史数据中缺少 run_id，需制定回填策略。可通过 spawn_history.created_at 与 token_usage.created_at 的时间窗口匹配 + agent_name 一致进行推断回填，无法推断的记录 run_id 标记为 NULL（遗留未归属）。
- event-bus 向后兼容验证：需验证现有 SSE 订阅方（如前端 Dashboard）在接收到携带新增 run_id 字段的事件时，不会因未知字段导致解析异常。应采用宽松解析策略。
- Run ID 生成位置的确定：若在 recordSpawnStart 内部生成，可保证一致性和原子性；若由调用方生成，则允许调用方在 spawn 之前即预知 run_id 用于预处理逻辑。需根据实际 spawn 调用链（POST /api/spawn、调度器、CLI 工具等入口）确认最优策略。
