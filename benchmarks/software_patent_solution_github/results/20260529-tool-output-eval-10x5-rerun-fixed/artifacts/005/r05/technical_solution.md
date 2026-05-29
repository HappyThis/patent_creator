## 技术方案

本方案在 Think agent 现有架构基础上，引入一套面向外部系统调用的异步任务提交与追踪机制，使 webhook、RPC 调用方或其他 Worker 能够以非阻塞方式向 Think agent 发起一次对话任务，立即获得提交确认，并后续通过任务标识符查询执行状态、取消或清理任务。方案在复用 Think 现有会话（Session）消息存储、流式执行（streamText）、恢复（runFiber/chatRecovery）和取消（AbortRegistry）语义的前提下，通过新增持久化任务表和配套生命周期方法，在不重写普通聊天消息保存路径的条件下支持可靠的外部任务提交。

### 1. 技术问题与方案边界

Think agent 当前提供 WebSocket（浏览器客户端）和 RPC（父 agent 调用子 agent）两类入口。无论哪种入口，调用方在发起一次对话请求后必须等待模型推理完成才能获得最终响应。当外部系统（如 webhook 回调、定时任务触发器、其他 Worker）需要向 Think agent 下达成百上千个对话任务时，这种同步等待模式带来三个问题：（1）调用方长时间占用连接等待推理完成，不利于高吞吐场景；（2）网络超时或中断导致调用方无法确认任务是否已被接收；（3）调用方重试同一请求时，现有机制会将消息重复插入会话，造成重复推理和消息冗余。

本方案要解决的核心问题是：在"调用方提交任务并获得快速确认"和"模型推理最终完成"这两个时刻之间，建立一个可靠的持久状态边界。方案不重写 Think 现有的 onChatMessage、会话消息存储、流式输出或 WebSocket 协议，而是在这些既有路径之上叠加一层轻量级任务管理层。

### 2. 持久任务表与状态机

方案在 Think agent 内部新增一张持久化任务表（例如 think_external_tasks），用于记录每一次外部提交的任务元数据。该表与 Think 已有的 assistant_messages（会话消息表）、think_config（配置表）并存于同一 Durable Object SQLite 实例中，借由 DO 的事务保证原子性。任务表的核心字段包括：task_id（主键）、idempotency_key（唯一幂等键，由调用方提供或系统自动生成）、status（任务状态枚举）、user_message（用户消息 JSON）、assistant_message_id（完成后回写的助手消息 ID）、created_at、updated_at、completed_at 和 error_message。

任务状态机定义以下状态及转换规则：

- accepted：任务记录已持久化，但尚未进入推理队列。由 submitTask 首次写入。
- running：任务已进入 TurnQueue 开始执行推理。由任务执行协程写入。
- completed：推理正常完成，助手消息已持久化到 Session。由任务执行协程写入。
- error：推理过程中发生不可恢复错误，但部分消息可能已持久化。由任务执行协程写入。
- cancelled：外部调用方通过 cancelTask 主动取消了任务。由取消操作写入。

状态在 SQLite 行中通过原子 UPDATE 推进，写入新状态时附带 WHERE 条件校验当前状态，防止并发场景下状态跳跃（例如已取消的任务不会被标记为完成）。崩溃恢复时，agent 在 onStart 中扫描所有 accepted 或 running 状态且 updated_at 早于一定阈值的任务，将它们标记为 error 并记录崩溃原因，确保外部调用方不会无限等待。

### 3. 任务提交流程与幂等机制

外部调用方通过新增的 RPC 方法 submitTask(userMessage, options) 提交任务。该方法接受用户消息（字符串或 UIMessage）和可选配置（idempotencyKey、AbortSignal 等），执行以下流程：

1. 幂等检查：若调用方提供了 idempotencyKey，系统首先在 think_external_tasks 表中按该键查找已有记录。若存在且状态为 accepted 或 running，则直接返回已有 task_id 和当前状态，不重复进入队列；若已完成、已取消或已出错，则返回对应状态和结果引用，调用方可据此决定是否重新提交。
2. 任务记录写入：对于新任务，生成 task_id（UUID），在 SQLite 中插入状态为 accepted 的行。idempotency_key 列上建有 UNIQUE 约束，若两个并发请求携带相同幂等键，INSERT 会因约束冲突而失败，后到达者读取已有行并返回已有 task_id，实现数据库层面的去重。
3. 快速确认返回：任务行写入成功后，方法立即向调用方返回包含 task_id 和状态 accepted 的响应，不等待推理开始。
4. 异步触发执行：在返回确认之后（同一 keepAliveWhile 作用域内），系统将任务入队：通过调用 Think 既有的 saveMessages 方法将用户消息注入 Session，saveMessages 内部将消息追加到 assistant_messages 表并触发 TurnQueue，依次完成 context 组装、streamText 推理、流式输出和助手消息持久化。任务表中的状态从 accepted 推进为 running，推理完成后推进为 completed（或 error）。
5. 结果关联：推理完成后，新持久化的助手消息 ID 被回写到任务表的 assistant_message_id 字段，供调用方后续通过 getTaskStatus 获取结果引用。

### 4. 状态查询、取消与清理

方案提供三个管理面 RPC 方法，供外部调用方在提交后对任务进行追踪和控制：

getTaskStatus(taskId)：查询任务当前状态。直接从 think_external_tasks 表读取指定 task_id 的行，返回 status、created_at、updated_at、assistant_message_id（若已完成）、error_message（若有）。对于 completed 状态的任务，调用方可进一步通过 assistant_message_id 从 Session 的 assistant_messages 表中获取完整的助手回复内容。该方法为只读操作，不修改任何状态。

cancelTask(taskId, reason?)：取消一个尚未完成的任务。方法通过原子 UPDATE 将状态从 accepted 或 running 推进为 cancelled，前提是当前状态确为 accepted 或 running（WHERE status IN ('accepted','running')）。若更新成功，系统调用 Think 既有的 AbortRegistry.cancel(requestId) 来中止正在进行的推理流（如果任务正在 running）。已中止的推理流仍会保留流式过程中产生的不完整助手消息——这与 Think 既有的"中止时保留部分消息"语义一致。若任务已处于终态（completed、error、cancelled），cancelTask 为无操作并返回当前状态。

deleteTask(taskId)：清理已完成或已取消的任务记录。将指定 task_id 的行从 think_external_tasks 表中删除。该方法不删除关联的会话消息——消息仍然保留在 assistant_messages 表中，遵循 Session 自身的生命周期（可通过 clearMessages 或其他方式管理）。这保持了任务管理层和消息存储层的职责分离。

### 5. 崩溃恢复与消息写入前后的一致性

方案将"快速确认接收"与"模型推理最终完成"之间的持久状态边界落实在 think_external_tasks 表上，并通过以下机制覆盖多种崩溃场景：

- 场景一——任务行已写入但推理尚未开始（accepted）：DO 在崩后重启时，onStart 执行恢复扫描：查询所有 status='accepted' 且 updated_at 早于当前时间减去心跳超时阈值的任务。对于这些任务，系统重新将其加入 TurnQueue 执行队列（复用原有 idempotency_key 和 user_message），状态更新为 running。由于 idempotency_key 的唯一约束，即使此时外部调用方因超时而重试提交同一请求，重试的 INSERT 也会因冲突而返回已有 task_id，不会重复创建任务。
- 场景二——推理进行中崩（running）：任务处于 running 状态且关联的 runFiber 尚未完成。DO 重启后，runFiber 机制（继承自 Agent 基类）通过 cf_agents_runs 表中的持久化检查点自动恢复执行；任务表中的 running 行被 onStart 扫描到时，会检查是否存在对应的活跃 fiber，若不存在且 updated_at 已超时，则将状态置为 error 并记录崩溃原因。若 fiber 恢复成功，任务继续执行直至终态。
- 场景三——消息写入中途崩：Think 的消息持久化通过 Session.appendMessage / updateMessage 操作 assistant_messages 表。这些操作与任务表的状态更新在同一个 DO SQLite 事务上下文中执行。若进程在消息写入和任务状态更新之间崩溃，重启后系统根据 assistant_messages 表中是否存在对应的助手消息来判断推理是否实际完成：若存在则补写 completed，否则回退到 accepted 并重试。
- 场景四——幂等键保护下的重复提交：外部调用方因网络问题重试同一提交请求。由于 idempotency_key 上有 UNIQUE 约束，INSERT 尝试会失败，系统转而查询已有行。若已有任务处于终态，返回终态信息；若处于 accepted 或 running，返回"进行中"状态，调用方可继续轮询 getTaskStatus。

### 6. 与既有消息保存路径的兼容性

方案的关键设计原则是不重写 Think 既有的消息保存路径。具体体现为：

- 消息持久化复用现有路径：任务执行内部调用 Think 既有的 saveMessages 方法，该方法将用户消息追加到 Session 的 assistant_messages 表，然后通过 TurnQueue 排队、_runInferenceLoop 组装上下文、streamText 执行推理、_streamResult / _persistAssistantMessage 完成流式输出和助手消息持久化。整个链路与浏览器 WebSocket 发起的普通聊天请求共享同一代码路径，只在任务管理层增加了任务状态追踪。
- 取消语义对齐：cancelTask 内部通过 AbortRegistry 中止推理流，这与浏览器客户端发送 cf_agent_chat_request_cancel 消息的机制完全一致——推理流的 abortSignal 被触发，模型调用中断，已流式输出的部分助手消息被保留。任务表中的 cancelled 状态仅记录外部取消意图。
- 恢复语义对齐：任务执行包装在 runFiber 中（当 chatRecovery 为 true），利用 agent 基类的持久化 fiber 机制。这与 saveMessages 和 _handleChatRequest 路径中的恢复策略一致——fiber 名称中包含 requestId，恢复时由 onFiberRecovered 钩子处理。
- 会话消息与任务记录的分离：任务表中的 assistant_message_id 字段仅作为引用，不复制消息内容。消息仍然完全由 Session 模块管理（tree-structured messages、compaction、FTS5 索引）。deleteTask 只移除任务记录，不影响消息。这保证了消息的完整生命周期（压缩、搜索、分支等）不受任务管理层影响。

### 7. 技术效果

本方案在 Think agent 现有架构上叠加任务管理层，带来以下技术效果：

- 快速确认与解耦：外部调用方提交任务后仅需等待一次 SQLite 写入（毫秒级）即可获得 task_id 确认，无需等待整个推理链路（秒至分钟级）。调用方与推理执行在时间维度上解耦，提升了外部系统的吞吐能力和容错性。
- 精确一次语义：通过 idempotency_key 的数据库级 UNIQUE 约束与提交时的幂等检查配合，确保同一外部请求无论重试多少次，在系统内部仅对应一条任务记录和一次推理执行。这比单纯依赖消息 ID 的 INSERT OR IGNORE 更上层——后者只防止同一条消息被重复存储，但不防止"同一意图"被重复推理。
- 可靠的持久状态边界：任务从 accepted 到终态（completed/error/cancelled）的每一次状态转换都通过原子 SQL UPDATE 持久化，调用方可在任意时刻通过 task_id 查询到一致的状态视图。崩溃后系统自动扫描并恢复或标注遗留任务，调用方不会陷入无限等待。
- 与现有架构的零侵入：方案新增的 think_external_tasks 表独立于 Session 的 assistant_messages 等核心表。任务执行路径内部调用 saveMessages 等既有方法，不修改 Think 的消息处理、流式输出或 WebSocket 协议逻辑。已有浏览器聊天、子 agent RPC 调用和 agent-as-tool 子运行路径完全不受影响。
- 管理面完整：getTaskStatus、cancelTask、deleteTask 构成完整的任务生命周期管理面。调用方可主动查询进度、取消未完成推理、清理已完成记录，实现了外部调用方对异步 agent 对话任务的全程可控。

### 8. 待确认问题与风险

方案在实施中需要注意以下风险点：（1）idempotency_key 的保留窗口：任务记录不能无限增长，需要定义清理策略（如按时间或终态批量删除），清理后相同幂等键可能被复用，需与调用方约定幂等键的时间窗口。（2）任务恢复的时效性：DO 崩后恢复依赖 onStart 扫描，若 agent 长时间无外部请求触发激活，遗留的 accepted 任务可能延迟恢复，需考虑与调度器结合或由调用方超时重试机制兜底。（3）并发提交同一幂等键时的竞态：UNIQUE 约束在 SQLite 层面处理竞态，但需要确认 INSERT 失败后的重试读取逻辑不会在极端并发下读到过时数据。
