## 技术方案

### 总体构思与系统边界

本方案在智能工作台的既有任务、agent、会话、事件、工具调用审计、成本统计和评估能力之上，设置“agent 执行运行记录”作为结构化沉淀对象。该运行记录不是由操作者事后填写的人工日志，而是在一次 agent 工作被触发、领取、派发、响应、状态更新、成本归集和质量评估的过程中自动生成并持续补全，用于回答一次执行从何处触发、由哪个 agent 执行、关联哪个任务或会话、产出什么结果、消耗多少资源、是否经过评估以及后续能否被查询复用。

运行记录以智能工作台中已经存在的执行实体为边界进行挂接：上游连接任务创建、队列领取、定时任务、外部 API、命令行或 MCP 工具调用等触发源；中间连接 agent 身份、工作区、项目票号、会话标识、模型和工具调用；下游连接任务结果、活动流、审计事件、token 与成本记录、质量评审和多层 agent 评估。由此，一次执行既能保留原有编排体系的实时性和兼容性，又能形成可追溯、可统计、可评价和可对外暴露的统一数据单元。

### 运行记录的数据模型

运行记录采用独立的 $run\_id$ 作为主键，外部引用键可由工作区、触发源、任务或会话标识以及递增序列或短哈希组合生成，用于 REST、CLI、MCP 和第三方系统稳定引用。为保证幂等，系统为每个触发事件计算 $idempotency\_key$，其输入至少包括工作区、触发类型、外部请求编号或任务标识、agent 标识和触发时间桶；重复到达的请求命中同一幂等键时只返回既有运行记录，不再新增记录。同一任务发生重试、继续执行或多 agent 协作时生成多条运行记录，并通过 $root\_run\_id$、$parent\_run\_id$ 和 $relation\_type$ 表达根执行、子执行、重试执行、继续会话执行或辅助 agent 执行的关系。

运行记录字段分为原始引用字段和物化摘要字段两层。原始引用字段保存 task、agent、session、activity、audit、token、MCP 调用和评估记录的标识或时间范围，用于回溯原始证据；物化摘要字段保存执行状态、最终 outcome、输出摘要、错误码、总耗时、总 token、总成本、工具调用次数、工具成功率、最新评估结论等高频查询结果。原始引用字段一经确认通常只追加不覆盖；物化摘要字段由对应来源事件增量更新，并记录 $summary\_version$、$last\_synced\_at$ 和 $source\_priority$，以便在迟到数据或人工修正出现时重新计算而不丢失原始链路。

| 字段组 | 关键字段 | 来源与更新规则 |
| --- | --- | --- |
| 必填标识 | $run\_id$、workspace、trigger_type、created_at、execution_status | 创建时写入；用于唯一定位和状态控制 |
| 可延后字段 | task_id、agent_id、sessionId、model、result_ref | 随队列领取、派发或响应事件补齐；未确认前为空 |
| 关系字段 | $root\_run\_id$、$parent\_run\_id$、relation_type | 用于重试、子任务、多 agent 协作和继续会话 |
| 摘要字段 | outcome、cost、token、tool_success_rate、latest_eval | 由原始记录增量计算；可按来源优先级重算 |
| 控制字段 | version、idempotency_key、locked_fields、last_synced_at | 用于幂等、并发控制、人工锁定和异步补偿 |

### 自动采集与关联流程

运行记录的采集由主执行链路和异步补全链路共同完成。主执行链路负责创建记录、绑定 agent、派发输入、回填响应和推进执行状态；异步补全链路负责归集成本、汇总工具调用、附着评估、修正迟到数据和隔离无法可靠归因的数据。两条链路均以 $run\_id$ 和幂等键作为写入入口，以执行状态和来源优先级控制字段覆盖，避免重复触发、并发领取或乱序事件导致同一执行被拆散或错误合并。

运行记录按状态机推进，执行状态可包括 created、queued、claimed、dispatching、running、waiting_tool、attribution_pending、attribution_done、reviewing、reviewed、completed、failed 和 cancelled。任务创建事件包含工作区和触发来源时，可立即创建 created 或 queued 状态记录，并写入触发者、任务、项目和票号；无任务上下文的 CLI 或 MCP 调用可先创建 created 状态记录，待后续生成任务或会话后补齐关联。队列领取必须满足任务处于可领取状态且 agent 未超过容量限制，系统在同一事务内执行任务状态条件更新和运行记录绑定，条件包括任务版本号、当前负责人为空或仍为原负责人、运行记录尚未绑定 agent；多个 agent 同时领取同一任务时，只有先完成条件更新的一方获得绑定，其余请求返回冲突或容量结果，并写入审计或重试队列，不覆盖既有绑定。

派发前置条件是运行记录已绑定 agent 且执行状态处于 claimed 或可恢复的 failed_retryable。派发时，系统将任务标题、描述、优先级、标签、项目票号、历史评审反馈和必要会话上下文映射为 agent 输入，将模型路由、网关通道和请求标识写入运行记录，并把状态推进为 dispatching 或 running。响应回填时，系统从 agent 响应解析文本结果、会话标识、模型信息、错误码、耗时和原始响应引用，按来源优先级更新输出摘要、sessionId、result_ref、error_code、completed_at 等字段；人工确认的 outcome 和反馈评分高于自动解析结果，终态 completed、failed、cancelled 不得被较早到达的 running 或 waiting_tool 事件回退。

#### 运行记录创建与绑定

运行记录存在两种创建模式。第一种为触发即创建：当任务创建事件已包含工作区、触发来源、任务标识和触发者时，系统以该事件的幂等键创建记录，初始状态为 created 或 queued，并写入 task_id、project_id、ticket_no、trigger_type、trigger_actor 和 created_at。第二种为领取时创建：当外部调用、会话续写或工具入口尚未形成任务标识时，系统先记录触发事件和外部引用，待 agent 轮询队列或会话被接管时再创建或补齐运行记录，初始状态为 claimed。两种模式均要求 workspace 和 trigger_type 为必填，task_id、agent_id、sessionId 和 model 可为空但必须带有补齐来源和更新时间。

领取与绑定采用任务条件更新和运行记录版本号共同控制。系统在事务中先判断任务状态属于 inbox、assigned 或可继续的 in_progress，且 agent 未超过容量限制；随后以 task_id、当前任务版本和运行记录未绑定状态作为条件更新任务负责人和执行状态，并将 agent_id、agent_name、claim_time、claim_source 写入运行记录。若事务提交失败或版本号已变化，说明其他 agent 已完成领取，当前请求不得覆盖原绑定，只记录领取冲突事件；若任务已被该 agent 领取，则返回既有运行记录并更新心跳时间，以支持幂等重试。

#### 执行响应回填与状态控制

| 执行状态 | 触发事件 | 迁移边界 |
| --- | --- | --- |
| created/queued | 任务创建或外部触发 | 可迁移到 claimed 或 cancelled |
| claimed | agent 成功领取 | 可迁移到 dispatching；不得改绑其他 agent |
| dispatching/running | 模型或网关调用开始 | 可迁移到 waiting_tool、completed、failed |
| waiting_tool | 工具调用或外部依赖等待 | 可恢复到 running 或失败 |
| attribution_pending | 结果已产生但成本或工具数据未齐 | 不阻塞 completed，可补偿到 attribution_done |
| reviewing/reviewed | 质量门禁或评估任务触发/完成 | 保留评估版本，不回退执行终态 |
| completed/failed/cancelled | 完成、失败或取消 | 为执行终态，除补归因和补评估外不得回退 |

派发输入由任务字段和运行上下文字段共同构造，至少包括任务标题、描述、优先级、标签、项目票号、历史反馈、agent 配置和可继续会话标识。派发请求写入 request_id、model、gateway_agent_id、prompt_ref 和 dispatch_started_at；agent 响应回填 output_text_ref、output_summary、sessionId、raw_response_ref、duration_ms、error_code、error_message 和 dispatch_finished_at。若同一运行记录收到多次响应，系统按响应序号和来源优先级处理：人工确认结果高于自动解析结果，重试后的成功结果高于早期失败结果，但被人工锁定的 outcome、feedback_rating 和 resolution 字段不得由自动评估覆盖。

#### 资源与工具调用归因

资源归因采用分级匹配算法。第一级使用 task_id 或 run_id 精确匹配 token 与成本记录，置信度为确定；第二级使用 sessionId 与 agent_name 匹配，要求会话时间与运行记录的 dispatch_started_at 至 completed_at 时间段相交，置信度为高；第三级使用 agent_name、workspace 和时间窗口匹配，窗口从领取时间向前保留少量缓冲并延伸至完成或失败后的补偿窗口，置信度为中；第四级使用活动事件、审计事件或外部引用链上下文匹配，置信度为候选。多个运行记录同时命中同一消耗记录时，优先选择更高等级匹配；等级相同则选择时间重叠比例更高且状态更接近 running 的记录；仍无法唯一确定时保留为未归因成本，不写入任何运行记录的成本摘要。

工具调用归因以 MCP 调用日志的 agent、server、tool、success、duration、error 和 created_at 为输入，先按 run_id 或任务上下文精确归并，再按 sessionId，最后按 agent、workspace 和运行时间段归并。每条工具调用记录通过调用日志标识或哈希键去重，重复事件只更新最近到达时间，不重复计数。归并后运行记录物化保存 tool_call_count、tool_success_count、tool_failure_count、tool_success_rate、avg_tool_duration_ms 和 top_error_codes；迟到工具事件只允许更新工具摘要和补偿版本，不得把已完成记录的执行状态回退到 waiting_tool。

#### 异步补偿、异常恢复与边界处理

异步补偿任务按运行记录的 last_synced_at、执行状态和待补字段扫描记录，处理事件乱序、重复、迟到和部分服务失败。每个外部事件携带 event_id、event_type、source_time 和到达时间；系统先按 event_id 或事件哈希去重，再比较 source_time 与运行记录状态版本。迟到事件只允许补充原始引用、成本摘要、工具摘要和评估记录，不允许用较早的中间状态覆盖终态。事件发布失败时，运行记录仍以数据库提交结果为准，并将失败事件写入待重放队列，重放成功后更新 activity_ref 或 audit_ref。

异常恢复规则按错误类型控制状态和重试。模型调用超时或网关错误使运行记录进入 failed_retryable，并记录 error_code、retry_count 和 next_retry_at；超过重试上限后转为 failed。agent 崩溃或心跳丢失时，若任务未完成则保持 claimed 或 running 并标记 stale，允许原 agent 恢复或由调度策略生成新的 retry 子运行记录。工具调用失败时，单次工具错误进入 waiting_tool 或记录 component_error，不必立即终止整个运行；任务取消将状态置为 cancelled 并锁定输出和成本归因边界。会话标识缺失时先以 task_id 和 agent 时间窗口归因，后续取得 sessionId 后再补齐；成本统计延迟时进入 attribution_pending；评估服务失败时记录 evaluation_error 并保留补评入口，不影响执行终态。

边界场景通过父子关系和归因边界处理。跨会话执行在同一 $run\_id$ 下追加多个 session 引用，并以时间段区分各会话消耗；同一会话并行多个任务时，必须优先使用 task_id 或外部请求编号，不能仅按 sessionId 分摊成本；父子 agent 调用和任务拆分分别生成子运行记录，父记录只保存聚合摘要，子记录保存实际执行和成本，避免重复计费；一个外部触发产生多个任务时，以同一 external_trigger_id 关联多个根记录；无任务上下文的 CLI/MCP 调用先形成独立运行记录，若后续转化为任务则追加 task_id，否则作为工具化执行记录保留。

### 评估附着与质量归因

评估记录作为运行记录的附着对象独立保存，至少包括 eval_id、run_id 或 run_group_key、eval_layer、eval_version、rule_version、evaluated_at、score、passed、detail_summary、evidence_refs 和 evaluator_type。eval_layer 可取 output、trace、component、drift：output 层使用任务完成、outcome、反馈评分和质量门禁结论评价结果质量；trace 层使用会话轨迹、工具调用数量、唯一工具数和循环倾向评价收敛过程；component 层使用 MCP 工具成功率、失败原因和耗时评价工具可靠性；drift 层使用 token 消耗、工具成功率等指标相对滚动基线的偏移评价稳定性。

评估版本采用“评估层级 + 规则版本 + 评估时间”的方式生成，同一运行记录可并存多条评估记录。查询展示时默认返回每个层级最新通过规则版本计算得到的 active_eval，同时保留 historical_evals 供回溯比较；当评估规则升级或基线变化时，系统新增评估版本而不覆盖历史版本。人工反馈、任务终态 outcome 和质量门禁结论属于高优先级质量来源，自动评估不得覆盖被人工锁定的字段；自动评估只能更新 latest_eval_score、risk_flags 和 evaluator_detail 等派生字段。

评估触发以执行结果可判定为前置条件：completed、failed、reviewing 或 reviewed 状态可以触发 output 层评估；存在会话或工具调用引用时可以触发 trace 与 component 层评估；达到时间窗口或基线更新条件时可以触发 drift 层评估。评估服务失败时写入 evaluation_error、retry_count 和 next_eval_at，不改变执行状态；补评成功后只追加新的评估记录并刷新运行记录的最新评估摘要。通过版本化附着，单次执行在完成当时的质量结论、人工反馈后的质量结论以及规则升级后的质量结论可以并存，从而保持质量回溯的可比性。

### 查询复用与扩展接口

运行记录向工作台界面、REST API、命令行工具、MCP 工具和外部系统提供统一查询入口。单条详情返回基础信息、触发来源、执行状态、任务和会话引用、输入输出摘要、成本摘要、工具统计、评估版本、错误字段和原始对象引用列表；聚合查询返回分组键、运行次数、完成率、失败率、平均耗时、总成本、平均成本、工具成功率、最新评估分数和时间窗口。查询条件包括工作区、项目、任务、agent、会话、触发来源、执行状态、outcome、时间范围、成本范围、工具成功率、评估层级和通过状态。

为降低跨源查询成本，运行记录索引层物化保存 status、outcome、agent_name、task_id、sessionId、project_id、created_at、completed_at、total_tokens、total_cost、tool_success_rate、latest_eval_score 和 risk_flags 等摘要字段，并对 workspace + created_at、workspace + agent + created_at、workspace + task、workspace + session、workspace + status + outcome、workspace + project + completed_at 建立组合索引。聚合统计以增量维护为主：运行记录状态变化、成本补归因、工具摘要更新或评估附着时，系统按受影响的 agent、项目、任务类型和时间窗口更新聚合快照；迟到成本或迟到评估到达时，根据补偿版本回滚旧摘要贡献并写入新贡献，避免排行榜和审计统计长期偏离。

上述查询复用机制的技术效果由具体数据结构和更新规则产生：通过保存任务、agent、会话、活动、审计、token、工具调用和评估的原始引用，实现从外部触发到执行结果的链路追溯；通过保存物化摘要字段和组合索引，减少排行榜、运行审计和外部工具查询时的跨表拼接；通过评估记录的版本化附着，保留不同评估规则和不同时间点下的质量结论；通过分级归因和未归因成本隔离，避免将会话派生消耗或迟到消耗错误附着到不相关的运行记录。
