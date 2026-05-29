## 技术方案

本方案在 Think agent 现有架构基础上增加一种面向外部系统的可靠任务提交与追踪机制。外部调用方（如 webhook、RPC 调用方或其他 Worker）通过提供幂等键向 Think agent 提交一次对话任务后，即可获得即时确认，无需等待模型推理完成；之后可通过任务标识查询执行进度，按需取消或清理任务。该机制复用 Think 现有的会话存储、TurnQueue 序列化执行、AbortRegistry 取消信号及 runFiber 持久执行能力，不重写已有聊天消息保存路径。

### 要解决的技术问题

Think agent 现有的 chat() RPC 接口和 saveMessages() 接口均为同步阻塞模式：调用方提交消息后，必须等待模型推理、工具执行、流式输出和消息持久化全部完成后才能获得返回结果。在多轮工具调用场景下，单次推理可能持续数十秒甚至数分钟。对于 webhook、消息队列消费者或其他 Worker 等外部调用方，长时间的同步等待会占用连接资源、增加超时风险，且在调用方因超时或网络问题重试时，可能导致同一消息被重复插入或同一推理任务被重复执行。现有架构中缺少一种"快速确认接收"与"模型推理最终完成"之间的持久状态边界，使得外部调用方无法可靠地提交、追踪和控制一次 agent 对话任务。

### 核心技术方案

核心思路是在 Think agent 的 SQLite 存储层中新增一张任务表（cf_agent_tasks），将外部任务提交的接收阶段与执行阶段解耦。提交阶段：接收调用方请求、检查幂等键、写入任务记录和用户消息、返回即时确认。执行阶段：由 TurnQueue 异步接管任务，驱动 agentic loop 完成推理，更新任务状态。崩溃恢复阶段：在 DO 重启时扫描未终态的任务记录，根据其状态和持久化消息决定恢复执行或标记失败。

任务表结构：cf_agent_tasks 表包含 idempotency_key（幂等键，唯一索引）、task_id（主键）、status（pending/running/completed/failed/cancelled）、user_message_id（关联 assistant_messages 中的用户消息）、assistant_message_id（关联助手回复）、request_id（关联 AbortRegistry 取消控制器）、error_message、以及 created_at/updated_at/completed_at 时间戳。该表在 Think agent 初始化时通过 ensureTable 模式自动创建。

### 任务提交流程

Think agent 对外暴露 submitTask(idempotencyKey, userMessage, options?) 方法作为外部任务提交的统一入口。该方法执行以下步骤：

1. 幂等检查：以 idempotencyKey 查询 cf_agent_tasks 表。若已存在记录，根据其当前状态决定行为——若为 pending 或 running，直接返回已有的 task_id 和状态，不重复插入消息也不重复创建任务；若为 completed/failed/cancelled 等终态，返回已有结果。这保证调用方因超时或网络问题重试同一请求时，系统不会重复插入用户消息或重复执行推理。
2. 任务记录写入：若幂等键不存在，生成 task_id，向 cf_agent_tasks 表 INSERT 一条 status=pending 的记录，同时通过 Session.appendMessage 将用户消息写入 assistant_messages 表（消息 ID 使用 task 携带的 message_id 或由系统生成，Session 层已有的按 ID 去重逻辑提供额外保护）。此 INSERT 在幂等键唯一索引的保护下是安全的——即使两个并发请求携带相同幂等键，数据库唯一约束会使其中一个失败，调用方可安全重试。
3. 即时返回：任务记录和用户消息持久化完成后，方法立即返回 { taskId, status: 'pending' }，不等待推理执行。
4. 异步执行入队：在返回之前，通过 TurnQueue.enqueue 将任务提交到执行队列。enqueue 的 generation 机制确保如果会话在任务排队期间被清空，该任务会被标记为 stale 并跳过执行。

### 任务执行与状态转换

当任务到达 TurnQueue 队首时，执行以下状态转换和推理流程：

1. 状态更新为 running：更新 cf_agent_tasks SET status='running', updated_at=now()，并生成 request_id 关联到 AbortRegistry。
2. agentic loop 执行：复用 Think 现有的 _runInferenceLoop 和 _streamResult 路径，包括 assembleContext 上下文组装、getModel 模型选择、streamText 推理调用、工具执行循环、流式输出累积。与 WebSocket 路径的关键区别在于：流式输出不发送到客户端连接，而是缓冲到 StreamAccumulator 并批量持久化。
3. 消息持久化：推理完成后，将 StreamAccumulator 构建的助手消息通过 _persistAssistantMessage 写入 assistant_messages 表，同时将消息 ID 回写到 cf_agent_tasks.assistant_message_id。
4. 终态更新：根据推理结果更新 status 为 completed 或 failed。成功时写入 assistant_message_id；失败时写入 error_message。更新 completed_at 时间戳。

整个执行过程包装在 keepAliveWhile 中，防止 DO 在长推理期间因空闲而被驱逐。在 Think 的 chatRecovery 配置开启时，进一步包装在 runFiber 中——如果 DO 在推理期间被代码更新或异常驱逐，runFiber 的持久化机制确保任务状态和已持久化的部分消息不丢失，下一次 DO 激活时通过 onFiberRecovered 继续执行。

### 状态查询、取消与清理

Think agent 对外暴露三个管理面方法，供外部调用方追踪和控制已提交的任务：

inspectTask(taskId)：查询任务状态。从 cf_agent_tasks 表读取 status、error_message、created_at、completed_at 等字段。若任务状态为 completed 且 assistant_message_id 非空，可进一步通过 Session.getMessage 读取助手回复内容一并返回。该方法为纯读操作，不修改任何状态。

cancelTask(taskId)：取消任务。若任务状态为 pending，直接将 status 更新为 cancelled 并记录 updated_at，TurnQueue 的 generation 检查会在任务到达队首时发现状态已非 pending 并跳过执行。若任务状态为 running，通过 AbortRegistry.cancel(requestId) 触发推断循环中的 abortSignal，使 streamText 中断；中断后 agentic loop 的 finally 块将持久化部分消息、更新 status 为 cancelled。对于已处于终态（completed/failed/cancelled）的任务，cancelTask 返回当前状态，不产生副作用。

deleteTask(taskId)：清理任务。将 cf_agent_tasks 中对应记录删除或标记为 deleted。可选地，通过 Session.deleteMessages 删除该任务关联的用户消息和助手回复消息。对正在 running 的任务，先执行 cancelTask 逻辑再清理。该操作不删除同一会话中其他来源的消息，不影响普通聊天路径。

### 崩溃恢复与持久状态边界

本方案的关键技术特征在于明确界定了"快速确认接收"和"模型推理最终完成"之间的持久状态边界，确保在 DO 生命周期内的任意时刻发生崩溃或驱逐时，系统状态均可恢复且不会产生副作用。

场景一——消息已写入、任务记录已写入、推理尚未开始：DO 重启后，cf_agent_tasks 表中存在 status=pending 的记录且 assistant_messages 表中存在对应的用户消息。Think 的 onStart 初始化阶段扫描 cf_agent_tasks 表，找出所有 status=pending 的任务，将其重新入队到 TurnQueue。由于用户消息已持久化，重新入队后推理流程与正常路径完全一致。

场景二——推理进行中（TurnQueue 已接管、status 已更新为 running）：若 chatRecovery 开启，推理体包装在 runFiber 中，DO 驱逐后 runFiber 通过 onFiberRecovered 恢复执行，无需额外处理。若 chatRecovery 未开启，onStart 扫描时将 status=running 且 updated_at 超过合理超时阈值（如 15 分钟）的任务标记为 failed，并写入 error_message='recovery timeout'。这样避免了因推理进程消失而永久停留在 running 状态。

场景三——推理已完成、终态已更新、DO 尚未被查询：任务状态已为 completed/failed/cancelled，onStart 扫描时跳过这些记录，不做额外处理。外部调用方通过 inspectTask 随时可查询到终态结果。

场景四——重复提交与消息去重：幂等键的唯一索引在数据库层面保证 cf_agent_tasks 中不会出现重复记录。用户消息层面，Session.appendMessage 在写入前检查消息 ID 是否已存在（assistant_messages 表的主键约束），已存在的消息会被跳过。两个层面的去重共同保证：即使调用方在提交响应丢失后重试，系统也不会重复插入用户消息，也不会创建重复任务。

### 与现有路径的兼容性

本方案在以下方面保证与 Think 现有架构的兼容性，不重写已有路径：

- TurnQueue 复用：任务执行通过现有的 TurnQueue.enqueue 入队，与 WebSocket 请求和 saveMessages 调用共享同一序列化队列。TurnQueue 的 generation 机制自动处理会话清空场景——若用户在任务排队期间清空了会话，generation 递增会使该任务变为 stale 并跳过执行。
- AbortRegistry 复用：cancelTask 通过 AbortRegistry.cancel(requestId) 触发取消，与 WebSocket 的 cf_agent_chat_request_cancel 消息共享同一取消通道。streamText 中的 abortSignal 同时响应外部取消和内部取消。
- Session 存储复用：任务关联的用户消息和助手回复消息写入 assistant_messages 表，与普通聊天消息完全一致。任务表是独立的新增表，不修改 assistant_messages 的已有 schema。deleteMessages 和 clearMessages 操作仅影响指定的消息 ID 范围，不影响其他来源的消息。
- 消息广播兼容：任务执行过程中持久化的消息通过现有的 _broadcastMessages 机制广播给所有连接的 WebSocket 客户端，因此通过 UI 打开的同一会话也能看到外部提交的任务消息和回复。
- 普通聊天路径不受影响：WebSocket 的 _handleChatRequest 路径和 RPC 的 chat()/saveMessages() 路径不做任何修改。cf_agent_tasks 表仅由 submitTask 写入，其他路径不感知任务表。

### 技术效果

本方案带来的技术效果包括：

- 可靠的任务提交：幂等键机制确保外部调用方可以安全重试提交请求，不会因网络抖动导致消息重复或任务重复。调用方在提交后立即获得确认，无需保持长连接等待推理完成。
- 明确的持久状态边界：通过 cf_agent_tasks 表将"已接收待执行"和"推理完成"两个阶段在持久化层面明确分离。每一个状态转换都伴随 SQLite 写入，DO 在任意时刻崩溃后都能从持久化状态判定恢复策略。
- 完整的任务生命周期管理：外部调用方可查询任务进度、取消执行中的任务、清理已完成的任务，获得了对 agent 对话任务的完整控制面，而不仅仅是"提交即遗忘"。
- 与现有架构的平滑集成：方案完全复用 Think 现有的 TurnQueue、AbortRegistry、Session 存储和 runFiber 持久执行能力，不引入新的并发模型或存储抽象。新增代码集中在 submitTask、inspectTask、cancelTask、deleteTask 四个方法和一张独立的 cf_agent_tasks 表，不影响已有聊天路径的任何行为。
- 并发安全：TurnQueue 的序列化执行保证同一 DO 内不会有两个推理任务并发运行。幂等键唯一索引在数据库层面保证并发提交同一请求时的安全性。AbortRegistry 的取消操作与 TurnQueue 的 generation 检查共同保证取消操作的最终一致性。

### 风险与待确认问题

以下为当前方案中需要后续确认和关注的风险点：

- 幂等键保留窗口：cf_agent_tasks 表随时间增长，已完成的任务记录会持续占用存储空间。需要设计保留策略——例如按 completed_at 时间清理超过 N 天的终态记录，或在 deleteTask 时物理删除。保留窗口过短可能导致调用方在合理时间范围内重试时无法命中已有记录。
- running 状态超时判定：DO 驱逐后，running 状态的任务如果没有 chatRecovery 保护，需要依赖超时阈值将其标记为 failed。阈值设置需要权衡：过短可能误杀正在执行的正常任务；过长则导致调用方长时间看不到终态。建议基于 DO 的 alarm handler 超时（15 分钟）和典型推理时长设定。
- 流式输出交付方式：当前方案中任务执行的流式输出不直接推送给外部调用方，而是缓冲后持久化。如果调用方需要流式获取中间结果，需要在 inspectTask 中增加流式查询能力或额外提供事件推送通道。这属于后续增强方向。
- 与 Sub-agent 路由的交互：在多会话场景（Chats + Think child）中，submitTask 的目标是具体的 Think child DO。调用方需要通过 Chats 的 subAgent 路由获取 child stub 后再调用 submitTask。需要确认 Chats 的路由层是否需要感知任务概念。
