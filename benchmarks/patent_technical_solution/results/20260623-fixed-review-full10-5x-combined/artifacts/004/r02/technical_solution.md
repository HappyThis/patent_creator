## 技术方案

本方案面向智能工作台中 agent 执行过程难以按单次工作进行结构化沉淀的问题，在任务、agent、会话、成本、工具调用、活动事件和评估记录之间设置统一的运行记录锚点。每一次由任务派发、队列领取、会话续跑或外部工具调用触发的 agent 工作，均被抽象为一条可追踪的运行记录，并通过该记录把“从哪里触发、由谁执行、关联什么上下文、产生什么结果、消耗多少资源、是否经过评估以及能否被查询复用”等信息组织为同一数据链路。

### 运行记录锚点与总体结构

运行记录锚点是按一次 agent 工作建立的最小追踪单元，可以落库为独立运行记录实体，也可以通过运行记录表与任务、会话、工具调用、token 记录、评估记录之间的映射表实现。无论采用哪种落库方式，运行记录均以 $runId$ 作为对外稳定标识，以工作空间为隔离边界，以任务、会话、agent、触发请求和结果对象为可选关联对象，并保存运行状态、结果状态、评审状态和归因状态四类状态。该锚点不替换既有对象，而是在既有对象产生或更新时写入关联，使分散记录能够按一次执行被重放、校验和查询。

运行记录采用“强关联优先、弱关联兜底”的关联规则。强关联是指来源记录显式携带 $runId$，或者任务、会话、工具请求中已经存在经系统或人工确认的运行绑定；弱关联是指来源记录缺少 $runId$，但可由工作空间、任务标识、会话标识、agent 标识、触发入口和时间窗口推断候选运行。关联优先级依次为显式 $runId$、任务或会话上的已确认绑定、人工确认绑定、规则推断弱关联；低优先级关联不得覆盖高优先级关联，弱关联不得覆盖已确认强关联。

弱关联候选以工作空间一致作为硬条件，并按任务标识一致、会话标识一致、agent 标识一致、事件时间落入运行开始和结束窗口、触发入口相同、来源对象类型匹配等因素计算关联置信度。若最高置信度低于预设阈值，来源记录保持未归因；若最高候选与次高候选的分值差小于冲突阈值，来源记录标记为 $ambiguous\_attribution$ 并进入人工确认；人工确认后写入确认人、确认时间和确认依据，后续自动规则只能追加证据，不能改写该确认绑定。

### 运行记录数据模型与状态机

运行记录的数据模型包括不可变字段、可更新字段和可为空字段。不可变字段包括 $runId$、$workspaceId$、$triggerType$、$triggerRequestId$、$createdAt$ 以及初始调用主体；其中 $runId$ 可由工作空间标识、触发入口、触发请求标识和随机或递增序列生成，保证在同一工作空间内唯一。可更新字段包括 $runStatus$、$resultStatus$、$reviewStatus$、$attributionStatus$、$startedAt$、$endedAt$、$lastEventSeq$、结果引用、成本快照版本和评估附着引用。可为空字段包括 $taskId$、$projectId$、$sessionId$、$agentId$、$parentRunId$、$retryOfRunId$ 和外部工单引用，用于兼容会话续跑、外部工具调用、历史数据补绑和非任务型运行。

运行状态 $runStatus$ 描述执行生命周期，结果状态 $resultStatus$ 描述 agent 输出是否成功，评审状态 $reviewStatus$ 描述质量门或人工评审结论，归因状态 $attributionStatus$ 描述关联关系可靠性。$runStatus$ 可以包括 pending、assigned、running、waiting_tool、review_required、completed、failed、requeued、cancelled 和 attribution_pending；$resultStatus$ 可以包括 none、partial、success、error；$reviewStatus$ 可以包括 not_required、pending、approved、rejected；$attributionStatus$ 可以包括 confirmed、weak、ambiguous、unattributed。四类状态分层保存，避免将执行失败、评审拒绝和关联不确定混为同一状态。

状态迁移由事件驱动并受允许迁移表约束。任务或外部请求登记后进入 pending，成功分配 agent 后进入 assigned，agent 开始处理或会话开始输出后进入 running，等待工具响应时可进入 waiting_tool，输出完成且需要质量门时进入 review_required，通过评审或无需评审时进入 completed，执行错误且不可继续时进入 failed，被调度器重新排队时进入 requeued，用户或系统终止时进入 cancelled。completed、failed、cancelled 属于最终态，迟到事件只能补充时间线、成本或诊断字段，不得把最终态回退为 running；requeued 或 retry 场景创建新的运行记录，并通过 $retryOfRunId$ 指向原运行，原失败或部分结果继续保留。

### 触发源、身份与上下文溯源

不同触发入口在满足前置条件后创建或绑定 $runId$。任务自动派发以任务已创建、具有工作空间且已确定或可解析目标 agent 为前置条件，在任务从待处理或已分配进入执行调度时创建运行记录，并把 $runId$ 写入任务 metadata、活动事件和派发请求；队列领取以 agent 身份通过鉴权、任务原子领取成功为前置条件，在领取成功的同一事务或补偿事务中创建运行记录，并绑定任务、agent 和领取请求；会话续跑以目标 session 存在且请求已验证为前置条件，每次独立续跑请求创建一个运行记录，并用 $triggerRequestId$ 区分同一 session 内的并发请求；MCP 或 REST 外部调用以工作空间鉴权通过、工具名和入参摘要可记录为前置条件，优先复用调用方传入的 $runId$，否则创建外部触发运行或写入未归因池。

若运行记录创建成功但 agent 未实际启动，系统在超过启动等待时间后将 $runStatus$ 标记为 failed 或 requeued，并保留触发请求和分配证据；若 agent 已执行但 $runId$ 回写任务、会话或工具日志失败，系统根据 $triggerRequestId$、session、task 和时间窗口进行补偿绑定，补偿前相关记录标记为 attribution_pending；若外部调用缺少必要鉴权或工作空间信息，则不创建确定运行记录，仅记录失败审计或未归因条目。

身份溯源由三组字段固定表达。agent 身份写入 $agentId$、agent 名称、角色、配置版本和可选 session key，来源为任务分配、agent 注册信息或队列领取主体；执行身份写入模型、网关、直接调用通道、会话通道、目标 session 和可选 gateway agent 标识，来源为派发器、会话控制或模型调用返回；调用身份写入 $callerType$、$callerId$、鉴权主体、外部工具标识和 $triggerRequestId$，来源为用户操作、调度器、其他 agent、MCP 工具或 REST 请求。三组身份可以不一致，查询时分别返回并标注来源，不用执行身份覆盖任务分配身份，也不用外部调用主体覆盖实际执行 agent。

上下文溯源不要求把所有上下文内容复制到运行记录中，而是保存可复现执行来源的引用和摘要。例如，任务类运行保存任务标题、描述摘要、优先级、标签、项目票号和上一轮评审反馈引用；会话类运行保存目标会话、最后用户提示、工作目录或 transcript 片段引用；工具类运行保存 MCP server、tool name、入参摘要和调用来源。由此既能支持审计和复盘，又避免运行记录因保存完整对话或大型结果而膨胀。

### 执行过程、结果与成本的归集

执行过程采用事件归集算法更新运行记录。每个来源对象输出事件时至少携带 $eventId$、$sourceType$、$sourceId$、$timestamp$、可选 $runId$、$eventType$、序号和摘要载荷；归集层先按 $eventId$ 或 $sourceType+sourceId+eventType+timestamp$ 去重，再按 $runId$ 写入运行时间线。没有 $runId$ 的事件进入关联队列，按工作空间、任务、会话、agent 和时间窗口进行强关联或弱关联。时间线按事件时间排序，接收时间作为并列排序和迟到事件判断依据；迟到事件可以补充历史节点和成本明细，但不得越过状态迁移规则覆盖最终态。

事件到状态的更新采用优先级和幂等规则。start 或 first_message 事件将 pending、assigned 推进为 running；tool_call_started 推进为 waiting_tool，tool_call_finished 若仍存在未完成工具则保持 waiting_tool，否则回到 running；agent_output_completed 将 $resultStatus$ 置为 success 或 partial，并根据是否需要质量门进入 review_required 或 completed；dispatch_failed、tool_error 或 timeout 事件在达到重试条件前可进入 requeued，达到终止条件后进入 failed；cancel 事件优先进入 cancelled。多个来源给出冲突状态时，以显式取消、不可恢复失败、完成、待评审、重排、运行中为优先顺序，并保留被覆盖事件作为冲突诊断。

结果归集分离保存 $runStatus$、$resultStatus$、$reviewStatus$ 和 $attributionStatus$。agent 正常返回时，结果正文保存摘要或截断文本，结果对象保存任务评论、任务 resolution、会话消息、导出文件或外部工单引用，$resultStatus$ 置为 success；执行失败但已经产生部分输出时，$resultStatus$ 置为 partial，$runStatus$ 可为 failed 或 requeued，部分结果仍作为诊断和复用材料保留；执行 success 但评审 rejected 时，$resultStatus$ 保持 success，$reviewStatus$ 置为 rejected，$runStatus$ 可回到 requeued 或保持 review_required/failed，不用评审拒绝覆盖 agent 已产出事实；评审 approved 也不得把执行失败的运行直接改为 completed，除非存在可验证的结果对象。

成本归集在运行结束、阶段刷新或迟到 usage 到达时生成带版本的运行级成本快照。直接携带 $runId$ 的 usage 记录优先累加，并按 usageRecordId 或来源序号去重；只有 session 或 task 维度成本时，按运行的开始结束时间、会话消息边界、任务派发边界和 agent 标识切分，无法切分的部分进入未归因或弱关联成本；模型返回缺失 usage 时，可按 transcript token 估算并标记为 estimated，不得用估算值覆盖直接模型 usage。每个 snapshotVersion 保存来源明细、归因置信度、输入/输出 token、模型、费用和未归因差额，使 agent、任务、项目和单次运行维度能够通过相同来源明细回算校验。

归集层出现写入失败时，来源事件先写入待处理队列或保留原始审计记录，并通过幂等键重试；工具调用成功但结果落库失败时，运行记录保存工具调用成功事件和落库失败异常，等待补偿任务重新拉取结果对象；多个运行争夺同一 usage、工具调用或评估记录且无法区分时，该来源记录保持 $ambiguous\_attribution$，不任意归属。历史数据迁移时，只有满足工作空间一致且至少命中任务、会话、agent 与时间窗口中的两类条件，才允许生成弱关联候选；否则保持未归因。

### 评估结果附着与质量闭环

评估附着区分单次运行评审、窗口级评估和 agent 级统计评估。单次运行评审以任务结果或运行结果对象为输入，写入 $reviewStatus$、评审者、评审备注、评审时间和评审对象引用；评审 approved 可将 review_required 推进为 completed，评审 rejected 可触发 requeued 或 failed，但只改变 $reviewStatus$ 和允许范围内的 $runStatus$，不删除原始结果。窗口级 output、trace、component 或 drift 评估写入评估层级、得分、是否通过、覆盖时间、覆盖任务集合、工具调用集合和 token 集合，并通过集合关系关联多个运行。

当评估结果属于统计窗口而非单次执行时，系统不将其写成某一运行的确定结论，而是作为 $evalAttachment$ 展示。单次运行查询时，可以返回该运行直接触发的评审结果、该运行落入的最近评估窗口、以及同一 agent 在该窗口中的 drift 或 component 指标。只有以该运行结果对象为输入的评审能够改变 $runStatus$；agent 级漂移、工具成功率统计和历史趋势只能改变 agent 可信度、告警标记或推荐策略，不能把某一次运行从 completed 改为 failed。

质量闭环由异常类型和状态映射共同驱动。运行失败、评审拒绝、工具调用失败率超过阈值、token 消耗超过任务或 agent 基线、同一任务连续 requeued、或运行长时间停留在 waiting_tool/running 时，系统向运行记录写入异常码、触发指标、检测时间和建议动作。建议动作可以是重新排队、创建 retry 运行、降低 agent 可信度、要求人工确认归因或仅生成告警；执行建议动作前需检查最终态约束和确认绑定约束，避免因统计异常覆盖已确认完成结果或人工确认归因。

异常恢复以补偿和终止条件区分处理。运行创建后未收到 start 事件的，按启动超时生成 failed 或 requeued；已收到 start 但长期无结束事件的，按心跳、会话活跃度和工具调用状态判断为 waiting_tool、running 或 timeout；工具调用回传延迟的，迟到事件补入时间线并重算成本快照，但不改变最终态；事件写入失败的，使用幂等键重放；历史数据不满足最低关联条件的，保持 unattributed 并只参与全局成本或活动统计，不为了报表完整性强制归入某个运行。

### 面向工具和外部系统的查询复用

以 $runId$ 为中心的查询接口或 MCP 工具返回归一化响应结构。响应包括 summary、trigger、identity、contextRefs、timeline、result、costSnapshot、evalAttachments、attribution 和 links 等部分：summary 给出 $runId$、工作空间、状态和时间；trigger 给出触发入口、请求标识和调用主体；identity 分别返回 agent 身份、执行身份和调用身份；timeline 返回去重排序后的事件；result 返回结果摘要、结果对象和评审状态；costSnapshot 返回版本、来源明细和未归因差额；evalAttachments 返回单次评审和窗口级评估；attribution 返回关联类型、置信度、确认来源和冲突标记；links 返回可访问的任务、会话、评论、导出文件或外部对象。

查询入口支持按运行标识精确查询，也支持按任务、项目、agent、会话、触发源、运行状态、结果状态、评审状态、时间范围、归因状态和置信度阈值过滤。查询时以工作空间和调用权限为硬边界，只返回调用主体可访问的任务、会话和结果链接；若某些关联来自弱关联或人工确认，响应中必须携带 correlationType、correlationConfidence 和 evidence 摘要；若来源记录仍处于 ambiguous 或 unattributed 状态，接口返回占位项和原因，而不是伪造成确定关联。

外部系统复用时，运行记录提供机器可读的归一化结构，而不是要求外部系统分别理解任务表、agent 表、session transcript、token usage、MCP 调用日志和 eval 记录的内部差异。外部审计工具可以按项目拉取运行链路，成本看板可以读取带来源明细的成本快照，agent 排行或训练数据生成工具可以筛选 completed、reviewStatus 为 approved 且归因为 confirmed 的运行，安全审计工具可以追踪某次运行调用过的 MCP server、tool 和错误记录。新数据优先写入确定 $runId$，历史或第三方数据先进入未归因池并按规则补绑；无法满足最低关联条件的数据保留原统计口径和未归因标记。
