## 技术方案

本方案提出一种保留式流式子 agent 工具编排方法，用于在基于服务端持久对象的 agent 框架中实现主 agent 与多个子 agent 之间的结构化协作。核心构思是：将子 agent 视为主 agent 在单次请求处理过程中可调用的"工具"，子 agent 本身是具备独立持久化存储、推理循环和流式输出能力的完整 chat agent 实例；主 agent 通过框架提供的编排层启动子 agent 运行、接收并转发子 agent 的流式事件到同一主会话视图，同时将运行元数据持久化于主 agent 侧并在子 agent 侧维护运行标识映射，使得页面刷新或网络断开后仍可恢复已发生的子 agent 过程。

上述各机制协同工作，形成了一套完整的保留式流式子 agent 工具编排方案。其技术效果体现在：子 agent 运行与主 agent 观察的解耦使网络中断和页面刷新不影响子 agent 执行，恢复后可通过持久化注册表和 chunk 存储完整重建历史视图；基于 runId 的幂等启动防止了重试路径上的重复 LLM 调用；逐层 AbortSignal 链路实现了无竞态窗口的精确取消；注册表门控确保子 agent 路由安全；独立 facet 存储使每个子 agent 拥有隔离的对话历史和推理上下文；并行扇出与独立执行上下文使多个子 agent 可同时处理不同任务并互不干扰。方案适用于需要主 agent 在研究、规划、比较、总结等任务中调用专业子 agent 的复杂协作场景。

### 整体架构

系统在架构上分为三层：主 agent（父 agent）层、子 agent 工具层和客户端层。主 agent 是浏览器 WebSocket 连接的唯一入口，负责与用户交互、执行自身的 LLM 推理循环、并在适当时机调用子 agent 工具。子 agent 工具是运行在与主 agent 相同的 Worker 进程内但拥有独立持久化存储（SQLite）的 Durable Object 子面（facet），通过类型化 RPC 与主 agent 通信。客户端仅与主 agent 维持一条 WebSocket 连接，子 agent 的运行过程和结果通过该连接以结构化事件帧的形式呈现给用户。

### 运行注册表与状态生命周期

子 agent 工具的一次运行（run）是框架管理的最小编排单元，由全局唯一的 runId 标识。主 agent 通过两种 API 形态触发子 agent 运行：其一是命令式 API runAgentTool(Cls, { input, ...options })，适用于确定性工作流、后台任务和非 LLM 编排场景；其二是工具工厂 agentTool(Cls, options)，生成可供主 agent 的 LLM 在推理过程中自主选择调用的 AI SDK 工具条目。两种形态共享同一底层编排机制。

一个完整运行的生命周期为：主 agent 先在自身的 SQLite 中向 cf_agent_tool_runs 注册表插入一行，状态标记为 starting；随后通过框架的 subAgent 原语创建或获取指定类型的子 agent facet，并向其发起带有 runId 和输入数据的启动调用；子 agent 在自身的 SQLite 中创建 cf_agent_tool_child_runs 行，记录 runId 到内部 requestId 和 streamId 的映射，随后启动其 chat 推理循环。当子 agent 的推理循环开始产出流式输出时，运行状态转为 running；当推理正常完成时转为 completed；当推理抛出异常或流式输出中出现错误时转为 error；当被显式取消时转为 aborted；当主 agent 在运行非终结状态下崩溃/重启时，恢复后将该运行标记为 interrupted。

### 幂等启动与子侧映射

子 agent 工具的启动操作以 runId 为键实现幂等。当主 agent 调用子 agent 的 startAgentToolRun 方法时，子 agent 首先查询自身的 cf_agent_tool_child_runs 表：若该 runId 已存在且为终结态（completed / error / aborted），则直接返回已有的运行检查结果；若已存在且为非终结态（running），则返回当前运行状态，不启动重复的 chat 轮次。这保证了重试路径、定时回调或重连恢复不会意外重复执行相同的 LLM 工作。

### 流式事件协议与广播机制

主 agent 与客户端之间通过一条结构化的 agent-tool-event 事件协议来传递子 agent 运行的全过程。事件帧以 JSON 形式在主 agent 的 WebSocket 广播通道上发送，由 type 字段标识为 "agent-tool-event"。每条事件帧包含 parentToolCallId（关联到主 agent LLM 推理中的工具调用，若为命令式调用则可为空）、runId（子 agent 运行标识）、sequence（该运行内从 0 开始的单调递增序号）和 event 载荷。支撑六种事件类型：

- started：携带 runId、agentType、inputPreview、order 和可选的 display 元数据。由主 agent 在子 agent 启动时合成，使客户端可在任何 chunk 到达前渲染子 agent 面板。
- chunk：携带 runId 和 body。body 为 JSON 编码的 UIMessageChunk 不透明字符串，与主 agent 自身的 chat 流式输出使用相同的 chunk 词汇表。客户端使用与主聊天相同的 applyChunkToParts 原语重建子 agent 的消息部件（文本、推理、工具调用、工具结果等）。
- finished：携带 runId 和 summary 文本摘要。由主 agent 在子 agent 正常完成后合成。
- error：携带 runId 和 error 消息。由主 agent 在子 agent 抛出异常或流式错误后合成。
- aborted：携带 runId 和可选的 reason，由主 agent 在显式取消子 agent 运行后合成。
- interrupted：携带 runId 和 error 消息，仅由主 agent 在崩溃恢复后合成，表示该运行因主 agent 侧观察者丢失而无法继续实时跟踪，但子 agent 的持久化 chunk 仍可回放。

### 流式传输通道与 chunk 转发

子 agent 的流式输出通过 DO RPC 的 ReadableStream 机制逐块传递到主 agent。子 agent 在自身推理循环中，每产生一个 chat-response chunk，即通过覆盖的 broadcast 方法将其捕获，编码为 NDJSON 帧（每行包含 { sequence, body }），写入 RPC ReadableStream。主 agent 端通过 reader 逐行读取，将每帧的 body 封装为 agent-tool-event 的 chunk 事件，通过主 WebSocket 广播给所有连接的客户端。子 agent 的 chunk 以二进制 Uint8Array 传输以适应 workerd 的 DO RPC 流式传输限制，主 agent 端进行 TextDecoder 解码和 NDJSON 分行解析。子 agent 的 chunk 持久化由其自身的 ResumableStream 机制独立完成——每个 chunk 在到达时即写入子 agent 的 SQLite，与推理循环的完成状态无关。主 agent 侧不存储 chunk 副本，仅通过子 agent 的 getAgentToolChunks(runId) RPC 方法按需读取。

### 持久化与恢复机制

系统将子 agent 工具的运行视为持久化工作，将流式观察视为该工作的一个可丢弃的观察通道。这一分离是恢复机制的基础：执行的持久化状态由子 agent 自身的 SQLite（chat 转录、ResumableStream chunk、runId 映射）和主 agent 的 cf_agent_tool_runs 注册表共同维护；观察通道（即主 agent 的实时广播循环）可因网络断开、页面刷新或主 agent 崩溃而消失，但执行本身不受影响。

### 断线重连与回放

客户端重连回放流程：当新客户端（或刷新后的同一客户端）通过 WebSocket 连接到主 agent 时，主 agent 在完成自身 chat 协议的恢复设置后，遍历 cf_agent_tool_runs 注册表中所有历史运行行。对每一行：先合成一条 started 事件（sequence=0）发送给新客户端，然后通过 subAgent 获取对应子 agent 的 RPC 桩，调用其 getAgentToolChunks(streamId) 方法获取该运行的持久化 chunk 列表，逐条封装为 chunk 事件发送，最后根据行的 status 字段合成对应的终结事件（finished、error 或 interrupted）。状态为 running 的行不发送终结事件——若主 agent 当前仍有活跃的观察循环，后续的实时 chunk 和终结事件将通过正常广播路径到达新客户端。回放帧携带 replay: true 标记，客户端使用 (parentToolCallId, runId, sequence) 三元组去重，正确处理回放帧与实时广播帧的交叠。

### 主 agent 崩溃恢复与状态调和

主 agent 崩溃/被驱逐后的恢复：当主 agent 的 Durable Object 因代码更新、资源限制或超时被驱逐后重新激活时，其 onStart 生命周期方法扫描 cf_agent_tool_runs 表中所有 status='running' 的行。对于每一行，尝试通过 runId 查询子 agent 的当前状态：若子 agent 报告 completed，则回放其持久化 chunk 并将主侧行更新为 completed；若报告 error 或 aborted，同步主侧状态；若报告 running（子 agent 仍在执行但原始观察循环已消失），在 V1 实现中将主侧行更新为 interrupted，并附上精确的错误说明。这种逐行调和机制确保主 agent 重启后所有子 agent 运行都有诚实的终结态或 interrupted 标记，不存在永远处于 running 的僵尸行。子 agent 的 chat 恢复由 Think/AIChatAgent 自身的 fiber 恢复机制独立处理：子 agent 的 chat 轮次通过调度回调从父 agent 的 alarm 路由回子 facet 内部执行恢复。

### 并发控制

系统在子 agent 实例级别实施并发防护。每个子 agent 实例维护一个同步的运行中标记（_runInProgress），在 runTurnAndStream 入口处检查并置位，在 finally 或 cancel 回调中清除。这防止了对同一子 agent 实例的并发调用导致 forwarder 或 requestId 状态污染。多个并行的子 agent 运行通过不同的子 agent 实例（不同的 runId/facet）天然隔离。此外，主 agent 层提供 maxConcurrentAgentTools 配置选项，限制同时处于 running 状态的子 agent 运行总数，超出限制时快速失败并发出明确的 error 事件，为成本控制提供粗粒度保障。

### 取消传播机制

取消机制沿 AbortSignal 链路从主 agent 逐层传递到子 agent 的推理循环：(1) 主 agent 的 chat 轮次或请求被取消时，传递给 runAgentTool 的 AbortSignal 触发；(2) 主 agent 将该信号注册为子 agent RPC reader 的 cancel 触发器——信号 abort 时调用 reader.cancel()；(3) workerd 的 RPC 桥将 reader 取消传播到子 agent 侧的 ReadableStream，触发其 cancel 回调；(4) 子 agent 的 cancel 回调 abort 一个本轮专用的 AbortController；(5) 该 AbortSignal 被线程化传入 saveMessages({ signal })，使 Think 的推理循环同步终止。关键设计：若 signal 在 reader 创建前已处于 aborted 状态，则在进入读取循环前同步取消 reader，确保 workerd 的源端 cancel 仍被触发。此外，取消观察流（reader.cancel）不等同于取消执行——仅当取消来源于主 agent 活动操作的显式 abort 信号时才中止执行；浏览器断开或主 agent 重启导致的 reader 断开仅分离观察，不中止子 agent 运行。cancelAgentToolRun(runId) 操作是幂等的：若子 agent 已处于终结态，延迟的取消不覆写为 aborted。

### 访问控制与钻入

子 agent 工具通过已有的子 agent 路由原语对外可寻址，URL 形态为 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。这支持"钻入"（drill-in）场景：开发者可独立连接到一个已完成或运行中的子 agent，使用正常的 useAgentChat 查看其完整对话历史。系统在父 agent 侧安装 onBeforeSubAgent 中间件钩子实施严格的注册表门控：仅当请求的 (childClassName, childName) 在 cf_agent_tool_runs 注册表中存在匹配行时才放行，否则返回 404。这防止了攻击者通过猜测 runId 创建任意子 agent facet 的安全风险。内部 subAgent() 调用绕过此钩子。runId 不作为独立的访问凭证：钻入 URL 始终通过父 agent 的已有身份（user id 等）路由，认证和租户隔离由父 agent 继承。在框架驱动的子 agent 工具运行期间，框架持有对该子 agent 实例的独占声明；钻入用户在此时发送 chat 消息应被延迟或明确拒绝，而非与进行中的运行交叉执行。

### 清理与保留策略

子 agent 工具运行完成后，其子 agent facet 和主侧注册表行默认保留而不自动删除。这是为了支持运行结束后的刷新回放、钻入查看、失败调试和审计追踪。系统提供显式的 clearAgentToolRuns(...) 清理 API，支持按时间范围（olderThan）和状态集合（status）过滤删除。清理操作同时删除主侧注册表行和对应的子 agent facet（包括其持久化存储），避免产生孤儿 transcript。在清理处于 running 状态的运行时，先执行取消操作再删除，防止留下无观察者的孤立 LLM 工作。默认仅持久化 inputPreview（输入摘要）而非完整原始输入，避免在编排表中产生敏感数据（如 prompt、凭证、文件内容）的第二份副本。应用层可自主决定保留策略、垃圾回收规则和输入/输出可见性。

### 并行执行与扇出

系统支持同一主 agent 工具调用下并行派发多个子 agent 运行。通过 Promise.allSettled 并发启动多个子 agent，每个子 agent 独立运行在自己的 Durable Object facet 上，互不干扰。并行场景下，每个子 agent 运行通过 parentToolCallId（相同）+ displayOrder（不同，如 0, 1, 2...）来定位：客户端按 order 排序渲染子 agent 面板，确保左右排列与 LLM 指定的输入顺序一致，而非由各子 agent 完成速度决定的竞态顺序。当并行运行中某一分支失败时，使用 allSettled 确保其他分支继续运行至完成。取消操作中，所有并行分支共享同一 AbortSignal——主 agent 的 chat 轮次被取消时，所有子 agent 的 RPC reader 同时被取消，各自的推理循环同步终止。

### 无父工具调用的后台子运行

除 LLM 通过工具调用触发的子 agent 运行外，系统通过命令式 API runAgentTool 支持无直接父工具调用的后台子运行。此类运行不由 LLM 的工具选择触发，而是由应用程序代码在以下场景直接调用：(1) 通过 @callable 装饰的 HTTP 端点或 RPC 方法触发；(2) 定时调度回调中启动的定期分析或报告生成；(3) 非 chat Agent（继承自 Agent 基类而非 Think/AIChatAgent）发起的确定性多阶段工作流。命令式运行不携带 parentToolCallId，其事件帧中该字段为空。客户端通过 useAgentToolEvents hook 的 unboundRuns 列表直接渲染这些运行，无需关联到任何 chat 消息中的工具调用部件。命令式运行的恢复语义更清晰——应用程序代码可在后续任意时间通过 runId 检查运行结果，无需重建飞行中的 LLM 轮次上下文。

### 客户端状态管理

系统在前端提供无头（headless）的 React 原语来处理 agent-tool-event 帧，包括：(1) applyAgentToolEvent 纯 reducer，负责过滤 agent-tool-event 消息、按 (parentToolCallId, runId, sequence) 去重、通过 applyChunkToParts 将 JSON 编码的 UIMessageChunk body 应用到子 agent 消息部件上、按 parentToolCallId 分组运行并按 displayOrder 排序；(2) useAgentToolEvents hook，订阅已有 useAgent 连接上的原始消息，暴露 runsById（按 runId 索引的运行状态映射）、runsByToolCallId（按工具调用 ID 分组的运行列表）和 unboundRuns（无 parentToolCallId 的命令式运行列表）。hook 不拥有面板 UI、钻入连接或服务端清理策略，仅负责状态重建。应用代码按需渲染子 agent 面板、钻入按钮和清理逻辑。

### 子 agent 调度与 Fiber 恢复

子 agent 的持久化恢复依赖框架的调度与 fiber 机制。虽然子 agent facet 不拥有独立的物理 alarm 槽位，但系统通过以下机制支持子 agent 侧的长时间运行和恢复：(1) 子 agent 的调度行以 owner path 存储在顶层父 agent 的调度表中，alarm 触发时路由回调到拥有该调度行的子 facet；(2) 子 agent 可注册由父 agent 持有的 keepAlive 心跳引用，在活跃工作期间防止被驱逐；(3) Think 的 chatRecovery 选项使子 agent 在 facet 重建后能通过 runFiber 恢复未完成的 chat 轮次。这意味着即使主 agent 的观察循环消失，子 agent 自身的推理工作可继续执行并在 facet 内持久化所有中间 chunk，主 agent 重启后可通过 runId 查询子 agent 状态并回放已存储的内容。
