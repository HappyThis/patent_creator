## 技术方案

### 技术问题与设计目标

Think agent 当前通过 WebSocket 协议（cf_agent_chat_*）和 Durable Object RPC（chat 方法）接收对话请求，所有入口路径均要求调用方在模型推理完成后才能得到最终响应。对于 webhook、RPC 调用方或其他 Worker 等外部系统而言，这种同步等待模式存在以下不足：（1）调用方必须维持长连接直到推理完成，增加超时风险；（2）外部系统因网络超时或连接中断重试同一请求时，可能导致重复插入用户消息或重复执行推理；（3）缺少标准化的状态查询、取消和清理接口，外部系统无法追踪已提交任务的执行进度。

### 整体架构

本方案的设计目标是：在 Think 现有架构上增加一层轻量级任务管理层，使外部调用方能够以一次 HTTP/RPC 调用提交对话任务并立即获得确认响应，后续通过轮询或查询接口获取执行状态，必要时取消或清理任务。方案遵循以下约束：（1）不重写 Think 现有的消息保存路径、Session 存储层或 agentic loop 推理流程；（2）复用已有的 TurnQueue、AbortRegistry、ResumableStream 和 fiber 机制；（3）通过幂等键保证重复提交不会产生副作用。

本方案在 Think Durable Object 内部新增一个任务管理子模块，负责在外部提交请求与 Think 现有推理流水线之间建立持久状态边界。外部调用方通过 HTTP endpoint（如 onRequest）或 RPC 方法提交任务，系统立即生成任务记录并返回接收确认；任务记录持久化到 Durable Object SQLite 中，随后通过已有的 TurnQueue 排队进入推理流水线。推理完成后，任务记录更新为终态。外部调用方可通过独立的状态查询接口获取当前任务状态和结果。

整体流程如下：（1）外部调用方携带幂等键和对话内容调用任务提交接口；（2）系统在 SQLite 中原子性地检查幂等键是否已存在，若为重复请求则直接返回已有任务记录；（3）若为新请求，则写入一条状态为"已接收"（received）的任务记录，持久化用户消息到 Session 存储，然后向调用方返回任务标识和"已接收"确认；（4）任务通过 TurnQueue 排队进入 agentic loop 执行推理；（5）推理过程中，任务状态逐步更新为"执行中"（running）、"已完成"（completed）、"已失败"（failed）或"已取消"（aborted）；（6）外部调用方通过状态查询接口轮询任务状态，或通过取消接口提前终止执行。

### 任务提交与幂等去重

任务提交接口接收外部调用方提供的幂等键（idempotency key）和对话输入（文本或 UIMessage），在执行任何持久化操作之前，首先在任务表中基于幂等键进行去重检查。任务表（如 cf_agent_tasks）在 Durable Object SQLite 中定义，包含以下核心字段：任务标识（task_id）、幂等键（idempotency_key，UNIQUE 约束）、请求标识（request_id，关联 TurnQueue 中的执行轮次）、状态（status）、用户消息标识列表、创建时间、状态更新时间、结果摘要和错误信息。

去重机制的工作原理如下：系统使用 SQLite 的 INSERT OR IGNORE 语义原子性地尝试插入任务记录。若幂等键已存在（违反 UNIQUE 约束），INSERT 被忽略，系统随后 SELECT 该幂等键对应的已有任务记录并返回——无论该任务是正在执行、已完成还是已失败。这保证了即使外部系统因超时或网络中断重试同一请求，也不会重复插入用户消息或重复触发推理执行。幂等键的保留窗口可在任务完成后的合理时间段（如 24 小时）内维持有效，超期后由定期清理任务移除，以控制存储增长。

消息写入与任务记录的持久化顺序遵循"先消息、后任务"原则：（1）调用 Session.appendMessage 将用户消息持久化到 assistant_messages 表（复用 Session 已有的 INSERT OR IGNORE 按消息 ID 去重）；（2）随后将任务记录 INSERT 到任务表。如果在步骤 1 和步骤 2 之间发生崩溃（DO 被回收），恢复时任务表中无对应记录，但消息表中已存在用户消息。此时外部调用方因未收到确认而重试，重试请求携带同一幂等键，系统可通过幂等键在任务表中无匹配记录这一事实，判断需要重新创建任务记录（而非直接返回已有结果），同时消息表因消息 ID 不变而被 Session 的 INSERT OR IGNORE 去重，不会产生重复消息。

### 持久状态机与生命周期管理

任务在其生命周期内经历明确的状态转换。状态机包含以下状态：received（已接收，任务已持久化但尚未开始推理）、running（执行中，已通过 TurnQueue 进入推理流水线）、completed（已完成，推理正常结束且助手消息已持久化）、failed（已失败，推理过程中发生未恢复的错误）、aborted（已取消，被外部调用方或系统取消）。状态转换规则如下：

1. received → running：当 TurnQueue 调度到该任务时，在调用 _runInferenceLoop 之前将状态更新为 running。此更新必须是条件性的（WHERE status = 'received'），防止在崩溃恢复场景下重复启动。
2. running → completed：推理流水线正常完成（_streamResult 完成、助手消息持久化），更新状态为 completed 并写入结果摘要。
3. running → failed：推理过程中抛出未恢复的错误，更新状态为 failed 并写入错误信息。部分已流式输出的内容仍按 Think 现有的部分持久化策略保存。
4. running → aborted：外部调用方通过取消接口发出取消请求，或 AbortRegistry 中的 AbortSignal 被触发（如 DO 关闭、调度超时），更新状态为 aborted 并保留已持久化的部分结果。
5. 任意非终态 → aborted：外部调用方通过清理/删除接口请求终止任务，系统对非终态任务执行取消操作并更新状态。

状态写入的时机和原子性保证是关键设计点。所有状态更新均通过 SQLite UPDATE 语句执行，利用 Durable Object 的单线程执行模型保证状态写入的序列化。从 received 到 running 的转换使用"条件 UPDATE"（UPDATE ... WHERE status = 'received'），确保即使在 DO 休眠恢复后重新执行任务调度逻辑时，已被其他路径启动的任务不会被重复启动。此机制与 Think 现有的 TurnQueue 世代号（generation）机制正交——TurnQueue 的 reset 用于清除对话级别的排队，任务状态机用于追踪单个任务的生命周期。

### 崩溃恢复与执行领取

Think agent 作为 Cloudflare Durable Object 运行，可能在推理执行期间因空闲而被休眠回收（eviction），随后在下次请求到达时重新激活。崩溃恢复机制需要处理以下几种场景：（1）任务记录已持久化但推理尚未启动时发生休眠；（2）推理执行中发生休眠，ResumableStream 中保留部分流式数据块；（3）推理完成但任务状态更新尚未写入时发生休眠。

DO 重新激活后的恢复流程如下：在 onStart 生命周期中，系统检查任务表中所有状态为 received 或 running 的记录。对于 received 状态的任务，若其关联的消息已存在于 Session 的 assistant_messages 中，说明消息写入步骤已在休眠前完成，系统将其重新加入 TurnQueue 排队执行；若消息不存在，说明消息写入尚未完成但任务记录已写入（"先消息后任务"顺序不会出现此情况，但作为防御性检查存在）。对于 running 状态的任务，系统检查 ResumableStream 是否存有对应的活跃流记录：若有，说明推理在流式输出过程中中断，按 ResumableStream 已有的恢复逻辑重建流状态并继续执行；若无活跃流记录但消息表中已有部分助手消息，说明推理完成但状态更新未写入，系统判断助手消息的完整性后更新任务状态为 completed 或 failed。

执行领取（execution claim）的并发控制利用 Durable Object 的单线程语义自然实现——同一 DO 实例不会并发执行多个恢复流程。对于 chatRecovery 模式（fiber 包裹的推理轮次），恢复时通过检查 cf_agents_runs 表中是否有对应 fiber 名称的记录来判断是否已有线程在执行：若 fiber 记录存在且状态为 running，则说明 DO 在 fiber 执行期间被回收，DO 重新激活后 Agent 基类的 onFiberRecovered 回调被触发，在回调中按上述恢复流程处理任务状态；若 fiber 记录不存在或已完成，则按常规路径处理。该机制与 Think 现有的 chatRecovery 标志和 runFiber 基础设施完全兼容，无需重写 fiber 管理逻辑。

### 状态查询、取消与清理

外部调用方通过以下接口与任务进行交互，所有接口均通过 Think 已有的 onRequest（HTTP）或 RPC 方法暴露：

- 状态查询（GET /tasks/:taskId 或 RPC inspectTask）：读取任务表中对应 taskId 的记录，返回当前状态、创建时间、状态更新时间、结果摘要（终态时）和错误信息（失败时）。对于 running 状态的任务，可选地返回 ResumableStream 中已流式输出的部分文本。
- 取消（POST /tasks/:taskId/cancel 或 RPC cancelTask）：对非终态任务执行取消操作。实现方式为：查找该任务关联的 requestId，通过 AbortRegistry 触发对应 AbortController 的 abort 方法，这与 Think 现有的 WebSocket cancel 消息处理路径完全一致。AbortRegistry 的取消信号传导至 streamText 的 abortSignal，导致推理循环中止。取消后将任务状态更新为 aborted，并按 Think 现有的部分持久化策略保留已流式输出的内容。
- 清理/删除（DELETE /tasks/:taskId 或 RPC cleanupTask）：对已完成、已失败或已取消的任务，删除任务记录及其关联的流式数据块（ResumableStream 中的 cf_ai_chat_stream_chunks 记录）。对 running 状态的任务，先执行取消再清理。清理操作可选地同时删除关联的对话消息（通过 Session.deleteMessages），或保留消息以备审计。

此外，提供列表接口（GET /tasks 或 RPC listTasks）用于管理面查询，支持按状态过滤（如仅列出 running 任务）和分页。该接口直接从任务表 SELECT 查询，不依赖 Session 的消息存储路径。可观察事件方面，任务状态变更时可选择性地通过 Think 的广播机制（broadcast）通知已连接的 WebSocket 客户端，或在 onChatResponse 生命周期钩子中触发外部回调（如 webhook 回执），但这些通知路径为可选增强，不改变核心状态机语义。

### 与现有消息存储路径的兼容

本方案的核心设计原则之一是不重写 Think 现有的消息保存路径。具体而言：（1）用户消息的持久化仍通过 Session.appendMessage 写入 assistant_messages 表，利用 Session 已有的 INSERT OR IGNORE 按消息 ID 去重和 parent_id 树结构管理；（2）助手消息的持久化仍通过 _persistAssistantMessage → Session.appendMessage/Session.updateMessage 路径，复用已有的 sanitizeMessage 和 enforceRowSizeLimit 处理；（3）推理流水线仍通过 _runInferenceLoop → streamText → _streamResult 路径执行，不改动 agentic loop 的内部逻辑；（4）TurnQueue 仍提供串行执行保证，任务管理层在 TurnQueue 之上增加了任务状态的持久化追踪，而非替代 TurnQueue。

任务提交路径与 Think 现有两条入口路径的连接点如下：对于 saveMessages 路径（程序化消息注入），任务管理层在 saveMessages 调用之前完成消息写入和任务记录创建，然后将任务关联的 requestId 传递给 saveMessages 的内部流程，saveMessages 完成后的 SaveMessagesResult（包含 requestId 和 status）用于更新任务状态。对于 chat（RPC 子代理）路径，类似地在 chat 调用之前完成消息写入和任务创建，chat 完成后的 onChatResponse 回调用于更新任务状态。两条路径的差异仅在于响应传递方式（流式回调 vs 轮询），不影响核心推理流程。

与对话清除（clear）操作的兼容：当用户通过 WebSocket 发送 cf_agent_chat_clear 清除对话时，Think 现有的 _handleClear 流程重置 TurnQueue、清空 Session 消息、清除 ResumableStream 状态。任务管理层在此流程中增加一步：将所有 running 或 received 状态的任务标记为 aborted（原因：对话已清除），但不删除任务记录本身，保证已提交任务的外部调用方仍能通过状态查询获取终态结果。这种设计使外部提交的任务在对话清除后保持可追溯性，同时不阻塞清除操作。

### 技术效果

本方案相比现有方式带来以下技术效果：

1. 快速确认与异步解耦：外部调用方在用户消息持久化后立即收到"已接收"确认，无需等待模型推理完成（推理可能耗时数十秒至数分钟）。提交和推理在时间维度上解耦，减少了调用方的超时风险和连接资源占用。
2. 幂等去重：通过幂等键和 SQLite UNIQUE 约束实现原子去重，重复提交不会产生重复消息或重复推理。"先消息后任务"的持久化顺序结合 Session 的消息级去重，保证了崩溃场景下的最终一致性。
3. 完整的生命周期追踪：通过持久状态机提供从 received 到终态的完整状态转换，外部调用方可随时查询任务进度。状态在 DO SQLite 中持久化，不受 DO 休眠回收影响。
4. 取消可控：通过 AbortRegistry 复用现有的取消信号路径，取消操作可传导至底层 streamText 的推理循环，避免无效计算资源浪费。已产生的部分结果按现有策略保留，不丢失上下文。
5. 崩溃恢复无数据丢失：利用 DO 重新激活时的 onStart 生命周期和 ResumableStream 的流恢复能力，休眠前后的推理进度得以保留。条件 UPDATE 保证 recovered 和 running 之间不存在重复启动竞态。
6. 与现有路径完全兼容：所有新增逻辑在 Think 现有架构之上工作，不改动 Session、agentic loop、消息序列化/清理、流式输出等核心模块的实现。普通 WebSocket 聊天路径完全不受影响，消息保存方式保持不变。

### 风险与待确认问题

本方案涉及的待确认和风险点如下：

- 幂等键保留策略：幂等键的保留窗口需要权衡存储开销和去重需求。建议默认保留 24 小时，超期后定期清理。具体保留窗口和清理频率需要根据实际负载确定。
- 结果传递方式：当前方案假设外部调用方通过轮询获取结果。是否需要支持回调（webhook 回执）作为可选的结果传递方式，需要根据业务场景确认。回调机制可在 onChatResponse 钩子中实现，但引入了回调失败重试的额外复杂度。
- 消息关联粒度：任务与消息的关联当前通过 taskId→消息 ID 列表的映射实现。若一个任务触发多轮对话（自动连续），多轮对话产生的消息如何归属需要明确——是在同一任务下累积，还是仅关联首轮响应。
- 与 SessionManager 多会话场景的关系：当前方案基于单个 Think DO 实例的单会话场景设计。若未来启用 SessionManager 的多会话功能（如通过 sessionId 区分不同对话），任务表需要增加 session_id 字段以隔离不同会话的任务。
- 并发任务限制：当前 TurnQueue 串行执行所有推理轮次，这意味着同一 DO 实例下的多个 submitted 任务将按序执行。如果业务需要多个外部提交任务并行执行，需要额外的调度机制，可能涉及跨 DO 实例的任务分发。
