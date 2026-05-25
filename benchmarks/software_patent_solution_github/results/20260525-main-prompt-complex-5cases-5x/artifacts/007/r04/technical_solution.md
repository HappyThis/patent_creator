## 技术方案

本方案在已有基于 Durable Object（DO）的 agent 框架基础上，引入一套子 agent 协作机制，使用户在与单一主 agent 对话时，主 agent 可按需调用一个或多个专门的子 agent 完成研究、规划、比较、总结等任务。子 agent 的执行过程（进度、输出片段、完成、失败、取消）通过流式事件实时回传到主 agent 所在会话视图，支持页面刷新和网络重连后的完整恢复。

### 1. 系统组件与整体架构

系统在现有 DO agent 框架基础上新增以下核心组件：（1）子 Agent 注册表（SubAgentRegistry），维护已注册子 agent 的定义、能力描述、输入输出 schema 和准入策略；（2）子运行管理器（SubRunManager），负责子 agent 实例的创建、生命周期跟踪、状态转换和结果持久化；（3）事件总线适配器（EventBusAdapter），将子 agent 执行过程中产生的流式事件桥接回主 agent 会话的 WebSocket 通道；（4）会话事件日志（SessionEventLog），以追加写方式记录主会话内所有子 agent 相关事件，支撑恢复与重放。

整体架构采用 DO 实例作为每个主 agent 会话的持久状态锚点。主 agent 的 DO 实例内部持有：当前会话的对话历史、活跃的子运行映射表（subRunId -> SubRunState）、以及一个事件序列号计数器（eventSeq）。子 agent 本身也在各自独立的 DO 实例中执行，拥有独立的消息历史和工具上下文。主 DO 与子 DO 之间通过 Durable Object 的存储 API 和内部 RPC 进行状态同步，不依赖外部消息队列。

### 2. 子 Agent 身份与父子关联

每个子 agent 执行实例由一个全局唯一的 subRunId 标识。subRunId 由主会话 ID（sessionId）、主请求轮次 ID（roundId）和子运行序号（runIndex）组合生成，格式为 {sessionId}:{roundId}:sub:{runIndex}，确保跨会话、跨轮次的唯一性和可追溯性。

父子关联通过两条链路建立。链路一（调用链关联）：当子 agent 由主 agent 的某次工具调用触发时，subRunState 中记录父调用 ID（parentCallId）；当子 agent 由服务端确定性流程直接触发时，parentCallId 为空，但记录触发上下文（triggerContext），包含触发原因和触发路径。链路二（会话归属关联）：每个 subRun 始终归属于一个主会话 sessionId，无论其触发方式如何。前端通过 sessionId 获取当前会话的所有子运行列表，按 roundId 和 parentCallId 进行分组展示。

### 3. 三种调用模式

系统支持三种子 agent 调用模式，覆盖模型自动决策、服务端确定性流程和后台执行场景。

模式一（模型工具调用）：子 agent 以 function tool 的形式注册到主 agent 的模型工具列表中。工具定义包含子 agent 名称、描述和参数 schema。当模型决定调用该工具时，主 DO 创建一个 SubRunState 记录，设置 parentCallId 为本次工具调用的 callId，然后异步触发子 DO 执行。工具调用立即返回一个 subRunId 给模型，模型可在后续轮次通过另一个查询工具获取子运行的最新状态和结果。

模式二（服务端确定性流程主动调用）：在服务端编排逻辑中，开发者可直接调用 SubRunManager.create() 创建子运行，无需经过模型工具调用路径。

该模式下 parentCallId 为空，但 triggerContext 记录调用来源（如路由名称、编排步骤序号）。子运行仍归属于当前主会话 sessionId，前端通过轮询或 WebSocket 事件感知新子运行的出现。典型场景包括：请求预处理流水线中自动启动合规检查子 agent、结果后处理中自动启动格式转换子 agent。

模式三（无直接父工具调用的后台子运行）：子 agent 可由系统事件、定时器或外部回调触发启动，不绑定到某个具体的主 agent 工具调用。

后台子运行仍需显式绑定到目标主会话 sessionId，并在主 DO 的 subRunMap 中注册。与模式一和模式二的关键区别在于，后台子运行在创建时即标记为 background 类型，前端在子运行列表中赋予独立的展示区域，不以工具调用的子项形式嵌套展示。该模式适用于长时间运行的监控、定时报告生成等场景。

### 4. 数据流、事件模型与状态管理

子 agent 执行过程中的所有可观察事件均通过统一的事件模型表示。每个事件包含：eventId（全局唯一）、subRunId、eventSeq（子运行内单调递增序号）、eventType（枚举值：run_created、thinking_delta、text_delta、tool_call、tool_result、run_completed、run_failed、run_cancelled）、timestamp、payload（事件类型相关的结构化数据）。

事件流传输路径为：子 DO 在本地以追加方式写入事件到自身的持久存储，同时通过 Durable Object 内部 RPC 将事件推送到主 DO。主 DO 收到事件后执行两步操作：（1）将事件追加写入 SessionEventLog；（2）若当前存在活跃的 WebSocket 连接，将事件通过该连接下发到前端。若当前无活跃 WebSocket 连接，事件仅持久化，等待重连后通过重放机制补推。

SubRunState 是子运行的核心状态结构，存储在主 DO 的持久化存储中，字段包括：subRunId、sessionId、roundId、parentCallId（可选）、triggerContext（可选）、agentType（子 agent 类型标识）、status（枚举：pending、running、completed、failed、cancelled）、input（启动参数）、resultSummary（完成后的结果摘要）、errorInfo（失败时的错误详情）、createdAt、lastEventSeq（已生成的最大事件序号）、ackEventSeq（前端已确认的最大事件序号）。

状态转换规则如下：子运行创建时状态为 pending；子 DO 开始执行时转为 running；正常结束转为 completed 并写入 resultSummary；执行异常转为 failed 并写入 errorInfo；收到取消请求且子 DO 响应取消后转为 cancelled。所有状态转换均原子写入主 DO 存储，利用 Durable Object 的单线程保证避免竞态。

### 5. 流式事件实时展示与重放去重

前端与主 DO 之间的 WebSocket 连接维持一个 session 级的事件通道。当多个子 agent 并行执行时，它们各自产生的事件通过该同一通道下发，前端根据事件中的 subRunId 字段将事件路由到对应的子运行视图组件。

去重机制：每个事件携带全局唯一的 eventId 和子运行内单调递增的 eventSeq。前端维护每个 subRunId 的已接收最大 eventSeq。当 WebSocket 连接因网络波动短暂断开并重新建立时，前端在重连握手消息中携带每个活跃子运行的 ackEventSeq。主 DO 根据 ackEventSeq 从 SessionEventLog 中读取该子运行中 eventSeq 大于 ackEventSeq 的所有事件，批量推送给前端（追赶重放）。前端在渲染侧根据 eventId 做幂等去重：若已存在相同 eventId 的事件则跳过。

页面刷新场景的完整恢复流程：前端加载后发起 HTTP GET /session/{sessionId}/state 请求，主 DO 返回当前会话的完整快照，包括对话历史、所有子运行列表及其 SubRunState、每个子运行的最后 ackEventSeq。前端据此重建 UI 骨架（对话和子运行卡片），然后通过 WebSocket 重连，对每个未完成的子运行（status 为 pending 或 running）发起追赶重放请求，补拉缺失的事件并恢复流式订阅。

### 6. 恢复机制

恢复机制建立在 Durable Object 的持久化存储和确定性重放之上。主 DO 崩溃或被迁移时，Cloudflare Durable Object 运行时自动在新的实例上重放已持久化的存储操作，主 DO 的状态（subRunMap、eventSeq 计数器、SessionEventLog）完全恢复。

子 DO 崩溃恢复：子 DO 同样受益于 DO 持久化。子 DO 在执行过程中定期将已生成的事件和中间状态写入存储。当子 DO 崩溃后在新实例上恢复时，从存储中读取最后持久化的事件序号，从中断点继续执行并生成事件。主 DO 通过心跳检测子 DO 的存活状态：若超过可配置的超时时间未收到子 DO 的新事件或心跳，主 DO 将对应 subRun 标记为 failed，错误信息注明可能的崩溃原因。

前端重连恢复：前端 WebSocket 断开后，前端库以指数退避策略自动重连。重连成功后，前端发送 reconnect 消息，携带 sessionId 和各子运行的 ackEventSeq 映射。主 DO 据此执行追赶重放，将缺失事件推送给前端。对于在断连期间状态已变更为 completed/failed/cancelled 的子运行，主 DO 在追赶重放末尾追加对应的终态事件，确保前端正确关闭该子运行的 UI 状态。

### 7. 取消与清理保留

取消机制：用户可通过前端发起对指定 subRunId 的取消请求。取消请求经 WebSocket 或 HTTP POST 到达主 DO，主 DO 先校验 subRun 当前状态是否为 running 或 pending，若是，则将状态标记为 cancelling，并向子 DO 发送取消信号。子 DO 在收到取消信号后，在下一个可中断点（如模型推理完成、工具调用返回后）停止执行，将持久化状态更新为 cancelled，并通过事件总线发送 run_cancelled 事件。若子 DO 在超时时间内未响应取消信号，主 DO 强制将状态更新为 cancelled 并记录强制取消标记。

清理保留策略：子运行的持久化数据（SubRunState、事件日志、子 DO 存储）采用基于时间的分层保留策略：（1）活跃会话内的所有子运行数据全量保留；（2）会话关闭（用户主动结束或长时间无活动）后，子运行摘要（SubRunState 的核心字段）保留 30 天，详细事件日志和子 DO 内部状态缩减为仅保留终态摘要，7 天后清理；（3）对于标记为 background 类型的后台子运行，其输出结果（如生成的报告）可配置为永久保留，但执行过程事件日志遵循上述缩减策略。清理由定时 DO Alarm 触发，逐个子运行检查 createdAt 和 lastEventSeq 决定是否执行缩减或删除。

### 8. 并发控制与重复请求处理

重复请求处理：系统在 SubRunManager.create() 入口处通过幂等键实现去重。调用方可在创建请求中提供可选的 idempotencyKey。若提供，主 DO 在 subRunMap 中按 idempotencyKey 索引查找：若已存在相同 idempotencyKey 且状态非 cancelled/failed 的子运行，直接返回已有的 subRunId 和当前状态；若已存在但状态为 cancelled 或 failed，则创建新的子运行（允许重试）。未提供 idempotencyKey 时，每次调用创建新的子运行。idempotencyKey 的保留窗口为 24 小时，超期后索引条目被回收。

并发执行控制：主 DO 对同一会话内的并发子运行数量实施软限制和硬限制。软限制（默认 5 个并发子运行）触发时，新的子运行创建请求仍被接受但标记为 queued 状态而非直接执行；硬限制（默认 10 个）触发时，超出请求被拒绝并返回错误。处于 queued 状态的子运行在当前活跃子运行数降至软限制以下时，由主 DO 的 alarm 机制触发自动启动。子 DO 自身的并发模型由 Durable Object 的单实例单请求语义自然保证，每个子 DO 实例内部不会并发执行多个请求。

### 9. Drill-in 访问控制

Drill-in 访问控制确保用户只能查看和操作自己会话内的子运行。控制点分为三层：（1）会话归属校验：所有子运行 API（查询状态、获取事件、取消）的第一步是校验 subRunId 中的 sessionId 是否与当前请求用户的会话匹配，不匹配则返回 403；（2）子 DO 直接访问防护：子 DO 不暴露公共 HTTP 端点，仅接受来自归属主 DO 的内部 RPC 调用；（3）事件订阅授权：WebSocket 重连握手时，主 DO 校验握手 token 与会话的绑定关系，仅下发该会话内子运行的事件，前端无法跨会话订阅。

对于需要细粒度权限控制的场景，子 agent 注册表中可为每个子 agent 类型配置访问策略（accessPolicy），包括：允许调用的用户角色列表、是否需要用户显式确认、单次调用的最大执行时长限制、结果可见性（仅调用者可见 / 会话内所有参与者可见）。服务端确定性流程调用子 agent 时，以系统角色（system）身份绕过角色检查，但仍在审计日志中记录调用来源。

### 10. 与现有 Chat/Think Agent 体系的兼容

本方案设计为现有 agent 框架的增量扩展，不要求应用开发者重写已有 agent 实现。兼容性体现在：（1）现有 chat agent 和 Think agent 的 DO 类定义无需修改，仅需在主 DO 的基类中增加 SubRunManager 和 SessionEventLog 的持有和方法委托；（2）子 agent 复用现有 agent 基类和流式输出基础设施，开发者通过实现标准的 SubAgent 接口（包含 execute、cancel、getProgress 三个方法）即可将任意 agent 注册为子 agent；（3）WebSocket 消息协议向后兼容：在现有消息帧中增加可选的 sub_run 命名空间字段，不识别该字段的旧版前端忽略该字段，不影响现有功能；（4）现有的重连恢复和流式输出机制被直接复用于子 agent 事件的传输和追赶重放。

### 11. 风险与待确认问题

以下为当前方案中需要后续确认和关注的风险点：（1）子 DO 间 RPC 调用的延迟上限：Durable Object 内部 RPC 在跨区域场景下的延迟可能影响事件推送的实时性，需评估是否引入区域亲和性调度策略。（2）SessionEventLog 的存储膨胀：在高频子 agent 调用场景下，事件日志的存储量可能快速增长，分层保留策略的参数需要根据实际负载调优。（3）子 agent 嵌套调用：当前方案假设子 agent 不再递归调用其他子 agent，若后续需要支持嵌套，需扩展 parentCallId 为调用链（callChain）并增加深度限制。（4）模型工具调用模式下的轮次延迟：模式一中子 agent 异步执行，模型需在后续轮次通过查询工具获取结果，可能增加端到端延迟，可考虑引入服务端推送通知（如 Cloudflare Queues）作为可选的主动通知通道。（5）子 DO 崩溃恢复的一致性问题：子 DO 从持久化检查点恢复后重放的事件可能与崩溃前已推送至主 DO 的事件重复，虽然前端 eventId 去重可解决展示侧问题，但若子 DO 在工具调用中途崩溃，外部副作用（如已发送的 API 请求）无法撤销，需要在子 agent 接口文档中明确幂等要求。
