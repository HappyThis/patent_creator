## 技术方案

本方案在 Think agent 现有会话、消息持久化、流式执行、恢复和取消语义的基础上，增加一套面向外部调用方的任务提交与生命周期管理机制，使 webhook、RPC 调用方或其他 Worker 能够以非阻塞方式提交对话任务、获取状态、取消或清理任务，同时保留普通聊天消息的完整保存路径不变。

### 技术问题

Think agent 当前通过 WebSocket 协议和 DO RPC 的 chat() / saveMessages() 方法接收对话请求。这两种路径都是同步等待模型推理完成后才向调用方返回结果。外部系统（如 webhook 处理器、定时任务、RPC 调用方）需要的是：提交任务后立即获得"已接收"确认，后续再通过异步查询获取执行状态。此外，外部系统可能因超时或网络抖动重试同一请求，系统必须避免由此导致的重复插入消息或重复执行。

### 整体架构

方案在 Think agent 的 SQLite 存储层新增一张任务提交表（think_submissions），记录每次外部提交的完整生命周期。该表与现有的 Session 消息表（assistant_messages）和 TurnQueue 串行队列协同工作，但不修改这三者的既有结构和语义。外部调用方通过 Think agent 暴露的 DO RPC 方法进行任务提交、状态查询、取消和清理。

### 任务提交表设计

think_submissions 表是方案的核心持久化结构，每条记录代表一次外部提交。表结构如下：

- id：任务唯一标识，由系统在提交时生成（UUID），同时作为对外返回的任务句柄
- idempotency_key：调用方提供的幂等键，与调用方身份标识组合形成唯一约束，用于去重
- session_id：关联的 Think Session 标识，指向消息所属的会话
- status：任务状态，枚举值为 received（已接收）、queued（已入队）、processing（执行中）、completed（已完成）、failed（已失败）、cancelled（已取消）
- request_id：关联的 TurnQueue 请求 ID，仅在 status 进入 queued 后写入
- user_message_id：关联的用户消息 ID，仅在消息成功写入 Session 后写入
- assistant_message_id：关联的助手回复消息 ID，在推理完成后写入
- error_message：失败原因，仅在 status 为 failed 时非空
- created_at、updated_at：时间戳，用于排序和清理策略

### 幂等提交与快速确认

外部调用方在提交时携带一个由自身生成的幂等键（idempotency_key）。系统在 think_submissions 表上对 (caller_identity, idempotency_key) 组合建立唯一索引。提交处理流程如下：

1. 调用方调用 submitTask RPC 方法，传入会话标识、用户消息内容和幂等键
2. 系统使用 INSERT OR IGNORE 语义尝试写入 think_submissions 表，状态初始为 received
3. 若唯一约束冲突（即同一调用方以相同幂等键重复提交），INSERT 被忽略，系统直接返回已存在记录的 id 和当前状态，不重复创建任务、不重复插入用户消息
4. 若插入成功，系统立即将任务 ID 返回给调用方，确认"已接收"，调用方可断开连接
5. 系统随后异步将任务从 received 推进到 queued，在此过程中将用户消息写入 Session，并将任务注册到 TurnQueue 的串行队列中

### 任务状态机与生命周期

任务状态机严格按照以下顺序转换，每个转换在 SQLite 事务中原子完成：

1. received → queued：用户消息成功写入 Session（通过 session.appendMessage），任务注册到 TurnQueue。此时写入 user_message_id 和 request_id。若消息写入后、入队前系统崩溃，恢复时通过 status=received 且无 request_id 的记录识别未完成入队的任务，重新执行入队操作
2. queued → processing：TurnQueue 将任务出队并开始执行推理循环。此转换在 TurnQueue.enqueue 的回调被实际调用时发生。TurnQueue 的 generation 机制保证若会话在排队期间被清除，任务以 skipped 状态结束
3. processing → completed：推理循环正常结束，助手消息持久化到 Session。写入 assistant_message_id
4. processing → failed：推理过程异常终止（模型错误、工具执行失败等）。写入 error_message。部分已生成的助手消息仍持久化到 Session（沿用 Think 现有错误处理策略）
5. processing → cancelled：调用方通过 cancelTask RPC 方法请求取消。利用 Think 现有的 AbortRegistry，以 request_id 为键中止对应的 AbortController，推理循环的 abortSignal 被触发，流式输出终止

### 崩溃恢复与持久状态边界

方案利用 Think agent 现有的 runFiber 持久执行机制保障任务在 Durable Object 休眠或崩溃后的可恢复性。关键边界处理如下：

- 提交写入崩溃：外部调用方的 HTTP/RPC 请求在 INSERT 返回前中断。由于 SQLite 写入在 DO 请求处理周期内是原子的，要么整行已持久化（任务可恢复），要么完全未写入（调用方重试，幂等键保证去重）
- 消息写入后崩溃：用户消息已通过 session.appendMessage 写入 assistant_messages 表，但任务尚未入队。恢复路径：在 onStart 或 onFiberRecovered 中扫描 status=received 且 request_id 为空的记录，重新将消息关联到 TurnQueue
- 推理执行中崩溃：任务处于 processing 状态，runFiber 的 cf_agents_runs 表中存在对应的 fiber 行。恢复路径：onFiberRecovered 被调用，系统检查 think_submissions 中对应 request_id 的状态。若消息已部分持久化（resumable_stream 中有 chunk），通过 onChatRecovery 钩子继续或保存部分结果。若未产生任何输出，将任务标记为 failed 并写入崩溃指示
- 迟到结果处理：若任务已被取消，但推理循环在取消信号到达前已完成并尝试写入结果，系统在更新状态前检查当前状态是否为 cancelled，若是则丢弃结果不覆盖状态

### 管理接口

Think agent 暴露以下 DO RPC 方法供外部调用方使用，所有方法均复用现有 Durable Object 的自动路由和身份机制：

- submitTask(sessionId, message, idempotencyKey)：提交任务。返回 { taskId, status: 'received' } 或已有任务的 { taskId, status }
- getTask(taskId)：查询任务状态。返回 think_submissions 中对应行的 id、status、error_message、created_at、updated_at，以及可选的消息摘要
- listTasks(sessionId, options?)：列出指定会话下的任务，支持按状态过滤和时间范围分页
- cancelTask(taskId)：取消任务。将状态更新为 cancelled，通过 AbortRegistry 以 request_id 中止推理。若任务已处于终态（completed/failed/cancelled），返回当前状态不变
- deleteTask(taskId)：清理任务记录。仅允许删除处于终态的任务。删除 think_submissions 行，可选清理关联的 resumable_stream 数据。不影响已持久化到 assistant_messages 的聊天消息

### 与现有路径的兼容性

本方案的关键设计约束是不重写 Think 现有的普通聊天消息路径。为此采取以下兼容策略：

- 消息持久化路径不变：外部提交的用户消息通过 session.appendMessage 写入，与 WebSocket 路径使用的完全相同的 Session 方法。消息的树形结构（parent_id）、compaction、FTS5 搜索等能力对两种来源的消息一视同仁
- TurnQueue 串行语义不变：外部提交的任务通过相同的 TurnQueue.enqueue 入队，与 WebSocket 提交的消息共享同一串行队列。generation 机制在 chat-clear 时同时使外部任务变为 stale
- 推理循环复用：任务进入 processing 后，完全复用 Think 现有的 _runInferenceLoop 和 _streamResult 方法，享有相同的工具执行、流式输出、错误处理和部分消息持久化逻辑
- AbortRegistry 复用：cancelTask 通过 AbortRegistry.cancel(requestId) 中止推理，与 WebSocket 的 chat-request-cancel 消息走完全相同的取消路径
- think_submissions 表为可选加入：不启用外部提交的 Think agent 实例无需创建该表，零开销。Session、TurnQueue、AbortRegistry 等现有组件不受影响

### 技术效果

相比现有方式，本方案带来以下技术效果：

- 快速确认与异步解耦：外部调用方提交任务后立即获得 taskId 和 received 状态，无需阻塞等待模型推理完成（可能持续数十秒至数分钟）。调用方与 Think agent 的生命周期解耦，各自独立伸缩和恢复
- 幂等去重：基于 (caller_identity, idempotency_key) 唯一约束的去重机制，在数据库层面防止重复提交。相比应用层检查，无需额外查询即可在 INSERT 时判定，且崩溃后恢复的一致性由 SQLite 事务保证
- 可恢复的状态边界：think_submissions 表作为持久状态锚点，使 received 到 completed 之间的每个状态转换都有明确的持久化时机。结合 runFiber 的恢复机制，崩溃后可从任意中间状态继续或安全终止
- 统一的管理面：外部调用方通过统一的 RPC 接口进行提交、查询、取消和清理操作，无需理解内部 TurnQueue、AbortRegistry 或 Session 的实现细节。列表查询支持按会话和状态过滤，便于构建监控和控制面板
- 普通路径零影响：所有新增机制通过 think_submissions 表独立运作，不修改 assistant_messages 表结构、不改变 TurnQueue 串行语义、不侵入推理循环。关闭外部提交功能时，系统行为与改造前完全一致

### 风险与待确认问题

以下为需要后续确认的风险点和设计决策：

- idempotency_key 的保留窗口：幂等键唯一约束在 SQLite 中持久存在，不会自动过期。需确认是否需要基于 created_at 的定期清理策略（例如保留最近 24 小时的幂等键），以避免 think_submissions 表无限增长
- 与 schedule/queue 系统的关系：Agent 基类已有 cf_agents_queues 表和 queue()/dequeue() 方法。需确认 think_submissions 是否应与现有队列系统统一，还是作为独立的语义层存在。当前方案倾向于独立，因为 think_submissions 面向外部调用方的任务生命周期管理，而 cf_agents_queues 面向 Agent 内部方法调用的 FIFO 队列
- 并发执行策略：当前 TurnQueue 严格串行化同一会话的对话轮次。若外部调用方需要同一会话内并发执行多个任务，需要评估 TurnQueue 的扩展方案或引入独立的任务执行槽
- 任务结果的通知机制：当前方案仅提供轮询式状态查询。是否需要增加 webhook 回调或 WebSocket 推送通知机制（如在任务完成时向调用方指定的 URL 发送回调），取决于实际使用场景
