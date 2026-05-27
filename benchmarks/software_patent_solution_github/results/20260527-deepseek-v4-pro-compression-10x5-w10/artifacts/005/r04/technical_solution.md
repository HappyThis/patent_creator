## 技术方案

本方案为 Think agent 增设外部系统触发任务的统一支持机制。核心思路是：在现有 saveMessages() 的"消息写入即触发推理"路径之外，新增一条"先确认接收、再异步执行"的外部任务提交路径。外部调用方（如 webhook、RPC、Worker）通过 HTTP 端点提交任务后，系统立即写入任务表记录并返回"已接收"确认，无需等待模型推理完成。任务的实际执行复用 Think 现有的 TurnQueue 排队机制、ResumableStream 流式持久化、AbortRegistry 取消机制和 runFiber 持久执行框架。同时引入幂等键机制实现重复提交去重，并通过任务状态表覆盖从接收到完成（或取消、清理）全生命周期的崩溃恢复。整个方案不重写普通聊天消息保存路径，所有新增机制均通过扩展点与现有路径并行协作。

### 外部提交入口

外部任务提交通过 Think agent 的 onRequest 处理函数暴露 HTTP 端点实现，无需引入额外的服务进程或中间件。端点的请求格式设计为与 saveMessages() 的参数结构保持兼容，但额外增加幂等键和可选的回调/通知配置字段。

具体端点设计：POST /tasks，接收 JSON 请求体，包含会话标识（sessionId 或新建会话参数）、消息内容（content）、模型配置（modelConfig，沿用 Think 现有 chat 接口的模型参数结构）、幂等键（idempotencyKey，由调用方生成的唯一标识）以及可选的超时和优先级参数。请求到达后，不直接调用 saveMessages()，而是先进入任务接收与去重流程。

该端点利用 Think 现有的 onRequest 机制嵌入 Durable Object 的 HTTP handler，天然享有 DO 的单实例保证和持久化能力。端点的鉴权、限流等横切关注点沿用 Think 现有中间件体系，本方案不做重复设计。

### 任务接收与去重

任务接收阶段的核心目标是在消息写入之前完成"已接收"状态的持久化，从而实现快速确认和崩溃恢复。引入一张任务状态表 cf_agent_external_tasks，至少包含以下字段：task_id（主键，系统生成）、idempotency_key（唯一索引，调用方提供）、session_id、status（枚举：received/queued/executing/completed/failed/cancelled/cleaned）、request_payload（原始请求 JSON）、result_summary、error_message、created_at、updated_at、started_at、completed_at。

接收流程如下：(1) 收到 POST /tasks 请求后，首先校验幂等键是否存在且格式合法。(2) 以幂等键查询任务表——若已存在记录，根据其当前状态做不同处理：状态为 received/queued/executing 时，直接返回 200 含 task_id 和当前状态，不重复执行业务逻辑；状态为 completed/failed/cancelled 时，返回 200 含已有结果摘要，同样不重复执行；状态为 cleaned 时，返回 409 表示该幂等键已被清理，不可重用。(3) 若幂等键不存在，以原子方式插入一条 status=received 的任务记录，将完整请求 payload 一并持久化。此时消息尚未写入会话，模型推理尚未触发。(4) 插入成功后立即向调用方返回 202 Accepted，携带 task_id、status=received 和用于后续查询的状态端点 URL。

去重的关键设计：(a) 幂等键的唯一索引保证数据库层面不会插入重复记录；(b) 幂等键有效期与任务清理策略联动——清理后的幂等键不可复用，防止过期请求被重新解释；(c) 对于已完成的重复请求，返回结果摘要而非重新执行，既保证语义正确又避免资源浪费。

### 任务执行与状态管理

任务在返回 202 后进入异步执行阶段。执行触发采用"写后即忘"模式：在插入任务记录并返回响应后，通过调用 saveMessages() 将任务 payload 中的消息内容写入会话，触发模型推理。整个执行过程的关键状态转换均在任务表中持久化记录。

状态转换规则：(1) received → queued：saveMessages() 被调用时，任务状态更新为 queued，表示消息已写入会话并已进入 TurnQueue 排队。(2) queued → executing：TurnQueue 分配执行槽位、SubmitConcurrencyController 允许提交后，任务状态更新为 executing。若 TurnQueue 因 resetTurnState() 或新代际产生而丢弃排队中的 turn，任务保持 queued 并等待下一次排队机会。(3) executing → completed：模型推理正常完成，ResumableStream 的所有 chunk 已持久化，summary 写入任务表，状态更新为 completed。(4) executing → failed：推理过程异常终止或超时，错误信息写入任务表，状态更新为 failed。(5) executing → cancelled：外部调用取消接口或 AbortRegistry 触发中止，状态更新为 cancelled。

执行过程复用 Think 现有机制：(a) TurnQueue 保证同一会话内任务按代际序列化执行，避免并发冲突；(b) SubmitConcurrencyController 的 debounce 策略可用于合并短时间内的重复提交；(c) ResumableStream 自动将模型输出的 chunk 批量缓冲写入 SQLite，提供崩溃后的流重放能力；(d) runFiber 将整个 chat turn 包装在持久执行上下文中，DO 休眠恢复后通过 _checkRunFibers 自动恢复未完成的执行；(e) keepAliveWhile() 在任务执行期间阻止 DO 空闲驱逐。

借鉴 AgentToolChildRunRow 模式，任务表提供与子运行类似的生命周期管理能力：start 时检查已存在 runId 去重、cancel 时更新状态、inspect 时返回当前状态和结果摘要。区别在于外部任务的入口是 HTTP 端点而非 Agent Tool 调用，且任务表独立于 Agent Tool 子运行表。

### 状态查询与流式推送

外部调用方可通过两种方式获取任务执行进展：主动轮询状态查询接口，或通过 WebSocket 接收流式事件推送。两种方式互补，适应不同集成场景。

状态查询接口：GET /tasks/{taskId}，返回任务当前状态、进度信息（如已生成 token 数）、结果摘要（完成后）或错误信息（失败时）。查询逻辑直接读取任务表记录，不依赖 ResumableStream 的重放——仅在调用方需要获取完整流式输出时，才通过 GET /tasks/{taskId}/chunks 触发 ResumableStream 的 chunk 重放。这与 Think 现有的 inspectAgentToolRun / getAgentToolChunks 接口模式保持一致。

流式事件推送：POST /tasks 请求中可包含 WebSocket 订阅参数，或在任务创建后通过 WebSocket 协议订阅特定 taskId 的事件流。底层复用 Think 现有的 WebSocket 协议框架，事件类型包括：task.queued（进入排队）、task.executing（开始执行）、task.chunk（增量输出 token，复用现有流式 chunk 推送通道）、task.completed（完成含摘要）、task.failed（失败含错误信息）、task.cancelled（已取消）。WebSocket 连接断开不会影响任务执行，重连后可继续订阅。

对于已完成的流，调用方 GET /tasks/{taskId}/chunks 时，系统利用 ResumableStream 的 replay 能力从 SQLite 中读取已持久化的 chunk 序列并流式返回。若任务仍在执行中，该端点可同时返回已持久化的 chunk 并保持连接以继续推送新 chunk，实现"赶上 + 尾随"的读取模式。

### 取消与清理

取消接口：POST /tasks/{taskId}/cancel。处理流程为：(1) 查询任务表确认任务存在且当前状态为 received/queued/executing（已完成或已取消的任务返回 409）；(2) 调用 AbortRegistry 中对应 requestId 的 AbortController.abort()，触发正在执行的 chat turn 中止——这与 Think 现有的 saveMessages({ signal }) 取消路径完全一致；(3) 更新任务状态为 cancelled，记录取消时间和操作来源；(4) 若任务仍在 TurnQueue 排队中尚未开始执行，resetTurnState() 会使队列中的 turn 因代际失效而被标记为 stale，任务状态随后更新为 cancelled。

清理接口：DELETE /tasks/{taskId}。清理操作为软删除——将任务状态更新为 cleaned，而非物理删除记录。此举保留幂等键的去重语义（已清理的幂等键不可复用），同时避免引用完整性问题。清理后，关联的 ResumableStream chunk 数据和会话消息不受影响——消息已通过 saveMessages() 正常持久化到会话中，chunk 数据按 ResumableStream 自身的保留策略管理。任务表记录保留 cleaned 状态用于审计和幂等键冲突检测。

批量清理：提供 GET /tasks 列表查询接口（支持按 session_id、status、时间范围过滤），配合 DELETE /tasks?session_id=xxx 或按状态批量清理，方便运维管理。清理操作不阻塞正在执行的任务——对 executing 状态的任务执行清理时，先触发取消流程再标记为 cleaned。

### 崩溃恢复

崩溃恢复覆盖任务生命周期的各个关键节点，确保在 DO 休眠恢复、进程崩溃或网络中断后，任务状态可被正确判定和继续推进。

恢复场景分析：(1) 任务记录已写入（status=received）但 saveMessages() 尚未调用——恢复后通过扫描 status=received 的任务记录，补偿调用 saveMessages()，将任务推进至 queued 状态。补偿调用前需再次检查幂等键，防止恢复期间外部重复提交已插入新记录。(2) 消息已写入、任务状态为 queued 但 TurnQueue 尚未消费——恢复后 TurnQueue 重新初始化，任务在下一轮排队中被正常消费。因为消息已持久化到会话中，不依赖任务表的排队状态。(3) 任务状态为 executing 但进程崩溃——恢复后 runFiber 的 _checkRunFibers 机制自动检测未完成的 chat turn 并恢复执行；ResumableStream 的孤儿流恢复机制保证崩溃前已输出的 chunk 不丢失，崩溃后从最后一个检查点继续。任务表状态保持 executing，待推理完成后更新为 completed 或 failed。(4) 取消请求在崩溃前到达但未完全处理——恢复后检查 AbortRegistry 状态与任务表状态的一致性：若任务表为 cancelled 但消息已写入会话，保持 cancelled；若 AbortRegistry 显示已中止但任务表未更新，补偿更新任务状态。

关键设计原则：任务表是"已接收"和幂等去重的权威数据源；会话消息是推理输入输出的权威数据源；ResumableStream 是流式输出的权威数据源。三者在崩溃恢复中各自独立恢复，通过任务 ID 和会话 ID 关联，不存在跨数据源的强一致性依赖。这种松散耦合使得各组件可以按自身既有的恢复路径独立恢复，降低恢复复杂度。

### 与现有机制的兼容关系

本方案所有新增机制均通过扩展点与 Think 现有路径并行协作，不重写任何现有代码路径。

与 saveMessages() 的关系：普通聊天路径（WebSocket chat 协议、程序化消息注入）继续直接调用 saveMessages()，消息即时写入会话并触发推理，不经过任务表。外部任务提交路径先写任务表、再调用 saveMessages()，两个路径共享同一底层消息持久化和推理触发逻辑。saveMessages() 的方法签名和行为不变，任务路径仅在调用前后增加任务表的状态更新。

与 TurnQueue / SubmitConcurrencyController 的关系：外部任务产生的 chat turn 与普通聊天 turn 在同一 TurnQueue 中排队，共享代际计数器。当会话发生 reset（如 _handleClear 或用户发送新消息触发 resetTurnState()）时，外部任务排队中的 turn 与普通 turn 一样被标记为 stale。SubmitConcurrencyController 对两种来源的提交一视同仁，debounce 窗口内的重复提交按既有策略处理。

与 ResumableStream 的关系：外部任务的流式输出与普通聊天使用相同的 ResumableStream 基础设施——同一张 cf_ai_chat_stream_chunks 表和相同的 chunk 缓冲写入逻辑。任务表通过记录 requestId 与 ResumableStream metadata 关联，无需修改 ResumableStream 的表结构或写入逻辑。

与 AbortRegistry 的关系：取消外部任务时调用 AbortRegistry 中对应 requestId 的 AbortController，与 saveMessages({ signal }) 的取消路径完全一致。AbortRegistry 不感知取消来源是外部 API 调用还是内部逻辑。

与 Session / 消息存储的关系：外部任务产生的消息通过 saveMessages() 正常写入会话消息树，参与 FTS5 索引、compaction 和 context block 构建。任务表仅记录任务元数据和执行状态，不重复存储消息内容。任务清理不影响会话消息的保留。

### 技术效果

本方案通过引入任务表作为"已接收"状态的持久化锚点，在消息写入之前即完成接收确认，解决了外部调用方长时间等待模型推理的问题。核心效果包括：

(1) 快速确认：外部调用方在任务表插入成功后（毫秒级）即获得 202 响应，无需等待模型推理（可能数十秒至数分钟），显著提升外部系统的吞吐和用户体验。(2) 精确去重：基于幂等键的唯一索引和全生命周期状态管理，保证同一请求无论重试多少次，业务逻辑只执行一次，且已完成请求的重复提交可直接返回已有结果。(3) 完整生命周期管理：覆盖接收、排队、执行、完成、取消、清理六个阶段，每个阶段的状态转换均持久化，外部调用方可随时查询、取消或清理任务。(4) 崩溃安全：任务表、会话消息、ResumableStream 三者松散耦合，各自独立恢复，任何单一组件崩溃不会导致任务丢失或状态不一致。(5) 零侵入扩展现有路径：普通聊天消息保存路径完全不受影响，所有新增机制通过扩展点与现有代码并行协作，不重写、不修改、不降级任何现有功能。(6) 流式可观察：通过 WebSocket 事件推送和 chunk 查询端点，外部调用方可实时跟踪任务执行进展，断开重连后仍可继续订阅或回溯已输出内容。
