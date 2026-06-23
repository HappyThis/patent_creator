## 技术方案

### 总体技术构思

本方案在既有对话 agent 的消息持久化、流式响应和持久执行能力之上，增加面向外部系统的任务接收层、任务登记表、异步执行器和恢复协调器。外部系统通过 webhook、RPC 或其他程序化接口提交一次对话任务时，接收层不直接等待模型推理完成，而是先把该提交转换为一个持久化任务记录，并在登记成功后立即返回“已接收”确认；后续的用户输入写入、对话 turn 执行、模型流式输出、取消和恢复均围绕该任务记录推进。

任务登记表用于把外部可见的任务标识与内部对话执行标识解耦。外部可见标识可以包括调用方提供的幂等键、系统生成的 $runId$ 或由规范化请求内容计算得到的请求指纹；内部标识可以包括本次对话 turn 的 $requestId$、流恢复标识 $streamId$、持久执行实例标识以及恢复快照位置。通过该映射，系统能够在外部重复提交、调用方超时重试、agent 休眠或重启后，以同一个任务记录作为状态判断和恢复处理的权威入口，而不是以某一次 HTTP 连接或某一次内存中的执行对象作为权威依据。

### 任务记录与状态模型

任务登记表中的每条记录对应一次外部对话任务，并作为外部查询、取消、清理和恢复的公共状态源。任务记录至少包括 $runId$、tenant 或 $callerId$、$idempotencyKey$、$requestFingerprint$、$payloadHash$、$conversationId$、$requestId$、$streamId$、$userMessageId$、$persistentExecutionId$、$status$、$statusVersion$、$executionEpoch$、$workerOwner$、$leaseUntil$、$attemptNo$、$cancelGeneration$、$cleanupGeneration$、$snapshotPointer$、$lastChunkSeq$、$terminalAt$、$errorCode$、$abortReason$、$retentionUntil$、$createdAt$ 和 $updatedAt$。其中外部标识、会话标识、请求摘要和创建时间在登记时写入；内部执行标识、消息标识、租约和快照指针在执行中条件更新；终态时间、错误码和取消原因进入终态后不得被迟到事件覆盖。

本方案将版本类字段固定为不同用途：$statusVersion$ 表示任务状态行的乐观锁版本，每次状态变化递增；$executionEpoch$ 表示当前允许写消息、写 chunk 和写最终结果的执行代次，清理、重置或恢复重新调度时递增；$cancelGeneration$ 表示取消请求代次，执行分支在模型回调、chunk 落盘和终态写入前均需读取并比对；$cleanupGeneration$ 表示清理代次，用于阻断清理前的旧分支继续写回。写入方必须同时满足状态、所有权、$statusVersion$ 或 $executionEpoch$ 条件，任一条件不匹配即按 stale 分支退出。

| 状态 | 含义与允许转移 |
| --- | --- |
| received | 任务已登记但尚未被执行器领取；可转为 starting、running、aborted、cleanupPending。 |
| starting | 执行器正在创建内部标识或入队；可转为 running、interrupted、aborted、cleanupPending。 |
| running | 已取得执行所有权并可能写入消息或调用模型；仅持有匹配 $executionEpoch$ 和租约的分支可转为 completed、error、aborted、interrupted 或 cleanupPending。 |
| completed | 模型结果已形成并写入最终 assistant 消息；属于终态，只能被查询或按清理规则转为 cleaned tombstone。 |
| error | 不可重试参数错误、超过重试上限的模型错误或确定性持久化失败形成的终态；只能被查询或清理。 |
| aborted | 显式取消生效或清理前取消完成形成的终态；只能被查询或清理。 |
| interrupted | 恢复材料不足、续接失败或执行所有权丢失但不能安全判定为 error 时形成的非成功状态；可由恢复协调器重新领取或由上层处理为 error、aborted、cleanupPending。 |
| cleanupPending | 清理已请求但仍需取消运行分支或等待租约过期；可转为 aborted、interrupted 或 cleaned。 |
| cleaned | 清理后的墓碑状态，保留 $runId$、幂等键摘要、清理时间和 $retentionUntil$，用于在保留期内识别重复提交。 |

### 外部任务接收与幂等登记

请求规范化采用确定性算法。接收层把会话标识、调用方标识、用户输入文本、工具或模型参数、自定义 body 中参与推理的字段、附件元数据和附件内容摘要组成规范化对象；字段按字典序排序，空值和未提供字段统一表示为固定空标记，数组按调用方提交顺序保留，附件不直接进入指纹而是以文件名、大小、内容哈希和媒体类型的有序摘要参与计算。接收层对该规范化对象计算 $payloadHash$，并结合 tenant 或 $callerId$、$conversationId$ 和任务类型生成 $requestFingerprint$。当调用方同时提供 $idempotencyKey$ 时，唯一匹配优先使用 $idempotencyKey$；未提供时使用 $requestFingerprint$。若相同 $idempotencyKey$ 命中既有记录但 $payloadHash$ 或 $requestFingerprint$ 不一致，系统返回 conflict 或固定错误状态，且不得沿用旧任务执行新的用户输入。

外部任务接收层在收到提交请求后，先按上述算法得到 $payloadHash$ 和 $requestFingerprint$，再在任务登记表上执行原子登记。任务登记表对 tenant 或 $callerId$ 与 $idempotencyKey$ 的组合设置唯一约束，并对未提供幂等键的场景在 tenant 或 $callerId$、$conversationId$ 与 $requestFingerprint$ 的组合上设置唯一约束。接收层在一个事务中执行 insert-if-absent 或 upsert：插入成功时创建状态为 received 的任务记录；命中唯一冲突时读取既有记录并比对 $payloadHash$，一致则返回既有 $runId$ 和状态，不一致则返回 conflict。该登记事务只创建或读取任务记录，不生成新的对话 turn，不追加用户消息，也不调用模型。

幂等登记先于内部 $requestId$ 生成和消息写入完成。登记成功后，接收层仅投递包含 $runId$ 的后台执行信号，并向外部系统返回 accepted 确认，确认内容包括 $runId$、当前 $status$、$statusVersion$、查询方法和冲突或去重提示。若后台队列投递失败，任务仍保持 received 状态，可由定时扫描或下一次恢复扫描重新投递；若队列重复投递，同一 $runId$ 仍需通过执行领取规则竞争所有权，未取得所有权的分支不得写消息或调用模型。由此，不重复追加用户输入由唯一约束、登记先于 $requestId$ 生成、以及后续 $userMessageId$ 持久化三者共同保证。

### 异步对话执行与状态推进

异步执行器以 $runId$ 领取任务。领取采用条件更新：仅当 $status$ 属于 received、starting 或 interrupted，且 $workerOwner$ 为空或 $leaseUntil$ 已过期，且任务未处于终态或 cleaned tombstone 时，执行器才可把 $status$ 更新为 running，写入新的 $workerOwner$、$leaseUntil$、$attemptNo$、$statusVersion$ 和当前 $executionEpoch$。条件更新成功的分支获得执行所有权；领取失败、租约过期后未续约成功或发现 $executionEpoch$ 已变化的分支不得追加消息、写入 chunk 或调用模型，只能退出或记录 stale。

取得所有权后，执行器在同一会话维度进入 turn 队列，队列键为 $conversationId$，同一会话内任务按创建时间排序，必要时可在同一创建时间内按调用方优先级或序号稳定排序。前序任务处于 running 或正在取消时，后序任务等待；前序任务被标记为 interrupted 且不能自动恢复时，后序任务默认进入 pending 等待上层处理或显式跳过策略，避免在上下文不确定时继续追加新的对话 turn。队列只串行化同一会话，不阻塞其他会话的任务。

$requestId$ 和 $streamId$ 在首次允许执行时一次性生成并持久化到任务记录，恢复、重复投递或租约换主时必须读取既有值，不得重新生成。执行器追加用户消息成功后，把返回的 $userMessageId$ 写入任务记录；后续再次进入执行流程时，若 $userMessageId$ 已存在，则跳过用户消息追加，直接进入模型调用或恢复判定阶段。若写用户消息成功但更新 $userMessageId$ 失败，恢复协调器通过消息存储中的 $runId$ 或 $requestId$ 关联反查并补写 $userMessageId$，不能再追加一条相同用户消息。

模型流式输出以 chunk 形式持久化。每个 chunk 至少记录 $chunkId$、$runId$、$requestId$、$streamId$、$executionEpoch$、$chunkSeq$、$offset$、内容片段、$isFinal$ 和写入时间，并在 $requestId$、$streamId$、$executionEpoch$、$chunkSeq$ 上设置唯一约束。重复 chunk 按唯一约束去重；乱序 chunk 可先落盘但查询时标记 partial-unordered；存在序号缺口时，查询结果只返回最后连续 $chunkSeq$ 之前的可确认 partial，并暴露缺口状态；旧 $executionEpoch$ 或已进入终态后的迟到 chunk 被丢弃或仅记录为 stale，不得参与最终消息聚合。

最终 assistant 消息由模型最终响应或连续 chunk 聚合得到。写入最终消息时，执行器必须仍持有匹配的 $workerOwner$、未过期租约、当前 $executionEpoch$，且任务未被取消、清理或置为终态；写入成功后记录 assistantMessageId、$terminalAt$，并把 $status$ 条件更新为 completed。若取消、错误或恢复分支已先一步通过条件更新写入终态，则正常完成分支的后续输出按 stale 处理；若最终 assistant 消息已经存在，任何迟到 chunk 或迟到最终响应均不得覆盖该消息。旧执行不覆盖新会话的效果由 $executionEpoch$、$cleanupGeneration$ 和终态条件写入共同保证。

错误状态按可恢复性分类处理。参数缺失、权限不匹配、幂等冲突等确定性错误进入 error；供应商临时错误、队列投递失败或锁超时在重试预算内保持 received、starting 或 interrupted 以便再次领取，超过预算后进入 error；持久化写入失败若发生在登记前则不返回 accepted，若发生在消息或 chunk 写入后则依靠已持久化标识恢复补偿；取消导致的模型中止进入 aborted；供应商续接失败、快照损坏、chunk 缺口无法闭合但又不能确认任务失败时进入 interrupted。每个错误推进均通过 $statusVersion$ 条件更新完成，失败的错误写入分支不得覆盖其他分支已经写入的终态。

### 查询、取消与清理

状态查询接口以 $runId$ 或调用方幂等键为入口，只读任务登记表、内部标识映射、消息存储和流片段存储，不在查询请求链路中领取任务、追加消息或启动模型调用。若查询发现非终态任务长时间无租约、租约过期或存在可恢复执行行，可以向独立 housekeeping 队列提交恢复扫描信号；该信号只包含 $runId$ 和观察到的版本，是否恢复由后台恢复协调器再次条件判断。由此，查询断开、轮询超时或重复查询只影响观察者，不影响执行生命周期；只有取消接口写入新的 $cancelGeneration$ 才表达取消意图。

取消接口只在收到外部系统明确取消指令时生效。取消请求以 $runId$ 为入口，在条件更新中写入递增后的 $cancelGeneration$、$abortReason$、$cancelRequestedAt$ 和 $statusVersion$；若任务尚未写入用户消息，则直接转为 aborted；若用户消息已写入但模型尚未启动，则保留该用户消息并可写入取消标记或状态说明，随后转为 aborted；若模型调用正在进行，则根据 $requestId$ 定位 AbortController 或等效中止句柄并发送 abort 信号。执行分支在写 chunk、处理模型回调和写最终消息前必须重新读取 $cancelGeneration$，发现取消代次变化时停止落盘并转为 aborted。

模型完成与取消同时发生时，以先完成条件终态更新的分支为准：若 completed 已写入，迟到取消返回既有 completed，不改写结果；若取消先递增 $cancelGeneration$ 并成功转入 aborted，模型后续 token 或最终响应即使继续到达，也因取消代次或 $executionEpoch$ 不匹配而不能写入。若 abort 信号发送失败或供应商仍继续返回 token，系统仍以任务登记表中的取消代次作为落盘边界，而不是依赖供应商是否实际停止生成。

清理接口用于删除或隐藏已完成任务的登记信息、输出片段和过期观察数据。清理非终态任务时，系统先把任务条件更新为 cleanupPending，递增 $cleanupGeneration$ 和 $executionEpoch$，并发起取消流程；在取消完成、租约过期或等待超时后，系统将记录转为 cleaned tombstone 或执行延迟硬删除。若孤儿模型调用无法停止、执行行无法获取锁或取消确认超时，递增后的 $executionEpoch$ 与 $cleanupGeneration$ 已阻断旧分支写回，因而可以先软删除可见结果并保留墓碑。墓碑在 $retentionUntil$ 前保留幂等键摘要、$payloadHash$ 和清理时间；相同幂等键在保留期内再次提交时返回 cleaned 或 conflict，不重新执行旧输入。

### 中断恢复与一致性判定

当 agent 因休眠、运行时重启、代码更新或执行超时而中断时，恢复协调器在下一次激活、定时 housekeeping 或异步恢复信号触发时扫描非终态任务和持久执行登记行。扫描时先排除 $terminalAt$ 不为空或 status 属于 completed、error、aborted、cleaned 的记录；再校验 $workerOwner$、$leaseUntil$、$persistentExecutionId$、$snapshotPointer$、$requestId$、$streamId$、$userMessageId$、$lastChunkSeq$ 和 $executionEpoch$。恢复协调器只有在通过条件更新取得新的执行所有权并递增恢复相关的 $executionEpoch$ 后，才可以重新调度对话执行。

恢复阶段按字段判定执行动作：$userMessageId$ 为空且状态为 received 或 starting，表示已登记未写消息，重新进入会话队列；$userMessageId$ 已存在但 $persistentExecutionId$ 为空，表示用户消息已写入但模型尚未启动，复用原 $requestId$ 和 $streamId$ 启动持久执行；$persistentExecutionId$ 存在且 $snapshotPointer$ 可读，表示模型 turn 已启动，恢复协调器读取快照、$providerResumeToken$、$lastChunkSeq$ 和连续 chunk 范围决定续接；$terminalAt$ 不为空或最终 assistant 消息已存在，保持终态并清理遗留执行行；快照缺失、$executionEpoch$ 不匹配或连续 chunk 范围无法确认时，标记为 interrupted。

模型续接遵循不覆盖 partial 输出的规则。若供应商支持续接且 $providerResumeToken$ 未过期，系统从最后连续 $offset$ 或 $chunkSeq$ 之后请求续写，并要求新返回内容与已持久化 partial 的边界一致；一致时继续写入后续 chunk，不一致时停止续接并标记 interrupted。若供应商不支持续接、续接 token 过期、快照损坏或已持久化 chunks 与供应商返回内容冲突，系统不得删除或覆盖既有 partial 输出；可将 partial 标记为不可续接并进入 interrupted，或者在上层明确允许重新生成时创建新的 $executionEpoch$ 和新的 stream 分支，将新输出与旧 partial 区分展示。

恢复处理遵循终态权威、单任务所有权和条件写回原则。终态记录不得被迟到的取消请求、迟到恢复扫描或旧执行分支覆盖；同一 $runId$ 在恢复过程中只能存在一个持有有效租约和当前 $executionEpoch$ 的写回分支。恢复数据流为：扫描非终态记录和 orphaned 执行行，校验消息、快照和 chunks，按阶段判定继续或 interrupted，通过条件领取获得所有权，再复用既有 $requestId$、$streamId$ 和 $userMessageId$ 执行后续动作。该流程使重复激活、重复恢复信号和多个观察者并发查询均只能产生一次有效执行。

### 可选实施方式与边界

在一种实现中，任务登记表、消息表、stream chunks 表和持久执行表位于 agent 本地 SQLite 存储中，并通过本地事务完成登记、条件更新和快照写入；在另一种实现中，任务登记表可以位于外部数据库，后台 worker 通过 RPC 或队列执行相同的领取和写回规则。无论采用哪种部署方式，存储层均需要提供唯一约束、insert-if-absent 或 upsert、条件更新、事务隔离、状态版本校验和持久快照能力；若某一存储不能提供这些能力，则需在其上层增加等效的租约、乐观锁和幂等表，否则不能作为任务状态权威源。

部署形态可以按接收层和执行层是否同进程划分：同进程形态中，接收层登记任务后通过本地 alarm、内存队列或持久执行单元触发后台执行；分离形态中，接收层只写任务登记表和队列消息，独立 worker 通过租约领取任务。外部任务提交的数据流保持一致，即请求参数经规范化得到摘要和指纹，进入任务登记表后投递异步执行，执行器按 $conversationId$ 串行写入消息，模型输出写入 stream chunks，最终聚合为 assistant 消息并写入终态。恢复数据流也保持一致，即非终态扫描、快照与消息校验、阶段判定、条件领取、继续执行或标记 interrupted。
