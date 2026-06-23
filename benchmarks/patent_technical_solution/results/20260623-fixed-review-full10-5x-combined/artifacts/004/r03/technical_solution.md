## 技术方案

### 总体构思与记录对象

既有智能工作台虽然已经具有任务派发、agent 管理、会话执行、活动事件、结果评论、质量评审、token 成本统计和 MCP 工具调用审计，但这些信息分别存储在任务、评论、活动流、评审、token usage 和工具审计记录中，缺少以“一次 agent 执行”为中心的唯一归集对象。由此会出现同一任务多次重试难以区分、某次输出对应的会话和成本难以精确归因、评审结论无法稳定指向被评审输出、外部系统只能读取任务最终状态而不能可靠复用某次具体产出的问题。

本方案增设结构化运行记录。运行记录是持久化对象，表示一次 agent 执行语义的运行实例，run id 是该对象在工作区内全链路传播的唯一标识。运行记录不是事后人工填写的日志，而是在任务创建、队列领取、自动派发、会话调用、工具调用、结果回写和评审流转等既有链路中由 run recorder 自动生成和更新，用于把触发来源、执行主体、关联任务或会话、状态、产出、成本、评审、工具调用和查询索引绑定为同一个可追溯单元。

一条任务可以触发多条运行记录，例如首次派发、拒绝后重试、人工重新派发或不同 agent 分阶段执行；一条运行记录可以关联已有会话或新建会话，并通过同一 run id 串联 token 统计、工具调用审计、活动流和评审结果。按照该粒度，系统能够从任一任务、agent、会话、事件、工具调用或评审结果反向定位到对应运行实例，并回答该次工作从何触发、由谁执行、产出何物、消耗多少、是否通过评估以及是否可被外部工具复用。

### 运行记录数据结构与关联关系

系统设置 runs 表或等效持久化对象作为运行记录主数据，并保留与任务、agent、会话和事件对象的外键或逻辑关联。创建运行记录前，run recorder 至少取得 workspace id、触发类型、触发对象标识、任务或会话中的至少一个关联对象、执行 agent 或路由目标以及幂等键；缺少 task id 但由外部 webhook、MCP 调用或独立会话触发的，允许先创建 unbound 运行记录，并在后续出现任务或会话映射时补齐关联，无法补齐时保持未绑定状态。run id 在首次创建运行记录时生成，在同一 workspace 内唯一，并在派发、会话、工具调用、成本、评审和查询链路中传播。

运行记录主字段包括 run id、workspace id、project id 或 project ticket、trigger type、trigger object id、task id、agent id、gateway agent id、dispatch group、phase、execution channel、model、session id、status、event sequence、started at、ended at、input summary、output summary、result reference、error summary、retry index、predecessor run id、parent run id、adopted result flag、record source、billing source、audit gap 标记、idempotency key 和 metadata。幂等键由 workspace id、触发类型、触发对象标识、task id、agent id、派发批次或外部 event id 组合生成，持久层设置 workspace id 与 idempotency key 的唯一约束；重复请求命中该约束时返回已有运行记录，而不是创建新记录。

运行记录不替代既有对象，而是索引和归集层。任务表继续保存任务标题、描述、状态、优先级、指派对象和元数据；agent 表继续保存 agent 身份、角色、会话键、运行状态和配置；评论保存 agent 输出或评审反馈；活动保存可读活动流；质量评审保存评审者、评审状态、说明和时间；token usage 保存模型调用 token 和成本；MCP 调用审计保存工具名、服务名、耗时、成功失败和错误信息。运行记录通过 run id 直接关联新产生的数据，通过 task id、agent id、session id、workspace id、event sequence 和时间窗口关联既有数据，使分散证据能够按一次执行重新组装。

与运行记录配套设置 run events、run cost items、run evaluations 和 result adoption 等附属表或等效对象。run events 保存归一化事件、event time、ingest time、sequence、payload hash 和来源对象；run cost items 保存 usage id、payload hash、成本数值、成本来源和归属置信状态；run evaluations 保存评估类型、目标 run、输入证据、分数或标签、阈值版本和结论；result adoption 保存任务最终采用的 adopted run id、采用原因、采用时间和操作者或自动规则。上述附属对象均携带 workspace id，并与主记录保持同一工作区约束。

为保证隔离，run、task、agent、session、tool audit、token usage、review 和 adoption 之间建立关联时必须校验 workspace id 一致；外部 trace id 只能作为同一工作区内的辅助匹配条件，不能作为跨工作区合并依据。对于模型路由理由、质量门禁版本、排行榜维度、插件自定义评估项等未固化字段，系统写入 metadata 或附属扩展表，并通过 record source 标记 native、reconstructed、recovered 或 degraded 等来源，以便查询方识别数据可信度。

### 执行生命周期总流程

运行记录的采集入口嵌入现有任务生命周期。触发输入首先进入 run recorder，run recorder 根据工作区、触发类型、触发对象、任务或会话、目标 agent 和幂等键生成或命中运行记录；派发器取得该 run id 后再将任务置为 in progress，并把 run id 写入会话调用、agent gateway 调用或 direct API 调用的上下文。随后，会话事件、模型事件、工具审计事件、活动事件、token usage 和评审请求均携带或可回填到该 run id；最终，结果、成本、评审和采用记录按 run id 聚合输出。

对于队列重复领取、人工重新派发与自动路由同时发生或 webhook 重复投递，系统在任务状态更新和 run 创建之间使用数据库事务、任务版本号或行级锁进行裁决。同一任务、同一 dispatch group、同一 phase 和同一派发批次只能存在一个 active run；重复请求命中 workspace id 与 idempotency key 唯一约束时返回已有运行记录；不同 agent 的合法并行执行必须使用不同 dispatch group 或 phase，从数据结构上区分并行分工与重复派发。

该总流程使 run id 成为贯穿触发、派发、执行、工具调用、成本、评审和查询的技术主线。唯一 run id 和幂等约束保证同一次触发不会被重复记账；事件序列和来源标记保证异步事件可以被补入而不破坏历史；状态机和结果采用记录保证任务最终结果与每次运行输出之间有明确边界。

### 运行状态机

运行记录采用显式状态机。初始状态为 pending，表示已生成运行记录但尚未实际调用 agent；派发器发出会话调用、agent gateway 调用或 direct API 调用后，状态从 pending 转为 running；收到 agent output received 且输出写入成功后，若任务需要评审则转为 waiting review，若不需要评审则转为 succeeded；执行通道返回不可恢复错误、超过最大重试次数或模型调用失败时转为 failed；用户或系统在执行完成前取消任务时转为 cancelled；评审拒绝时原 run 转为 rejected 或保持 reviewed rejected 的最终评审状态，并触发新的 retry scheduled 记录或新 run 创建。

succeeded、failed、cancelled、rejected 和 reconstructed 为最终态或准最终态。最终态运行记录不允许被后续普通事件覆盖为另一最终态：已 cancelled 的 run 收到迟到输出时仅追加 late output 事件，不改为 succeeded；已 succeeded 或评审通过的 run 收到失败回调时仅记录 late failure；已 rejected 的 run 不因后续重试成功而改写自身结论，重试成功应体现在后继 run。只有成本补齐、评估补齐、关联补齐或经明确补正规则确认的误关联修正可以更新最终态 run 的附属字段。

状态冲突按优先级处理：已确认的用户取消优先于尚未完成的输出；已评审通过或已采用的结果优先于迟到失败；执行失败后的重试创建新 run 而不是复用原 run；工作区不一致、任务与 agent 关系不合法或事件无法可信匹配时进入 abnormal 或 pending association 分支。abnormal run 只保存安全摘要、异常原因和来源证据，不参与排行榜或自动采用，除非后台修复后转为 recovered 或 reconstructed。

### 事件采集与归一化

系统将 task event、activity event、SSE event、MCP audit event、token usage event、gateway 回调和评审回调归一化为 run events。每条归一化事件包括 run id、workspace id、task id、agent id、session id、event type、event time、ingest time、sequence、source object type、source object id、external trace id、payload hash 和摘要载荷。sequence 在同一 run 内单调递增；event time 表示事件实际发生时间，ingest time 表示系统接收时间，用于处理乱序和迟到事件。

事件匹配采用分层规则。事件自带 run id 且 workspace id 一致时直接关联；没有 run id 时，先按 task id、session id、agent id、execution channel 和时间窗口查找候选 run；若候选 run 多于一个，则优先选择处于 running 或 waiting review 的 run，其次选择 event time 与运行时间区间最接近的 run，再按派发序号、dispatch group、phase 和执行通道一致性排序。仍无法唯一确定时，事件进入 pending association 队列，等待后续 session id、task id 或 gateway trace id 补齐；若最终无法匹配，则生成 unbound 或 abnormal 事件，不强行写入任一 run。

迟到事件允许追加到最终态 run 的事件表，但默认不得改变最终状态。例如 token usage、MCP completed 或 gateway 统计晚于 succeeded 到达时，只补齐成本、工具耗时或证据链；agent output 晚于 cancelled 到达时记录为 late output；失败回调晚于评审通过到达时记录为 late failure。只有当迟到事件满足同一工作区、同一通道、同一会话、payload hash 未重复且处于允许补齐字段范围时，系统才更新对应附属字段。

工具调用链按 tool call id 串联。tool called 事件创建工具调用子记录，tool completed、tool failed 或 tool timeout 必须匹配同一 tool call id、run id 或可验证的会话和时间窗口；完成事件缺失 run id 时按前述事件匹配规则补齐。相同 tool call id 或相同工具名、参数 hash、会话和时间窗口的重复完成事件被去重。超时、部分成功、重复参数调用次数超过阈值、相同工具和参数形成循环或总耗时超过阈值时，系统写入 trace anomaly 标签，并作为 trace eval 和 component eval 的输入证据。

### 结果写入、重试与采用规则

当 agent 返回结果时，系统将输出正文继续写入任务评论或任务结果字段，同时在运行记录中保存输出摘要、结果对象引用、响应长度、完成时间和状态。若该任务存在多条成功或待评审 run，任务最终结果不由最新写入自动覆盖，而由 result adoption 记录确定 adopted run id。采用规则可以按评审通过、人工选择、指定阶段输出、优先级最高 agent 或最新成功运行依次裁决；一旦 adopted run id 写入并通过权限校验，未采用 run 的输出仅作为历史证据、对比样本或后续重试输入，不得覆盖已采用结果。

失败和拒绝不改写历史 run，而是形成重试链路。执行失败时，原 run 保存失败阶段、错误摘要、重试序号和是否可重试；若任务仍允许重试，调度器创建新的 run，并将 predecessor run id 指向失败 run。评审拒绝时，原 run 的评审结论保持 rejected，新 run 的 predecessor run id 指向被拒绝 run，并在输入摘要或 prompt 构造中引用拒绝原因摘要。这样，任务层面可以继续推进到新的执行结果，而每次失败、拒绝和改进来源都保留为不可覆盖的证据。

在分阶段执行场景中，parent run id 表示同一任务下的上级编排运行，predecessor run id 表示同一阶段的前一次尝试，phase 表示需求分析、实现、检查、修复等阶段。不同 phase 的输出可以分别被采用到不同结果槽位；同一结果槽位只能有一个 adopted run id。该规则使并行 agent 协作、串行重试和最终结果采用具有不同数据含义，避免多个成功输出相互覆盖。

### 成本与评估附着

成本归集以运行记录为中心进行。对于直接模型调用产生的 token usage，系统在写入模型、session id、输入 token、输出 token、总 token 和成本时同步写入 run id；未携带 run id 的 token usage 按 workspace id、session id、task id、agent id、模型、执行通道和时间窗口回填到候选 run。每条 token usage 通过 usage id 或 payload hash 去重，默认只能归属一个 run；若一个 run 包含多个模型调用，则生成多条 run cost item 后汇总；若多个 run 共用同一 session，则优先匹配时间区间覆盖调用时刻、状态为 running、执行通道和模型一致且派发序号最近的 run。

成本归属分为 exact、candidate 和 unassigned 三类。携带 run id 或能唯一匹配的为 exact；存在多个候选但可排序的先记为 candidate 并保留候选列表和置信理由；无法可信匹配的保持 unassigned，不计入单个 run 的精确成本。经 gateway 回传、token 面板统计或外部计费数据补齐后，精确成本可以替换估算成本，但原 billing source、估算数值、替换时间和替换依据仍保存在成本附属记录中，以便审计成本治理和排行榜统计的来源差异。

质量评审和评估结果作为运行记录的附属证据。任务进入 review 或 quality review 阶段时，评审请求携带 review target type、run id、task id、result reference 和评审版本；reviewer 返回 approved、rejected、notes 或分数时，系统先校验该 run 是否仍为该任务当前待评审 run，且 result reference 未被其他 adopted run 替换。校验通过后，评审结论写入 run evaluations 或质量评审表并关联 run id；校验不通过时，结论记录为 stale review，仅保留证据而不改变任务最终状态。

四层 agent 评估采用统一的输入和输出结构。output eval 以任务标题、描述、验收条件、agent 输出摘要和结果引用为输入，输出 pass/fail、分数、失败原因标签和证据片段；trace eval 以 run events、MCP 工具调用序列、重复参数 hash、超时和循环标签为输入，输出收敛性、异常工具链标签和风险等级；component eval 以该 run 涉及的工具成功率、耗时、错误类型和部分成功记录为输入，输出工具可靠性标签；drift detection 以同一 agent、任务类型、模型层级和工作区的历史基线为输入，输出质量、耗时或成本偏离标签。各评估项均保存阈值版本和证据引用，避免后续阈值调整后无法解释历史结论。

排行榜和质量统计仅使用权限内且来源可信的运行记录聚合。通过 run id、agent id、task type、model、workspace、evaluation type、billing source 和时间索引，系统可以计算通过率、平均成本、重试率、工具失败率和漂移风险；其中 exact 成本优先用于精确统计，candidate 成本可用于带置信标记的估算统计，unassigned 成本不归入单个 agent 的精确排行榜。

### 工具化查询与外部复用

为使运行记录能够被其他工具和外部系统查询复用，系统在现有 REST API、MCP server、SSE 事件和 outbound webhook 之上设置运行记录查询出口。查询接口支持按照 run id 精确读取，并支持按照 agent、task、project ticket、workspace、session、状态、触发来源、模型层级、评审结论、成本区间、时间范围和工具调用结果过滤。查询执行时先解析调用者身份和 workspace，再校验其对目标 task、agent、session、result reference 和评审对象的访问权限；展开关联对象时沿用原对象权限，不因通过 run 查询而绕过任务、会话或结果正文的访问控制。

返回结果按权限分层裁剪。基础层返回 run id、状态、时间、agent、task、成本来源、评审摘要和 adopted 标记；具备任务访问权限时返回输入摘要、输出摘要和结果引用；具备会话或工具审计权限时返回工具调用链、工具参数摘要和错误摘要。敏感输入、输出正文、工具参数、错误堆栈和外部 trace id 按权限脱敏、摘要化或省略；webhook 和 MCP 工具默认读取摘要与引用，需要更高权限才可读取完整结果或审计明细。

MCP 工具侧在既有 agent、task 和 attribution 工具的基础上提供按运行记录读取或分组归因能力。外部自动化工具先按 agent、任务或时间范围查询运行记录，再读取某次运行的输入摘要、输出引用、评审状态、成本来源和工具调用链，判断该结果是否可作为后续任务上下文或证据。实时联动场景中，运行记录状态变化通过 SSE 或 webhook 推送，推送载荷携带 run id、task id、agent id、状态、event sequence 和摘要字段，接收方凭 run id 回查详情，从而保证界面、MCP 工具和外部 API 对同一次 agent 工作使用同一解释口径。

### 异常、冲突与恢复处理

异常和冲突处理围绕“保留证据、禁止错误覆盖、可恢复则标记来源”的原则执行。重复触发由幂等键和唯一约束合并；并发派发由任务版本号、事务锁和 active run 约束裁决；乱序事件由 event time、ingest time 和 sequence 保留；迟到事件只补充事件链或附属字段，不覆盖最终态；任务取消后的迟到输出标记为 late output；评审通过后的迟到失败标记为 late failure；多个成功输出通过 adopted run id 决定最终采用。

跨工作区或非法关联按拒绝优先处理。run、task、agent、session、tool audit、token usage、review 和 result reference 的 workspace id 必须一致；候选对象跨工作区时，系统不写入对方对象引用，只在当前工作区记录安全摘要、拒绝原因和 external trace id 的哈希。任务与 agent 指派关系不合法、session 不属于该工作区或工具审计来源无法验证时，事件进入 abnormal 或 pending association，不参与自动采用、排行榜和精确成本统计。

关键节点采用事务内写入或 outbox/retry 队列保证记录可信。任务状态从 assigned 转为 in progress、会话调用发出、agent 输出写入、评审请求发出和结果采用写入等节点，与 run event 或 outbox 消息在同一事务边界内提交；若 run recorder 暂时失败，系统生成包含 workspace id、task id、session id、activity id、触发类型和错误摘要的最小诊断事件，并由后台修复任务按 task、session、activity、comment、token usage 和工具审计记录回补 run。

后台回补根据证据完整性设置 record source。能够确定 run id 或唯一候选 run 的标记为 recovered；只能根据时间窗口、会话和活动近似还原的标记为 reconstructed；存在关键事件缺失、成本无法归属或工具链断裂的标记为 degraded，并设置 audit gap。带 audit gap 的运行记录仍可用于人工审计和问题定位，但在自动排行榜、精确成本归因和外部复用时被排除或显示风险提示。

### 兼容与扩展边界

本方案优先复用现有 SQLite 存储、迁移体系、任务派发器、活动日志、事件总线、质量评审、token 统计和 MCP 审计能力，只在必要位置新增 runs、run events、run cost items、run evaluations、result adoption、事件映射或恢复队列表。新增表由核心迁移或插件迁移注册生成，并保持 workspace id、run id、idempotency key、event sequence、source object id 等索引，以支撑按运行实例的追溯、查询和聚合。

采集逻辑不要求 agent 主动填写日志，也不要求外部调用方理解内部表结构。各执行入口只需在触发、开始、完成、失败、评审、工具调用和结果采用等关键节点调用统一的 run recorder 接口，由该接口负责幂等判断、状态转换、关联字段补齐、事件广播、权限裁剪和错误降级。旧任务若没有历史 run id，后台修复任务通过任务状态时间、评论、活动、session id、token usage 和工具审计记录尽力回填，并按 recovered、reconstructed、degraded 或 audit gap 标记可信等级。

扩展时可以增加新的触发来源、评估维度、成本来源、工具审计字段或外部 trace 标识，但扩展数据均围绕同一 run id 附着，并遵守工作区一致性、幂等约束、最终态不可覆盖和权限过滤规则。由此，可信审计建立在唯一 run id、事件序列、来源标记和状态机之上；成本治理和排行榜建立在 token usage 去重、成本来源标记、估算到精确的替换规则和按 agent、task、model、workspace 的索引聚合之上；外部复用建立在 adopted run id、结果引用、评审附着和敏感信息裁剪之上。
