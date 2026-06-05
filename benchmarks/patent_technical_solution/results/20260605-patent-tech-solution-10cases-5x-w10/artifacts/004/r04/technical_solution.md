## 技术方案

本技术方案围绕 agent 执行过程的结构化沉淀、溯源追踪、评估附着和工具化查询，在现有的任务管理、agent 注册、session 跟踪、事件总线和 token 用量记录等基础设施之上，构建一套统一的执行记录（Run Record）体系。该体系以每一次 agent 执行为最小记录单元，在不改变现有执行流程的前提下，将分散在各子系统中的执行信息自动聚合为结构化的可查询记录，同时为质量评测、排行榜、运行审计和外部工具集成预留扩展空间。

### 执行记录的数据模型

执行记录的核心是一条 Run Record，它以一次 agent 发起到一次结果产出的完整执行为粒度，将触发来源、执行主体、关联任务与 session、资源消耗、产出结果和评估结论聚合为一条结构化记录。该记录在现有数据库基础上新增 run_records 表存储，并通过外键或逻辑关联与已有的 tasks、agents、token_usage、quality_reviews、audit_log 等表建立可追溯的引用链。

一条 Run Record 至少包含以下字段：记录唯一标识；触发来源类型（任务派发、定时任务、pipeline 步骤、webhook 回调、手动触发、agent 自主轮询）；触发来源标识（对应的 task_id、cron_job_id、pipeline_run_id 或 webhook_id）；执行 agent 标识；绑定的 session 标识；执行时间窗（启动时间戳、结束时间戳、持续时长）；资源消耗摘要（输入 token 数、输出 token 数、token 总消耗、估算成本）；执行结果（产出摘要或结果引用、执行状态：成功/失败/部分完成/超时/被终止）；关联的质量审查结果引用；以及可选的扩展元数据（JSON 格式，供不同触发场景附加领域特定信息）。

### 执行记录的自动捕获机制

执行记录不是独立采集的日志系统，而是在现有执行流程的关键节点上，通过事件总线驱动自动捕获和聚合。具体机制如下：

- 触发捕获：当调度器将任务从 assigned 翻转为 in_progress 时、当 cron 触发模板实例化一个子任务时、当 pipeline 执行引擎启动一个步骤时、当 webhook 回调触发一次 agent 执行时，均生成一条初始 Run Record，填充触发来源类型和标识、发起 agent 和启动时间戳。
- session 绑定与 token 归集：现有的 token_usage 表已按 session_id 记录每次模型调用的 token 消耗。Run Record 通过 session_id 与 token_usage 建立关联。在执行结束时，系统从 token_usage 表中汇总该 session 在本次执行时间窗内的所有记录，得到输入/输出 token 总量和成本合计，回填至 Run Record 的消耗摘要字段。
- 结果捕获与状态判定：当任务状态变更为 review/done/failed 时，事件总线触发任务状态变更事件；Run Record 监听器捕获该事件，将任务的 resolution 字段作为产出摘要写入 Run Record，并根据任务 outcome 字段（success/failed/partial/abandoned）判定执行状态。
- 审查附着：当 quality_reviews 表新增一条审查记录时，通过 task_id 关联找到对应的 Run Record，将审查结论（审批通过/驳回/驳回原因）附加到记录中，使得每次执行的质量判定可追溯。

### 多维溯源链

执行记录体系通过多维关联实现端到端的溯源能力，使系统能够回答"一次 agent 工作从哪里触发、由谁执行、关联了什么、产出了什么、消耗了多少、是否经过评估"。

溯源链由以下关联维度构成：（1）前向溯源——从 Run Record 出发，通过触发来源类型和标识定位本次执行的发起者：若为任务派发，关联到具体 task 及其项目、创建者；若为定时任务，关联到 cron 配置及其调度历史；若为 pipeline 步骤，关联到 pipeline_run 及其所属 pipeline 定义；若为 webhook，关联到 webhook 配置及其投递记录。（2）执行主体溯源——通过 agent 标识关联到 agent 的注册信息、SOUL 配置和运行状态历史。（3）执行过程溯源——通过 session_id 关联到 session 的 transcript（对话记录、工具调用序列），可进一步通过 token_usage 表按时间区间展开每一次模型调用的详情。（4）结果溯源——通过 task_id 和 quality_review 引用，关联到任务的具体产出（resolution）和审查结论。（5）审计附着——Run Record 的创建、更新和状态变更操作同步写入 audit_log，使得记录的生成过程本身可审计。

### 评估附着与排行榜支撑

执行记录为质量评测提供结构化的数据基座。系统基于 Run Record 的量化和定性数据构建多层评估附着机制。

第一层为指标计算附着：系统在 Run Record 落盘后自动触发指标计算，包括执行成功率、平均 token 消耗、平均执行时长、成本效率比等，结果以扩展元数据形式回写至 Run Record。第二层为审查附着（已在前述捕获机制中说明）：quality_reviews 的审批结论通过 task_id 关联附着到 Run Record。第三层为排行榜支撑：系统以 agent 为维度，对窗口期内的 Run Record 进行聚合统计，输出各 agent 的任务完成数、成功率、平均评分、总 token 消耗、总成本等排行指标，支撑 agent 排行榜的生成。第四层为信任评分：基于 Run Record 的历史成功率、审查通过率和执行稳定性，更新 agent_trust_scores 表，为后续任务派发时的 agent 选择提供决策依据。

### 工具化查询与外部集成

执行记录通过多通道向内部工具和外部系统暴露查询能力，使其成为可编程、可集成的数据资产。

MCP 工具扩展：在现有 MCP Server 的 35 个工具基础上，新增 mc_list_runs、mc_get_run、mc_run_stats 三个工具。mc_list_runs 支持按 agent、task、时间范围、执行状态、成本区间等条件组合筛选，返回符合条件 Run Record 列表。mc_get_run 按记录 ID 返回单条记录的完整详情，包括溯源链展开后的关联实体信息。mc_run_stats 返回按 agent、按触发来源、按时间粒度的聚合统计。以上工具可被任何接入 MCP 协议的 agent 或 IDE 直接调用，无需人工登录面板。

REST API 端点：在 /api/runs 路径下提供 GET（查询列表）、GET /:id（获取详情）、GET /stats（聚合统计）。API 支持分页、排序、字段筛选和结构化导出（JSON/CSV），供外部系统通过标准 HTTP 调用集成。

CLI 扩展：在现有 mc-cli.cjs 的 runs 命令组下提供 list、get、stats、export 子命令，支持 --json 模式输出 NDJSON 流。确保 headless/自动化脚本可直接消费。

Webhook 集成：执行记录生成和状态变更时，通过现有的 webhook 投递机制将 Run Record 摘要推送到已配置的外部端点。webhook 事件类型包括 run.created、run.completed、run.failed、run.reviewed，payload 包含 Run Record 的核心字段和关联实体标识，外部系统可按需回查完整记录。

审计导出：在 /api/export 现有导出能力基础上，增加导出 run_records 的能力，支持按时间范围和 agent 筛选，满足合规审计需求。

### 存储策略与扩展机制

执行记录的存储策略与现有数据保留机制对齐。run_records 表通过可配置的保留天数（默认与 token_usage 的 MC_RETAIN_TOKEN_USAGE_DAYS 保持一致）自动清理过期记录。清理策略支持按 agent 或按状态设置差异化的保留周期，例如保留失败记录更长时间以便回溯分析。

在扩展性方面，Run Record 的 metadata 字段采用 JSON 格式，允许不同触发场景附加定制信息而不影响核心结构。trigger_source_type 字段采用可注册的枚举扩展机制，当系统新增触发方式（如新增消息平台接入、新增外部编排框架适配器）时，只需注册新的触发类型标识，无需改动核心记录逻辑。
