## 技术方案

### 总体架构与运行角色

本方案在保持用户只与一个主 agent 对话的前提下，在主 agent 内引入“保留式流式子 agent 工具编排”机制。主 agent 仍负责主会话的身份、上下文入口、最终响应组织和客户端连接；当一次用户请求中需要研究、规划、比较、总结等专门能力时，主 agent 不将这些能力实现为一次性的内存函数调用，而是按工具调用语义启动一个或多个具备持久状态的子 agent 运行，并把子 agent 的执行过程和最终结果带回同一主会话视图。

本文中，主 agent 与父 agent 指同一服务端持久对象，父侧存储即该主 agent 的持久存储；父工具调用标识表示主会话中触发子任务的工具调用位置，允许为空以表示后台运行；$runId$ 表示一次子 agent 工具运行的稳定编排标识；子 agent 实例表示由主 agent 按子 agent 类型和实例名定位的独立持久对象，子 facet 表示与主 agent 同属一组存储和路由层级、但拥有独立会话状态的子实体；chat turn 请求标识用于定位子 agent 内部的一次推理请求，可恢复流标识用于定位该请求产生的持久化流式片段；观察通道只负责把子 agent 输出转发到主会话，不等同于执行控制通道。

每个子 agent 是可独立对话、可持久化消息和流式输出的真实 agent 实例或子 facet，而不是主 agent 临时拼接出的提示词片段。创建时，主 agent 先根据子 agent 类型白名单解析目标类，再由 $runId$ 或与 $runId$ 绑定的实例名定位唯一子 agent 实例，并使该实例使用独立的消息表、运行映射、工具状态和可恢复流存储；同一子 agent 实例的生命周期以父侧运行登记为锚点，运行被保留时允许后续查看，运行被清理时同步删除或失效对应子实例状态。主 agent 通过编排接口提交输入、观察输出，并将生命周期事件封装为主连接上的非聊天帧。

在一种实现中，编排层提供两类入口：一类由确定性工作流显式调用，用于后台任务、分阶段报告或并行扇出/汇总；另一类包装成普通工具，使主 agent 的语言模型可在生成过程中选择调用特定子 agent。两类入口最终都落到相同的运行登记、子 agent 启动、事件转发和恢复逻辑上，因此无论子 agent 是由模型工具调用触发，还是由服务端业务逻辑触发，客户端都能以统一方式展示其状态和输出。

### 保留式子 agent 运行登记

为使子 agent 运行在刷新、重连、父进程重启或后续查看时仍可被识别，本方案在父 agent 的持久存储中维护子 agent 工具运行登记表。父侧记录至少包括 $runId$、幂等键、parentConversationId、parentToolCallId、childAgentType、childAgentInstanceId、displayOrder、status、inputPreview、summary、errorCode、errorMessage、createdAt、startedAt、finishedAt、lastForwardedSequence、retentionDeadline、createdByUserId 和 tenantId 等字段。$runId$ 为主键；同一 tenantId、parentConversationId 和幂等键构成唯一约束；parentConversationId、parentToolCallId、status、retentionDeadline 和 childAgentInstanceId 建立索引，用于会话重放、状态恢复、保留清理和 drill-in 授权校验。

$runId$ 由父侧在通过权限、白名单和配额检查后生成，可以采用随机唯一值，也可以由父请求标识、子任务序号和服务器端盐值派生；无论采用哪种方式，都应把用户、租户、父会话和父请求边界纳入幂等键，避免不同用户相同输入或不同后台触发器误复用同一运行。用户重试同一父请求时使用相同幂等键并命中既有记录；同一用户对相同文本发起新的会话请求、不同用户提交相同输入、后台任务按新触发时间再次执行时，应生成新的幂等键和新的 $runId$。

启动流程按事务边界分为父侧预登记、子侧幂等创建和父侧回填三段。父侧先验证父会话存在、当前用户具备访问权限、parentToolCallId 属于该会话或允许为空、childAgentType 位于白名单、输入预览已生成且资源配额允许；随后在父侧事务中插入 status=starting 的登记记录。子侧收到 $runId$ 后在本地以 $runId$ 幂等创建运行映射，分配 chat turn 请求标识和可恢复流标识，并将状态推进为 running。父侧观察到子侧启动成功后回填 childAgentInstanceId、startedAt 和必要的流标识摘要；若子侧创建失败且未产生运行映射，父侧记录 error 或 interrupted，而不是删除预登记后静默重跑。

该运行登记表位于父 agent 侧，是因为重放和展示是以主会话为视角完成的：父 agent 需要知道某个子运行属于哪个父工具调用、兄弟子运行之间的排列关系，以及在客户端重新连接时应合成哪些生命周期事件。登记表默认保留已完成的记录，以便用户刷新页面后仍能看到子 agent 的执行轨迹，也能在稍后进入对应子 agent 查看细节；应用可以根据策略显式删除记录或按保留期限回收历史。

为避免登记表成为敏感输入的第二份副本，父侧默认只保存经过裁剪或脱敏的 inputPreview，而不保存完整提示词、凭证、文件内容或用户隐私数据。生成 inputPreview 时可采用字段级规则：去除认证令牌、密钥、Cookie、文件正文和大段引用内容；对邮箱、手机号、内部路径等标识符进行掩码或哈希；对自然语言输入按长度截断并保留任务类型、语言、文件数量、时间范围等低敏摘要。完整输入由子 agent 在自身执行上下文中按原有会话和工具规则处理；父侧登记表仅保存足以恢复展示、排序、访问校验和结果摘要的编排元数据。

子 agent 侧维护与父侧 $runId$ 对应的运行映射，字段至少包括 $runId$、chatTurnRequestId、recoverableStreamId、childConversationId、status、lastPersistedChunkSequence、cancelControllerId、terminalReason、summary、errorInfo、startedAt 和 finishedAt。$runId$ 在子侧为唯一键，并与父侧 childAgentInstanceId、childAgentType 和 tenantId 共同校验，防止父侧登记指向错误子实例。chat turn 请求标识用于取消或恢复具体推理请求，可恢复流标识用于读取该次运行产生的原始流式片段；三者相互区分，可以防止把后续 drill-in 对话产生的新流误认为原工具调用的输出。

### 独立执行上下文与流式事件桥接

流式转发流程按“子侧先持久化、父侧再封包、客户端再应用”的顺序执行。子 agent 生成聊天响应片段后，先把原始 UIMessageChunk 写入 recoverableStreamId 对应的可恢复流，并将子侧 lastPersistedChunkSequence 更新为该片段序号；父 agent 通过观察通道读取该片段，生成 agent-tool-event 封包，并在父侧持久化待发送事件或其发送水位；随后再向主会话客户端发送，发送成功或确认已进入可重放发送队列后推进 lastForwardedSequence。若子侧写入失败，则该片段不进入可恢复输出；若父侧发送失败，则恢复时以子侧可恢复流、父侧待发送事件和 lastForwardedSequence 为依据重新封包发送；若客户端应用失败，则客户端在重连时按复合去重键重新接收并应用。

子 agent 工具事件可以采用统一封包结构，例如在主连接上发送类型为 agent-tool-event 的帧，帧内包含 tenantId、parentConversationId、parentToolCallId、$runId$、childAgentType、sequence、replay 标记以及事件体。sequence 由父侧按每个 $runId$ 独立分配，并覆盖 started、chunk、finished、error、aborted 和 interrupted 等事件类型；每次分配时先在父侧持久化该事件对应的 sequence 或至少持久化 lastForwardedSequence，再发送给客户端。chunk 事件的 body 透明承载原 chat 响应片段的 JSON 编码内容，客户端使用与普通聊天响应相同的片段应用逻辑恢复消息内容。

客户端去重键至少包括 tenantId、parentConversationId、parentToolCallId、$runId$ 和 sequence；对于没有父工具调用标识的后台运行，parentToolCallId 取空值。客户端对每个子运行维护已应用的最大 sequence 和消息部件索引，只接受 sequence 大于已应用值或尚未见过的同序事件；重复帧被丢弃，乱序帧可暂存至缺口补齐或通过重放重新拉齐。chunk 中若包含工具调用、推理片段、附件引用、消息修订或多消息输出，客户端以事件所属 $runId$ 定位子 agent 展示区域，再以 chunk 内的 messageId、partId 或修订标识定位合并目标，避免不同子 agent 或主会话消息相互污染。

摘要结果在子运行进入终态时生成并写回父侧登记。对于 completed，摘要可以取子 agent 本次 chat turn 新增的最终 assistant 消息，也可以由子 agent 或父 agent 基于本次运行消息生成压缩摘要；对于 error，摘要字段保留已成功产生的部分结果或为空，同时写入 errorCode 和 errorMessage；对于 aborted，摘要标记为用户取消或资源取消导致的未完成结果；对于 interrupted，摘要只表示父侧观察链路无法安全恢复，不冒充子 agent 的最终输出。主 agent 在汇总时读取这些结构化终态和摘要，而不是从客户端显示文本反推结果。

执行与观察相互解耦。子 agent 工具运行一经创建，即由 $runId$ 标识为持久工作；父 agent 对该工作的事件转发只是观察者。浏览器连接断开、页面关闭、观察流取消或某个标签页关闭，只关闭对应观察通道，不查找或触发 cancelControllerId；用户点击取消、服务端资源回收或配额治理触发的显式取消请求，才以 $runId$ 查询父侧登记和子侧映射，并把取消信号传播到 chat turn 请求控制器。该区分使短暂断网不会误杀长任务，同时保留主动中止和资源治理能力。

### 恢复重放、后续查看与访问控制

重连恢复流程以父侧登记表保存的展示关系、子侧可恢复流保存的原始 chunk、统一事件封包和客户端复合去重键共同实现。客户端刷新、断网重连或父 agent 重启后，父 agent 先校验当前用户仍可访问 parentConversationId，再按 displayOrder 读取保留运行；对 status=starting 的记录合成表示“父侧已登记、子侧尚未确认推理开始”的 started 事件，对 status=running 或终态记录则根据已知 startedAt 和子侧映射合成已开始事件。随后父 agent 读取 recoverableStreamId 中已持久化的 chunk，以 replay 标记重新封包，并按登记表状态补发 finished、error、aborted 或 interrupted。

恢复时以子侧运行映射作为读取原始输出的依据。若父 agent 已登记 $runId$，但崩溃发生在父侧尚未获得请求标识或流标识的窗口内，父 agent 可向对应子 agent 查询该 $runId$ 的映射，以补齐恢复所需的标识。若找不到子 agent 或找不到对应子运行，父 agent 不任意重跑同一输入，而是将父侧记录标记为 interrupted。若子 agent 已处于 completed、error 或 aborted 等终态，则父 agent 先重放子侧持久化片段，再以子侧终态更新仍为 starting 或 running 的父侧记录；若父侧已经是终态，则不被迟到状态覆盖，只记录冲突原因供诊断。

对于仍在 running 的子运行，若实现支持重新 tail，父 agent 先从子侧可恢复流重放到父侧 lastForwardedSequence 或客户端已确认 sequence 中较大的位置，再以 lastForwardedSequence+1 或相应原始流偏移重新挂接实时观察。实时 tail 的第一帧若与 replay 最后一帧具有相同 $runId$ 和 sequence，客户端按去重键丢弃；若实时终态事件与 replay 终态同时到达，以父侧先持久化的终态为准，迟到事件只记录为冲突。若实现不支持重新 tail，则父 agent 可重放已持久化片段后标记 interrupted，明确表示仅观察链路中断，而不伪造 completed。

为了支持用户在主会话中展开或跳转查看某个子 agent 的完整对话，本方案将子 agent 的访问路径与父 agent 路径嵌套，并在父 agent 上设置访问门禁。drill-in 连接前，父 agent 校验请求用户身份、tenantId、父会话归属、父会话访问权限、childAgentType 白名单、$runId$ 所属 parentConversationId、childAgentInstanceId 与登记表一致性，以及记录状态未处于 cleaning 或 deleted。任一条件不满足时拒绝连接，且不得因访问请求自动创建新的空子 agent；该机制防止猜测 URL、跨租户访问、错误类型端点访问和已清理结果被重新激活。

子 agent 输出可以作为主会话历史的可查看附属结果保留，也可以由用户删除、会话清理、retentionDeadline 到期或容量配额触发回收。保留策略与访问门禁配合，使用户在后续查看时仍通过主 agent 的授权关系定位子 agent，而不是依赖裸露的子 agent 地址。若清理与 drill-in 查看并发发生，系统可选择拒绝清理、将记录置为 cleaning 并阻止新的 drill-in、或通知既有只读连接在当前重放结束后断开；无论采用哪种策略，进入 deleted 后都拒绝新的观察和 drill-in，并且删除操作保持幂等。

### 并发、幂等、取消、清理与后台运行

子 agent 工具运行采用显式状态机管理。starting 表示父侧已预登记但子侧尚未确认推理开始；running 表示子侧映射已建立并存在可观察或可恢复的执行；completed、error、aborted 和 interrupted 为终态，分别表示正常完成、执行错误、显式取消或父侧无法安全继续观察；cleaning 表示正在回收保留结果和子侧资源；deleted 表示父侧不再允许展示、恢复或 drill-in。允许迁移包括 starting→running、starting→error/interrupted/aborted、running→completed/error/aborted/interrupted、任一终态→cleaning→deleted；禁止从 deleted 或 cleaning 回到 running，也禁止终态之间相互覆盖，迟到事件只能记录为冲突元数据。

对于需要比较、交叉验证或分解执行的请求，主 agent 可以在同一个父工具调用下并行启动多个子 agent 运行。每个子运行拥有独立 $runId$、独立子 agent 上下文和独立 sequence；父侧通过相同 parentToolCallId 把它们归入同一展示组，通过 displayOrder 保持并排或先后排列。汇总阶段以每个子运行的结构化终态、summary、errorCode、errorMessage 和 displayOrder 为输入：要求强一致的任务等待全部分支 completed，否则返回无法完成的错误；比较或研究类任务可在截止时间到达后使用已 completed 分支生成部分结论，并列出 error、aborted 或超时分支原因；用户取消单个分支时，该分支标记为 aborted，主 agent 仅在任务规则允许部分结果时继续汇总。

重复请求通过幂等键、父侧登记查询、子侧 $runId$ 映射查询、终态不可覆盖和已有输出返回共同处理。若同一编排请求因父侧重试、浏览器重连或服务端恢复而再次到达，父 agent 先按 tenantId、parentConversationId 和幂等键查询登记表；命中后再向子 agent 以 $runId$ 查询本地映射，返回当前状态和已持久化输出，而不再次调用模型。若父侧为 running 而子侧为 completed、error 或 aborted，则以子侧终态和可恢复流补齐父侧；若父侧为 aborted 而子侧仍 running，则再次传播取消并保留 aborted 意图；若父侧为 completed 而子侧迟到返回 error，则不覆盖 completed，只记录异常审计。

终态竞争按“已持久化终态优先、取消不覆盖完成、恢复不覆盖取消、观察中断不覆盖子侧确定结果”的规则收敛。completed、error、aborted 和 interrupted 中任一状态已经写入父侧后，后到的其他终态不改变 status；如果后到事件来自子侧确定结果且与父侧 interrupted 冲突，可以保留父侧 status 或按应用策略升级为子侧确定终态，但必须记录原 interrupted 原因和更新来源。starting 只表示父侧登记存在，不保证子侧已开始推理；running 才表示子侧已建立映射并可查询请求或流标识，因此恢复界面可对 starting 展示为“正在排队或启动中”，避免误导为已经产生推理输出。

同一个子 agent 实例内部可以设置单实例并发保护，避免两个 chat turn 同时写入同一转发器、请求标识或消息流。需要真正并行的任务由主 agent 创建多个独立子 agent 运行来实现，而不是在同一子 agent 实例内交错执行多个推理 turn。该边界使每个子运行的可恢复流、取消信号、最终摘要和错误信息都能准确归属到对应 $runId$。

显式取消流程以 $runId$ 为目标从父侧传播到子侧。父 agent 接收取消请求后，先校验用户、租户和父会话权限，再以条件更新方式把 starting 或 running 改为 aborted 或 canceling 意图状态，并调用子 agent 的取消接口；子 agent 依据 cancelControllerId 或 chatTurnRequestId 取消对应请求控制器，使模型推理和工具执行尽快终止，再把子侧状态收敛为 aborted。若运行已处于终态，则取消请求只返回既有状态，不改变历史结果；若父侧已记为 aborted 而子侧仍 running，恢复检查会再次发送取消，直至子侧终止或被标记为冲突待清理。

清理流程按“停止执行、阻止新访问、删除子侧、删除父侧”的顺序执行。触发清理时，父 agent 先把记录置为 cleaning 或设置清理锁，拒绝新的观察和 drill-in；若状态仍为 starting 或 running，则先执行显式取消并等待子侧映射进入 completed、error、aborted 或 interrupted 等可收敛状态，或达到治理超时后记录 terminalReason。随后删除或归档子侧运行映射、可恢复流、消息片段和子 agent 实例状态，最后删除或标记父侧登记为 deleted。每一步均以 $runId$ 和 childAgentInstanceId 为幂等键，重复清理请求遇到已删除资源时视为成功，避免半删除状态导致资源泄漏或历史列表悬挂。

本方案还允许主 agent 启动没有直接 parentToolCallId 的后台子运行，例如用户请求后的异步研究、预生成报告或定时恢复任务。后台运行仍使用同一 $runId$、运行登记、子侧映射、事件封包和恢复重放机制，只是在客户端展示时归入未绑定运行区域。当前台实时请求与后台运行竞争模型、连接或存储配额时，调度器优先保障用户正在等待的前台运行；后台运行可被限速、暂停、延后启动或按策略取消，并在父侧登记中写入资源治理原因。由此，主 agent 可以在不破坏单一会话入口的情况下管理前台工具调用和后台子任务，并在用户返回会话时继续展示这些任务的进度和结果。
