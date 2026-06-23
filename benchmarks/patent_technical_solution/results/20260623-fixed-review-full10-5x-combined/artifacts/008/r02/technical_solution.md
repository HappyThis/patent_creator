## 技术方案

### 总体架构与核心构思

现有持久化 chat agent 在被父 agent 当作工具使用时，存在一组互相牵连的技术矛盾：子任务若只作为普通工具函数返回结果，则执行过程中的流式片段难以保留，刷新或短线重连后无法重放；若把子任务过程写回父 agent 消息，又会破坏子 agent 的独立上下文并增加父侧状态耦合；若网络重试或多标签页重复触发同一子任务，可能重复启动模型推理并产生重复计费；若父侧连接断开即取消子侧执行，则长任务或后台任务容易被误杀；若父侧状态与子侧转录缺乏稳定关联，则完成、取消、恢复和清理之间会出现结果覆盖或孤儿转录。

本方案在既有持久化 chat agent 框架之上增加“保留式流式子 agent 工具编排”机制。为便于说明，本文将一次由主 agent 分派、由子 agent 独立执行并可被保留查看的工作称为“子 agent 工具运行”；将主 agent 持久存储中记录该工作的逻辑数据结构称为“父侧运行登记”；将子 agent 持久存储中记录 $runId$ 与具体 chat turn、可恢复流之间关系的逻辑数据结构称为“子侧运行映射”；将主会话中用于展示子 agent 进度的带信封消息称为“观察事件”；将取消、独占声明、清理等影响执行本身的信号称为“执行控制信号”。用户侧仍只连接并感知一个主 agent，主 agent 可以把研究、规划、比较、总结等子任务分派给一个或多个专门子 agent，并将其生命周期和输出流桥接回同一主会话视图。

在一种实现中，主 agent 通过两类入口触发该编排：其一为命令式的 $runAgentTool$，用于确定性多阶段流程、HTTP 或 callable 触发的报告、后台任务以及需要 fan-out/fan-in 的程序化逻辑；其二为面向模型工具选择的 $agentTool$，用于父级语言模型在生成过程中选择某一子 agent 作为工具执行。两类入口底层均复用已有 $subAgent(Cls,name)$ 语义创建或访问命名子 agent，不引入新的子 agent 基类；所谓 agent tool 是普通可聊天子 agent 在一次父侧操作中的编排角色。

该机制将执行通道、观察通道和控制通道分离。执行通道位于子 agent 内部，负责运行程序化 chat turn、保存 $UIMessageChunk$、维护可恢复流以及生成结构化输出或文本摘要；观察通道位于父 agent 与客户端之间，负责把子 agent 已持久化或正在产生的片段包装为观察事件并发送或重放；控制通道由父 agent 主动发起，负责启动、取消、独占声明、恢复协调和清理。浏览器 WebSocket 断开只关闭观察通道，不直接触发执行控制信号；只有父侧请求、工具调用或后台任务的显式中止才会传入子 agent 的取消接口。

### 保留式运行登记与标识映射

父侧运行登记可以采用逻辑表 $cf\_agent\_tool\_runs$ 或等价键值记录实现。其作用域为一个父 agent 会话或父持久对象，并可同时带有 $tenantId$、$userId$、$applicationId$、$parentAgentId$、$parentSessionId$、$agentType$、$runId$ 等授权和命名空间字段。$runId$ 在同一父会话、同一租户且同一子 agent 类型范围内唯一，可通过联合唯一键 $(tenantId,parentSessionId,agentType,runId)$ 或等价索引保证；后台运行的 $parentToolCallId$ 为空时仍属于同一父会话命名空间，并通过单独的未绑定运行索引查询。登记行还包含输入预览、脱敏标志、状态、摘要、错误码、错误消息、展示元数据、$displayOrder$、最后转发序号、开始时间、更新时间和结束时间。$displayOrder$ 冲突时按 $(displayOrder,startedAt,runId)$ 稳定排序，使同一父工具调用下的兄弟任务在实时和重放时顺序一致。

子侧运行映射可以采用逻辑表 $cf\_agent\_tool\_child\_runs$ 或等价记录实现，至少以 $runId$ 为主键，并保存本次执行的 $requestId$、$streamId$、子侧状态、最后持久化 chunk 序号、摘要、错误信息、开始时间和结束时间。$runId$ 是面向产品和编排的稳定标识，贯穿重放、钻取查看、取消、清理和日志；$requestId$ 是一次模型请求或 chat turn 的内部标识；$streamId$ 是可恢复流持久化的标识。子侧可在 $runId$ 上建立唯一约束，在 $streamId$ 上建立查询索引，使父侧能够从父侧运行登记定位到子 agent，再由子侧运行映射定位到准确的可恢复流，避免钻取后的追加对话覆盖原工具运行的重放来源。

子 agent 工具运行的启动按如下步骤闭合：第一，父 agent 校验父会话有效性、用户和租户授权、子 agent 类型白名单、输入模式以及 $runId$ 唯一性；第二，父 agent 在事务中插入状态为 $starting$ 的父侧运行登记，若唯一键冲突则读取已有登记并进入观察或返回既有结果；第三，父 agent 通过 $subAgent(Cls,runId)$ 创建或取得子 agent facet；第四，子 agent 在本地事务中对 $runId$ 执行唯一插入或 CAS 状态更新，只有插入成功或从可重入的 $starting$ 状态成功声明执行权的一方可以创建 $requestId$、启动 chat turn 并写入 $streamId$；第五，父 agent 收到子侧声明成功后以 CAS 将父侧状态从 $starting$ 改为 $running$。若第四步发现子侧映射已存在，则父 agent 读取该映射的状态、$requestId$、$streamId$、最后序号、摘要、错误信息以及是否可继续 live-tail，不再启动第二个模型推理。若父登记已创建但子侧声明失败且无法确认子侧是否启动，父侧保持或转入 $interrupted$，并在恢复时按 $runId$ 重新检查子侧映射，而不默认重跑。

对于由 $agentTool$ 触发的路径，父 LLM 先根据工具描述和输入模式选择子 agent 工具，框架获得 $toolCallId$ 和 $abortSignal$ 后调用命令式启动流程，并把 $toolCallId$ 写入 $parentToolCallId$。父 LLM 的工具执行结果在子 agent 工具运行达到终态后形成：$completed$ 时返回经输出模式校验的结构化输出，未配置输出模式时返回文本摘要；结构化输出校验失败时，父侧运行登记写入 $error$ 和校验错误，保留子侧原始文本摘要供查看，但返回给父 LLM 的工具结果为明确失败；$error$、$aborted$、$interrupted$ 时分别返回包含错误类型、错误消息、$runId$ 和可选摘要预览的失败对象，父 LLM 不接收空结果或伪成功摘要。若应用设置工具等待超时，超时只影响父工具结果形成，可将父侧状态写为 $interrupted$ 或返回可稍后查询的运行标识，是否取消子侧执行仍由执行控制信号决定。

### 流式事件桥接与同屏恢复

子 agent 在执行过程中产生的 chat 响应片段仍采用框架既有的 $UIMessageChunk$ 格式持久化。每个 chunk 在子侧可恢复流中按写入顺序获得单调递增的子侧序号；父侧桥接层为同一 $runId$ 生成观察事件序号时，优先复用该子侧序号，或在父侧运行登记的 $lastForwardedSequence$ 基础上以事务方式递增并记录映射，使同一 $runId$ 的实时事件和重放事件使用稳定序号。$agent\text{-}tool\text{-}event$ 信封包含 $runId$、$parentToolCallId$、$sequence$、$replay$、事件类型、事件时间、子 agent 类型以及事件体；$chunk$ 事件体完整封装原始 $UIMessageChunk$，并可附加子消息标识、chunk 类型、哈希校验值和是否为最终 chunk 的标记，但不把子 agent 文本改写为父 assistant token。

客户端侧采用无样式的 reducer 或 hook 对观察事件进行还原。reducer 维护 $runsById$、按 $parentToolCallId$ 分组的运行列表、未绑定运行列表、每个 $runId$ 已接收序号集合和最高连续序号。对于相同 $(parentToolCallId,runId,sequence)$ 的实时事件与重放事件，若事件哈希或 body 一致则丢弃后到重复项；若不一致，则以可持久化重放来源中的 chunk 为准，将差异记录为诊断状态但不重复追加。乱序事件先暂存，待缺失序号到达后再应用；超过缺失等待阈值时，reducer 可标记该运行存在 gap 并请求按 $runId$ 从指定序号重放。终态事件到达后，该运行被锁定为终态展示，终态后迟到的 $chunk$ 不再追加到可见消息，只可计入诊断或审计，避免客户端内容回退或重复增长。

当浏览器刷新、WebSocket 短断或用户稍后重新打开会话时，主 agent 先校验当前用户仍具备访问父会话和相应子 agent 工具运行的权限，再遍历父侧运行登记。对于每条登记，父 agent 合成 $started$ 观察事件，随后使用登记中的 $agentType$、$runId$ 和子侧运行映射中的 $streamId$ 定位可恢复流，从指定 $sequence$ 或起始序号读取已持久化 chunks 并重放为 $chunk$ 事件；若 $streamId$ 缺失但 $runId$ 存在，则可按子侧映射回查准确 stream；若父登记存在但子侧映射或 stream 不可读取，则根据是否能确认子侧异常分别写入 $error$ 或 $interrupted$。登记已处于终态时，父 agent 在 chunks 后合成对应终态事件；登记仍运行但当前实现不支持 live-tail 重新附着时，父 agent重放已保存 chunks 后将父侧登记 CAS 写入 $interrupted$，并可按策略向子侧发送取消以停止继续消耗资源，或允许子侧自然结束但不再把后续 chunks 展示到该父侧运行。

### 并发编排与后台运行

主 agent 可以在一次父请求中同时启动多个子 agent 工具运行，例如将两个研究问题并行分派给同类 Researcher 子 agent，或将资料整理、方案规划、风险比较分派给不同类型的子 agent。每个运行具有独立 $runId$、独立子 agent 上下文和独立输出流，父侧仅通过 $parentToolCallId$ 与 $displayOrder$ 组合成同一父工具调用下的兄弟任务；当多个兄弟任务的 $displayOrder$ 相同，按 $startedAt$ 和 $runId$ 作稳定排序。父 agent 可使用 $Promise.allSettled$ 或等价聚合机制等待多个子运行终态，并形成包含 $runId$、$agentType$、$status$、$summary$、$output$、$error$ 和 $displayOrder$ 的数组式聚合结果；部分成功部分失败时，不丢弃成功项，也不把整体伪装为成功，而是向父 LLM 或调用方返回带有逐项状态的聚合对象。

对于无需由父 LLM 工具调用直接承载的任务，主 agent 可通过命令式入口启动无 $parentToolCallId$ 的后台子 agent 工具运行。此类运行仍写入父侧运行登记并保留子侧转录，客户端或服务端可通过按父会话、租户和用户过滤的查询接口获取未绑定运行列表，列表项包括 $runId$、$agentType$、状态、输入预览、摘要、错误信息、开始时间、更新时间和可继续重放的最后序号；也可按 $runId$ 查询单个运行的状态和从指定序号开始的已保存 chunks。这样，同一主会话中可以同时存在“本轮回答需要的子 agent 工具运行”和“后续可查看的后台运行”，二者使用同一套标识、观察事件、恢复、取消和清理规则。

为了避免并发破坏子 agent 的独立执行上下文，同一 $runId$ 对应的框架驱动 chat turn 在子 agent 实例上持有独占声明，该声明可由子侧运行映射的唯一键、运行中状态 CAS 或短期租约字段实现。重复启动请求无法取得执行权时，返回已有运行的检查结果，包括当前状态、$requestId$、$streamId$、最后序号、摘要、错误信息、是否终态以及是否支持 live-tail。若用户通过钻取界面直接连接该子 agent 并尝试发送新消息，系统在独占声明释放前拒绝或排队该消息，拒绝结果应说明该子 agent 正在执行框架驱动运行，避免用户消息与工具运行交错写入同一 transcript。

父侧可以配置最大并发子 agent 工具运行数。启动前，父 agent 在同一父会话或租户范围内统计状态为 $starting$ 或 $running$ 的登记数量，若已达到 $maxConcurrentAgentTools$，则不创建子 agent facet，而是写入或返回一条状态为 $error$ 的父侧运行登记，并向客户端发送 $error$ 观察事件，错误码可为 $CONCURRENCY\_LIMIT\_EXCEEDED$。该拒绝同样返回给父 LLM 或命令式调用方，使成本控制、用户反馈和运行登记保持一致。

### 恢复、取消、访问控制与清理

本方案采用明确的运行状态机管理恢复与终态。父侧登记中的状态至少包括 $starting$、$running$、$completed$、$error$、$aborted$ 和 $interrupted$，其中 $completed$、$error$、$aborted$、$interrupted$ 为终态。终态由父侧登记行权威记录，迟到的取消请求不得把已完成或已失败的运行改写为中止，恢复过程也不得把已中止运行改写为中断。$interrupted$ 由父侧在无法安全继续观察或恢复时产生，用于如实告知用户存在已发生但不能继续完整承接的父侧执行断点。

| 目标状态 | 允许前置状态 | 触发事件 | 写入主体 | 规则 |
| --- | --- | --- | --- | --- |
| $starting$ | 无登记 | 父侧通过授权、输入校验和并发检查后创建父侧运行登记 | 父 agent | 插入失败表示重复启动，转入读取既有登记路径 |
| $running$ | $starting$ | 子侧运行映射声明成功并返回 $requestId$ 或 $streamId$ | 父 agent | 通过 CAS 从 $starting$ 更新，失败则读取现有终态 |
| $completed$ | $running$ | 子 agent 正常完成并返回摘要或结构化输出 | 父 agent | 终态不可逆，写入摘要、输出引用和结束时间 |
| $error$ | $starting$ 或 $running$ | 输入/输出校验失败、子 agent 明确异常或并发阈值拒绝 | 父 agent 或子 agent 通过父侧协调写入 | 保留错误码和错误消息，必要时保留原始文本摘要供查看 |
| $aborted$ | $starting$ 或 $running$ | 父侧主动操作发出执行控制取消信号且子侧确认中止 | 父 agent | 迟到取消不得覆盖已写入的完成或错误 |
| $interrupted$ | $starting$ 或 $running$ | 父侧恢复时无法确认子侧状态、无法恢复观察链路或安全承接父工具调用 | 父 agent | 表示父侧不能继续完整承接，不等同于子 agent 明确异常 |

父 agent 重启或恢复时，对仍处于 $starting$ 或 $running$ 的登记逐条协调。恢复前置条件包括父侧运行登记存在、当前用户和租户仍有访问权限、子 agent 类型仍被允许、子侧运行映射存在且 $streamId$ 可读取。若不存在匹配的子 agent 或子侧运行映射，父侧通过 CAS 将登记写入 $interrupted$；若子侧明确报告异常，则写入 $error$；若子侧报告完成或中止，则先重放子侧已保存 chunks，再写入 $completed$ 或 $aborted$。若子侧仍在运行但当前实现不支持晚到 live-tail 重新附着，父侧可先重放已有 chunks，再将父侧登记写入 $interrupted$，并根据运行策略选择立即发送取消以停止后续成本，或允许子侧自然结束但阻止其后续 chunks 进入该父侧可见时间线。

终态写入采用 CAS 或等价版本条件，条件为当前状态仍属于 $starting$ 或 $running$。当完成事件、错误事件、取消信号、恢复判定或清理回调并发到达时，先成功写入 $completed$、$error$、$aborted$ 或 $interrupted$ 的一方成为权威终态，后到事件只记录审计日志或诊断信息，不覆盖终态摘要、错误和结束时间，也不得把终态改回 $running$。错误与中断的区分依据为：子 agent 或输出校验明确返回异常时为 $error$；用户、系统或父侧主动操作显式中止时为 $aborted$；父侧无法确认子侧状态、无法恢复观察链路或无法安全承接父 LLM 工具调用时为 $interrupted$。

访问控制通过父 agent 的子 agent 路由前置钩子实现。外部钻取请求到达路径中指定的 $(agentType,runId)$ 时，父 agent 先解析当前身份，校验 $tenantId$、$userId$、$applicationId$、$parentSessionId$ 与父侧运行登记中的字段一致，并确认登记中的 $agentType$ 与路径中的子 agent 类型匹配、运行未被清理或墓碑禁止访问；只有全部通过时，请求才被放行到对应子 agent。内部 $subAgent$ 调用携带框架内部上下文，可绕过外部门禁以完成启动和恢复，但仍应受父 agent 的类型白名单和会话作用域限制。嵌套子 agent 工具运行可将父层 $runId$ 作为命名空间前缀或在登记中记录 $parentRunId$，使不同层级、不同父会话或不同租户的 $runId$ 不发生碰撞，权限也沿父会话和父运行链路继承。

取消控制沿父侧主动操作传播，而不是沿观察连接传播。父 chat turn、工具调用或后台请求收到显式中止信号时，$runAgentTool$ 按 $runId$ 调用子侧取消接口；子 agent 中止本次 turn 的 $AbortController$，并将该信号传入 $saveMessages$ 或等价的程序化 chat turn 入口，使模型推理循环尽快停止并报告 $aborted$。相反，浏览器断开、父连接重放失败或观察者取消读取仅表示观察脱离，不应自动取消子 agent 执行，以免刷新页面或网络短断导致后台任务被误杀。

运行结果默认保留，以支持刷新后重放、完成后钻取查看、失败诊断和审计。清理的前置条件包括当前主体具备删除权限，且能够通过锁、租约或 CAS 取得清理权。清理流程按顺序执行：先将父侧运行登记标记为 $deleting$ 或写入清理租约以阻止新观察者和新启动复用；若运行仍为 $starting$ 或 $running$，先按 $runId$ 发送取消并等待终态确认或等待超时；随后删除子侧 chunks、transcript 和子侧运行映射，再删除对应子 agent facet；最后删除父侧运行登记，或保留只含 $runId$、删除时间和禁止重放标志的 tombstone。若中途删除失败，父侧登记保留 $deleting$ 或 $cleanup\_failed$ 诊断状态和重试所需的子 agent 类型、$runId$、$streamId$，后续清理任务可幂等继续，避免出现父侧已不可见但子侧仍残留的孤儿数据。

### 实施边界与扩展

该方案适用于能够由程序触发 chat turn、输出 $UIMessageChunk$、将 chunks 写入可恢复流、接受外部 $AbortSignal$ 且能按 $runId$ 检查和取消的 chat agent 家族。初始实现可以优先适配已有 Think 类 agent；在另一实现中，也可以通过隐藏的子适配层支持 AIChatAgent 或其他可聊天 agent，而不要求应用开发者继承新的 AgentTool 基类。

在输出契约方面，命令式入口可以通过调用点类型约束或应用自定义校验约束输入输出；$agentTool$ 入口应提供输入模式供父 LLM 工具选择和运行时校验，并可选择提供输出模式。若配置了输出模式，子 agent 需要通过显式结构化输出或适配器返回满足该模式的数据；校验失败时不自动从 prose 中二次抽取结构化数据，而是将子 agent 工具运行标记为 $error$，向父 LLM 返回包含校验错误的失败对象，并保留原始文本摘要供用户钻取查看。该规则减少隐藏模型解析带来的额外成本、延迟和不确定失败模式。

该机制还为后续扩展预留边界：子 agent 可以继续嵌套创建自己的子 agent；运行登记和事件协议中的 $runId$、$sequence$ 与观察/执行分离设计可支持未来 live-tail 重新附着；生命周期钩子可用于计量、日志和成本控制；父侧也可设置最大并发数，在超过阈值时生成明确错误事件。上述扩展均不改变用户只与主 agent 对话、子 agent 独立执行并将保留式流式过程带回主会话视图的核心结构。
