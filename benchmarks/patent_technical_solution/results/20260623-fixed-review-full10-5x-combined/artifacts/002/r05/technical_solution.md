## 技术方案

### 总体结构与外部提交入口

本方案解决的典型失效场景包括：外部 webhook 或 RPC 同步等待模型推理导致调用方超时；外部系统因网络异常重复提交而追加相同用户消息或重复触发模型；多个入口同时修改同一会话 transcript 导致消息顺序不确定；取消请求与最终响应保存发生竞态；Durable Object 休眠、运行时重启或部署更新使内存队列和上游连接丢失；流式输出中断后只留下部分 chunk 而任务状态不明。针对上述场景，本方案在既有对话 agent 外侧增加外部任务接收、持久化登记、异步 turn 执行、任务级查询/取消/清理和确定性恢复机制。

系统采用入口层、任务登记层、对话执行层和恢复层的分层结构。入口层对应 HTTP webhook、agent callable 方法、同一运行环境内的 Durable Object RPC 或其他服务端调用方式，负责调用方认证、目标 agent/session 校验、输入大小和格式校验、幂等键可接受性校验，并把不同入口归一化为同一种提交对象。归一化对象包含调用方标识、目标会话标识、消息文本、附件或数据 part 引用、工具参数、原始请求上下文、可选幂等键和取消信号来源。任务登记层把归一化对象写入持久化任务记录；对话执行层把该记录转换为 AIChatAgent 可处理的用户消息和程序化 turn；恢复层依据任务 phase、requestId 映射、messageId、chunk 序号和运行快照判定中断后的动作。

本文中，taskId 表示系统返回给外部调用方的任务标识；callerId 表示调用方身份；idempotencyKey 表示调用方显式提供的幂等键；digestKey 表示系统在未提供幂等键时根据规范化内容生成的兜底摘要键；requestId 表示内部一次对话 turn 的请求标识；messageId 表示写入 transcript 的用户消息或 assistant 消息标识；turnQueueId 表示任务在内部执行队列中的入队标识；generation 表示目标会话在当前 transcript 清空或重置前后的代际；status 表示任务状态，phase 表示更细的执行阶段；cancelRequested 和 cleanupRequested 分别表示取消请求标志和清理请求标志，已取消、已清理则是任务终态；completed、skipped、aborted 等仅作为内部执行返回码，需映射为任务状态后再对外返回。

### 任务登记、快速确认与幂等去重

本方案设置外部任务登记表，用于把外部一次提交与内部一次或多次对话 turn 关联起来。任务记录的最小字段包括：taskId、callerId、targetSessionId、idempotencyKey、digestKey、inputDigest、normalizedInputRef、requestIdList、inputMessageId、assistantMessageIdList、turnQueueId、status、phase、generation、cancelRequested、cleanupRequested、retryCount、createdAt、updatedAt、errorCode、errorMessage、snapshotRef 和 cleanupTombstone。callerId、targetSessionId 与 idempotencyKey 组成显式幂等唯一索引；未提供 idempotencyKey 时，callerId、targetSessionId、digestKey 和时间窗口组成兜底唯一索引；taskId 为外部查询、取消和清理的主键；requestIdList、messageId、generation、phase 和 snapshotRef 用于恢复判定。

幂等去重按如下步骤执行：首先对输入进行规范化，去除不影响语义的空白差异，统一文本编码和语言标记，按稳定顺序排列工具参数，并把附件、数据 part 或外部资源引用转换为内容哈希或版本化引用；然后计算 inputDigest。外部系统提供 idempotencyKey 时，digestKey 不参与主判定，但 inputDigest 被保存用于冲突检测；未提供 idempotencyKey 时，digestKey 由 callerId、targetSessionId、规范化消息内容、附件/工具参数摘要和可配置时间窗口计算得到。若唯一索引冲突且 inputDigest 相同，入口层读取既有任务并返回原 taskId、status、phase 和 idempotentHit=true；若同一显式幂等键对应不同 inputDigest，则返回幂等冲突错误，不沿用先到任务也不创建新任务；若同一内容确需重复执行，调用方应提供新的 idempotencyKey 或设置允许重复的提交参数，使系统绕过摘要兜底窗口创建新任务。

提交确认采用持久化事务临界区实现。事务内先锁定或插入幂等唯一键，再生成 taskId 和首个 requestId，读取目标会话当前 generation，写入 normalizedInputRef、inputDigest、requestIdList、status=已接收、phase=输入已登记、inputMessageId=null、turnQueueId=null、cancelRequested=false 和 cleanupRequested=false；随后写入“待写入用户消息”标记。事务提交成功后，入口层立即返回 taskId、status、phase、idempotentHit=false 和查询地址；事务提交前发生数据库写入失败或唯一键冲突未处理时，不返回已接收。这样，“外部确认”只发生在任务已经具备可恢复记录之后，模型推理和工具调用均在确认之后异步进行。

提交事务完成后，系统创建或确认 turnQueueId，并将任务从已接收转为排队中。turnQueueId 由 targetSessionId、generation、taskId 和序号组成，持久化队列记录按 createdAt、序号排序；同一 taskId 只能存在一个未完成的队列记录。若入队动作因实例中断未完成，恢复器可根据 status=已接收且 phase=输入已登记的任务补建队列记录；若入队已完成但状态未更新，则根据 turnQueueId 反向补齐排队中状态。队列执行仍通过会话级串行化锁或 TurnQueue 保证同一 targetSessionId 在同一 generation 内一次只运行一个 turn。

### 异步执行、状态查询与取消清理

队列执行器取得 turnQueueId 后，先在事务中读取并锁定任务记录，检查 status、phase、cancelRequested、cleanupRequested、generation 和 inputMessageId。若会话 generation 已变化，说明 transcript 已被清空或重置，任务转为已跳过并释放队列；若 cleanupRequested=true 且用户消息尚未写入，任务转为已清理；若 cancelRequested=true 且尚未调用模型，任务转为已取消；若 inputMessageId 已存在，则复用该 messageId 进入后续阶段，不再追加用户消息；若 inputMessageId 为空，则根据 normalizedInputRef 生成 AIChatAgent 用户消息，持久化到 transcript 后把 messageId 写回任务记录，并把 phase 更新为消息已持久化。

任务状态机采用受限迁移规则：已接收只能转为排队中、已取消或已清理；排队中只能转为运行中、已跳过、已取消或已清理；运行中只能转为已完成、失败、已取消或已跳过；已完成、已跳过、已取消、失败和已清理均为终态，终态不得被排队中或运行中覆盖。phase 是状态内的细粒度阶段，可取输入已登记、已入队、消息已持久化、模型响应中、工具调用中、部分响应已保存、最终响应已保存、终态已写入、临时记录已清理等。状态更新使用比较式写入：只有当前状态仍等于预期状态时才允许迁移；若发现终态已存在，执行器停止写入并返回该终态，从而避免恢复器、取消器和正常执行器互相覆盖。

进入模型调用前，执行器把 requestId 写入 requestIdList 并把状态更新为运行中、phase=模型响应中，然后通过程序化 saveMessages 或 continueLastTurn 触发 onChatMessage。saveMessages 的输入是一个基于当前持久化 transcript 的函数，使排队期间产生的新消息能够被纳入上下文；其输出 requestId 和 completed、skipped、aborted 等执行返回码被映射为任务状态。模型流式输出期间，chunk 按 streamId 和 chunk_index 持久化；最终 assistant 消息写入后，将 assistantMessageId 追加到 assistantMessageIdList，并以事务方式把 phase 更新为最终响应已保存、status 更新为已完成。若出现“消息已保存但任务状态未更新”的崩溃，恢复器通过 assistantMessageId 或 requestId 对应的 completed stream 补齐任务终态；若“终态已写但运行记录未删”，恢复器只删除残留运行记录，不再次执行。

查询接口以 taskId 或幂等键为输入，返回 taskId、status、phase、idempotentHit、requestIdList、inputMessageId、assistantMessageIdList、partialResultRef、finalResultRef、cancelRequested、cleanupRequested、errorCode、errorMessage 和 updatedAt。聚合规则为：若任务已有终态，则直接返回终态；若存在最终 assistantMessageId，则 finalResultRef 指向该消息；若没有最终消息但存在 streaming 或 completed chunk，则 partialResultRef 指向按 chunk_index 排序得到的部分输出；若 requestIdList 中多个 requestId 状态不一致，优先级为已完成高于运行中，运行中高于排队中，已取消和失败仅在没有后续完成结果时作为任务终态返回，同时保留原错误摘要。由此，外部系统通过读取持久化状态即可获知任务阶段和可用结果，而不需要保持长连接等待模型完成。

取消接口以 taskId 或幂等键为输入，先以比较式写入设置 cancelRequested=true 并记录 cancelAt。若任务仍为已接收或排队中，执行器在下一次状态检查时转为已取消且不调用模型；若任务处于运行中，系统通过 requestId 关联的 AbortController 或外部 AbortSignal 触发 abortSignal，使模型循环、工具等待或流读取尽快停止；若取消请求到达时最终 assistant 消息已经开始事务性保存，则以已完成为准，取消接口返回“已完成，取消未生效”；若 cancelRequested 早于最终保存但 abort 未及时生效，则后续未提交的输出被丢弃，已持久化 chunk 作为部分结果保留，任务转为已取消；若任务已经失败或已清理，则取消接口只返回当前终态。

清理接口以 taskId 或幂等键为输入，区分请求标志和清理终态。对已接收或排队中的任务，清理设置 cleanupRequested=true，若 inputMessageId 为空则可直接转为已清理；若任务运行中，清理先设置 cleanupRequested=true 并触发取消，待运行停止或转为终态后再删除临时数据；对已完成、已取消或失败任务，清理可删除 normalizedInputRef、原始请求上下文、临时 snapshotRef 和过期 chunk，或用 cleanupTombstone 保留幂等索引、taskId、status、requestIdList、inputMessageId 和 assistantMessageIdList 的脱敏引用。清理后重复提交命中同一幂等键时，若 tombstone 仍保留则返回已清理及历史终态引用；若调用方要求重新执行，必须使用新的 idempotencyKey。清理不得删除当前 transcript 仍引用的 messageId；只有显式会话清空操作才能删除 transcript 消息并推进 generation，使旧任务在后续执行检查中转为已跳过或已清理。

### 休眠、重启和中断后的恢复一致性

恢复流程可由实例重新激活、持久化 alarm、外部查询、外部提交命中未终态任务或执行器启动触发。恢复器扫描 status 属于已接收、排队中、运行中且 updatedAt 超过可配置静默阈值的任务，以及存在 runFiber 运行记录、streaming 流式元数据或残留 turnQueueId 的任务。扫描后按 taskId 获取恢复锁；未取得锁的恢复器只读取状态，不执行修复，以避免正常执行器和恢复器同时处理同一任务。恢复判断同时读取任务登记记录、requestIdList、inputMessageId、assistantMessageIdList、streamId、chunk_index、运行记录和 stash 快照。

恢复决策按崩溃发生点确定：一是任务已接收但 turnQueueId 为空且 inputMessageId 为空时，补建队列记录并维持同一 taskId、requestId 和 generation；二是已入队但状态仍为已接收时，补写排队中和 phase=已入队；三是 inputMessageId 为空且存在待写入标记时，重新执行消息写入步骤；四是 inputMessageId 已存在但没有模型响应 streamId 时，复用该 inputMessageId 和 requestId 继续触发响应，不追加第二条用户消息；五是存在部分 chunk 但没有最终 assistantMessageId 时，按 chunk_index 重建部分 assistant 消息或标记部分结果后决定是否续跑；六是最终 assistantMessageId 已存在但 status 未终态时，补写已完成；七是任务已终态但 runFiber 或 stream 元数据残留时，仅删除或标记临时记录，不再调用模型。

长耗时对话 turn 包装在持久化执行单元中。执行单元启动时写入运行记录，名称包含 taskId 和 requestId；执行过程中在消息写入、模型响应开始、工具调用开始、收到上游响应标识、chunk 刷盘、部分消息固化、最终消息保存和取消检测等关键点同步 stash 当前 phase、messageId、streamId、chunk 最大序号、外部任务标识、取消/清理标志和可重试计数。stash 返回后视为检查点已经落盘。若崩溃发生在任一检查点之后，恢复器以检查点和任务记录中较新的 phase 为准；若二者冲突，优先采用已持久化 messageId、assistantMessageId 和 chunk 序号所证明的更后阶段。

孤立流的判定条件为：关联 requestId 的流式元数据仍为 streaming；当前实例没有该 streamId 的存活 reader 或 active reader 注册；最后 chunk 更新时间或运行记录更新时间超过静默阈值，或者对应 runFiber 记录已被恢复器判定为 orphaned。处理时先 flush 已缓存 chunk，再按 chunk_index 升序读取并去重，相同 streamId 与 chunk_index 只取最早成功写入的一条；随后解析 chunk 得到 partialText 和 partialParts。若任务记录中已经存在相同 streamId 的 partialMessageId 或 partialHash，则不重复保存部分 assistant 消息；否则写入一条标记为部分结果的 assistant 消息或仅写入 partialResultRef，并把 phase 更新为部分响应已保存。之后根据恢复策略决定停止、失败终止或调用 continueLastTurn 续跑，续跑产生的新 requestId 必须追加到 requestIdList。

恢复续跑不得覆盖已固化的部分结果。若部分 assistant 消息已保存，continueLastTurn 生成的后续内容可以追加到同一 assistant 消息的 continuation 部分，或作为新的 assistantMessageId 关联到同一 taskId；无论采用哪种形式，都必须记录 sourcePartialMessageId 和 continuationRequestId，查询时按消息创建顺序和 continuation 关系返回合并视图。若原 requestId 失败但续跑 requestId 完成，任务 status 为已完成并保留原错误摘要作为 historyError；若原 requestId 保存了部分结果而续跑被取消，任务 status 为已取消但查询返回 partialResultRef；若多个 requestId 都产生 assistant 消息，finalResultRef 选择最新完成且未被标记为中间结果的 assistantMessageId。

异常处理区分可重试和不可重试两类。模型调用超时、上游连接断开、临时 5xx、运行时驱逐或恢复钩子短暂失败属于可重试异常，按 retryCount、指数退避和最大重试次数重新入队，重试时复用同一 taskId、inputMessageId 和已确认的 requestId 映射；外部工具明确返回不可重试业务错误、工具声明为非幂等且已执行但结果未知、输入超过大小限制、鉴权失败或幂等键冲突属于不可重试异常，写入失败终态和错误摘要。数据库写入失败发生在提交事务提交前时，外部入口返回未接收；发生在消息或终态写入后续阶段时，恢复器依据已落盘的 messageId、chunk 和运行快照补偿状态，补偿失败超过阈值后转为失败并保留人工排查所需的 errorCode。

### 与现有对话持久化能力的协同

外部任务登记层调用既有 AIChatAgent 能力时，采用明确的输入输出关联。登记层把 normalizedInputRef 转换为 AIChatAgent 的 UIMessage，并在 saveMessages 的函数式输入中基于最新 this.messages 追加该消息；saveMessages 返回的 requestId 和 completed、skipped、aborted 执行返回码写回任务记录。TurnQueue 或等效会话锁提供串行化执行，generation 用于识别会话清空后旧任务应跳过。AbortRegistry 以 requestId 为键连接外部取消信号。ResumableStream 以 streamId、requestId 和 chunk_index 保存流式输出，供查询和恢复重建 partialResultRef。runFiber 包装长耗时 turn，stash 写入 snapshotRef，onChatRecovery 接收 requestId、partialText、partialParts、recoveryData 和 messages 后返回是否保存部分结果及是否续跑。

新增登记层形成任务级语义：登记事务提交后立即返回 taskId 产生“接收即确认”；callerId、targetSessionId、idempotencyKey 或 digestKey 的唯一约束，结合 inputMessageId/requestId 复用和终态不得被运行态覆盖规则，产生“异常重试不重复写入、不重复执行”；phase、runFiber 运行记录、stash 快照、stream chunk 序号、messageId 和 requestIdList 共同产生“中断后可判定阶段”；清理 tombstone、messageId 引用检查和 generation 失效规则共同保证“任务临时数据可清理但不破坏当前会话 transcript”。因此，外部系统获得的是稳定任务接口，内部仍沿用既有对话消息、工具调用和流式响应的一致性处理。
