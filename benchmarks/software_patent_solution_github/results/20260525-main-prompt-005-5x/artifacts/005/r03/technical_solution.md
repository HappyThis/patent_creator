## 技术方案

### 技术问题

Think agent 的现有架构支持通过 WebSocket 协议接收浏览器客户端的聊天请求，以及通过 saveMessages() 方法在程序内部注入消息并触发模型推理。但对于 webhook、RPC 调用方或其他外部 Worker 系统触发的对话任务场景，缺少一套完整的任务提交、确认、追踪和控制机制。外部调用方提交任务后需要等待模型推理完成才能获得结果，无法在提交后快速得到确认并异步查询执行状态。当外部系统因超时或网络问题重试同一请求时，系统缺乏幂等保护，可能重复插入用户消息或重复执行同一项工作。

### 整体架构

本方案在 Think agent 现有架构之上增设一个任务提交层（Task Submission Layer），与现有 WebSocket 聊天路径和程序化 saveMessages() 路径并列共存。任务提交层负责接收外部系统的任务请求、持久化任务记录、返回即时确认、异步驱动模型推理，并提供任务状态查询和生命周期控制接口。

任务提交层由以下核心模块组成：（1）任务接收端点，负责对外暴露 HTTP 或 RPC 接口；（2）任务持久化表，在 SQLite 中记录每条任务的完整生命周期；（3）幂等键去重机制，防止重复提交产生副作用；（4）异步执行调度器，利用 Think 现有的 TurnQueue 和 runFiber 基础设施驱动模型推理；（5）状态查询与控制接口，供外部调用方查询进度、取消任务或清理已完成任务记录。

### 任务状态机与持久化边界

每条外部提交的任务在任务持久化表（例如 cf_external_tasks 表）中维护一条记录，该记录随任务生命周期在各阶段被更新。任务状态机包含以下状态及其转换规则：

1. accepted——任务记录已写入 SQLite，返回任务 ID 给调用方。此状态在任务接收端点收到请求并完成幂等检查后立即写入，不等待模型推理开始。该写入是调用方获得“已接收”确认的唯一持久化依据。
2. dispatched——任务已被 TurnQueue 领取，关联的 requestId 已写入任务记录，消息已追加到 Session。此状态在异步执行路径中，消息持久化和 TurnQueue 入队成功后写入。
3. running——模型推理循环已启动，runFiber 已创建对应的 fiber 行。此状态在 streamText 调用开始前写入。
4. completed——模型推理正常结束，助手消息已持久化。此状态在 _streamResult 完成后、onChatResponse 触发前写入。
5. failed——推理过程发生不可恢复错误，错误信息写入任务记录。此状态在 onChatError 处理路径中写入。
6. aborted——任务被外部调用方或内部超时机制取消，已持久化的部分消息保留。此状态在 AbortRegistry 触发取消后写入。
7. cleaned——任务记录的关键字段（如消息内容）已被清理以释放存储空间，但状态摘要和完成时间保留。此状态由显式清理操作触发。

核心持久化边界位于 accepted 状态与 dispatched 状态之间。accepted 状态写入发生在任务接收端点（快速路径，仅涉及单表 INSERT），而 dispatched 及之后状态的写入发生在异步执行路径中（涉及 Session 消息追加、TurnQueue 入队、runFiber 创建和模型推理）。这一边界确保外部调用方在模型推理开始之前就已获得持久化确认，系统崩溃后可从持久化记录中恢复任务。

### 幂等键与重复提交去重

外部系统在遇到超时或网络错误时可能重试同一提交请求。本方案通过幂等键（idempotency key）机制防止重复插入用户消息和重复执行。具体机制如下：

调用方在提交任务时附带一个由调用方生成的幂等键（如 UUID 或业务流水号）。任务接收端点首先以该幂等键查询任务持久化表：若已存在匹配记录且处于 accepted、dispatched、running 状态，则直接返回已有任务 ID，不执行任何副作用操作；若已存在匹配记录且处于 completed、failed、aborted 等终态，根据配置可选择返回已有结果或拒绝重复提交。

幂等键的查询和任务记录的 INSERT 在同一 SQLite 事务中完成，使用 INSERT OR IGNORE 语义：若幂等键不存在，原子性地插入 accepted 状态的新记录并返回新任务 ID；若已存在，回退到查询已有记录。这保证了即使在并发重试场景下，同一幂等键只会产生一条任务记录和一次副作用。

### 崩溃恢复与消息写入安全性

本方案利用 Think agent 已有的 runFiber 持久化执行机制和 SQLite 存储实现崩溃恢复。不同阶段的恢复策略如下：

1. 任务接收端点阶段的崩溃：若任务记录已写入 accepted 状态但异步执行尚未启动，Agent 重启后通过 onStart 中的恢复扫描（类似 _checkRunFibers 机制）扫描所有处于 accepted 状态且超过一定时间阈值未进入 dispatched 状态的任务记录，按序将它们重新入队执行。
2. 消息写入阶段的崩溃：用户消息通过 Session.appendMessage 写入 assistant_messages 表。该写入在 dispatched 状态更新之前完成。若崩溃发生在消息写入之后、dispatched 状态更新之前，恢复扫描检测到 accepted 状态记录关联的消息已存在于 Session 中，直接推进到 dispatched 状态并继续执行。
3. 模型推理阶段的崩溃：推理过程通过 runFiber 包裹，fiber 行持久化在 cf_agents_runs 表中。崩溃后 Agent 重启时，_handleInternalFiberRecovery 检测到孤儿 fiber，触发 onChatRecovery 钩子。恢复逻辑从 ResumableStream 中读取已持久化的流式块，重建部分助手消息，并根据配置决定是否从断点继续推理或标记任务为 failed。
4. 终态写入的幂等性：completed、failed、aborted 等终态的写入使用条件 UPDATE（WHERE status NOT IN 终态列表），防止并发恢复路径导致重复写入或状态倒退。

### 状态查询与生命周期控制

外部调用方通过任务 ID 或幂等键查询任务的当前状态、进度和结果。系统提供以下查询和控制接口：

- 状态查询：通过 getTask(taskId) 或 getTaskByIdempotencyKey(key) 接口，返回任务的当前状态、创建时间、状态变更时间、可选的错误信息，以及任务完成后可获取的助手消息摘要。该接口仅读取任务持久化表，不触发副作用。
- 任务取消：通过 cancelTask(taskId) 接口取消尚未进入终态的任务。实现上，该接口向 AbortRegistry 中对应 requestId 的 AbortController 发送 abort 信号，并将任务状态条件更新为 aborted。若任务尚未 dispatched，则直接从 accepted 状态转为 aborted，不启动推理。
- 任务清理：通过 cleanTask(taskId) 接口清理已完成任务的消息内容以释放 SQLite 存储空间，保留状态摘要和时间戳用于审计。清理后的任务状态标记为 cleaned，不可再查询完整消息内容。

### 与现有路径的兼容性

任务提交层与 Think agent 现有的三条消息处理路径完全兼容，不重写任何已有机制：

- WebSocket 聊天路径：浏览器客户端通过 cf_agent_use_chat_request 协议消息提交聊天请求，该路径走 _handleChatRequest 方法，不经过任务提交层。两套路径共享 TurnQueue、AbortRegistry、Session、runFiber 和 _runInferenceLoop，但任务提交层有独立的任务记录表，WebSocket 路径的任务不产生外部任务记录。
- 程序化 saveMessages 路径：内部调用 saveMessages() 注入消息并触发推理，该路径不经过任务提交层。saveMessages 直接操作 Session 和 TurnQueue，不写入 cf_external_tasks 表。
- 子 Agent RPC 路径：通过 chat() 方法的 RPC 调用同样不经过任务提交层。这意味着作为子 Agent 的 Think 实例可以同时接受来自父 Agent 的 RPC 调用和来自外部系统的任务提交。

所有路径共享的核心基础设施——TurnQueue 的序列化语义确保同一 DO 实例上不会同时运行两个推理循环；AbortRegistry 的按请求 ID 管理确保取消操作精确路由到目标推理；Session 的 appendMessage 以 INSERT OR IGNORE 按消息 ID 幂等写入，与任务提交层的幂等键机制在各自层面独立运作，互不干扰。

### 任务提交与执行调度流程

外部任务提交的完整处理流程分为快速确认阶段和异步执行阶段两个阶段：快速确认阶段在任务接收端点完成，异步执行阶段通过 Think 的调度基础设施驱动。

在快速确认阶段：（1）任务接收端点验证请求合法性，提取用户消息内容、幂等键和可选配置参数；（2）在 SQLite 事务中以幂等键为唯一约束执行 INSERT OR IGNORE，写入 status='accepted' 的任务记录；（3）若插入成功，返回新生成的 taskId 和 status='accepted' 给调用方，HTTP 响应码为 202；若幂等键冲突，返回已有记录的 taskId 和当前状态；（4）任务接收端点立即返回，不等待模型推理开始。

在异步执行阶段：（1）任务接收端点通过 schedule() 方法设置一个零延迟的一次性定时任务（或通过直接调用 _dispatchTask 立即启动），唤醒 DO 的异步执行路径；（2）_dispatchTask 从任务持久化表中按创建时间顺序读取一条 status='accepted' 的记录，将其状态条件更新为 dispatched，并通过 Session.appendMessage 将用户消息写入 assistant_messages 表；（3）生成 requestId，通过 TurnQueue.enqueue 将推理任务加入串行队列，同时调用 runFiber 包裹整个推理执行，使推理过程具备持久化恢复能力；（4）推理完成后，在 _streamResult 的完成路径中将任务状态更新为 completed 并写入结果摘要。若推理过程中断或出错，相应更新为 aborted 或 failed。

### 任务持久化表设计

任务持久化表 cf_external_tasks 是任务提交层的核心数据结构，其设计遵循最小充分原则：仅存储任务生命周期管理和外部调用方查询所需的字段，消息内容通过已有的 assistant_messages 表和 Session 机制管理。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| task_id | TEXT PRIMARY KEY | 系统生成的唯一任务标识 |
| idempotency_key | TEXT UNIQUE | 调用方提供的幂等键，用于去重 |
| status | TEXT NOT NULL | 任务状态：accepted/dispatched/running/completed/failed/aborted/cleaned |
| request_id | TEXT | 关联的 Think 推理请求 ID |
| fiber_name | TEXT | 关联的 runFiber 名称，用于崩溃恢复 |
| user_message_id | TEXT | 写入 Session 的用户消息 ID |
| assistant_message_id | TEXT | 推理完成后助手消息 ID |
| error_message | TEXT | 失败或中止时的错误信息 |
| created_at | INTEGER NOT NULL | 任务创建时间戳 |
| status_updated_at | INTEGER NOT NULL | 状态最后更新时间戳 |
| completed_at | INTEGER | 任务完成/失败/中止时间戳 |

### 技术效果

本方案通过在 Think agent 现有架构上增设任务提交层，相较现有仅支持 WebSocket 交互和程序化内部调用的方式，带来了以下技术效果：

- 快速确认与异步解耦：外部调用方在任务记录持久化后立即获得确认（accepted 状态），无需等待模型推理完成。accepted 与 dispatched 之间的持久化状态边界使“已接收”确认与“模型推理开始”成为两个独立可追踪的阶段。
- 精确的重复提交去重：通过幂等键在 SQLite 事务中执行原子性的 INSERT OR IGNORE，保证同一任务不会被重复执行，也不会重复插入用户消息。幂等键由调用方生成，不依赖系统内部的请求 ID。
- 完整的崩溃恢复覆盖：任务提交层复用 Think 现有的 runFiber 持久化执行和 SQLite 存储，在任务生命周期的每个阶段都有明确的恢复策略。accepted 状态的任务记录在 Agent 重启时被扫描和重新调度，dispatched/running 状态的任务通过 fiber 恢复机制重建执行上下文。
- 外部可观察的生命周期：通过任务状态查询接口，外部系统可以在任何时刻获取任务的精确状态、时间戳和结果信息，无需维持长连接或轮询推理流。
- 生命周期控制：外部调用方可通过任务 ID 取消尚未完成的任务，或清理已完成任务的消息内容以管理存储空间。取消操作通过 AbortRegistry 精确路由到对应的推理请求。
- 不重写现有路径：WebSocket 聊天路径、saveMessages 程序化路径和子 Agent RPC 路径不受影响。任务提交层作为增设的并行路径，共享 TurnQueue、Session、runFiber 等核心基础设施，但拥有独立的任务记录表和幂等机制。

### 风险与待确认问题

以下方面需要在具体实现中进一步确认和细化：

- 幂等键的保留窗口：需要确定幂等键在任务持久化表中的保留策略。若已完成任务记录被清理（cleaned 状态），后续以相同幂等键重试时应返回何种响应（建议返回已完成但不可恢复的指示）。
- 并发任务调度策略：当前 TurnQueue 为串行队列，同一 DO 实例同时只运行一个推理循环。对于外部任务提交场景，可能需要支持多个 accepted 任务的自动串行调度（类似 _dispatchTask 的循环自调度），或考虑按优先级排序。
- 任务超时处理：对于长时间处于 accepted 状态但未被 dispatched 的任务（如 DO 长时间无激活），需要设置超时机制自动标记为 failed。超时阈值需根据 DO 的休眠/唤醒特性确定。
- 状态查询接口的访问控制：HTTP 状态查询端点需要适当的鉴权机制，防止未授权的外部调用方查询或操作其他用户的任务。
- 任务记录表的存储膨胀：大量已完成任务记录可能占用较多 SQLite 空间。cleanTask 操作可清理消息引用，但任务记录本身需设计 TTL 或定期清理机制。
