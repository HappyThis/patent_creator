## 技术方案

本技术方案描述一种基于 Durable Object 的多代理协作系统，在已有 chat agent / Think agent 和子代理（sub-agent/facet）基础设施之上，构建一套代理工具编排层（Agent Tool Orchestration），使主代理在一次请求处理中能够按需派发一个或多个子代理执行研究、规划、比较、总结等任务，同时将子代理的执行进度、输出片段和生命周期事件实时回传至主代理所在会话视图，并在断线重连后完整恢复已发生的执行过程。

### 系统整体架构

系统由三层组件构成：浏览器客户端通过 WebSocket 与主代理（Parent Agent）保持长连接；主代理是一个基于 Durable Object 的 chat-capable 代理（如 Think 或 AIChatAgent 子类），负责处理用户对话并作为编排中心；子代理（Agent Tool Sub-Agent）同样是 chat-capable 代理，通过 Durable Object 的 facet 机制与父代理共置部署，拥有独立的 SQLite 存储、模型调用能力和会话上下文。

主代理通过框架提供的两种 API 派发子代理任务：agentTool(Cls, options) 将子代理封装为模型可调用的工具（AI SDK Tool），模型在推理过程中自主决定何时调用哪个子代理；runAgentTool(Cls, options) 提供命令式接口，适用于服务端确定性流程、后台任务、定时触发、HTTP 回调等非模型驱动的编排场景。两种 API 共享同一套子代理运行注册表、事件协议和生命周期管理机制。

### 子代理的创建与身份体系

子代理通过父代理调用 this.subAgent(Cls, name) 创建。底层基于 workerd 运行时的 ctx.facets 机制实现：ctx.facets.get(name, () => ({ class: exports[Cls.name] })) 创建或获取一个与父代理共置的子 Durable Object。每个子代理拥有独立的 SQLite 数据库实例，在存储层面与父代理完全隔离。

子代理通过 SubAgentStub 暴露类型化 RPC 接口。SubAgentStub 通过映射类型自动排除 Agent 基类的内部方法（如 sql、schedule、broadcast 等），仅暴露子类自定义的公共方法，使父代理对子代理的调用具有编译时类型安全。子代理之间和父子之间通过 RPC 传递结构化可克隆数据，流式数据通过 ReadableStream<Uint8Array> 在 DO RPC 通道上传输。

每个子代理在创建时被赋予唯一标识 runId，该标识贯穿整个生命周期：作为子代理的 Durable Object 名称、父代理运行注册表的主键、子代理侧运行映射的外键、以及重放、drill-in、取消和清理操作的统一句柄。此外，系统维护祖先身份链（parentPath / selfPath），记录从根代理到当前代理的完整路径，支持反向引用（parentAgent(Cls) 获取直接父代理的类型化 RPC 桩）和嵌套子代理场景（子代理可继续派发其自身的子代理）。

### 代理工具运行注册表

主代理在 SQLite 中维护框架管理的代理工具运行注册表（cf_agent_tool_runs），记录每一次子代理派发的元数据。该表包含以下核心字段：run_id（主键，即子代理名称）、parent_tool_call_id（关联的父工具调用 ID，用于将子代理事件挂载到正确的聊天工具部件下；命令式调用时可为空）、agent_type（子代理类名）、input_preview（输入摘要，默认持久化而不保存完整原始输入）、status（运行状态）、summary（完成后的文本摘要）、error_message（错误信息）、display_order（展示顺序，用于并行派发时的确定性排序）、stream_id（子代理持久化流标识）、started_at / completed_at 时间戳。

注册表的原子写入顺序是：在子代理实际启动之前，先插入一行 status='starting' 的记录。这保证了即使父代理在子代理启动过程中崩溃，重连恢复时也能通过扫描非终态行进行状态协调。子代理运行过程中，status 更新为 'running'；完成后更新为终态（'completed'、'error'、'aborted' 或 'interrupted'）。终态一旦写入即为权威状态，后续的延迟取消或重复协调不得覆写已完成的终态。

子代理侧维护独立的运行映射表（cf_agent_tool_child_runs），将编排层的 runId 映射到子代理内部的 requestId（聊天回合标识）和 streamId（可恢复流标识）。这一映射在子代理启动时持久化，使父代理在崩溃恢复时能够通过 runId 向子代理查询内部的请求标识和流标识，从而准确重放对应的聊天内容，而不依赖父代理未持久化的内存状态。启动操作按 runId 实现幂等：如果子代理已存在该 runId 的运行记录，则返回现有状态而不会创建重复的聊天回合。

### 子代理事件协议

主代理通过统一的 agent-tool-event 协议帧将子代理的执行过程广播给所有连接的客户端。每个帧包含：type（固定为 "agent-tool-event"）、parentToolCallId（父工具调用 ID，命令式运行时可为 null）、sequence（在该子代理运行内的单调递增序号）、可选的 replay 标记、以及 event 载荷。

event 载荷定义六种事件类型：(1) started — 子代理已启动，携带 runId、agentType、inputPreview、order 和可选的 display 元数据（名称、图标）；(2) chunk — 子代理输出片段，body 字段为 JSON 编码的 UIMessageChunk，客户端使用与主聊天流相同的 applyChunkToParts 原语重建消息部件（文本、推理过程、工具调用、工具结果等）；(3) finished — 正常完成，携带 summary 文本摘要；(4) error — 子代理执行失败，携带 error 错误描述；(5) aborted — 被显式取消；(6) interrupted — 父代理在协调过程中发现无法安全恢复的运行。

终端事件类型具有明确语义分工：error 表示子代理内部执行失败（如模型调用错误、工具异常）；aborted 表示被父代理显式取消（用户点击停止按钮、标签页关闭、兄弟代理取消传播）；interrupted 是父代理专属状态，表示父代理在崩溃恢复后发现某个子代理仍在运行但已无法实时追踪其输出（V1 阶段标记为 interrupted 并提供已持久化的 chunk 重放）。这一区分使客户端能够以不同视觉状态呈现失败、取消和中断三种不同的终态。

### 模型工具调用路径（agentTool）

当主代理的模型在推理过程中需要派发子代理时，使用 agentTool(Cls, options) 将子代理封装为 AI SDK 可调用工具。该工厂函数接收子代理类、工具描述（供模型选型）、inputSchema（Zod schema，用于工具参数验证）和可选的 outputSchema（结构化输出验证），生成标准的工具定义条目。工具定义中的 execute 函数内部调用 runAgentTool，并将模型提供的 toolCallId 作为 parentToolCallId 传入，同时将模型的 abortSignal 线程化传入以实现取消传播。

工具返回给模型的结果取决于子代理终态：completed 时返回结构化 output（若配置了 outputSchema）或文本 summary；error 时返回 { ok: false, error } 结构化失败信息；aborted 时返回取消提示；interrupted 时返回中断提示并阻止模型基于不完整数据生成幻觉性总结。这确保模型始终获得诚实的执行结果反馈，能够据此决定是否重试、跳过或向用户报告失败。

### 确定性流程调用路径（runAgentTool）

runAgentTool(Cls, { input, runId?, parentToolCallId?, displayOrder?, signal? }) 提供命令式接口，适用于非模型驱动的编排场景。典型场景包括：通过 @callable 装饰的 RPC 方法触发的定时报告生成；HTTP webhook 触发的后台分析任务；多阶段工作流中某个阶段以代码逻辑决定派发子代理；父代理直接继承 Agent（而非 chat agent）但仍需派发子代理执行 AI 任务。

runAgentTool 按 runId 实现幂等：如果传入的 runId 已存在终态运行记录，直接返回已有结果而不重新执行；非终态运行不会启动重复工作。这使得 runAgentTool 可以安全地从重试路径、定时告警和重连恢复逻辑中调用，避免意外复制 LLM 工作。该方法返回 RunAgentToolResult，包含 runId、agentType、status 和可选的 output/summary/error，调用方可以同步等待结果（用于串行流水线）或通过 Promise.allSettled 实现并行扇出并逐分支处理成败。

### 无父工具调用的后台子运行

runAgentTool 支持不带 parentToolCallId 的调用模式，即子代理的运行不关联到父代理的任何特定工具调用部件。这种后台子运行的事件在客户端被归入 unboundRuns 集合，由应用层自行决定渲染位置（例如独立的进度面板、通知栏或后台任务列表），而非嵌入聊天消息的工具调用部件内部。典型场景包括：父代理通过 HTTP 端点或 @callable RPC 触发的后台研究任务；用户刷新页面后，后台子代理继续运行并在完成时通过事件协议推送结果。

### 流式事件实时展示与重放去重

子代理在执行过程中产生的每个 UIMessageChunk（文本增量、推理增量、工具调用开始/输入/输出等）通过以下链路实时到达浏览器：子代理的 chat-capable 基类（Think/AIChatAgent）通过其内部 _resumableStream 产生 chat-response 帧；子代理的 broadcast 覆写方法将这些帧捕获，编码为 NDJSON 格式（{ sequence, body }）并写入 RPC ReadableStream；父代理通过 stream.getReader() 读取该 RPC 流，解析每一行 NDJSON，将 body 重新封装为 agent-tool-event 的 chunk 事件，并通过 this.broadcast 发送到主代理的所有 WebSocket 连接。

客户端通过 (parentToolCallId, runId, sequence) 三元组进行事件去重。sequence 由父代理按子代理运行维度单调递增分配：started 事件为 sequence 0，chunk 事件为 sequence 1..N，finished/error/aborted 事件为 sequence N+1。重放帧（replay: true）与实时帧（无 replay 标记）共享同一 sequence 空间，客户端在接收重放帧和实时帧的交叉场景下，通过三元组判断丢弃已接收的重复帧。事件帧不依赖父代理的聊天流持久化——子代理的可恢复流（_resumableStream）是其自身事件日志的持久化存储。

### 连接断开恢复机制

系统在多个层面实现断线恢复。子代理层面：chat-capable 子代理启用 chatRecovery 模式，其内部的 _resumableStream 将每个 UIMessageChunk 在生成时即持久化到子代理的 SQLite。子代理通过主代理的物理 alarm 实现逻辑调度——子代理将自身恢复回调注册到主代理的 alarm 表中并附带所有者路径，主代理 alarm 触发时路由回调到对应的子代理 facet 内执行，从而实现长期运行子代理的休眠恢复。

父代理层面：onStart 生命周期中执行非终态行协调——将所有 status='running' 的注册表行标记为 'interrupted'（因为父代理的实时观察循环已随进程退出而丢失）。onConnect 中对每个注册表行进行事件重放：先从行数据合成 started 事件，然后通过子代理的 getChatChunksForReplay(streamId) RPC 方法获取该运行已持久化的所有 chunk，逐条以 replay: true 发送，最后根据行的终态合成 finished/error 或 interrupted 事件。streamId 在子代理完成时被捕获并存入注册表行，确保重放读取的是该次工具调用的原始回合数据，而非 drill-in 用户后来追加的对话回合。

### 取消传播机制

取消传播链从用户操作（点击停止按钮、关闭标签页）开始，沿以下路径传导至子代理的模型推理循环：用户操作触发主代理聊天回合的 AbortSignal → agentTool 工具执行函数的 abortSignal 被触发 → 父代理调用 RPC reader.cancel(signal.reason) → workerd 的 DO RPC 层将取消传播到子代理侧 ReadableStream 的 cancel 回调 → 子代理的 cancel 回调触发其内部 per-turn AbortController.abort() → 该 AbortSignal 通过 saveMessages({ signal }) 传入 Think/AIChatAgent 的推理循环 → 推理循环同步终止。

关键设计原则：观察流断开不等于执行取消。浏览器断开连接或父代理重启只应分离观察者，不应自动取消子代理的执行。只有显式的 AbortSignal 传播才能触发取消。此外，取消操作按 runId 幂等——如果子代理已到达终态（completed/error），延迟到达的取消信号不得覆写终态为 aborted。并行扇出场景下，所有子代理共享同一个父级 AbortSignal，但通过 Promise.allSettled 而非 Promise.all 收集结果，确保单个分支失败不阻塞其他分支的独立完成和状态更新。

### 访问控制与 Drill-in

子代理通过现有子代理路由原语对外可寻址：URL 模式为 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。客户端可通过 useAgent({ agent: parentClass, name: userId, sub: [{ agent: childClass, name: runId }] }) 建立到特定子代理的直接 WebSocket 连接，获取完整的对话历史——这称为 Drill-in（深入查看）。

系统通过 onBeforeSubAgent 钩子实现严格的注册表门控访问控制。任何来自外部的子代理 HTTP/WebSocket 请求进入父代理时，该钩子在路由到子 facet 之前被调用。默认的安全策略是：检查请求的 (className, name) 是否在父代理的 cf_agent_tool_runs 注册表中存在对应行——仅当该子代理确由父代理通过 runAgentTool 合法创建时才放行。未在注册表中的请求返回 404，防止攻击者通过猜测 runId 构造任意子代理 facet。内部 subAgent() 调用绕过此钩子，因为它们是同 isolate 内的 trusted 调用路径。此外，runId 本身不作为能力令牌使用——Drill-in URL 始终通过父代理的身份体系（useAgent({ agent: parent, name: userId, sub: ... })）到达，认证和租户隔离从父代理继承。

### 清理与保留策略

子代理及其运行记录在完成后默认保留，不自动删除。保留是必须的：完成后的刷新重放、Drill-in 深入查看、失败运行的调试和审计追踪都依赖于子代理 facet 和注册表行的持续存在。系统提供显式清理 API：clearAgentToolRuns() 删除所有注册表行及对应的子代理 facet；clearAgentToolRuns({ olderThan }) 按时间窗口清理；clearAgentToolRuns({ status }) 按状态过滤清理（如仅清理 completed 和 error 的旧运行）。清理操作采用"先取消再删除"的安全顺序：对仍处于 running 状态的运行，先执行取消操作终止 LLM 工作，再删除注册表行和子代理 facet，防止留下无观察者且无法终止的孤立推理任务。

### 并发控制与重复请求处理

系统在多个层面处理并发和重复请求。子代理实例层面：每个子代理维护同步互斥标志（_runInProgress），拒绝并发的框架驱动的 runTurnAndStream 调用，防止多个调用竞争式地覆写转发器和 requestId 状态。同一 runId 的重复 runAgentTool 调用通过注册表幂等检查处理：终态运行直接返回已有结果，非终态运行不启动重复工作。

父代理层面提供 maxConcurrentAgentTools 并发上限配置，当正在运行的子代理数量达到上限时，新的派发请求快速失败并产生 error 事件，而非无限排队。并行扇出场景下，多个子代理各自拥有独立的 Durable Object 实例和 SQLite，彼此之间不存在共享可变状态竞争。但 drill-in 用户在子代理正在执行框架驱动的回合时发送聊天消息，应由 chat 基类的回合队列机制处理——推迟或拒绝而非与运行中的推理回合交错执行。

### 关键处理流程

模型驱动子代理调用的完整处理流程如下：(1) 用户通过 WebSocket 向主代理发送消息；(2) 主代理的推理循环中，模型决定调用某个 agentTool 封装的子代理工具，生成 toolCallId 和结构化参数；(3) agentTool 的 execute 函数调用 runAgentTool，传入 toolCallId、模型参数和 abortSignal；(4) runAgentTool 生成 runId，在注册表中插入 status='starting' 行，通过 subAgent(Cls, runId) 创建子代理 facet，广播 started 事件；(5) 通过子代理的 startAgentToolRun 驱动聊天回合，子代理将 runId→requestId→streamId 映射持久化到自身的子运行表；(6) 子代理的推理循环产生 UIMessageChunk，经 broadcast 覆写方法捕获并通过 RPC ReadableStream 回传父代理；(7) 父代理读取 RPC 流，将每个 chunk 封装为 agent-tool-event 的 chunk 帧通过 WebSocket 广播给客户端；(8) 子代理完成回合后，父代理读取子代理的最终文本摘要（getFinalTurnText RPC），广播 finished 事件，更新注册表行为 completed；(9) 父代理将摘要作为工具返回值返回给模型，模型基于摘要继续推理并生成最终回复。

断线重连的恢复流程如下：(1) 客户端建立新的 WebSocket 连接；(2) 主代理的 onConnect 触发，Think 基类先完成聊天消息的恢复或全量广播；(3) onConnect 查询 cf_agent_tool_runs 注册表，按 started_at 升序遍历所有行；(4) 对每行：从行数据合成 started 事件（replay: true, sequence=0）；通过子代理的 getChatChunksForReplay(streamId) RPC 获取持久化的 chunk 列表，逐条以 replay: true 发送；根据行的终态合成 finished/error/interrupted 事件；(5) 客户端通过 (parentToolCallId, runId, sequence) 三元组去重，与可能正在进行的实时广播无缝合并。

### 技术效果

本方案在已有 Durable Object 代理框架基础上，以最小的侵入性实现了多代理编排能力：(1) 子代理复用现有 chat-capable 代理体系（Think/AIChatAgent），应用开发者无需重写代理基类，仅需定义子代理的系统提示词和工具集即可作为可编排能力单元；(2) 注册表驱动的状态管理将编排元数据与对话内容分离，父代理的崩溃恢复不依赖内存状态，子代理的对话内容独立持久化在各自的 SQLite 中；(3) 统一的事件协议使模型工具调用、命令式调用和后台运行三种场景共享同一套客户端渲染基础设施，减少了前端适配成本；(4) 流式 chunk 复用标准 UIMessageChunk 格式，客户端使用相同的 applyChunkToParts 原语处理主代理和子代理的输出，无需为子代理开发独立的渲染管线；(5) onBeforeSubAgent 注册表门控提供了零配置的默认安全策略，防止未授权访问。

### 风险与待确认问题

以下问题在 V1 方案中已识别但尚未完全解决：(1) 延迟实时追踪重连（late live-tail reattach）：V1 阶段当父代理在子代理运行期间崩溃恢复后，只能重放已持久化的 chunk 并将运行标记为 interrupted，无法重新挂载实时观察流。需要 tailAgentToolRun 接口和 observer 状态分离机制来支持。(2) 跨回合子代理（multi-turn agent tools）：当前一个 runId 映射到一个子代理聊天回合。若需要子代理与用户进行多轮交互（如追问澄清），需要扩展子代理侧运行映射表支持多回合索引。(3) 结构化输出提取：当前 outputSchema 的可选验证依赖于子代理显式返回结构化数据，不支持从子代理的自然语言输出中自动抽取结构化结果。(4) 跨机器子代理：当前 facet 机制要求子代理与父代理共置在同一机器上，远程子代理需要不同的存根类型和故障模式处理。(5) 追踪与成本核算：子代理的 LLM 调用成本可能数倍于单代理场景，V1 通过生命周期钩子提供扩展点，但未标准化 OpenTelemetry 或计费集成。
