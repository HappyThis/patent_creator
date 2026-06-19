## 技术方案

本方案在现有任务、Agent、会话、活动、审计、令牌消耗、质量评审和 MCP 调用记录基础上，增加面向每一次 Agent 执行过程的结构化沉淀机制。该机制不是仅保存自然语言日志，而是把一次执行抽象为可查询、可关联、可评估的运行记录，使工作台能够从同一条链路回答执行由谁触发、交给哪个 Agent、对应哪个任务或会话、产生了什么结果、消耗了多少资源、是否经过评估以及能否被其他工具复用。

### 总体构思与运行记录主键

本方案设置统一的 Agent 运行记录实体，记为 agent run。每条运行记录以 run_id 作为主键，并可以同时保存 trigger_id、idempotency_key、task_id、agent_id、agent_name、gateway_agent_id、session_id、workspace_id、project_id、activity_id、audit_id 等关联字段。其中 run_id 用于贯穿任务派发、队列领取、会话调用、结果写回、质量评审、令牌统计和工具调用；idempotency_key 用于识别同一派发动作的重复提交；task_id、session_id 和 project_id 用于把执行过程回连到现有任务、会话和项目上下文。

运行记录的内容可以分为基础标识、输入上下文、执行状态、输出结果、资源消耗、评估结论和外部复用信息七类。基础标识保存 run_id 与各类关联键；输入上下文保存任务标题、优先级、标签、目标会话、模型选择和提示词摘要；执行状态保存开始、派发、完成、失败、重试、评审等时间点；输出结果保存结果摘要、结果存储位置和截断信息；资源消耗保存 token、模型、耗时和估算成本；评估结论保存质量评审和多层 eval 的结果；外部复用信息保存可对外暴露的引用标识、权限范围和脱敏策略。

### 执行触发与溯源采集

在执行触发阶段，本方案将触发来源标准化为调度器自动派发、Agent 轮询队列领取、人工更新任务、Webhook、MCP 工具调用、会话继续执行或外部 API 请求等类型。系统在任务从 inbox 或 assigned 进入 in_progress、向指定会话发送提示词、创建新会话调用 gateway、或直接调用模型接口之前，生成或复用 run_id，并记录触发类型、触发主体、原始任务状态、目标 Agent、目标会话、模型路由结果和派发提示词摘要。

对于现有系统中的自动派发路径，任务在被查询为可派发后先由 assigned 转为 in_progress，并记录 task_dispatched 活动；本方案在该状态变化附近同步创建运行记录。对于队列轮询路径，系统通过原子更新领取 assigned 或 inbox 任务以避免并发竞争；本方案将该原子领取返回结果作为 claimed 事件写入运行记录。对于会话路径，目标会话来自任务 metadata 中的 target_session 或 gateway 返回的 sessionId；本方案将其写入 session_id 或 target_session 字段，使后续会话转录、token 统计和继续会话操作都能回溯到同一次执行。

### 结果、消耗与评估附着

在结果沉淀阶段，本方案将 Agent 返回文本、结构化结果、失败原因、截断标记、任务 outcome、resolution、评论写入、状态变化和事件广播统一附着到对应 run_id。对于派发到既有会话的异步执行，运行记录先进入 dispatched 或 in_flight 状态，并保存 target_session；当后续会话、评论、任务状态或审计事件出现同一 correlation 信息时，再补齐完成时间、结果摘要和最终状态。这样可避免仅依赖任务当前状态而丢失一次执行的中间过程。

资源消耗附着通过 token_usage、会话成本和任务成本聚合共同完成。现有 token_usage 已能保存 model、session_id、input_tokens、output_tokens、created_at，并可扩展 agent_name、task_id 和 cost_usd；本方案进一步以 run_id 或 correlation_id 将 token 记录与具体执行绑定。当直接模型调用只能形成 task-任务编号形式的 session_id，或 gateway 会话只能提供 agent 前缀时，系统通过 task_id、session_id、agent_name、时间窗口和 idempotency_key 进行二次归并，并把归并置信度写入运行记录，避免未归因消耗长期停留在独立报表中。

评估附着包括任务质量评审和 Agent 多层评估两部分。任务完成后进入 review 或 quality_review 状态时，系统把评审请求、评审 Agent、评审 verdict、拒绝理由、通过时间和关联 comments 写入运行记录；若评审拒绝并重新进入 assigned 或 failed，则该运行记录保留 rejected 结论并与后续重试运行记录形成父子关系。对于 output、trace、component、drift 等 eval 层，系统把 eval_runs、eval_traces、mcp_call_log 和任务 outcome 中与本次执行相关的评分、工具调用次数、工具成功率、收敛情况和漂移指标附着到 run_id，使单次结果能够同时支持质量追责和长期能力分析。

### 工具化查询与复用接口

本方案在 REST API 和 MCP 工具层提供围绕运行记录的查询入口。查询条件至少包括 agent、task、session、project、timeframe、status、evaluated、has_cost、trigger_type 和 external_ref，并支持返回运行摘要、完整溯源链、成本明细、评审结论、相关评论、相关 MCP 调用以及可继续查看的会话转录引用。现有按 Agent 归因的身份、审计、变更和成本查询可以扩展为以 run_id 为中心的归因查询，从而减少仅按 agent_name 或 session_id 模糊匹配带来的误归因。

面向外部系统复用时，运行记录不直接暴露完整提示词和原始回复，而是按权限返回脱敏后的摘要、状态、关联对象、成本区间和评估结论。需要复盘时，具备权限的调用方再通过 run_id 获取活动流、审计记录、会话转录或评论内容。MCP 工具可提供 list_runs、get_run、search_runs、get_run_attribution 等能力；REST API 可提供按 run_id 获取详情、按 task_id 获取执行历史、按 agent_id 获取近期执行、按 project_id 聚合成本和质量的接口。

### 一致性、幂等与异常补偿

为保证沉淀结果一致，本方案将运行记录更新设计为状态机式写入。运行状态可包括 created、claimed、dispatched、in_progress、completed、reviewing、approved、rejected、failed、retrying 和 abandoned。每次状态跃迁均带有上一状态、触发事件、时间戳和操作者，且同一 idempotency_key 在未完成窗口内只能对应一条活跃运行记录。若派发失败、Agent 离线、会话超时或评审拒绝，系统不覆盖原始运行记录，而是追加失败事件、重试次数、下一次可重试时间和重新入队原因。

对于部分数据迟到或来源不完整的情况，本方案采用可补偿的关联策略。会话扫描、token 导入、MCP 调用审计、质量评审和任务 outcome 汇总可以在执行结束后异步写入同一 run_id；若暂时无法确认 run_id，则先形成待归因记录，并依据 session_id、agent_name、task_id、workspace_id、project_id 和时间窗口进行候选匹配。匹配完成后追加 attribution_resolved 事件；匹配失败则保留为 unattributed，并在查询接口中提示缺失的关联字段。

该机制与现有活动流和审计流保持兼容。activities 和 audit_log 继续记录人可读的系统事件，agent run 作为归一化索引层保存机器可查询字段，并通过 activity_id、audit_id 或事件数据与原始事件互相引用。这样既不破坏已有任务看板、Agent 归因、成本统计和质量评审能力，又能把分散在任务、评论、会话、token、MCP 调用和评估表中的事实组织成一条完整执行链。
