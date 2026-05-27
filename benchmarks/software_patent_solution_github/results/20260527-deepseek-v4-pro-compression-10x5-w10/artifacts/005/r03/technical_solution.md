## 技术方案

本技术方案在Think智能体框架现有架构基础上，提出一种面向外部系统的异步任务提交与执行机制。该机制使外部调用方（如Webhook处理器、RPC调用方、后台Worker）能够将对话任务提交至Think智能体后立即获得"已接收"确认，无需等待大语言模型推理完成。方案重点解决"快速确认接收"与"模型推理最终完成"之间的持久状态边界问题，覆盖重复提交去重、消息写入前后崩溃恢复、执行领取与恢复、状态查询、取消及清理等完整生命周期。

该方案建立在Think现有的Session消息存储体系、TurnQueue串行执行队列、AbortRegistry取消机制、ResumableStream流式恢复以及runFiber持久化执行框架之上，不重写普通聊天的消息保存路径，而是新增一条独立的异步任务通道，通过新增的任务状态表和增强的执行调度逻辑实现对外部触发场景的完整支持。

### 整体架构

系统在Think智能体内部新增一条异步任务通道，与现有WebSocket实时聊天通道并行运行。整体架构包含以下核心组件：(1) 任务接收层——在Agent的onRequest或onRPC入口处解析外部调用请求，提取任务参数和幂等键；(2) 任务状态表（think_tasks）——持久化存储每条异步任务的生命周期状态；(3) 任务调度器——负责在TurnQueue的基础上协调异步任务的领取、执行和完成；(4) 状态查询接口——对外暴露任务执行状态的查询端点。

异步任务的数据流如下：外部调用方通过HTTP请求或RPC调用向Think智能体提交任务，智能体在验证请求合法性后，将任务元信息（包括幂等键、用户消息内容、回调通知配置等）写入think_tasks表，同时将用户消息通过Session.appendMessage()写入assistant_messages表，这两步在同一数据库事务中完成。写入成功后立即向调用方返回包含任务标识的"已接收"响应。随后任务调度器异步领取该任务，通过TurnQueue排队执行模型推理，推理过程中的流式输出通过ResumableStream缓冲；推理完成后更新任务状态为已完成，并可选地向外部回调地址发送通知。

### 任务提交与快速确认机制

外部调用方通过Agent的onRequest方法或RPC callable方法提交任务。请求中携带：待执行的对话消息（用户输入文本或结构化UIMessage）、幂等键（由调用方生成，用于去重）、可选的回调通知URL及可选的任务优先级标记。智能体接收到请求后，执行以下快速确认流程：

1. 解析请求并提取幂等键，查询think_tasks表是否已存在相同幂等键的任务。若存在且已完成，直接返回已完成结果；若存在且未完成，返回当前状态及任务ID。
2. 若幂等键不存在，在同一个SQLite事务中执行两步写入：(a) 调用session.appendMessage()将用户消息持久化到assistant_messages表，消息采用树形结构存储，parent_id指向当前会话的最新叶子节点；(b) 在think_tasks表中插入一条新记录，状态为PENDING，同时存储幂等键、用户消息ID、回调配置及创建时间戳。
3. 事务提交成功后，立即向调用方返回HTTP 202或RPC响应，包含任务ID和状态PENDING。总耗时控制在数毫秒级别。

该设计的关键在于：用户消息的持久化与任务记录的创建在同一数据库事务中原子完成。这意味着在任何崩溃场景下，不会出现"任务已记录但消息丢失"或"消息已写入但任务未登记"的不一致状态。事务提交是快速确认的前置条件——只有数据确已落盘，才向调用方返回"已接收"。这与普通WebSocket聊天的消息保存路径（同样使用session.appendMessage()）完全兼容，差异仅在于普通聊天路径不生成任务记录，而是由TurnQueue直接驱动推理执行。

### 持久化任务状态机

think_tasks表是持久化任务状态机的存储载体，其核心字段设计如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | TEXT PRIMARY KEY | 系统生成的任务唯一标识 |
| idempotency_key | TEXT UNIQUE | 调用方提供的幂等键，用于去重 |
| status | TEXT NOT NULL | 任务状态：PENDING/CLAIMED/EXECUTING/COMPLETED/FAILED/CANCELLED |
| user_message_id | TEXT | 关联的assistant_messages记录ID |
| assistant_message_id | TEXT | 模型回复消息ID（完成后填充） |
| callback_url | TEXT | 可选的回调通知URL |
| error_message | TEXT | 失败时的错误信息 |
| claimed_at | DATETIME | 领取时间戳 |
| created_at | DATETIME | 创建时间戳 |
| completed_at | DATETIME | 完成时间戳 |

任务状态遵循严格的状态机转换规则：PENDING（已接收，等待执行）→ CLAIMED（已被执行器领取）→ EXECUTING（模型推理进行中）→ COMPLETED（推理成功完成）/ FAILED（推理失败）。任意非终态（PENDING/CLAIMED/EXECUTING）可被取消，转换至CANCELLED。每条状态转换均在SQLite中通过UPDATE语句立即持久化，确保DO崩溃或休眠后状态可恢复。

### 幂等性与重复提交去重

重复提交去重通过幂等键（idempotency_key）实现。调用方在每次提交时携带一个全局唯一的幂等键；智能体在think_tasks表中对该字段建立UNIQUE约束，从根本上防止重复任务记录的创建。去重逻辑分为三种情形：

- 情形一：幂等键不存在——正常创建PENDING任务，返回任务ID。
- 情形二：幂等键存在且任务处于终态（COMPLETED/FAILED/CANCELLED）——直接返回已有结果，包括assistant_message_id和完成状态，不创建新任务，不重复写入用户消息。
- 情形三：幂等键存在且任务处于非终态（PENDING/CLAIMED/EXECUTING）——返回当前任务ID和状态，让调用方自行通过状态查询接口轮询或等待回调。

为防止幂等键的无限积累，系统对幂等键设置保留窗口（默认24小时）。超过保留窗口的历史任务记录在查询时仍可返回结果（用于幂等响应），但在compaction或定期清理时其幂等键可被回收。调用方如需对超过保留窗口的请求重新执行，应更换新的幂等键。该设计与Stripe等主流API的幂等键语义一致，确保重试安全且不产生副作用。

### 崩溃恢复与执行领取

崩溃恢复是本方案的核心难点之一。Think智能体运行在Durable Object（DO）环境中，可能因代码更新、运行时重启或空闲超时而被驱逐。当DO重新激活时，需要正确恢复中断的异步任务。方案复用并扩展Think现有的runFiber持久化执行机制来实现安全的执行领取与恢复。

执行领取（Claim）机制采用条件UPDATE实现并发安全：任务调度器执行 UPDATE think_tasks SET status='CLAIMED', claimed_at=NOW() WHERE status='PENDING' ORDER BY created_at LIMIT 1 RETURNING *。SQLite的原子UPDATE保证多个潜在的执行上下文（如DO激活后的恢复检查、新任务到达时的立即调度）之间不会重复领取同一任务。领取成功后，任务进入EXECUTING状态，调度器调用Think现有的onChatMessage流程执行模型推理。推理过程中，assistant消息的流式持久化由ResumableStream和现有的_persistAssistantMessage机制处理，无需修改。

DO重启后的恢复流程如下：在onStart生命周期中，系统扫描think_tasks表中所有状态为CLAIMED或EXECUTING的记录。这些记录代表上一次DO实例在执行中被驱逐。对于CLAIMED状态的任务（已领取但尚未开始推理），将其重置回PENDING状态，由任务调度器重新领取。对于EXECUTING状态的任务（推理进行中被驱逐），检查对应会话中是否已存在完整的assistant回复消息——若存在，将任务标记为COMPLETED；若不存在，利用runFiber机制重新启动推理执行，并根据对话历史的树形结构（parent_id链）判断是否需要追加消息还是重建整个推理过程。恢复逻辑实现在onFiberRecovered钩子中，与Think现有的fiber恢复框架完全一致。

### 状态查询接口

系统对外暴露两类查询接口，供外部调用方获取任务执行状态：

- 按任务ID查询：通过RPC callable方法或HTTP GET端点，传入任务ID，返回任务的当前状态、创建时间、完成时间及assistant_message_id（若已完成）。查询直接从think_tasks表读取，对推理执行无影响。
- 按幂等键查询：通过幂等键查询任务状态，与提交时的去重查询使用同一路径，返回相同结构。
- 批量状态查询：通过SessionManager的list机制或自定义查询，列出当前智能体实例下所有异步任务及其状态摘要，支持按状态过滤和时间范围过滤。

### 取消与清理机制

取消操作允许外部调用方终止一个尚未完成的异步任务。取消流程如下：调用方通过RPC或HTTP端点发送取消请求（携带任务ID或幂等键），系统在think_tasks表中执行条件UPDATE：UPDATE think_tasks SET status='CANCELLED' WHERE id=? AND status IN ('PENDING','CLAIMED','EXECUTING')。若更新成功（affected rows > 0），表示任务被成功取消。同时，若任务正处于EXECUTING状态，系统通过AbortRegistry触发对应推理流的abort信号，中断正在进行的模型调用。被取消的任务保留在think_tasks表中，其assistant_message_id为空。取消操作是幂等的——重复取消已取消的任务不会产生副作用。

清理操作移除任务记录及其关联的会话消息。清理通过Session的clearMessages或deleteMessages完成消息侧清理，同时从think_tasks表中删除对应记录。对于已完成任务的清理，支持基于时间窗口的策略（如清理7天前的已完成任务），通过定时调度或手动触发。清理操作在执行前检查任务状态——仅终态（COMPLETED/FAILED/CANCELLED）任务可被清理；对非终态任务的清理请求将被拒绝，需先取消再清理。清理同样通过数据库事务保证原子性：删除任务记录和删除关联消息在同一事务中完成。

取消与迟到的推理完成之间的竞态处理：当取消请求到达时，若推理已接近完成，可能出现AbortSignal发送与streamText自然完成之间的竞态。系统通过以下机制处理：在persistAssistantMessage写入前，再次检查think_tasks表中任务状态是否为CANCELLED——若已取消，丢弃推理结果，不更新assistant_message_id，保持状态为CANCELLED。该检查与消息持久化在同一事务中完成，确保不会出现"已取消的任务被错误标记为完成"的不一致情况。

会话清除（clearMessages）与异步任务的联动：当外部调用方或WebSocket客户端触发会话清除操作时，系统自动将当前会话关联的所有非终态异步任务标记为CANCELLED。这一联动通过Session.clearMessages的调用链触发——清除消息前，先查询think_tasks表中状态为非终态且user_message_id属于当前会话的记录，批量更新为CANCELLED，再执行消息清除。该设计确保清除会话后不会出现"孤儿任务"（已无对应会话消息但仍处于PENDING/EXECUTING状态的任务）继续消耗资源。

### 与现有系统的集成关系

本方案的设计原则是增量叠加而非重写。新增的异步任务通道与Think现有各组件的集成关系如下：

Session消息存储：异步任务的用户消息和assistant回复消息均通过现有的session.appendMessage()和session.updateMessage()写入assistant_messages表，消息以树形结构（parent_id）组织，完全复用Session的FTS5全文搜索、compaction压缩覆盖层和上下文块机制。普通WebSocket聊天的消息保存路径不受任何影响。

TurnQueue串行执行：异步任务领取后，通过现有的TurnQueue排队执行模型推理，确保同一会话内的消息按序处理，避免并发推理导致的会话状态错乱。TurnQueue的generation tracking机制确保恢复后的任务不会与当前活跃的WebSocket发起的推理产生冲突。

AbortRegistry取消机制：取消异步任务时，若推理正在进行，通过AbortRegistry查找对应的AbortController并发送中止信号，中断streamText调用。该路径与WebSocket客户端的stop操作使用相同的底层机制。

ResumableStream流式恢复：异步任务的模型推理过程同样受益于ResumableStream的chunk缓冲与重放能力。若DO在推理过程中被驱逐，已生成的流式chunk不会丢失；DO恢复后，通过STREAM_RESUMING/STREAM_RESUME_ACK握手协议重建流式连接，推理继续。该机制对异步任务透明——任务仅关心最终结果，流式中间态的恢复由框架层自动处理。

runFiber持久化执行：异步任务的执行领取和恢复流程直接构建在runFiber框架之上。每个异步任务对应一个fiber，fiber的快照（stash）中保存任务ID和当前状态。DO驱逐后的恢复通过onFiberRecovered钩子触发，与Think现有的chatRecovery机制共享恢复基础设施。think_tasks表的状态字段与cf_agents_runs表的fiber记录协同工作：前者面向外部调用方的可观察状态，后者面向框架内部的执行恢复。

SQLite表所有权划分：新增的think_tasks表由Think智能体直接管理，存储任务生命周期状态。assistant_messages、assistant_compactions、assistant_fts、assistant_config等现有表继续由Session管理，不受异步任务通道的影响。think_config表继续存储Think私有的配置信息。cf_agents_runs表继续由runFiber框架管理执行快照。该表所有权划分确保异步任务通道可以在不侵入Session内部实现的前提下，充分利用Session提供的消息持久化、搜索和压缩能力。

### 回调通知机制

为减少外部调用方的轮询开销，系统支持可选的任务完成回调通知。调用方在提交任务时提供callback_url，当任务进入终态（COMPLETED/FAILED/CANCELLED）时，系统向该URL发送HTTP POST请求，携带任务ID、终态状态、完成时间戳及assistant_message_id（若成功）。回调发送采用指数退避重试策略（复用Think的retry机制，默认最多3次），确保在网络抖动时可靠送达。回调发送失败不影响任务本身的终态判定——任务状态已在数据库中持久化为终态，回调仅是最佳努力通知。
