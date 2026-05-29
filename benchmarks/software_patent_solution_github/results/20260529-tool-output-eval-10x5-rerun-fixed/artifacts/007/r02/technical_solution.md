## 技术方案

本方案在现有基于 Durable Object 的 agent 框架基础上，扩展支持「主 agent 按需调度多个子 agent」的协作模式。核心思路是：将每个子 agent 分配一个独立的、持久化的子运行实例（SubRun），该实例拥有独立的执行上下文和事件流；主 agent 通过工具调用或服务端确定性流程发起子运行；子运行的生命周期事件（开始、进度、输出片段、完成、失败、取消）通过统一事件总线回流至主 agent 所在会话，并持久化到 Durable Object 存储中，支持页面刷新重放和断线恢复。

### 技术问题概述

在现有 Durable Object agent 框架中，单次对话由主 agent（chat agent 或 Think agent）独占处理，缺乏将复杂任务分解并委托给独立子 agent 协同执行的机制。具体需要解决的问题包括：（1）子 agent 作为可调用能力时，如何同时支持模型自动决策调用和服务端确定性流程调用两种模式；（2）子 agent 的独立执行上下文如何与主会话的事件视图关联与同步；（3）流式输出如何按子运行维度分组、实时推送，并在断线重连后完整重放；（4）如何保证子运行的幂等启动、并发安全与重复请求去重；（5）如何实现子运行的取消、清理保留策略，以及 drill-in 时的访问控制。

### 系统整体架构

系统在现有 Durable Object agent 架构基础上新增以下核心组件：

- SubRunManager（子运行管理器）：内嵌于主 agent 的 Durable Object 中，负责子运行的创建、状态跟踪、取消和清理。通过 SubRunManager 提供的工具定义，模型可将子 agent 作为 function tool 调用；服务端流程也可直接调用其 API 发起子运行。
- SubRun（子运行实例）：每个子运行拥有独立的 Durable Object 或 Durable Object 内的隔离执行槽位，持有独立的消息历史、模型上下文和事件流，不污染主 agent 的执行状态。
- EventBus（事件总线）：统一的事件发布-订阅通道，子运行的流式事件通过 EventBus 推送至主会话的 WebSocket 连接，同时写入持久化事件日志。
- EventLog（事件日志）：基于 Durable Object Storage 的追加型事件存储，每个子运行的事件按序列号有序记录，支持断线重连后的事件重放。
- SubRunTool（子运行工具定义）：注册到主 agent 工具列表中的工具，参数包括子 agent 类型、任务描述、超时时间、幂等键等，由模型或服务端调用触发子运行创建。

### 子 Agent 调用机制

子 agent 的调用支持三种触发模式，覆盖不同的协作场景：

模式一：模型工具调用。将 SubRunTool 注册到主 agent 的工具列表中（与其他 function tool 地位等同）。当模型在推理过程中判定需要委托子 agent 时，发出 tool_call，参数中指定子 agent 类型（如 research-agent、planning-agent、compare-agent）、任务描述文本、超时时长和可选幂等键。主 agent 的 tool 执行循环捕获该调用，委托 SubRunManager 创建子运行。这种模式下，子运行有明确的父 tool_call_id，与主 agent 的工具调用生命周期绑定。

模式二：服务端确定性流程调用。在不需要模型参与决策的场景下，服务端代码可直接调用 SubRunManager.createSubRun() API，同步或异步发起子运行。此时子运行可以没有父 tool_call_id（parent_call_id 设为 null），但仍关联到当前主 agent 的 round_id 和 message_id。典型场景包括：系统预设的并行质检流程、审批链式调用、外部 webhook 触发的后台分析等。

模式三：无直接父工具调用的后台子运行。子运行可以由系统事件触发（如定时任务、外部回调）而不关联任何主 agent 工具调用。此时子运行关联到会话（session_id）但不绑定到特定 round 或 message。后台子运行的生命周期事件仍回流到该会话的事件视图中。对于需要 drill-in 查看的后台运行，系统通过子运行列表接口暴露。

### 事件模型与流式传输

每个子运行产生的事件统一通过 EventBus 推送至主会话，事件模型设计如下：

- 事件类型：sub_run_start（子运行开始，包含 sub_run_id、agent_type、parent_call_id、幂等键）、sub_run_delta（流式输出增量片段，包含 content 和序列号 seq）、sub_run_tool_call / sub_run_tool_result（子 agent 内部的工具调用事件）、sub_run_complete（正常完成，含最终结果摘要）、sub_run_error（失败，含错误信息）、sub_run_cancelled（被取消）。
- 父子关联：每个子运行事件携带 parent_round_id、parent_message_id 和可选的 parent_call_id。parent_call_id 为 null 时表示无直接父工具调用（后台子运行或服务端确定性调用）。
- 事件路由：EventBus 根据 parent_round_id 将事件路由到对应主会话的活跃 WebSocket 连接。若当前无活跃连接，事件仅写入 EventLog 持久化。
- 事件持久化：每个子运行的事件按序列号 seq 递增写入 Durable Object Storage 中的 EventLog。写入采用追加模式，保证顺序一致性。
- 去重机制：每个子运行事件携带唯一的 event_id（UUID）。客户端收到事件后按 event_id 去重；EventBus 在推送层面也检查 event_id 避免同一事件因重连等因素被重复推送。

### 状态管理与恢复机制

子运行的状态管理基于 Durable Object 的持久化能力，确保崩溃恢复和重连后状态一致：

子运行状态机：每个 SubRun 维护一个状态机，状态包括 CREATED（已创建，尚未开始执行）、RUNNING（执行中）、COMPLETED（正常完成）、FAILED（执行失败）、CANCELLED（被取消）。状态转换由 SubRunManager 原子操作控制，写入 Durable Object Storage 后生效。任何时刻只能存在一个有效状态。

恢复机制：当 Durable Object 因崩溃或迁移重新激活时，SubRunManager 从 Storage 中加载所有子运行记录。状态为 RUNNING 的子运行，检查其心跳时间戳：若心跳超时（例如超过 60 秒未更新），标记为 FAILED 并记录超时原因；否则基于已持久化的消息历史和事件日志，从中断点继续执行。模型调用通过保存的 conversation history 回放至中断前的状态后继续。

客户端重连重放：用户刷新页面或 WebSocket 重连后，客户端携带上次接收到的最大 event_seq 发起事件同步请求。服务端从 EventLog 中读取该 seq 之后的所有事件（按时间排序，跨所有子运行和主对话），批量推送给客户端。客户端按 event_id 去重后渲染，实现子运行执行过程的完整恢复。对于仍在 RUNNING 的子运行，重连后新产生的事件通过新 WebSocket 连接继续实时推送。

### 去重与并发控制

去重与并发控制通过幂等键和数据锁机制实现：

幂等键（idempotency_key）：子运行创建请求可携带幂等键。SubRunManager 在创建子运行前，以幂等键为索引查询当前 round 或 session 范围内是否已存在同名子运行。若存在且状态为 COMPLETED 或 FAILED，直接返回已有结果（结果复用）；若存在且状态为 RUNNING，返回该运行引用而不重复创建；若不存在则创建新记录并将幂等键写入索引。幂等键的有效窗口为当前 round 生命周期，round 结束后索引清理。此机制同时防御网络重试导致的重复创建和用户快速双击等场景。

并发领取控制：当多个 Worker 可能同时尝试领取同一个 RUNNING 状态的子运行（例如 Durable Object 迁移后的恢复竞争），SubRunManager 使用 Durable Object Storage 的原子 compare-and-swap 操作：先读取当前状态和版本号，仅当状态仍为 RUNNING 且版本号匹配时才写入领取标记（包含 worker_id 和时间戳）。失败的竞争者读取更新后的状态，若已被领取则放弃。

事件写入顺序：同一子运行的事件通过单线程写入 EventLog，天然保证 seq 递增和顺序一致性。跨子运行的事件在 EventBus 中按到达时间排序，客户端按子运行维度分组渲染。

### 取消与清理机制

取消与清理机制覆盖主动取消、级联取消和过期清理三个维度：

主动取消：用户或主 agent 可通过取消接口（cancelSubRun）取消指定的子运行。SubRunManager 将目标子运行的状态原子更新为 CANCELLED，并向子运行内部发送中断信号。子运行的执行循环在下一个检查点（如模型调用前后、工具调用间隙）检测到中断信号后，停止执行、发送 sub_run_cancelled 事件并清理临时资源。已产生的部分输出和事件日志保留，用户仍可查看。

级联取消：当主 agent 的某轮对话被取消（或主 round 终止）时，该轮发起的所有子运行—无论通过工具调用还是确定性调用—均进入级联取消。SubRunManager 遍历该 round_id 下的所有非终态子运行，逐一触发取消流程。通过 parent_call_id 关联的子运行在父工具调用被取消时也一并取消。后台子运行（无 parent_call_id）不参与级联取消，保持独立生命周期。

清理保留策略：子运行记录和事件日志在会话级别保留，默认保留期为会话关闭后 7 天。保留期内用户可 drill-in 查看历史子运行的完整事件流和执行结果。超出保留期后，清理任务按批次删除 EventLog 和子运行元数据，仅保留摘要信息（子运行 ID、类型、最终状态、耗时）。正在运行中的子运行不受清理影响。

### 访问控制与 Drill-in

子运行的访问控制基于会话归属和权限传递模型：

会话归属验证：每个子运行在创建时记录其归属的 session_id 和 user_id。任何对子运行的 drill-in 请求（查看详情、事件流、结果）必须携带相同的 session_id 和有效的用户认证令牌。SubRunManager 在服务端校验请求者的 session_id 与子运行记录的 session_id 一致，防止跨会话数据泄露。

权限传递：子 agent 在执行时的权限范围由创建时的权限上下文快照决定。创建子运行时，SubRunManager 从当前主 agent 的权限上下文中复制一份受限快照（可配置允许的工具列表、文件访问范围、API 调用白名单等），注入子运行的执行环境。子运行无法访问超出该快照范围的资源。确定性流程调用时，权限快照由调用方显式指定。

Drill-in 接口：前端通过 GET /api/sessions/{session_id}/sub-runs/{sub_run_id} 获取子运行摘要（状态、耗时、agent_type 等），通过 GET /api/sessions/{session_id}/sub-runs/{sub_run_id}/events?since={seq} 获取事件流。两组接口均执行上述会话归属验证。列表接口 GET /api/sessions/{session_id}/sub-runs 支持按状态、round_id 和 agent_type 过滤，按创建时间倒序返回。

### 异常处理

异常处理覆盖子运行生命周期中的典型异常场景：

- 子运行超时：创建子运行时指定的超时时长到期后，SubRunManager 触发超时中断，子运行状态转为 FAILED，事件日志记录 timeout 原因。部分输出保留。
- 模型调用失败：子 agent 调用模型时如遇 API 错误或限流，按指数退避重试（最多 3 次）。全部重试失败后子运行转为 FAILED，错误详情记录在 sub_run_error 事件中。
- DO 崩溃恢复：Durable Object 崩溃后重新激活时，SubRunManager 检查 RUNNING 状态的子运行，对超时无心跳的标记 FAILED，对仍在心跳窗口内的从中断点恢复执行。
- 迟到结果处理：子运行已被取消或标记 FAILED 后，其异步执行的延迟回调可能仍会返回结果。SubRunManager 在接收结果时检查当前状态，若非 RUNNING 则丢弃结果并记录警告日志，不触发状态变更。
- 并行冲突：多个子运行同时修改共享会话状态时，通过 Durable Object 的单线程执行模型天然序列化，避免竞态。

### 技术效果

本方案预期的技术效果包括：

- 透明协作：用户能在同一会话视图中实时观察所有子 agent 的执行进度、中间输出和最终结果，实现多 agent 协作过程对用户透明。
- 可靠恢复：基于 Durable Object Storage 的事件日志和状态快照，断线重连后可完整恢复子运行视图，不丢失已产生的输出。
- 灵活调度：同时支持模型自主决策调用、服务端确定性调用和后台触发三种模式，覆盖从交互式对话到自动化流水线的多种场景。
- 资源安全：通过幂等键防止重复创建、通过 CAS 控制并发领取、通过权限快照限制子 agent 访问范围，保证系统在并发和分布式环境下的安全稳定。
- 兼容现有体系：子运行机制作为增量扩展，不要求修改现有 chat agent、Think agent 和工具调用循环的核心逻辑，SubRunTool 以标准工具形式注册即可接入。

### 风险与待确认事项

以下事项需要在具体实现时进一步确认：

- Durable Object 子运行粒度：子运行是使用独立 DO 实例还是主 agent DO 内的隔离槽位，需根据平台 DO 并发限制和延迟要求评估。独立 DO 隔离性更好但增加 DO 实例管理开销；隔离槽位共享 DO 实例但需确保内存/CPU 不互相影响。
- 事件日志存储上限：长期运行会话可能积累大量子运行事件日志，需评估 Durable Object Storage 的容量限制和成本，必要时增加事件日志的分页归档或冷存储策略。
- 跨会话子运行共享：当前方案中子运行归属于单一会话。若未来需要跨会话共享子运行结果（如组织级知识库分析），需额外设计结果缓存和权限传递机制。
- 与现有子 agent 路由的关系：当前项目已支持子 agent 路由能力，需确认新增的子运行机制与现有路由机制是替代、互补还是共存关系，避免功能重叠或冲突。
