## 技术方案

### 总体架构

本技术方案提出一种保留式流式子 Agent 工具编排系统，在已有基于服务端持久对象的 Agent 框架之上，构建主 Agent 与子 Agent 之间的结构化协作层。核心思路是：将每个子 Agent 视为主 Agent 的一个可调用工具，子 Agent 保持独立的持久执行上下文（独立的 SQLite 数据库、独立的聊天消息流、独立的模型推理循环），主 Agent 通过框架提供的统一 API 调度子 Agent 执行，并将子 Agent 的执行过程和结果以流式事件的形式带回主会话视图。

系统包含以下核心层次：（1）底层依托已有的子 Agent 路由与存储原语（subAgent、基于 Durable Object 的 Facet 机制），每个子 Agent 作为主 Agent 的子 Facet 存在，享有隔离的 SQLite 存储和独立的生命周期；（2）中间层是框架提供的 Agent Tool 编排层，包括运行注册表、子侧映射表、适配器协议和事件转发管道；（3）上层提供两种 API 形态：面向 LLM 自主调度的 agentTool 工具工厂，以及面向确定性工作流的 runAgentTool 命令式 API。

### 核心数据模型：父侧运行注册表

主 Agent 侧维护一个框架管理的 Agent Tool 运行注册表（cf_agent_tool_runs），用于记录每一次子 Agent 调用的元数据。该注册表至少包含以下字段：run_id（运行标识，作为跨系统的主键）、parent_tool_call_id（父工具调用标识，用于将子 Agent 运行关联到父 LLM 的工具调用）、agent_type（子 Agent 类型名）、input_preview（输入摘要，默认仅保存摘要而非完整输入以避免敏感数据泄漏）、status（运行状态）、summary（完成后的文本摘要）、error_message（错误信息）、display_order（展示排序）、started_at 和 completed_at（时间戳）。

该注册表存储在主 Agent 的 SQLite 中，由框架拥有和维护。注册表的记录插入发生在子 Agent 被实际启动之前，确保即使在父 Agent 崩溃后，也能通过注册表获知曾经发起的子 Agent 运行。框架负责在子 Agent 启动、完成、出错、被取消或中断时更新状态，并在父 Agent 重启后对非终态记录进行调和（reconciliation）。应用层负责策略决策（如保留时间、垃圾回收规则、访问控制），而非注册表的实现机制。

### 核心数据模型：子侧运行映射表

子 Agent 侧维护一个框架管理的运行映射表（cf_agent_tool_child_runs），用于将编排层的 run_id 映射到子 Agent 内部的聊天轮次标识和流持久化标识。该映射表至少包含：run_id（编排层运行标识）、request_id（子 Agent 内部聊天轮次标识）、stream_id（可恢复流的持久化标识）、status（运行状态）、summary（文本摘要）、error_message（错误信息）、started_at 和 completed_at。

将 run_id 与 request_id 和 stream_id 分离的设计具有重要意义：run_id 是产品/编排层的标识，request_id 是聊天轮次/取消注册表的标识，stream_id 是可恢复流持久化的标识。这种分离避免将"一次 Agent Tool 运行等于一次聊天轮次"的假设固化到数据模型中，未来可以自然演进为支持单次 Agent Tool 运行包含多个子聊天轮次。

子侧映射表是恢复的权威数据源。如果父 Agent 在子 Agent 启动后、父 Agent 存储生成的 id 之前崩溃，父 Agent 必须能够通过 run_id 从子 Agent 查询恢复 request_id 和 stream_id。启动操作基于 run_id 是幂等的：如果子 Agent 已存在该 run_id 的运行记录，则返回已有运行状态而不启动重复的聊天轮次。

### 子 Agent 适配器契约

本方案引入子 Agent 适配器（AgentToolChildAdapter）作为框架内部的抽象契约，使任意继承自聊天能力基类（如 Think 或 AIChatAgent）的子 Agent 子类自动获得作为 Agent Tool 被编排的能力，无需用户继承特定的工具基类。适配器对子 Agent 实例必须支持以下操作：

（1）startAgentToolRun：以 run_id 和输入数据启动一次 Agent Tool 运行，返回运行检查结果。该操作是幂等的——已存在的终态运行返回其终态检查结果，已存在的运行中运行返回运行中检查结果，不创建重复的聊天轮次。（2）cancelAgentToolRun：通过 run_id 取消运行，同样是幂等的——终态运行不会被延迟取消重写为 aborted。（3）inspectAgentToolRun：通过 run_id 查询运行状态和内部映射（requestId、streamId），这是恢复的权威数据源。（4）getAgentToolChunks：重放已持久化的流式块，用于重连后的历史回放。

适配器设计的关键是将"执行"与"观察"分离。启动一个 Agent Tool 创建由 run_id 标识的持久化工作；将事件转发给父 Agent 是对该工作的观察。丢弃观察者流不自动取消执行；显式取消是针对 run_id 的独立操作路径。这一分离保证了网络短暂断开或父 Agent 重启后，子 Agent 的已有工作不会被误取消。

### Agent Tool 事件协议

主 Agent 通过其 WebSocket 连接向客户端发送 Agent Tool 事件。事件消息包含以下字段：type 固定为 "agent-tool-event"；parentToolCallId 将事件关联到父 LLM 的特定工具调用（命令式调用可缺省）；sequence 是父 Agent 加盖的单调递增序号，用于客户端去重和排序；replay 标记区分实时事件和历史重放事件。事件体（event）分为以下类型：

（1）started 事件：携带 runId、agentType、inputPreview、order（展示排序）和可选的 display（名称、图标等展示元数据），在子 Agent 启动时合成，使 UI 在流式块到达前即可渲染面板。（2）chunk 事件：携带 runId 和 body，其中 body 为 JSON 编码的 UIMessageChunk（AI SDK 标准块格式），框架不发明第二种块词汇表，客户端可直接复用 applyChunkToParts 原语重建子 Agent 的消息部件。（3）finished 事件：携带 runId 和 summary，表示子 Agent 正常完成。（4）error 事件：携带 runId 和 error，表示子 Agent 执行失败。（5）aborted 事件：携带 runId 和可选的 reason，表示被显式取消。（6）interrupted 事件：携带 runId 和 error，表示父 Agent 在恢复时发现子 Agent 仍在运行但无法重新挂载观察者。

终态事件彼此独立，使 UI 能对不同终态（失败、取消、中断）做差异化渲染。sequence 在单次 Agent Tool 运行内单调递增。客户端的去重键为 (parentToolCallId, runId, sequence)，因为同一父工具调用下的并行子 Agent 各自合法地从 sequence 0 开始。

### 流式事件转发与观察者机制

流式事件转发是连接子 Agent 独立执行上下文与主会话视图的核心管道。当主 Agent 通过 runAgentTool 启动子 Agent 后，框架执行以下步骤：（1）在主侧注册表中插入状态为 starting 的记录；（2）通过 subAgent 原语获取子 Agent 的 RPC 桩；（3）调用子 Agent 适配器的 startAgentToolRun 启动运行，获得运行检查结果；（4）将注册表状态更新为 running；（5）合成并广播 started 事件；（6）打开子 Agent 聊天流的读取器，逐块读取 UIMessageChunk 并封装为 chunk 事件广播到主 Agent 的所有连接客户端；（7）在子 Agent 到达终态后，读取最终输出文本或结构化输出，合成 finished/error/aborted 事件广播，更新注册表状态。

关键设计要点：子 Agent 的聊天流（ResumableStream）是权威的持久化事件日志。每个块在广播到客户端的同时被写入子 Agent 的 SQLite。主 Agent 仅做转发，不复制存储子 Agent 的流式块——这保持了"状态归属于产生它的 Agent"原则，避免主 Agent 与子 Agent 之间的数据不一致。

框架将转发管道与观察者生命周期解耦。转发管道中的 RPC 流读取器是一个观察者。当浏览器断开连接、父 Agent 重启或重放连接失败时，观察者流被丢弃，但子 Agent 的执行不因此取消。子 Agent 继续运行并持久化块到自己的 SQLite；后续重连时可通过重放路径恢复已产生的全部块。

### 持久化恢复与重连重放

系统提供两种观察路径：（1）原始实时观察——当调用 runAgentTool 的父操作仍在存活期间，事件通过 WebSocket 实时广播；（2）持久化重放——在重连、刷新或父 Agent 重启后，通过 getAgentToolChunks(runId) 从子 Agent 的持久化存储中读取块进行重放。

父 Agent 重启时的恢复流程如下：父 Agent 遍历注册表中所有非终态（starting 或 running）记录，对每条记录执行调和。若子 Agent 不存在或子侧运行记录不存在，说明子 Agent 从未真正启动或已被删除，将父侧记录标记为 interrupted。若子 Agent 报告 completed，则重放持久化块、读取最终输出、将父侧记录标记为 completed。若子 Agent 报告 error 或 aborted，重放持久化块并将父侧记录标记为对应终态。若子 Agent 报告 running，表示执行仍在进行但原始观察者已丢失——在 V1 中重放已持久化块后将父侧记录标记为 interrupted，并附精确的错误说明。

重连重放的具体机制：当新客户端连接时，父 Agent 的 onConnect 处理遍历注册表，对每条记录合成 started 事件（从注册表行数据构建），通过子 Agent 的 getChatChunksForReplay 获取持久化块并逐块发送 chunk 事件，最后根据行状态合成 finished、error 或相应的终态事件。running 状态的行不合成终态事件——实时广播循环将在子 Agent 完成时自动发送。重放事件携带 replay: true 标记，客户端可据此区分批量重放和实时流，优化渲染行为。

重放的正确性依赖 stream_id 的精确传递。子 Agent 在完成每次运行时捕获其 ResumableStream 的 stream_id，父 Agent 将其存入注册表行。重放时按 stream_id 精确读取该次运行的块，避免后续钻入（drill-in）操作产生的新聊天轮次覆盖原始运行的内容。

### 取消传播与并发控制

取消传播采用 AbortSignal 链式传递机制，从父操作逐层传递到子 Agent 的推理循环。传播路径为：（1）父聊天轮次或工具调用被取消；（2）runAgentTool 通过 runId 显式取消子 Agent 运行；（3）子 Agent 的适配器取消其内部 per-turn AbortController；（4）该 AbortController 的 signal 被传入聊天轮次的 saveMessages 方法；（5）子 Agent 的推理循环（如 streamText）检测到 signal 已取消，同步终止推理。该设计消除了传统方案中取消信号到达与推理循环检测之间的竞态窗口。

关键区分：取消观察者流本身不构成取消执行的请求。浏览器断开、父 Agent 重启或重放连接失败仅分离观察，不取消执行。只有来自父 Agent 活跃操作的显式 AbortSignal 才触发执行取消。取消操作是幂等的——如果子 Agent 已到达终态（completed 或 error），延迟取消不会将其重写为 aborted。

并发控制：框架提供父级别的 maxConcurrentAgentTools 选项，限制同时运行的 Agent Tool 数量。超过限制时快速失败并返回清晰的错误事件。精细的配额、基于 token 的预算和计费集成可通过生命周期钩子（onAgentToolStart、onAgentToolFinish）在框架之上分层实现。此外，单子 Agent 实例内部通过同步标志位防止并发的框架驱动轮次，保证同一实例上一次只有一个推理循环在运行。

### 钻入访问与安全控制

子 Agent 通过已有的子 Agent 路由原语保持外部可寻址，URL 形态为 /agents/{父类}/{父名}/sub/{子类}/{子名}。客户端可通过 useAgent({ agent: 父类, name: 父名, sub: [{ agent: 子类, name: runId }] }) 直接连接到子 Agent 进行钻入查看。由于子 Agent 本身就是完整的聊天 Agent，钻入用户可以使用 useAgentChat 获得完整的聊天界面。

访问控制通过父 Agent 的 onBeforeSubAgent 中间件钩子实现。该钩子在父 Agent 将请求转发到子 Agent 之前被调用，可以返回 void（放行原请求）、修改后的 Request（转发修改后的请求）或 Response（直接返回响应而不唤醒子 Agent）。框架在 Agent Tool 机制中安装严格的注册表门控：对 (agentType, runId) 的钻入请求，仅当父侧注册表中存在匹配的运行记录时才放行。这防止了通过猜测 run_id 随意创建子 Agent 实例的攻击。

runId 本身不作为权能凭证。钻入 URL 始终通过父 Agent 已有的身份标识（useAgent({ agent: parent, name: userId, sub: ... })）到达，认证和租户隔离来自父 Agent。框架框架驱动的 Agent Tool 轮次运行期间，子 Agent 实例持有排他声明：对同一 runId 的并发 runAgentTool 调用返回已有运行状态（幂等启动），钻入用户在框架驱动轮次期间发送聊天消息应被延迟或拒绝，而非与运行中的轮次交织。

### 清理保留与生命周期管理

默认策略为保留运行结果。子 Agent 的 Facet 实例和父侧注册表行在运行完成后默认保留，因为完成正是后续检查（刷新重放、钻入查看、失败运行调试、审计追踪）的开始。框架提供显式的清理 API：clearAgentToolRuns() 清空全部运行记录和对应子 Agent 实例；clearAgentToolRuns({ olderThan }) 按时间范围清理；clearAgentToolRuns({ status }) 按状态筛选清理。

清理操作同时删除父侧注册表行和对应的子 Agent Facet，避免仅删除注册表行而遗留孤儿子 Agent 副本。在清理一个状态为 starting 或 running 的运行记录时，框架先取消该运行再删除——跳过取消步骤将留下无观察者、无结果呈现途径的孤立推理工作。

子 Agent 的调度和保活机制：子 Agent 虽然不拥有独立的物理告警槽，但可以通过顶层父 Agent 的告警机制实现逻辑调度。子 Agent 存储自己的调度行（包含所有者路径），当顶层父告警触发时，框架将回调路由回拥有该调度行的子 Agent，子 Agent 的 this 指向自身。类似地，runFiber 的持久化执行和 Think 的聊天恢复（chatRecovery）也适用于子 Agent——fiber 行存储在子 Agent 自己的 SQLite 中，父 Agent 在根侧维护活跃 facet fiber 索引，恢复检查从父告警路由回子 Agent。这使得长时间运行的子 Agent 工作即使在父观察者消失后依然能持久化存续。

### 命令式 API 与工具工厂 API

本方案提供两种互补的 API 形态，共享同一底层机制。第一种是命令式 API（runAgentTool），适用于确定性多阶段工作流、通过 @callable 或 HTTP 触发的报告生成、非 LLM 编排以及扇出/扇入（fan-out/fan-in）场景。调用方式为：const result = await this.runAgentTool(子Agent类, { input, parentToolCallId?, displayOrder?, signal? })，返回 runId、status、summary 和可选的 output。该 API 是幂等的——传入已有 runId 时，终态运行返回已有结果而不重复执行；非终态运行返回 interrupted。

第二种是工具工厂 API（agentTool），将子 Agent 封装为父 LLM 可调用的标准 AI SDK 工具。在 getTools() 中声明：research: agentTool(Researcher, { description, inputSchema, outputSchema?, displayName? })。生成的工具接收 AI SDK 的 toolCallId 和 abortSignal，内部调用 runAgentTool 并将结果返回给父 LLM。需要 inputSchema 用于父 LLM 的工具选择和参数校验；outputSchema 可选，缺省时返回文本摘要。终态对应的工具返回值：completed 返回结构化 output 或文本 summary；error 返回 { ok: false, error }；aborted 返回取消错误；interrupted 返回中断错误。父 LLM 永远不会对非 completed 运行看到静默空结果。

### 协同运行与技术效果

上述各机制协同运作，构成完整的保留式流式子 Agent 工具编排系统。一个完整的调用生命周期为：父 Agent 收到用户请求后，通过 agentTool 封装的工具由父 LLM 决定调度子 Agent，或通过 runAgentTool 由应用程序代码直接调用；框架在父侧注册表插入 starting 记录，通过 subAgent 获取子 Agent 实例，调用适配器启动运行；子 Agent 在自己的推理循环中产生流式块，每个块被持久化到子 Agent 的 SQLite 的同时通过 RPC 流传回父 Agent；父 Agent 封装为 agent-tool-event 帧广播到所有连接的客户端。刷新页面或网络短暂断开后，父 Agent 的 onConnect 通过注册表恢复全部历史运行记录，从子 Agent 的持久化存储重放流式块，客户端重建完整的子 Agent 执行视图。

本方案的技术效果包括：（1）子 Agent 执行与主会话视图的解耦——用户始终只与主 Agent 对话，但能实时观察和事后查看每个子 Agent 的完整执行过程；（2）独立持久化——每个子 Agent 拥有独立的 SQLite 数据库和聊天流持久化，状态归属于产生它的 Agent，不存在跨 Agent 的数据一致性问题；（3）完整的断线恢复——刷新页面或网络短暂断开后，所有已发生的子 Agent 运行记录和流式内容均可完整重放；（4）精确的取消控制——取消信号沿 AbortSignal 链同步传播，推理循环即时终止，无竞态窗口；（5）安全的钻入访问——通过注册表门控确保只有合法产生的子 Agent 实例可被外部访问；（6）灵活的编排模式——同时支持 LLM 自主调度和命令式工作流，支持并行扇出和多级嵌套。
