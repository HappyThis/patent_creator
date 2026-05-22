## 技术方案

本方案提出一种基于事件溯源的 Agent 执行过程统一运行记录（Run Record）系统。该系统在现有事件流（Event Stream）基础设施之上叠加 Run Record 抽象层，以 run_id 为统一查询键，将每一次 agent 完整执行封装为可追踪、可汇总、可附着评估结果、可被外部工具访问的结构化记录。

核心技术手段包括：（1）定义 Run 边界——以 agent 开始处理到产生 agent_output 作为一次完整执行的封闭窗口；（2）自动提取 Run 元数据——从事件流中聚合执行耗时、工具调用次数、子 agent 派生列表、模型标识与消耗信息；（3）建立 spawn history 反向索引——通过 parent_run_id 字段将子 agent 执行链接到父 agent，形成可沿树结构追溯的 agent 派生关系；（4）设计可扩展评估附着机制——支持自动评估器、外部系统和人工评审三种路径按 run_id 附加评分、等级和标签；（5）提供多维查询契约——以 RESTful 接口对外暴露按 run_id、session_id、时间范围、评估结果等维度的查询能力。

Run Record 层正交于现有事件流：不改变事件写入路径，仅在 agent_output 事件写入后异步提取生成；每条事件可通过 events_range 反向定位所属 Run，每个 Run 可展开为完整事件子序列。该方案复用已有的 agent 编排、子 agent 派生、事件序列化和会话管理基础设施，同时为质量评测、排行榜、运行审计和外部工具集成提供标准化数据入口。

### Run Record 数据契约

Run Record 是 agent 执行过程的统一结构化记录，每条记录以全局唯一的 run_id 标识。其数据契约分为标识锚定、执行成本、评估附着和事件追溯四个维度。

标识与锚定字段包括：run_id（全局唯一主键，外部查询入口）；session_id、round_id、message_id（三级锚定，将 Run Record 挂载到现有会话体系）；parent_run_id（指向父 Run，主 agent 为 null，子 agent 指向触发派生的父执行）；agent_scope（对应事件流中的 scope 字段，如 main 或 subagent:material_analyst）；root_run_id（执行树的根 Run，加速全树查询）。

执行与成本字段包括：model 与 provider（从 agent_message 事件提取的模型标识）；status（completed、failed、timeout、cancelled 四种终态）；start_ts、end_ts、duration_ms（时间边界与执行耗时）；events_range（[start_seq, end_seq] 区间，指向事件流中本次执行的完整事件子序列，支持按序回放）；tool_call_count（工具调用次数）；subagent_count（派生的子 agent 数量）。

评估附着字段 evaluation 采用可扩展结构，包含 score（数值评分）、grade（等级，如 A/B/C）、tags（标签数组）、evaluator（评估来源标识）、evaluated_at（评估时间戳）和 custom（扩展字段，承载评估器自定义数据）。该字段支持多次附着，保留评估历史。

### Run Record 生成与维护流程

Run Record 的生成采用异步提取模式，不阻塞 agent 执行主路径。当 agent_output 事件写入事件流后，Run Extractor 模块被触发执行以下步骤：

1. 边界识别：以 agent_output 事件为终点，沿 message_id 向前回溯到执行起点。主 agent 的起点为该 message_id 下的首条 agent_message 事件；子 agent 的起点为同 parent_call_id 下的首条 agent_message 事件。
2. 元数据聚合：扫描 events_range 区间内的事件序列，自动统计 tool_call 事件的次数（含嵌套子 agent 的工具调用）、识别 agent_message 事件中的 model 和 provider 字段、计算首末事件的 ts 差值得出 duration_ms。
3. 状态判定：正常产生 agent_output 且无工具错误标记则为 completed；出现 tool_result 错误且无后续 agent_output 则为 failed；超过预设时间阈值未产生 agent_output 则为 timeout；被外部信号中断则为 cancelled。
4. 存储与索引：将生成的 Run Record 写入 Run Store，同时建立 run_id、session_id、parent_run_id 和 root_run_id 的复合索引，支持多维度快速查询。

为保证一致性，系统在启动时执行幂等扫描：遍历事件流中所有 agent_output 事件，检查对应 run_id 是否已存在于 Run Store，对缺失的 Run Record 执行补建操作。该机制确保崩溃恢复场景下事件流与 Run Store 最终一致。

### Spawn History 与执行树追踪

系统通过 parent_run_id 字段建立子 agent 派生关系的有向无环图（DAG），即 spawn tree。当主 agent 通过 execute_subagent 调用派生子 agent 时，子 agent 的所有事件携带 parent_call_id 指向触发调用的工具调用。子 agent 的 Run Record 生成时，将 parent_run_id 设为父 Run 的 run_id，同时 root_run_id 统一设为执行树的根 Run。

该机制在以下场景提供关键追踪能力：（1）单个 Run 的完整上下文查询：通过 GET /runs/{run_id}/tree 返回以该 Run 为根的完整执行树，包含所有后代子 agent 的 Run Record 摘要；（2）自底向上的血缘追溯：外部系统沿 parent_run_id 链向上递归，定位到最终触发本次执行的用户输入消息；（3）成本归集：将执行树中所有 Run 的耗时和工具调用次数汇总，得出一次用户请求的总资源消耗。spawn tree 的构建依赖事件流中的 parent_call_id 字段，不引入额外的追踪标记开销。

### 评估附着机制

Run Record 的 evaluation 字段支持三种评估路径，均以 run_id 为附着键，互不冲突且可叠加：

- 自动评估（同步）：Run Record 生成后，内置评估器按 events_range 回放事件子序列，检查工具调用错误率、输出格式合规性、执行耗时超标等指标，自动写入 evaluation 字段。该路径适合作为质量关卡，拦截明显的执行异常。
- 外部系统评估（异步）：排行榜、审计面板或质量评测平台通过查询接口获取待评估 Run 列表，按自身评分逻辑计算后通过写入接口回写 evaluation 字段。系统校验 evaluator 身份后允许追加评估记录，已有评估不被覆盖。
- 人工评审：审核人员通过 run_id 回放完整执行过程（含思维链内容），手动标注评分、等级和标签后提交。人工评审记录与自动评估记录共存，通过 evaluator 字段区分来源。

评估记录支持版本化追加：同一条 Run Record 可累积多条 evaluation 记录（通过 evaluated_at 时间戳排序），外部系统可据此追踪评估历史演变，支持排行榜的时效性排名和审计合规要求。

### 外部查询接口契约

Run Record 通过 RESTful 查询接口向外部系统暴露，以 run_id 为统一访问入口。接口设计遵循以下契约：

- GET /runs/{run_id}：返回完整 Run Record，可选参数 include_events=true 时同时展开 events_range 内的完整事件子序列，支持执行回放。
- GET /sessions/{session_id}/runs：返回指定会话下所有 Run 的摘要列表，按时间倒序排列。
- GET /runs?from={ts}&to={ts}：按时间范围过滤 Run 列表，支持按 status、agent_scope、model 等字段组合过滤。
- GET /runs?evaluated=true&grade={grade}：按评估状态和等级过滤，为排行榜和质量评测系统提供数据源。
- GET /runs/{run_id}/tree：返回以指定 Run 为根的完整 spawn tree，包含所有后代子 agent 的 Run Record 摘要，支持嵌套展开。
- GET /runs/{run_id}/events：按 events_range 的 [start_seq, end_seq] 区间回放完整事件子序列，包含思维链内容，用于审计和问题排查。

接口返回的 Run Record 中，events_range 字段提供与事件流的双向索引——外部工具通过该区间直接定位到 JSONL 文件中的对应行范围，无需全量扫描。同时，每条事件的元数据中可附加 run_id 引用，使事件流本身也支持从事件反向查询所属 Run。

### 与现有系统的关联映射

Run Record 层与现有系统概念的关联关系如下：

与 Session 的关联：Run Record 通过 session_id 锚定到会话。一个 Session 包含多个 Round，每个 Round 内的 agent 执行产生一个或多个 Run Record（主 agent 一个，每个子 agent 各一个）。Session 维度的 Run 列表支持会话回放和成本归集。

与 Round 和 Message 的关联：Run Record 通过 round_id 和 message_id 定位到具体的交互轮次和用户输入消息。一次用户输入可能触发多层 agent 派生，但所有派生 Run 共享同一 message_id，通过 parent_run_id 区分层次。

与 Spawn History 的关联：Run Record 的 parent_run_id 字段直接对应 execute_subagent 调用产生的 parent_call_id 所关联的父 agent 执行。该关联无需额外存储，完全由事件流中的血缘信息推导。

与成本统计的关联：Run Record 的 duration_ms、tool_call_count 和 subagent_count 字段构成基础成本度量。通过在查询接口中按 session_id 或 message_id 聚合这些字段，可得出会话级和消息级的资源消耗汇总。若模型 API 提供 token 消耗数据，可扩展 cost 子字段记录输入/输出 token 数。

与事件流的关联：events_range 字段以 [start_seq, end_seq] 形式建立 Run Record 到事件流的双向索引。外部工具通过该区间直接定位到 JSONL 事件文件中的对应行范围，无需全量扫描；事件流中也可通过扩展 run_id 字段实现事件到 Run 的反向查询。

与工具入口的关联：Run Record 中的 tool_call_count 汇总了本次执行中所有工具调用次数。events_range 区间内的事件子序列完整保留了每次 tool_call 和对应的 tool_result，外部系统可通过事件回放还原工具调用的参数和返回值，用于审计和回归测试。

### 技术效果

本方案相比现有方式产生以下技术效果：

- 可查询：以 run_id 为统一键，通过多维查询接口实现按会话、时间、状态、评估等级等条件的精确检索，替代人工翻阅日志文件的方式。
- 可追踪：通过 events_range 双向索引和 spawn tree 父子关联，完整还原单次 agent 执行的事件序列和派生关系，支持端到端审计。
- 可评估：通过可扩展 evaluation 字段和三种评估路径，将质量评测结果结构化附着到 Run Record，为排行榜和持续改进提供数据基础。
- 可复用：外部工具通过标准化查询接口消费 Run Record，无需直接解析 JSONL 事件流，降低集成成本。
- 非侵入：Run Record 层正交于事件流写入路径，不改变 agent 执行逻辑，仅异步提取生成，对执行性能无显著影响。
- 可扩展：evaluation 字段的 custom 扩展和接口的过滤参数均为后续排行榜、成本看板、合规审计等场景预留了扩展空间。

### 风险与待确认问题

以下为当前方案中需要后续确认的技术风险点：

- Task 概念的独立化：当前系统中 session 可视为 task 的实例，但需求中提及的任务（task）概念是否需要作为独立实体引入（例如在多 session 协作场景中作为聚合层），需进一步明确。若引入，需扩展 Run Record 的 task_id 字段。
- Token 消耗数据的获取：Run Record 的 cost 维度目前仅包含耗时和调用次数。若需记录 token 消耗，依赖模型 API 返回的 usage 信息能否稳定获取并写入事件流。需确认所用模型提供商的 API 响应格式。
- Run Store 存储方案：Run Store 可采用独立轻量数据库（如 SQLite）或 JSONL 附加索引文件。不同方案在查询性能、部署复杂度和与现有事件流文件的耦合程度上有权衡，需根据实际部署场景确定。
- 多层嵌套的深度边界：子 agent 可多层嵌套派生，极端情况下 spawn tree 深度可能较大。当前方案通过 root_run_id 优化全树查询，但超深嵌套场景下的递归查询性能需实际测试验证。
- 评估字段的并发写入：evaluation 字段支持多次追加，在外部系统并发写入同一 run_id 的场景下需保证写入顺序和一致性。建议通过乐观锁或 evaluator 维度去重机制处理。
