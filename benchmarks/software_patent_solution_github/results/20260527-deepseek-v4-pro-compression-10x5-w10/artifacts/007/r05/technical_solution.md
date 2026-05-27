## 技术方案

本方案提出一种基于 Durable Object（DO）facet 机制的父子 Agent 协作系统，运行于 Cloudflare Workers 边缘计算平台。系统将子 Agent 作为父 Agent 的 colocated DO facet 部署，通过 Typed RPC Stub 实现同进程内的高效通信，并在 Agent SDK 的 Think 框架之上构建一套 Agent Tool 编排层，使父 Agent 可以将子 Agent 作为 LLM 可调用的工具来调度，同时支持流式事件实时展示、故障恢复、取消传播、访问控制和并发保护等完整协作能力。

### 整体架构

系统以 Durable Object 为基本运行单元。父 Agent 继承自 Think 基类（Agent SDK 提供的 ReAct 循环 Agent），在同一个 DO 实例内通过 this.subAgent(Cls, name) 创建子 Agent。子 Agent 作为 DO 的命名 facet 与父 Agent 共享 DO 存储（SQLite），但拥有独立的 DO Storage 命名空间和独立的 Agent 生命周期。父子之间通过 DO 内置的 Typed RPC Stub（SubAgentStub<T>）进行调用，RPC 请求在同一 DO 进程内基于队列调度执行，无需网络序列化开销。

在通信层面，父 Agent 调用子 Agent 的 runTurnAndStream 方法获取 ReadableStream<Uint8Array>，流中承载 NDJSON 格式的 chat-response chunk 帧。子 Agent 内部通过 broadcast 方法的 tee 转发机制将流式输出同时分发给父端监听器和子端自身的 SSE 客户端，保证一次生成、多方消费。

### 子Agent生命周期管理

子 Agent 的完整生命周期由三个 API 覆盖。创建：this.subAgent(Cls, name) 在父 DO 实例下注册一个类型为 Cls 的命名子 Agent facet，返回 SubAgentStub<T> 句柄；该子 Agent 与父 Agent 共享底层 DO 实例但具有独立的 SQLite 存储命名空间和独立的 Agent 状态机。终止：abortSubAgent(name) 向指定子 Agent 发送取消信号，中断其当前执行回合但不删除 facet 和持久化数据，子 Agent 仍可接收新的调用。删除：deleteSubAgent(name) 彻底移除子 Agent facet 及其关联的持久化存储，释放资源。子 Agent 支持嵌套——子 Agent 内部可继续调用 this.subAgent 创建孙子 Agent，形成树状协作拓扑。

### Agent Tool 编排层

Agent Tool 编排层是将子 Agent 包装为 LLM 可调用工具的核心抽象，提供命令式和声明式两种接口。runAgentTool(Cls, options)：命令式 API，由父 Agent 的业务代码直接调用，传入子 Agent 类型和 runId、input 等选项，返回包含流式事件订阅器和结果 Promise 的句柄。agentTool(Cls, options)：声明式工具工厂，返回符合 OpenAI Function Call 规范的 tool definition 对象，LLM 模型可在推理过程中自主决定调用哪个子 Agent、传入何种参数；父 Agent 的 Think 循环在检测到 tool_call 后自动路由到对应的 agentTool 执行逻辑，将子 Agent 的运行结果以 tool result 消息回注到对话上下文中。

两种接口共享同一底层执行引擎，均将子 Agent 的一次完整运行抽象为一个 tool run，分配唯一 runId，并在父端注册表（cf_agent_tool_runs）中持久化记录运行状态。子 Agent 内部通过 cf_agent_tool_child_runs 映射表将 runId 关联到具体的 requestId 和 streamId，实现父子端运行状态的解耦关联。

### 运行注册与状态跟踪

系统通过两级注册表实现运行状态的持久化跟踪，确保在 DO 崩溃重启后仍可恢复运行状态。

父端注册表 cf_agent_tool_runs（或 cf_agent_helper_runs）位于父 Agent 的 DO Storage 中，每条记录包含：runId（运行唯一标识）、parentToolCallId（父工具调用 ID，用于关联 LLM 的工具调用轮次）、status（pending/running/completed/failed/aborted/interrupted）、stream_id（关联的流标识）、inputPreview（输入的脱敏摘要，默认不存储原始 input 以保护敏感数据）、以及子 Agent 的类型标识。该注册表同时充当访问控制的白名单。

子端映射表 cf_agent_tool_child_runs 位于子 Agent facet 的 DO Storage 中，将 runId 映射到 requestId（子 Agent 内部请求标识）和 streamId，使得父端可以通过 runId 向正确的子 Agent facet 查询运行状态或获取重放数据。

### 流式事件协议与实时展示

父子 Agent 之间的流式通信基于 agent-tool-event（或 helper-event）协议帧，每条帧为 NDJSON 行，包含以下字段：kind（事件类型，取值为 started/chunk/finished/error/aborted/interrupted）、runId（本次运行标识）、helperId（子 Agent 的名称标识）、parentToolCallId（关联的父工具调用 ID）、sequence（递增序号，从 0 开始）、以及可选的 payload（chunk 类型时为文本片段，error 类型时为错误信息）。finished 帧在 sequence 上使用 -1 作为终止哨兵。

实时展示方面，子 Agent 的 broadcast 机制通过 tee 转发将 chat-response chunk 同时写入父端 RPC 流和子端 SSE 连接。父 Agent 端的 React 客户端组件通过订阅 runAgentTool 返回的事件流，在 UI 中实时渲染子 Agent 的思考过程和输出内容。每个子 Agent 运行在界面中表现为一个独立的 surface 卡片，卡片内按 sequence 顺序展示流式文本。

### 事件重放与去重机制

事件重放服务于两个场景：客户端重连时补齐已丢失的事件，以及父 DO 崩溃重启后的状态恢复。重放时，子 Agent 从其 SQLite 存储中按 runId 检索已持久化的 chat-response chunks，以 agent-tool-event 帧格式逐条发送，并在每条帧上设置 replay 标记为 true，以区别于首次实时传输的帧。

客户端去重规则：客户端以 (parentToolCallId, runId, sequence) 三元组为去重键。对于 sequence 相同的帧，若已接收过非 replay 帧则丢弃 replay 帧；若仅有 replay 帧则保留。对于 finished 帧（sequence = -1），以 runId 为键做终端态去重。此去重策略保证即使在重放与实时流并发到达的窗口期内，客户端 UI 也不会出现重复文本。

### 故障恢复机制

当父 DO 因崩溃、休眠回收或 Workers 平台迁移而重启时，系统在 DO 的 onConnect 生命周期钩子中执行恢复流程。恢复逻辑遍历 cf_agent_tool_runs 注册表中所有 status 为非终端态（非 completed、非 failed、非 aborted）的记录，对每条记录：通过子 Agent 类型和 runId 定位到对应的子 Agent facet，调用 child.inspectAgentToolRun 查询该运行在子端的实际状态，再通过 child.getChatChunksForReplay 拉取已生成的全部 chat chunks，以重放帧（replay=true）发送给客户端。

当前版本（V1）不支持 live-tail reattach——即无法在恢复后接续到仍在子端执行的流式输出的实时尾部。对于恢复时子端仍在运行的情况，系统将该运行标记为 interrupted 状态，通知客户端该子 Agent 的运行结果可能不完整，由客户端决定是否触发重新执行。此设计保证了恢复流程的确定性：要么拿到完整的已生成内容，要么明确知道内容不完整。

### 取消传播链路

取消信号从用户界面到子 Agent 执行引擎沿着 AbortSignal 链路逐级传播。用户在客户端触发取消操作后：父 Agent 端，对应工具调用的 AbortSignal 被触发，导致父端对子 Agent RPC 返回的 ReadableStream reader 执行 reader.cancel()，中断父端的事件消费。子 Agent 端，reader.cancel() 通过 DO RPC 层传播到子 Agent 的 per-turn AbortController，触发 abort 事件，子 Agent 在下一个检查点（通常为 LLM 推理完成后的 saveMessages 调用处）检测到 signal.aborted 并将 signal 传入 saveMessages({ signal })，从而安全终止当前回合的消息持久化和后续推理。

取消后的状态处理：子 Agent 的运行状态在注册表中被标记为 aborted；已生成并持久化的消息（在 abort 检测点之前完成保存的部分）得以保留在子 Agent 的对话历史中，后续恢复或新一轮调用时仍可访问；未完成的部分不产生副作用。

### 清理与保留策略

系统提供 clearAgentToolRuns（或 clearHelperRuns）接口用于清理子 Agent 运行产生的持久化数据。清理操作分两步执行：首先从父端注册表 cf_agent_tool_runs 中删除指定 runId 的行记录；然后定位到对应的子 Agent facet，调用 deleteSubAgent 将其连同 SQLite 存储一并移除。清理接口支持按 runId 单条清理和批量清理两种模式，批量清理时遍历注册表所有记录逐一执行上述两步。

保留策略遵循“默认保留、主动清理”原则：子 Agent 的运行记录、对话历史和生成的输出在子 Agent 被显式 deleteSubAgent 或 clearAgentToolRuns 之前持久保留。父 DO 休眠时不触发自动清理，保证用户重新访问时历史数据可用。input 原始数据的保留策略更为保守——注册表默认只存储 inputPreview（脱敏摘要），完整 input 仅存在于子 Agent 自身的对话消息中，随子 Agent 删除而销毁。

### Drill-in 访问控制

Drill-in 场景指用户通过 URL 或深层链接直接访问某个子 Agent 的运行详情页面。由于子 Agent 是 DO facet，其路由通常包含 agentType 和 runId 参数，存在恶意用户通过猜测 runId 构造 URL 越权访问的风险。系统通过 onBeforeSubAgent 门控钩子阻断此类攻击。

onBeforeSubAgent 在 DO 路由到子 Agent facet 之前被调用，接收 agentType 和 runId 参数。门控逻辑查询父端注册表 cf_agent_tool_runs，检查是否存在匹配 (agentType, runId) 的记录：若存在且该 runId 属于当前会话的授权上下文（如相同的父 DO ID 和用户身份），则放行；否则拒绝 facet 创建请求，返回 403 或 404 错误。此机制确保只有经过父 Agent 正式编排注册的子 Agent 运行才能被外部访问，runId 的不可猜测性由 UUID 生成策略保证。

### 并发守卫与幂等

系统通过两层机制防止并发冲突和重复执行。并发守卫：父 Agent 侧维护 _runInProgress 布尔标志，在任意子 Agent 运行期间设为 true，阻止同一父 Agent 实例同时发起第二个子 Agent 调用；同时提供 maxConcurrentAgentTools 配置选项，允许用户在吞吐量和资源隔离之间按需调整并发上限。对于需要并行 fan-out 的场景（如同时调用 Researcher 和 Planner 两个子 Agent），通过 Promise.allSettled 对多个 runAgentTool 调用做并发编排，每个调用内部仍受守卫保护。

幂等：runAgentTool 按 runId 做幂等处理。调用时首先检查注册表中是否已存在该 runId 的记录：若存在且状态为终端态（completed/failed/aborted），直接返回已有结果和事件流，不重新执行；若存在但状态为非终端态（pending/running），不重复启动新运行，而是订阅现有运行的事件流；若不存在，则创建新记录并启动子 Agent 执行。此设计保证在网络重试、客户端重复提交等场景下不会产生重复的 LLM 推理消耗和副作用。

### 与现有体系的兼容关系

本方案构建于 Cloudflare Agents SDK 的现有体系之上，与已有机制保持清晰的连接点。Agent 基类继承链：子 Agent（如 Researcher、Planner）继承自 HelperAgent，HelperAgent 继承自 Think（SDK 提供的标准 ReAct Agent），因此子 Agent 天然具备 Think 的全部能力——包括消息管理、工具调用循环、流式输出等——无需重新实现基础 Agent 逻辑。会话与消息存储：子 Agent 拥有独立的 SQLite 存储命名空间，其 saveMessages 和对话历史管理完全复用 Think 基类的现有路径，仅在取消场景下增加 signal 传递。

流式输出管道：子 Agent 的 runTurnAndStream 方法复用 Think 的 runTurn 流式管道，通过 broadcast tee 机制将同一份 chat-response chunk 分流到父端 RPC 流和子端 SSE 连接，与现有单 Agent 的流式输出体系完全兼容。鉴权与会话路由：子 Agent facet 的 HTTP 路由复用 DO 的 fetch 入口，通过 onBeforeSubAgent 门控在现有路由匹配逻辑之前插入访问控制检查，不改变已有路由表结构。此设计确保本方案的增量机制（编排层、注册表、事件协议、恢复等）以非侵入方式叠加在现有 Agent 框架之上，现有单 Agent 路径不受影响。
