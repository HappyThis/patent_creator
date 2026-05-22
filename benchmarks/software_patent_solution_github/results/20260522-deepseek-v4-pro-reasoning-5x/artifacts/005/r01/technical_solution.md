## 技术方案

本方案基于 Think agent（@cloudflare/think）现有架构，设计一套轻量级的外部任务提交与异步执行控制系统，使 webhook、RPC 调用方或其他 Worker 能够向 Think agent 提交对话任务后快速获得确认，而不必等待模型推理完成。方案复用 Think 现有的 Session 消息存储、TurnQueue 串行执行、saveMessages 程序化触发、runFiber 持久执行、AbortRegistry 取消机制和调度/队列系统，以最小侵入方式实现任务接收、去重、调度、状态追踪、取消和清理的全生命周期管理。

### 技术问题说明

Think agent 当前通过 WebSocket 协议或 RPC 调用接收对话消息后，在同一个请求-响应周期内完成消息持久化、模型推理、流式输出和结果持久化的完整链路。这意味着调用方必须保持连接并等待模型推理完成，无法在提交任务后立即获得确认。当外部系统（如 webhook、定时任务、RPC 调用方）需要向 Think agent 提交对话任务时，这种同步模式存在以下问题：（1）调用方因超时而断开连接，导致任务状态不确定；（2）调用方重试同一请求时，系统可能重复插入用户消息或重复执行推理；（3）调用方无法在提交后查询任务执行状态或取消任务。

### 整体架构

本方案在 Think 现有架构之上引入一个轻量级的任务提交层（Task Submission Layer），将"接收并确认"与"调度并执行"分离为两个阶段。该层由四个核心模块组成：任务接收器（Task Receiver）、幂等控制器（Idempotency Controller）、异步调度器（Async Dispatcher）和状态管理器（Status Manager）。整体架构如下：

- 任务接收器：对外暴露提交接口（HTTP endpoint 或 RPC 方法），接收外部调用方的任务请求，快速验证后返回包含任务标识的确认响应，不等待模型推理完成。
- 幂等控制器：基于请求中携带的幂等键（idempotency key），在消息写入前进行去重判断，保证同一请求被重试时不会产生重复的用户消息或重复推理。
- 异步调度器：利用 Think 现有的 saveMessages 语义和 TurnQueue 串行执行机制，将已持久化的用户消息排入推理队列，触发模型推理。调度器同时利用 Agent 基类的 schedule 和 runFiber 机制，保证任务在 Durable Object 休眠/唤醒周期中不丢失。
- 状态管理器：为每个提交的任务维护一条持久化的状态记录（存储于 SQLite），记录任务从"已接收"到"执行中"再到"已完成/已失败/已取消"的完整生命周期，供外部调用方查询和控制。

### 任务接收与快速确认机制

任务接收器在 Think agent 上新增一个对外接口（例如 HTTP POST /tasks 端点或通过 Agent RPC 暴露的 submitTask 方法），专门用于接收外部系统的任务提交。与现有 WebSocket chat 路径和 RPC chat() 路径并行存在，不修改二者的行为。

接收流程如下：（1）调用方发送请求，携带必要的对话内容（用户消息文本或 UIMessage 结构）、可选的幂等键（idempotency_key）和可选的客户端工具定义。（2）接收器首先检查幂等键：若该键已关联到一条已存在的任务记录，直接返回该任务的当前状态，不执行任何新的写入或调度。这是"快速确认"的第一层保障。（3）若幂等键不存在或未提供，接收器将用户消息通过 INSERT OR IGNORE 语义写入 Session 的消息表——该语义天然保证同一消息 ID 不会重复写入。（4）写入成功后，接收器在任务状态表中创建一条新记录，状态标记为"accepted"（已接收），并将该任务排入异步调度队列。（5）接收器立即向调用方返回包含 taskId、状态"accepted"和时间戳的确认响应。整个接收路径不等待 TurnQueue 中的推理开始，更不等待模型推理完成。

### 持久状态边界与崩溃恢复

"快速确认接收"和"模型推理最终完成"之间的持久状态边界是本方案的核心设计点。两类持久化操作分别落盘到不同的 SQLite 表中，且写入时序经过精心编排以支持崩溃恢复。

持久化层次分为三层：（1）消息层——用户消息通过 Session.appendMessage 写入 assistant_messages 表，使用 INSERT OR IGNORE 语义。该写入在任务状态记录创建之前完成，确保即使接收器在写入状态记录前崩溃，消息也已安全落盘。重启后，未被关联到任务记录的消息可通过"孤儿消息扫描"被重新调度。（2）任务状态层——任务元数据写入专用的任务状态表（如 cf_agent_task_submissions），包含 taskId、幂等键、状态枚举（accepted/dispatched/running/completed/failed/aborted/deleted）、关联的消息 ID、创建时间和状态变更时间。（3）执行层——调度器将任务状态从 accepted 更新为 dispatched，然后调用 saveMessages 触发推理。saveMessages 内部通过 TurnQueue.enqueue 排队，并且 Thinking 默认启用 chatRecovery = true，意味着每个推理轮次被包装在 runFiber 中。如果 Durable Object 在推理过程中被驱逐，runFiber 会在下次激活时通过 onFiberRecovered 钩子恢复执行，任务状态表中的记录会反映最终的执行结果。

崩溃恢复的具体机制：（1）Think agent 启动时（onStart），扫描任务状态表中状态为 accepted 或 dispatched 但尚未完成的记录，对每条记录检查关联的消息是否已持久化；若消息存在，将任务重新排入调度队列；若消息缺失（极端情况），将任务标记为 failed。（2）对于状态为 running 的记录，检查对应的 runFiber 是否已恢复；若 runFiber 未恢复且超过合理时间窗口，标记为 failed 并允许外部调用方重新提交。（3）孤儿消息扫描：查找 assistant_messages 中未被任何任务记录引用的用户消息（通过 role='user' 和时间窗口过滤），将其与可能的幂等键关联尝试匹配，若无法匹配则视为普通聊天消息保留，不破坏现有数据。

### 重复提交去重机制

去重机制在三个层面协同工作，覆盖外部重试、消息重复和推理重复三种场景。

第一层——幂等键去重：外部调用方在提交请求时携带幂等键（如 idempotency_key）。接收器在创建任务状态记录前，先查询任务状态表中是否存在相同幂等键的记录。若存在且状态不是 failed 或 deleted，直接返回该记录的当前 taskId 和状态，跳过所有后续写入和调度。幂等键与 taskId 的映射关系持久化在任务状态表中，在 Durable Object 休眠/唤醒周期中保持不变。若已存在的任务最终执行失败，调用方可使用相同幂等键重新提交，此时接收器检测到原任务状态为 failed，创建新任务记录并关联到该幂等键（覆盖旧映射）。

第二层——消息 ID 去重：用户消息通过 Session.appendMessage 写入，底层使用 INSERT OR IGNORE 基于消息 ID 主键。如果外部调用方在每次提交时使用相同的消息 ID（例如基于幂等键派生的确定性 UUID），即使幂等键检查因并发窗口而漏过，消息写入层面的主键冲突也会阻止重复插入。这与 Think 现有的消息持久化语义完全一致，无需修改。

第三层——推理去重：调度器在将任务从 accepted 变为 dispatched 时，检查该任务是否已被另一个并发调度路径处理。通过在任务状态行上使用 CAS（compare-and-swap）更新——即 UPDATE ... WHERE status = 'accepted'——保证只有一个调度路径能成功认领该任务。如果任务已被认领或已完成，后续的调度尝试将无操作。这与 Think 现有的 TurnQueue 代际（generation）跟踪机制配合，若用户在任务执行期间清空了对话，任务状态会被标记为 skipped 而非重复执行。

### 异步执行与状态追踪

任务提交后，系统通过以下机制完成异步执行和状态追踪。

调度与执行流程：（1）接收器完成任务状态记录创建后，触发调度信号。调度器查询状态为 accepted 的最早任务，通过 CAS 更新将其置为 dispatched。（2）调度器调用 Think 现有的 saveMessages 方法，传入已持久化的用户消息。saveMessages 内部将消息排入 TurnQueue，触发模型推理的完整生命周期：assembleContext → streamText → 流式输出 → 结果持久化 → onChatResponse。该路径完全不修改 Think 的推理逻辑，仅通过任务状态表追踪进度。（3）推理过程中，状态管理器在关键节点更新任务状态记录：推理开始时置为 running，正常完成时置为 completed 并记录输出消息 ID，异常失败时置为 failed 并记录错误信息，被取消时置为 aborted。

状态查询接口：外部调用方可通过 taskId 向 Think agent 查询任务状态。查询接口（例如 HTTP GET /tasks/:taskId 或 getTaskStatus RPC 方法）读取任务状态表中的记录，返回包含 taskId、status、创建时间、状态变更时间、以及（若已完成）关联的输出消息 ID 或错误信息的结构化响应。状态枚举值包括：accepted（已接收，尚未调度）、dispatched（已调度，等待 TurnQueue 执行）、running（推理进行中）、completed（推理已完成）、failed（推理失败）、aborted（已被取消）、deleted（已被清理）。

与现有调度和队列系统的关系：本方案的异步调度器复用 Agent 基类的 schedule 和 queue 能力，但将调度粒度控制在"任务"级别而非单个消息级别。对于需要延迟执行的任务（例如外部系统要求在指定时间执行），接收器可接受 scheduled_at 参数，利用 this.schedule 设置定时回调；对于需要可靠重试的任务，利用 schedule/queue 内置的 retry 配置。调度器还复用 keepAliveWhile 和 runFiber 机制，确保长时间推理任务不会因 Durable Object 空闲驱逐而丢失。

### 任务取消与清理

系统提供三种任务控制操作：取消、删除和清理。

取消操作：（1）外部调用方通过 taskId 发起取消请求（如 POST /tasks/:taskId/cancel）。（2）系统查询任务状态：若任务状态为 accepted 或 dispatched，直接从调度队列中移除该任务的调度条目并将状态置为 aborted；若任务状态为 running，通过 AbortRegistry 获取该任务对应的 AbortController 并调用 abort()，触发推理的取消信号。（3）AbortRegistry 是 Think 已有的机制——每个请求 ID 对应一个 AbortController，abort() 调用后，streamText 的 abortSignal 被触发，模型推理中止。Think 的现有行为是：中止后保留已生成的部分消息（partial persistence），onChatResponse 收到 status: 'aborted'。（4）任务状态管理器在 onChatResponse 中检测到 aborted 状态后，将任务状态记录更新为 aborted。

删除与清理：（1）删除操作将任务状态标记为 deleted，并可选地删除关联的用户消息和助手消息。删除时不删除任务状态记录本身（保留审计痕迹），仅标记状态。（2）清理操作在删除的基础上额外清理关联的流式缓冲数据（ResumableStream 的 cf_ai_chat_stream_chunks 和 cf_ai_chat_stream_metadata 表）、续状态（ContinuationState）以及可能的 client tools 持久化数据。（3）清理时需注意：仅清理该任务专属的数据，不影响同一会话中其他任务或普通聊天消息。通过消息 ID 关联来精确识别属于该任务的消息范围。

与现有 Clear 操作的关系：Think 现有的 _handleClear 操作会清空整个会话的消息、流状态和续状态。本方案的任务级取消/删除/清理与 Clear 是正交的——Clear 影响整个会话，任务操作只影响单个任务。当用户执行 Clear 时，系统遍历所有未完成的任务记录将其标记为 skipped（因为 TurnQueue 代际已变更），与 Think 现有的代际跳过机制一致。

### 与现有聊天消息路径的兼容

本方案的核心设计原则之一是"不重写"：任务提交路径作为 Think 现有三条消息入口路径（WebSocket chat、RPC chat()、saveMessages）之外的第四条路径，与前三者并行存在且互不干扰。

消息持久化路径兼容：（1）任务提交路径写入用户消息时，复用 Session.appendMessage 的 INSERT OR IGNORE 语义，与 WebSocket 路径的 _persistIncomingMessage 和 RPC chat() 路径的消息写入使用同一底层接口。（2）消息在 assistant_messages 表中与其他聊天消息共同存储，不引入额外的消息表或修改消息表结构。这保证了 Session 的 getHistory()、compaction、FTS5 搜索等功能对任务提交产生的消息同样生效。（3）消息广播（_broadcastMessages）不受影响——任务提交路径产生的消息变化同样通过 WebSocket 广播给已连接的浏览器客户端，用户可以在聊天界面中看到外部系统触发的对话。

推理路径兼容：（1）任务调度器通过 saveMessages 触发推理，saveMessages 内部走的是与 WebSocket 和 RPC 完全相同的 _runInferenceLoop → _streamResult → _persistAssistantMessage 流水线。（2）TurnQueue 的串行化执行保证了任务提交产生的推理与用户通过聊天界面提交的推理不会并发执行，而是按提交顺序依次处理。同一个 TurnQueue 代际机制（generation）对两种来源的提交一视同仁。（3）chatRecovery = true 意味着任务提交产生的推理同样被包装在 runFiber 中，在 Durable Object 驱逐后可通过 onChatRecovery 恢复。（4）onChatResponse、onChatError 等生命周期钩子对任务提交产生的推理同样触发，不区分消息来源。

不修改的模块清单：本方案不修改 assistant_messages 表结构、Session 的 getHistory/appendMessage/updateMessage/clearMessages 接口、TurnQueue 的 enqueue/reset 语义、StreamAccumulator 的流式累积逻辑、ResumableStream 的缓冲和重放机制、AbortRegistry 的注册和取消语义，以及现有的 WebSocket 协议消息类型和处理流程。

### 技术效果

本方案通过将"接收确认"与"推理执行"分离为两个阶段，在 Think agent 现有架构上以最小侵入方式实现了对外部系统触发任务的全生命周期支持，带来以下技术效果：

- 快速确认：外部调用方在消息持久化和任务记录创建完成后立即收到包含 taskId 的确认响应，耗时仅为 SQLite 写入的毫秒级别，不受模型推理耗时（秒至分钟级）影响。
- 可靠提交：三层去重机制（幂等键、消息 ID、CAS 调度认领）保证外部重试不会产生重复消息或重复推理，即使接收器在写入过程中崩溃，重启后的孤儿消息扫描和任务状态恢复也能保证 at-most-once 或 exactly-once 语义。
- 状态可观测：每个提交的任务在完整的生命周期中具有明确的状态枚举值，外部调用方可随时查询。任务状态持久化在 SQLite 中，在 Durable Object 休眠/唤醒周期中不丢失。
- 可控性：支持取消正在执行的任务（利用 AbortRegistry），删除已完成任务的消息（保留审计记录），以及清理任务关联的流式缓冲数据。
- 兼容性：不修改 assistant_messages 表结构、Session 接口、TurnQueue 语义、推理流水线和 WebSocket 协议。普通聊天、已存在的消息保存方式和现有工作流系统均不受影响。任务提交产生的消息同样支持 compaction、FTS5 搜索、context blocks 和流式重放。
- 崩溃恢复：消息先于任务状态记录写入，配合启动时的任务状态扫描和孤儿消息扫描，覆盖接收器崩溃、调度器崩溃和推理过程中 Durable Object 驱逐三种故障场景。runFiber 和 chatRecovery 机制保证推理过程本身可在驱逐后恢复。

### 风险与待确认问题

以下为当前方案中需要后续确认的风险点和待定设计决策：

- 任务状态表的具体 schema 设计：需要确认任务状态表（如 cf_agent_task_submissions）的字段是否与现有的 cf_agent_tool_child_runs 表合并或独立建表，以及是否需要迁移现有数据。
- 孤儿消息扫描的性能边界：启动时扫描未关联任务记录的用户消息，在消息量极大的会话中可能产生不可忽略的启动延迟。需评估是否需要为消息表增加 task_id 外键列来替代扫描方案。
- 任务清理的粒度：当调用方请求清理任务时，是否应同时删除该任务产生的助手消息，还是仅清理任务记录。删除助手消息可能影响后续对话的上下文连贯性。
- 并发调度竞争：如果多个调度信号同时触发（例如接收器写入后立即触发的调度 + 启动时的恢复扫描），CAS 更新机制能否完全消除竞争。需验证 SQLite 在 Durable Object 单线程模型下的行为。
- 幂等键的过期策略：长期保留幂等键映射可能导致存储膨胀。需设计合理的过期策略，例如基于时间的 TTL 或在任务进入终态（completed/failed/aborted/deleted）后保留一段时间再清除。
- 与现有 Agent Tool Child Run 机制的关系：Think 已有 startAgentToolRun/cancelAgentToolRun 提供类似的任务提交-追踪-取消能力。需评估本方案是复用该机制（扩展其功能）还是作为独立路径实现，以避免功能重叠和维护负担。
