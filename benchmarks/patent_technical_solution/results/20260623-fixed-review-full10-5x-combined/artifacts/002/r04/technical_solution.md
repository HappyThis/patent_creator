## 技术方案

### 总体构思

本方案在现有支持 HTTP 请求、Webhook、RPC、调度回调等入口唤醒 agent，并支持通过程序化消息触发对话轮次的系统之上，增加外部对话任务登记与执行协调机制。外部系统提交的内容不直接等同于一次立即执行的模型调用，而是先被转换为具有唯一任务标识、幂等判定信息、执行状态和取消标记的持久化任务记录；接收端完成鉴权、参数校验和任务登记后即可返回“已接收”结果，使外部调用方无需等待模型推理、工具调用或流式输出完成。

该机制将“接收确认”和“对话执行”解耦：接收阶段只负责把外部请求可靠地落入任务注册表并返回任务标识；执行阶段再由队列、调度回调或 agent 内部方法读取任务记录，按既有对话消息持久化能力注入用户消息并触发模型轮次。任务注册表成为状态查询、重复提交识别、取消、清理和恢复判断的共同事实来源，从而避免依赖内存中的请求对象、连接状态或未持久化的 promise 判断任务进度。

### 任务注册表与状态机

在默认实施方式中，路由键由 agentId 与 sessionId 共同组成：agentId 标识被唤醒的 agent 实例，sessionId 标识该实例内的对话分支或业务会话；仅按业务实体独占一个 agent 时，sessionId 可以取固定值。requestId 表示一次模型对话轮次，streamId 表示该轮次的流式输出缓冲，generation 表示会话清空或切换后的队列世代，fiberId 或等效记录表示可恢复执行上下文。任务注册表用 taskId 连接上述字段，使外部任务、持久化消息、模型轮次和恢复上下文具有同一关联主键。

任务注册表至少包含 taskId、callerId、agentId、sessionId、idempotencyKey、payloadHash、hashVersion、payloadNormalized、status、statusVersion、attemptNo、nextAttemptAt、executorId、leaseExpireAt、cancelRequestedAt、cleanupRequestedAt、messageId、requestId、streamId、generation、parentMessageId、lastMessageSeq、checkpointId、resultSummary、errorCode、errorDetail、createdAt 和 updatedAt。statusVersion 在每次状态更新时递增，用于解决取消、完成、清理和恢复同时写入时的竞争；attemptNo 在执行器成功领取任务并准备执行时递增，而不是在重复投递或重复查询时递增。

状态集合划分为可执行态、运行态、等待态和终态。可执行态包括 accepted、queued、retry_waiting，运行态包括 starting、running、aborting，等待态包括 cancel_requested、cleanup_pending，终态包括 completed、error、aborted、skipped、interrupted 和 cleaned。允许迁移为：accepted 经队列投递进入 queued；queued 或 retry_waiting 被执行器领取后进入 starting；starting 在消息注入并关联 requestId 后进入 running；running 在响应完成后进入 completed，在可重试错误后进入 retry_waiting，在取消生效后进入 aborted，在无法安全恢复时进入 interrupted，在不可重试错误后进入 error。终态不得被后到达的取消、重复提交或旧队列项覆盖。

重试规则以 attemptNo、nextAttemptAt 和错误类型控制。执行器领取任务时递增 attemptNo；若稳定性检查超时、队列投递失败、临时网络错误或模型服务短暂不可用，则写入 retry_waiting，并按照指数退避加抖动计算 nextAttemptAt；超过最大尝试次数或出现鉴权失败、载荷不可解析、幂等冲突等不可重试错误时进入 error。由于重试只针对同一 taskId 和同一规范化消息体进行，且消息注入受 messageId 与 taskId 唯一约束保护，重试不会导致 transcript 中出现重复用户输入。

### 外部任务接收与幂等登记

外部入口可以为 webhook 路径、RPC 方法或其他服务端程序化调用入口。接收端先依据入口来源执行签名验证、访问令牌验证或调用方身份校验，并将请求归一化为统一任务描述。归一化过程采用确定性编码规则：将 JSON 字段按字段名排序，统一字符编码和换行形式，去除签名、到达时间、重试次数、传输链路 request-id 等不影响业务语义的易变字段，保留调用方标识、目标 agentId、sessionId、外部事件编号、用户输入内容和业务上下文；同时写入 hashVersion，用于后续摘要算法或字段规则升级时区分旧任务。载荷摘要由 hashVersion 与归一化后的字节序列计算得到，从而使同一业务请求在多次重试中得到相同 payloadHash。

幂等键的默认生成顺序为：优先使用调用方显式提供的 idempotencyKey；其次使用经签名验证可信的外部事件编号；再次使用 callerId、agentId、sessionId 与 payloadHash 的组合生成系统幂等键。任务注册表对 callerId、agentId、sessionId 与 idempotencyKey 建立唯一约束，并同时保存 payloadHash。若同一幂等键对应相同 payloadHash，则判定为同一任务重试并返回既有 taskId；若同一幂等键对应不同 payloadHash，默认返回幂等冲突错误且不创建新任务，防止调用方复用键导致不同业务输入被错误合并。另一实施方式允许创建派生键，但派生键必须显式加入冲突序号并在响应中告知调用方，且不得作为外部重试的默认语义。

接收阶段采用同一事务或等效原子单元写入任务记录和 outbox 事件。首次提交时，系统插入状态为 accepted 的任务记录，并插入类型为 enqueue_task 的 outbox 记录；事务提交后由投递器把 outbox 记录投递到任务队列并将任务推进为 queued。若任务记录已创建但进程在投递前崩溃，恢复扫描会发现 accepted 且缺少有效队列投递的任务并重新投递；若队列重复投递，执行器仍必须通过任务状态、租约和 taskId 消息唯一约束再次去重。该 outbox 加恢复扫描的结构使“已接收但未入队”的断点可被补偿，同时唯一约束使重复外部请求收敛到同一 taskId。

### 异步调度与对话轮次串行化

执行器领取任务必须通过条件更新完成：仅当任务处于 accepted、queued 或 nextAttemptAt 已到期的 retry_waiting，未设置 cancelRequestedAt，未处于 cleanup_pending，且 leaseExpireAt 为空或已过期时，才可把状态改为 starting，并写入 executorId、leaseExpireAt、attemptNo 和新的 statusVersion。若条件更新影响行数为零，表示任务已被其他执行器领取、已取消、已进入终态或正在清理，当前执行器放弃该队列消息。租约到期而任务未进入 running 或终态时，恢复扫描可清除 executorId 并将任务放回 retry_waiting，由此避免多个执行器同时驱动同一 taskId。

执行器在注入消息前执行可判定的稳定性检查。允许注入的条件包括：turn 队列的 activeRequestId 为空且 isActive 为 false；当前 generation 与领取任务时记录的 generation 一致；同一会话不存在优先级更高或更早入队且尚未处理的 turn；消息表最新 revision 或 lastMessageSeq 与执行器读取时一致；不存在等待客户端工具结果或审批的未决 requestId；自动延续队列已经排空。若仅因活跃 turn 或自动延续未完成导致不稳定，则任务回到 queued 或 retry_waiting；若长期等待客户端工具交互超过阈值，则标记为 interrupted 或保持 retry_waiting 并记录等待原因；若 generation 已变化，旧任务不得注入新 transcript，转为 skipped 或 interrupted。

当对话稳定后，执行器把外部任务转换为一条带 taskId、callerId、idempotencyKey 和 payloadHash 元数据的用户消息，并通过程序化消息路径触发新的对话轮次。消息追加采用会话 revision、消息序号或 compare-and-swap 条件：只有任务仍处于 starting、尚未关联 messageId 或 requestId、同一 taskId 的用户消息不存在、generation 未变化且最新消息序号符合预期时，才允许写入用户消息。写入用户消息、记录 messageId、生成或接收 requestId、把任务更新为 running 应在同一事务中完成；若底层程序化消息接口只能在事务外返回 requestId，则先写入 pending_request 标记并由补偿器根据消息元数据和响应回调补齐 requestId，补齐前不得再次追加同一 taskId 消息。

对话轮次由串行 turn 队列承载。该队列以先进先出方式执行 WebSocket 消息、程序化消息和自动延续，并维护 generation、activeRequestId、isActive 以及排队数量等运行信息；当会话被清空、切换或主动重置时，generation 递增，使旧 generation 中尚未执行的任务在到达队首时自动跳过。冲突处理优先级为：终态优先于取消请求，取消标记优先于新执行，generation 不一致优先跳过，已存在 taskId 用户消息时禁止再次追加。上述优先级使重复队列消息、旧 generation 队列项、恢复器和新调度器即使同时运行，也只能推动同一任务的一条合法状态迁移。

### 状态查询、取消与清理

状态查询接口不读取短生命周期连接或内存 promise，而是读取任务注册表，并返回 taskId、当前状态、statusVersion、创建和更新时间、agentId、sessionId、attemptNo、是否已开始执行、messageId、requestId、streamId、可选 resultSummary 或错误信息。completed 状态在模型轮次结束、assistant 消息持久化成功且 onChatResponse 或等效完成回调写入 resultSummary 后形成；resultSummary 可以由 assistant 消息文本摘要、最终消息标识、输出 token 统计或业务结果引用组成。若流式输出已有部分 chunk 但最终错误，系统根据是否存在可恢复 checkpoint 分别进入 retry_waiting、interrupted 或 error，并保留 partial 标记和 streamId 供查询，而不把未完成输出误报为 completed。

取消接口以 taskId 为对象并保持幂等。若任务仍为 accepted、queued 或 retry_waiting，系统通过状态版本条件更新写入 cancelRequestedAt 并转为 aborted；若任务已进入 starting 或 running，系统先把状态改为 cancel_requested 或 aborting，并触发与 requestId 对应的 AbortSignal，模型流、自动延续和可取消工具调用应接收该信号并停止。对于已经发出的不可撤销外部副作用，任务记录写入 sideEffectCommitted 或等效标记，表示取消只能阻止后续步骤，不能回滚已完成外部动作。若 completed 及 resultSummary 已成功提交，后到取消不得覆盖 completed；若取消标记先提交而模型随后返回结果，完成回调必须比较 statusVersion，仅在任务仍为 running 时采纳结果，否则只保存为部分输出或审计信息。

清理接口按任务状态决定动作。对终态任务，系统先写入 cleanup_pending，解除或归档任务记录、幂等索引、结果摘要和流式 chunk 缓冲之间的引用，再进入 cleaned；在幂等索引保留期限内保留 tombstone，记录 callerId、agentId、sessionId、idempotencyKey、payloadHash、原 taskId 和清理时间，使相同幂等键的迟到重试不会被当作新任务执行。对 starting 或 running 任务，cleanup_pending 下执行器不得新启动任务；清理器先写入取消标记并等待终态，超过等待阈值时使租约失效、关闭残留 streamId 的观察连接，并将任务标记为 interrupted 后再归档。由此可避免任务记录被删除而后台模型调用或旧重试继续污染会话。

### 休眠或重启后的恢复一致性

agent 运行环境休眠、重启或执行中断后，本地计时器、闭包、AbortController 和未持久化 promise 可能丢失，因此恢复流程以任务注册表、消息记录、流式 chunk 缓冲、fiber 或队列记录为依据。恢复器比较 taskId、messageId、requestId、streamId、generation、parentMessageId、lastMessageSeq、checkpointId 与当前持久化 transcript；只有这些字段能够证明部分输出仍属于当前会话分支时，才允许继续原轮次。若任何终态已经写入，恢复器不得改变终态；若 cleanup_pending 已写入，恢复器不得启动新的执行。

恢复判定按分支执行：第一，任务无 messageId、无 requestId 且处于 accepted、queued 或 retry_waiting 时，说明用户消息尚未写入，恢复器重新投递同一 taskId，不写入新的幂等记录；第二，任务已有 messageId 但无 assistant 输出时，恢复器复用该 messageId 和 requestId 等待原队列收敛，或在原 requestId 可恢复时继续该轮次，不再追加同一 taskId 的用户消息；第三，任务已有部分 assistant 输出且 checkpointId、parentMessageId、lastMessageSeq 与当前 transcript 匹配时，恢复器通过继续上一轮的方式续跑，并复用原 requestId 或记录 continuation requestId；第四，存在部分输出但工具状态、checkpoint 或会话分支不可恢复时，任务进入 interrupted。

generation 不一致、parentMessageId 不再位于当前会话分支、lastMessageSeq 小于已确认的新消息序号、checkpoint 超过有效期，或外部工具明确返回不可恢复时，均属于不可安全恢复条件。此时系统将任务标记为 interrupted，保留 taskId、messageId、streamId、部分输出和错误原因供查询，但禁止自动重新注入同一用户输入；外部调用方如需继续业务处理，应以新的幂等键提交后续任务。该分支规则形成因果闭环：持久化字段证明安全时延续原轮次，无法证明安全时停止自动重放，从而同时避免任务丢失和重复推理。

### 可选实施边界

在一种实施方式中，任务注册表、outbox、消息表、流式 chunk 缓冲和幂等 tombstone 位于每个 agent 实例的 SQLite 存储中，以便通过同一持久化边界完成条件更新和恢复扫描。在另一实施方式中，外部协调存储保存全局任务索引，具体 agent 实例保存会话消息和 stream 数据；此时全局索引中的 taskId、agentId、sessionId 与 agent 内部 messageId、requestId 之间需要双向校验，且以 agent 内部 transcript 的 generation 和 lastMessageSeq 作为是否可注入消息的最终依据。

在默认实施方式中，需要立即处理的一次外部对话任务被包装为普通用户消息，并在消息元数据中写入 taskId、callerId、idempotencyKey、payloadHash 和 sourceType。另一实施方式可以把输入包装为带来源说明的多部分消息，或在仅补充背景信息时采用只持久化消息而不触发响应的路径；该路径不进入上述 running 状态机，除非后续任务显式引用该背景消息并触发模型轮次。无论采用哪种包装方式，同一 taskId 在消息表中均应具有唯一索引或条件写入保护。

确认响应的字段名称不作限定，但默认至少返回 taskId、status、statusVersion、查询地址和幂等命中标记。鉴权失败、载荷校验失败和同键不同摘要的幂等冲突不创建任务记录；已清理任务在 tombstone 保留期内返回原 taskId 的 cleaned 或 archived 状态；超过保留期后的相同请求是否作为新任务，由调用方配置的保留策略决定。通过把快速确认、规范化摘要、幂等 tombstone、条件领取、会话稳定检查、taskId 消息唯一约束、状态版本和恢复分支组合起来，系统能够在重复提交、队列重复投递、取消完成竞争、清理执行竞争和运行环境中断时维持一致的任务生命周期。
