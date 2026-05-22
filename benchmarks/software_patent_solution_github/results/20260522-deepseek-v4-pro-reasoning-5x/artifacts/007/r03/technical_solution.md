## 技术方案

### 技术问题概述

本方案要解决的核心问题是：在基于 Durable Object 的 agent 框架中，如何让主 agent 在一次用户请求处理过程中，按需调用一个或多个专门的子 agent 执行研究、规划、比较、总结等任务，同时让用户界面能够实时看到每个子 agent 的执行过程和结果，并且在页面刷新、网络断开后能够恢复已发生的子 agent 执行视图。现有方案中，子 agent 的创建和管理已经通过 facet 机制实现（即同一个 Worker 内的共址子 Durable Object），但缺乏将子 agent 的执行流式事件回传到主 agent 会话视图、跨断线恢复、并行子 agent 事件分流、以及模型自动调用与确定性流程调用统一抽象的编排层。

### 系统总体架构

系统沿用 Cloudflare Workers Durable Object 架构。主 agent（如 Think 或 AIChatAgent 的子类）作为一个顶层的 Durable Object，通过 WebSocket 与浏览器客户端维持长连接。子 agent 是主 agent 通过 Durable Object 的 facet 机制在同一 Worker 内创建的子 Durable Object，每个子 agent 拥有独立的 SQLite 存储、独立的 LLM 推理上下文和独立的会话状态。主 agent 与子 agent 之间通过 Durable Object RPC（远程过程调用）进行通信，子 agent 的执行输出通过 RPC ReadableStream 以流式方式回传给主 agent。

### 核心组件与数据流

系统架构包含五个核心层次：（1）浏览器客户端层，通过单一 WebSocket 连接主 agent，接收主 agent 的聊天响应和所有子 agent 的执行事件；（2）主 agent 层（父 Durable Object），负责用户对话管理、子 agent 调度、事件转发、运行注册表维护和断线恢复；（3）子 agent 层（子 Durable Object facet），各自独立执行 LLM 推理任务，维护自己的对话流、工具调用和持久化存储；（4）运行注册表层，在父 agent SQLite 中存储子 agent 运行的元数据（运行标识、状态、关联的工具调用、显示顺序等），在子 agent SQLite 中存储运行到内部请求/流的映射；（5）事件协议层，定义标准化的子 agent 事件信封格式，在父 agent WebSocket 上传输。

### 子Agent运行注册表与生命周期

主 agent 维护一个框架拥有的子 agent 运行注册表（在父 agent SQLite 中建表存储）。该注册表记录每一次子 agent 运行的以下关键信息：运行标识（runId，全局唯一）、关联的父工具调用标识（parentToolCallId，当子 agent 由模型工具调用触发时关联到对应的 toolCallId；当由确定性流程调用时可为空）、子 agent 类型（agentType，对应子 agent 的类名）、输入预览（inputPreview，默认仅存储安全的摘要信息而非完整输入）、运行状态（status，包括 starting、running、completed、error、aborted、interrupted 六种状态）、输出摘要（summary）、错误信息（errorMessage）、显示元数据（display，如名称、图标）、显示顺序（displayOrder，用于多子 agent 并行时的排序）、开始时间（startedAt）和完成时间（completedAt）。注册表的行在子 agent 运行开始之前被插入，并在运行终态时更新。

子 agent 侧维护运行映射表，将编排层的 runId 映射到子 agent 内部的请求标识（requestId）和持久化流标识（streamId）。该映射是幂等的：如果子 agent 已经存在相同 runId 的运行记录，再次调用时返回已有运行状态而非创建重复的推理轮次。这保证了从重试路径、告警回调或重连恢复中调用 runAgentTool 时不会意外重复执行 LLM 推理工作。

运行状态的生命周期为：starting（注册表行已插入、子 agent 尚未确认启动）→ running（子 agent 已开始聊天轮次）→ 终态（completed/error/aborted/interrupted 之一）。其中 interrupted 是特殊的父 agent 侧状态：子 agent 自身不报告 interrupted；只有父 agent 在丢失观察流且无法安全恢复实时观察时，才将非终态运行标记为 interrupted。终态一旦确定即为权威状态，后续的取消操作不能将 completed/error 重写为 aborted，后续的协调操作不能将 aborted 重写为 interrupted。

### 子Agent调用方式

系统提供两种互补的子 agent 调用方式，覆盖模型自动决定调用和服务端确定性流程主动调用两大场景，以及对无直接父工具调用的后台运行的统一支持。

1. 模型驱动调用（agentTool）：agentTool 是一个工具工厂函数，将子 agent 类包装为父 agent LLM 可调用的 AI SDK 工具。在父 agent 的 getTools() 中注册后，当 LLM 决定调用该工具时，内部自动执行：创建运行注册表行、启动子 agent 推理轮次、将子 agent 流式输出以事件转发到父 agent WebSocket、将子 agent 最终摘要返回给父 agent LLM 继续推理。inputSchema 必填（供 LLM 工具选择），outputSchema 可选（供结构化输出校验）。非 completed 终态向 LLM 返回包含错误信息的结构化结果。
2. 确定性流程调用（runAgentTool）：供服务端确定性流程使用的命令式 API。开发者可在 @callable 方法、HTTP 处理器、后台任务中直接调用 this.runAgentTool(ChildClass, { input, parentToolCallId?, displayOrder?, signal? })，返回 { runId, status, summary?, error? }。runAgentTool 按 runId 幂等：终态运行直接返回已有结果；非终态运行不启动重复工作，可安全用于重试路径和重连恢复。parentToolCallId 可选：提供时事件关联到聊天工具部件；不提供时事件进入 unboundRuns 列表。
3. 无父工具调用的后台运行：当 runAgentTool 不传 parentToolCallId 时，子 agent 作为独立后台运行，生命周期事件通过主 agent WebSocket 发出。运行身份由 runId 唯一确定，通过 runId 可进行后续 drill-in 查看、取消和清理，覆盖从 HTTP 端点、定时调度、外部回调等非聊天入口启动子 agent 的场景。

### 流式事件系统

子 agent 的执行输出通过 DO RPC 的 ReadableStream 机制流式回传给父 agent。子 agent 在执行推理轮次时，将其 LLM 推理流（AI SDK 的 UIMessageChunk）逐块写入 RPC ReadableStream，格式为 NDJSON 行（每行包含 { sequence, body }，其中 body 是 JSON 编码的 UIMessageChunk）。父 agent 读取该流后，将每个块包装为 agent-tool-event 事件信封，通过父 agent WebSocket 的 broadcast 方法发送给所有连接的客户端。

事件协议定义六种标准事件类型：started（子 agent 启动，携带 runId、agentType、inputPreview、displayOrder、display 元数据）、chunk（流式输出块，body 为 JSON 编码的 UIMessageChunk，客户端使用与主 agent 聊天流相同的 applyChunkToParts 原语重建子 agent 消息部件）、finished（正常完成，携带 summary）、error（执行失败，携带 error 消息）、aborted（显式取消，携带 reason）、interrupted（父 agent 恢复限制导致的中断）。每个事件以 { type: "agent-tool-event", parentToolCallId?, runId, sequence, event, replay? } 的信封格式在 WebSocket 上传输。

多子 agent 并行时的事件分流机制：每个 agent-tool-event 信封同时携带 parentToolCallId（关联到父 agent 消息的特定工具调用部件）和 runId（唯一标识该子 agent 运行）。客户端通过 (parentToolCallId, runId, sequence) 三元组进行去重——因为同一父工具调用下的并行子 agent 各自的 sequence 都从 0 开始独立编号，仅靠 sequence 无法区分。父 agent 在事件广播时对每个子 agent 运行维护独立的单调递增 sequence，started 事件为 sequence 0，chunk 事件为 sequence 1..N，finished/error/aborted/interrupted 为 N+1。当子 agent 作为无父工具调用的后台运行时，parentToolCallId 为 null，去重键退化为 (null, runId, sequence)。

### 状态管理与恢复机制

系统的状态管理采用双存储策略：子 agent 的运行注册表（运行元数据和生命周期状态）存储在父 agent 的 SQLite 中，因为回放是以父 agent 为视角的——只有父 agent 才知道哪个父工具调用触发了哪个子运行、兄弟运行的显示顺序、以及重连后应合成哪个生命周期事件。子 agent 的对话转录和持久化流块存储在子 agent 自己的 SQLite 中（通过 Think 的 ResumableStream 机制），保证状态归属清晰：子 agent 拥有自己的推理内容，父 agent 拥有编排元数据。

重连回放机制：当客户端重新连接（页面刷新或网络恢复）时，父 agent 的 onConnect 方法在完成聊天协议设置后，遍历运行注册表中所有行，对每一行依次：(1) 从注册表行数据合成 started 事件；(2) 通过 subAgent 获取子 agent 引用，调用其 getChatChunksForReplay 方法按 streamId 获取持久化的聊天流块（利用子 agent 自身 ResumableStream 的 getStreamChunks 能力），将所有块按 chunkIndex 顺序作为 chunk 事件发送；(3) 根据行的终态合成 finished/error/interrupted 事件。所有回放事件携带 replay: true 标记，client 端应用相同的去重逻辑处理回放与实时事件到达的竞争。

父 agent 崩溃恢复（父 Durable Object 被驱逐后重新唤醒）：父 agent 在 onStart 中执行启动协调——将所有 status 为 running 的行标记为 interrupted（因为转发循环已随父 agent 一起消失），并记录协调时间。然后父 agent 遍历每个 previously-running 行：通过 subAgent 获取子 agent 引用，调用子 agent 的 inspectAgentToolRun 方法获取子 agent 侧的实际运行状态；如果子 agent 报告 completed，则回放存储的流块并将父行更新为 completed；如果子 agent 报告 error 或 aborted，则以对应终态更新父行；如果子 agent 仍在 running，在当前实现中将父行标记为 interrupted（未来可通过 tailAgentToolRun 重新挂载实时观察）。子 agent 侧的运行记录不受父 agent 崩溃影响——子 agent 的持久化流和推理轮次在 facet 独立 SQLite 中持续存在。

### 取消、清理与访问控制

取消传播链：取消信号通过 AbortSignal 沿父 → 子方向传播。当父 agent 的聊天轮次被取消（用户点击停止、关闭标签页、或并行分支中止），父 agent 的 AbortSignal 触发，该信号被传递给 runAgentTool 的调用。runAgentTool 取消子 agent RPC ReadableStream 的 reader，workerd 的 RPC 桥将该取消传播到子 agent 侧的流源，触发子 agent 的 cancel 回调，回调内部中止子 agent 推理轮次的 AbortController。该 AbortController 的 signal 被传入子 agent 的 saveMessages({ signal })，使 LLM 推理循环同步终止。关键设计原则：取消观察流不等于取消执行——浏览器断开或父 agent 重启应仅分离观察者，只有显式的 AbortSignal 从父 agent 当前活动操作中发出时才取消执行。

清理保留策略：子 agent 运行在完成后默认保留而非自动删除。这是因为完成时刻正是运行后检查最有价值的时刻——刷新回放、drill-in 查看、失败调试和审计跟踪都依赖保留的子 agent facet 和注册表行。系统提供显式的清理 API（clearAgentToolRuns），可按时间范围（olderThan）和状态过滤（如仅清理 completed 和 error 状态的行）进行批量清理。清理操作同时删除父 agent 注册表行和对应的子 agent facet（deleteSubAgent），确保不会留下孤儿子 agent 转录。当清理操作涉及处于 starting 或 running 状态的运行行时，系统先执行取消再执行删除，避免留下无观察者且无回收途径的运行中 LLM 工作。

Drill-in 访问控制：子 agent 通过已有的子 agent 路由原语对外可寻址（URL 形状：/agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}）。为防止攻击者通过猜测 runId 创建任意子 agent facet，系统在父 agent 的 onBeforeSubAgent 钩子中实现严格注册表门控：仅当父 agent 的注册表中存在匹配 (className, name) 的行时，才放行请求到子 agent。该检查仅作用于外部 HTTP/WebSocket 请求；父 agent 内部的 subAgent 调用绕过此钩子。drill-in URL 始终通过父 agent 的已有身份路由（useAgent({ agent: parent, name: userId, sub: [...] })），认证和租户隔离来自父 agent 的既有机制，不鼓励将 runId 作为独立承载令牌传递。

并发执行控制：系统在父 agent 级别提供 maxConcurrentAgentTools 并发限制选项，当正在运行的子 agent 数量达到上限时，新的 runAgentTool 调用快速失败并产生 error 事件，防止 LLM 推理成本不受控膨胀。子 agent 实例级别的并发保护：每个子 agent 实例在 runTurnAndStream 入口处设置同步运行标志，防止对同一子 agent 实例的并发框架驱动调用破坏内部状态。通过 runId 幂等启动机制，并发调用同一 runId 的 runAgentTool 会返回已有运行状态而非启动重复推理轮次。

### 技术效果

本方案的技术效果体现在以下方面：

- 统一的编排抽象：通过 agentTool 和 runAgentTool 两种互补 API，将模型自动调用和服务端确定性调用统一到同一套子 agent 运行注册表、事件协议和恢复机制之下，避免应用开发者重复实现编排逻辑。
- 实时多子 agent 可见性：利用 Durable Object RPC ReadableStream 和父 agent WebSocket broadcast 的组合，将子 agent 的推理过程以标准化事件流实时推送到用户界面，支持多个并行子 agent 在单一父工具调用下的独立事件分流和 side-by-side 展示。
- 断线恢复与重连回放：通过父 agent 运行注册表 + 子 agent ResumableStream 双存储策略，在页面刷新或网络断开后，重连客户端通过 onConnect 回放机制完整恢复所有已发生的子 agent 执行时间线，包括生命周期事件和流式块内容。去重机制保证回放与实时事件的正确合并。
- 父 agent 崩溃恢复：父 Durable Object 被驱逐后，启动协调机制根据子 agent 侧的实际运行状态诚实更新父侧注册表行，已完成的子 agent 运行不丢数据、不重复执行。子 agent 作为独立 facet 的存储隔离保证其推理内容不受父 agent 生命周期影响。
- 取消传播的完整性：AbortSignal 沿父 agent → runAgentTool → RPC reader cancel → 子 agent cancel 回调 → 子 agent AbortController → saveMessages({ signal }) 逐级传播，保证子 agent 推理循环在父 agent 取消后同步终止，无竞态窗口。
- 访问控制与安全隔离：onBeforeSubAgent 注册表门控防止 URL 猜测攻击创建任意子 agent；子 agent 独立 SQLite 存储实现数据和上下文隔离；drill-in 通过父 agent 身份路由保证认证和租户一致性。
- 兼容现有体系：方案完全基于已有的 subAgent facet 机制、Think/AIChatAgent 聊天基类、ResumableStream 持久化、子 agent 路由和 onBeforeSubAgent 钩子构建，不需要应用开发者重写 agent 框架或引入新的基类约束。子 agent 即为普通聊天 agent 子类，通过内部适配器支持编排能力。

### 风险与待确认问题

以下为当前方案中需要后续确认或完善的风险点和技术边界：

- 实时重挂载（Live-tail reattach）：当前方案在父 agent 丢失观察者且子 agent 仍在运行时，将父行标记为 interrupted 而非重新挂载实时观察流。完整的实时重挂载需要 tailAgentToolRun 能力（基于子 agent 的 ReadableStream 从指定 sequence 后开始订阅），属于后续迭代。
- 跨机器子 agent：当前 facet 机制下子 agent 与父 agent 共址运行（同一 Worker 同一机器）。跨机器的远程子 agent 需要不同的 API 和故障模型，不在当前方案范围内。
- 多轮子 agent：当前方案将一个 runId 映射到一个子 agent 聊天轮次。多轮子 agent（一个逻辑运行内包含多个推理轮次）的 API 语义需要后续设计。
- 结构化输出自动提取：当前方案要求子 agent 显式返回结构化输出；从子 agent 的散文中自动提取结构化 JSON 需要额外的模型调用或解析器，属于后续增强。
- 成本追踪与 OpenTelemetry 集成：当前通过生命周期钩子提供可扩展的观测点，但尚未标准化计费或分布式追踪面。runId 是连接父注册表、子转录、日志和追踪的统一键。
- 子 agent 嵌套调用：子 agent 可以调用自己的 runAgentTool 产生孙 agent，但嵌套运行的观察不自动向上桥接。跨层追踪依赖 runId 作为关联键。
