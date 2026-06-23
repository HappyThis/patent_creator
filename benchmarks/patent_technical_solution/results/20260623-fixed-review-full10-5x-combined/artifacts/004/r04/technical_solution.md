## 技术方案

### 总体构思与统一运行记录锚点

本方案在智能工作台已有的任务、agent、会话、活动流、质量评审、token 使用和工具调用记录基础上，设置统一的 agent 运行记录作为执行过程的权威索引。每一次由任务派发、会话续写、定时调度、外部 API、MCP 工具或人工操作触发的 agent 工作，均以 $run_id$ 作为主键进行沉淀；任务 metadata、activity.data、审计日志和查询视图只保存该 $run_id$ 或其派生摘要，不作为判断一次运行状态的最终依据。由此，系统通过 $run_id$、状态机、事件序号和跨对象引用字段共同还原触发、派发、会话、结果、成本和评估链路，而不是在事后从分散日志中模糊拼接。

运行记录的数据结构至少包括：$run_id$、workspace_id、trigger_type、trigger_id、task_id、project_id、agent_id、agent_name、idempotency_key、attempt_no、previous_run_id、parent_run_id、session_key、session_id、dispatch_session_id、status、version、sequence_no、started_at、ended_at、result_ref、result_summary、error_message、input_tokens、output_tokens、cost_usd、cost_status、review_status、review_id、eval_refs、trace_refs 和 archived_ref。workspace_id、trigger_type、agent_name、status、created_at 为必填字段；task_id、project_id、session_id、result_ref、成本和评估字段允许在创建时为空，并随着派发、返回、评审或成本回填逐步补齐。系统在同一 workspace_id、trigger_type、trigger_id、idempotency_key 作用域内建立唯一约束，用于识别重复触发；同时保留 version 或 sequence_no，用于拒绝迟到事件覆盖较新的运行状态。

### 核心数据模型与状态机

本方案将运行状态、任务状态、评审状态和结果字段分离定义。status 表示单次运行状态；task.status 表示任务在工作台中的流程位置；review_status 表示质量门禁或评估结论；outcome 和 resolution 表示任务或运行的业务产出与摘要。运行状态至少包括 created、dispatching、running、waiting_review、pending_cost、succeeded、review_rejected、failed、timeout 和 cancelled。created 表示记录已创建但尚未完成派发；dispatching 表示正在选择模型、agent 和会话通道；running 表示请求已发送并等待 agent 结果；waiting_review 表示结果已入库但仍待质量检查；pending_cost 表示结果和状态已确认但成本或 transcript 尚未唯一归因；succeeded、review_rejected、failed、timeout、cancelled 为终态或准终态，其中终态不得被 started、running 等较早阶段事件覆盖。

合法状态迁移按执行时序限定：created 只能进入 dispatching 或 cancelled；dispatching 只能进入 running、failed 或 timeout；running 可进入 waiting_review、pending_cost、succeeded、failed、timeout 或 cancelled；waiting_review 可进入 succeeded、review_rejected 或 failed；pending_cost 在成本补齐后回到 succeeded 或保持带有 cost_status 的 succeeded 视图。若更新请求试图从 succeeded、failed、cancelled 等终态回退到 running，或跨 workspace 修改记录，系统拒绝状态覆盖并写入不可变审计事件。状态判断同时比较 sequence_no 和 version，较小序号的重复事件只用于补齐缺失的非冲突字段，不改变已确定的终态和结果引用。

一个 task_id 下允许存在多个运行记录，形成执行序列。系统按同一 task_id 内已有 run 数量生成 attempt_no，previous_run_id 指向上一尝试，parent_run_id 指向因评审拒绝、超时重试或人工重新派发而派生的源运行，retry_reason 记录触发原因。任务处于 in_progress 时至多选择一个 active_run_id；若业务允许并行探索，则并行 run 必须显式标记 parallel_group，且不得自动覆盖 active_run_id。任务最终结果 run 优先选择最新的 succeeded 且 review_status 不为 rejected 的记录；若不存在成功 run，则聚合最近一个 failed、timeout 或 cancelled 的错误作为任务错误摘要，并保留其他失败 run 的独立成本和责任边界。

### 运行记录的采集与状态同步流程

创建运行记录的前置条件为：调用方已通过 workspace_id 对应的鉴权，trigger_type 属于预设枚举，trigger_id 指向的任务、会话或外部请求存在，或被标记为允许弱引用；idempotency_key 在 workspace_id、trigger_type 和 trigger_id 作用域内可校验。满足条件时，系统先在事务内插入 agent_runs 并得到 $run_id$，状态为 created；随后计算模型选择结果，模型选择结果以 model、reason、confidence、fallback_model 和 policy_version 表示；再根据任务 metadata 中的目标 session、agent 配置中的 gateway 标识、可用直接模型密钥等条件确定 session_strategy，取值可以为 existing_session、gateway_new_session、gateway_existing_session 或 direct_model。完成上述字段写入后，运行状态进入 dispatching。

派发写入采用固定顺序：先创建或复用 agent_runs，再把 active_run_id、attempt_no 和 dispatch_session_id 的占位值写入任务 metadata，然后记录 task_dispatched 或 agent_run.started 活动，最后通过 outbox 或事件总线发布 agent_run.started 与 task.status_changed 事件。agent 返回后，系统先写 result_ref、result_summary、error_message、session_id 或 dispatch_session_id，再按状态机更新 status、ended_at 和 version，随后写入评论、token_usage 或 quality_reviews 的关联引用，最后发布 agent_run.completed、agent_run.failed 或 agent_run.timeout 事件。若事件发送失败，数据库内的 outbox 记录保留待发布事件；若 task metadata 写入失败但 agent_runs 已创建，后台补偿任务按 $run_id$ 重放缺失写入并重建查询视图。

### 幂等并发与异常恢复规则

同一 workspace_id、trigger_type、trigger_id 和 idempotency_key 的请求重复到达时，系统先读取既有运行记录：若请求中的 agent、task、session_strategy 和输入摘要与既有记录一致，则返回既有 $run_id$ 并追加一次 idempotent_hit 活动，不再新建 run；若关键参数不一致，则拒绝新建并写入 conflict 审计事件，避免同一触发被解释为两次不同执行。更新运行记录时，调用方必须携带 $run_id$ 或可唯一定位的 idempotency_key，且当前 status 必须允许迁移；否则更新被视为无效事件，仅进入审计或死信队列。

同一 task_id 同时出现人工派发和自动派发时，系统以任务行锁或条件更新保证只有一个请求能把任务从 assigned 改为 in_progress 并写入 active_run_id；人工派发可以配置为更高优先级，但必须在自动派发尚未进入 running 前完成抢占。若自动派发已经进入 running，人工派发只能创建新的 attempt_no 或取消旧 run 后再派发，不得静默替换 active_run_id。agent 超时后运行状态进入 timeout；若外部会话随后返回结果，系统不回退为 running，而是把结果写入 late_result_ref，并按任务是否仍处于可接收状态决定是否发起新的评审。人工取消后收到的结果同样只作为迟到结果和审计材料保存，不覆盖 cancelled 状态。

事件采用 event_id、$run_id$、sequence_no 和 workspace_id 共同去重。若 completed 事件先于 started 事件到达，系统根据 sequence_no 和状态优先级先确认结果状态，再把迟到的 started 事件标记为已重放，不降低状态；若事件总线重复投递，event_id 命中既有 outbox 记录时仅更新投递次数。活动流写入失败、事件发送失败或查询视图丢失时，后台恢复任务从 agent_runs、outbox 和不可变审计日志重放事件，重建以 task_id、agent_name、session_id 和时间窗口为索引的查询视图，使实时看板与审计查询最终收敛到同一运行链路。

### 溯源关联与资源消耗沉淀

溯源引用采用分层字段定义：session_key 表示工作台或 gateway 中用于定位会话的逻辑键，session_id 表示底层模型或 gateway 返回的实际会话标识，dispatch_session_id 表示一次任务派发阶段捕获并回填到任务 metadata 的会话标识，transcript_ref 表示可读取 transcript 的路径或查询令牌。result_ref 可以指向任务评论、产物记录、对象存储条目或 transcript 片段；trace_refs 指向工具调用序列、会话 transcript 或 eval_traces；review_notes_ref 指向 quality_reviews 记录或评审评论。被引用对象删除或归档时，运行记录不删除引用字段，而是把引用状态改为 archived 或 unavailable，并保存 archived_ref、摘要和校验值，以维持可追溯性。

资源消耗采用“会话级采集、运行级归因”的算法。直接模型调用路径中，每次请求生成 usage_id 或 call_id，usage 写入 token_usage 时同时携带 $run_id$、task_id、agent_name、model、input_tokens、output_tokens 和 cost_usd；同一 run 内多次模型调用按 usage_id 去重后累加，成本估计与后续最终成本不一致时以修正记录覆盖汇总值并保留原始估计。gateway 调用路径中，优先使用返回的 dispatch_session_id 或 session_id 归因；外部会话路径中，按显式 session_id、idempotency_key、session_key、agent_name 加 started_at 至 ended_at 扩展窗口的顺序匹配 transcript 和成本记录。多个 run 争用同一 usage 时，已确认归因的 usage 不再分配；一个 run 命中多个 usage 时按时间重叠、agent_name 一致和会话标识一致性累加；无法唯一确认时将 cost_status 置为 pending_cost，而不是强制归因。

对于一次任务包含多次派发或多次重试的情况，每次尝试均生成独立 $run_id$，并通过 task_id、attempt_no、retry_count、previous_run_id 和 retry_reason 归并为执行序列。评审拒绝、超时、工具失败或人工重新派发只产生新的 run，不覆盖旧 run 的 result_ref、session_id、成本、review_status 和 trace_refs。这样，任务层面的 outcome 可以表示最终是否解决，运行层面的 status、成本和评审结果则分别表示每次 agent 尝试的责任、消耗和质量，二者通过执行序列关联而不相互混淆。

### 评估结果附着与质量视图

质量评审写回以被评审结果与运行记录的一致性为前置条件。任务进入 review 或 quality_review 时，系统根据 task.active_run_id、最新 result_ref、评论标识和 result_hash 定位待评审 run；只有 quality_reviews 对应的 task_id、result_ref 或 result_hash 与该 run 一致时，才把 review_id、reviewer、review_status 和 review_notes_ref 写回 $run_id$。若任务在评审期间发生人工修改、重新派发或结果覆盖，旧评审记录仍保留在 quality_reviews 中，但被标记为 stale_review，不改变新 run 的评审状态。评审拒绝触发重新派发时，新 run 只继承 review_notes_ref、retry_reason 和必要输入上下文，不继承旧 run 的 session_id、result_ref、成本或终态。

agent 能力评价按触发时机和输入来源写回运行记录。输出层在任务完成或评审结束时读取 outcome、feedback_rating、review_status 和 result_hash，形成 completion_score 与 correctness_score；轨迹层在 transcript_ref 或 mcp_call_log 可用后读取工具调用数量、唯一工具数、重复调用比例和步骤耗时，形成 convergence_score 或 loop_risk；组件层按 mcp_call_log 中 tool_name、success、duration_ms 和 error 统计工具成功率，低于预设阈值时写入 component_warning；漂移层按近 7 天 token、工具成功率和完成率与历史基线比较，写入 drift_score 和 drifted 标记。上述 eval_refs 既可按 agent 聚合形成排行榜，也可写回到具体 $run_id$，使某次执行是否经过评估、采用哪些输入、是否通过阈值判断均可被查询。

### 工具化查询、事件输出与外部复用

运行记录查询视图按 workspace_id 强制过滤，并建立 $run_id$、task_id、agent_name、session_id、status、created_at 和 attempt_no 等索引。get_run 返回单次运行的结构化字段、状态迁移、触发信息、会话引用、结果摘要、成本汇总、评审摘要和 eval_refs；get_task_runs 按 attempt_no 或 started_at 排序并分页返回某任务全部尝试；get_agent_runs 支持按时间窗口、status、review_status 和成本区间筛选；get_run_transcript 只返回 transcript_ref 指向的授权片段。查询接口默认返回摘要和引用，不返回完整敏感输入；分页游标包含 workspace_id 和排序键，避免跨工作区拼接结果。

权限过滤按对象、字段和片段三级执行。对象级规则要求所有写入和查询均携带 workspace_id，并与调用方身份绑定，不一致时直接拒绝；字段级规则对 session_key、API key、原始 prompt、错误堆栈和外部路径进行脱敏或以引用标识替换；片段级规则对 transcript 按角色、时间范围、最大长度和敏感标记裁剪。若 transcript_ref 不存在、已归档或调用方无权访问，接口返回 unavailable、archived 或 forbidden 状态及可展示摘要，不泄露原始内容。外部 MCP 或 REST 工具调用运行记录接口时，还需携带调用方身份和可选 idempotency_key，系统据此进行鉴权、去重和不可变审计写入。

事件输出采用同源 outbox 机制：事务内写入 agent_runs 状态变化时同步写入待发布事件，事件包含 event_id、$run_id$、workspace_id、sequence_no、event_type、payload_hash 和 created_at；事务提交后由发布器投递到实时事件流、活动流或外部 webhook。订阅端按照 event_id 去重、按照 sequence_no 重排，并可在断线后从指定 sequence_no 重放。查询视图可由 agent_runs 和 outbox 重建，因此看板、审计模块和外部订阅方即使经历重复投递、乱序到达或临时发送失败，也能以同一 $run_id$ 和同一事件序列收敛到一致的运行链路。
