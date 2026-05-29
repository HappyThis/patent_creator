## 技术方案

### 技术问题概述

本方案要解决的技术问题是：在基于 Durable Object 的 agent 框架中，如何实现主 agent 在一次请求处理过程中按需调用一个或多个子 agent 执行研究、规划、比较、总结等专项任务，使子 agent 的执行进度、流式输出片段、完成、失败、取消等状态能够实时回传到主 agent 所在的同一会话视图中，并且支持页面刷新或网络断开后的恢复，同时兼容现有 Think/AIChatAgent 体系。

### 系统架构总览

系统延续现有 Durable Object (DO) 架构。每个 agent（无论是主 agent 还是子 agent）都是一个 DO 实例，拥有独立的 SQLite 存储和 WebSocket 客户端集。子 agent 通过框架的 subAgent(Cls, name) 原语创建为父 DO 的子 Facet，与父 DO 共置于同一台机器上，通过 DO RPC 进行类型化方法调用，通过嵌套 URL（/agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}）实现外部寻址。

本方案在现有架构之上新增三个核心机制：（1）父智能体侧的运行注册表（cf_agent_tool_runs），记录每次子 agent 调用的元数据和状态；（2）子智能体侧的运行映射表（cf_agent_tool_child_runs），将编排层 runId 映射到 chat turn 的 requestId 和 resumable stream 的 streamId；（3）智能体工具事件协议，定义 started、chunk、finished、error、aborted、interrupted 六种事件类型，在主 agent 的 WebSocket 连接上复用 broadcast 通道传输子 agent 的事件流。

### 父智能体侧：运行注册与事件转发

父 agent 维护一个框架管理的内部运行注册表 cf_agent_tool_runs，表结构关键字段包括：run_id（主键，唯一标识一次子 agent 运行）、parent_tool_call_id（关联的父工具调用 ID，可为空）、agent_type（子 agent 类名）、input_preview（输入摘要，默认仅保留摘要而非完整原始输入以保护敏感数据）、status（starting/running/completed/error/aborted/interrupted）、summary（输出摘要）、error_message、display_order（同一次工具调用中多子 agent 的排序）、started_at、completed_at。

父 agent 在启动子 agent 运行前，先在 cf_agent_tool_runs 中插入一行 status='starting' 的记录。随后通过 subAgent(Cls, runId) 获取或创建子 DO 实例，以 runId 作为子 DO 的名称，调用子 agent 的 startAgentToolRun 接口并将输入和 AbortSignal 传入。子 agent 运行期间产生的每个 MSG_CHAT_RESPONSE 流式 chunk，由父 agent 封装为 agent-tool-event 帧（type: "agent-tool-event"），携带 runId、parentToolCallId、单调递增的 sequence 序号，通过 this.broadcast() 广播给连接到父 agent 的所有 WebSocket 客户端。

runAgentTool 以 runId 为幂等键。如果调用者传入已存在的 runId，终端态运行直接返回已有结果而不重新执行；非终端态运行不启动重复工作——V1 阶段回放已存储 chunk 并标记为 interrupted（因不支持延迟 live-tail 重连）。这使得 runAgentTool 可安全用于重试路径、alarm 回调和重连恢复，不会意外重复 LLM 工作。

### 子智能体侧：独立执行与流式输出

子 agent 本身是一个普通的 Think 或 AIChatAgent 子类实例，不做特殊继承。子 agent 侧维护 cf_agent_tool_child_runs 表，将编排层 runId 映射为 chat turn 的 requestId 和 resumable stream 的 streamId。这种三层 ID 分离设计使未来可将一次逻辑 runId 扩展为多轮 chat turn（run_id + turn_index）。

子 agent 通过框架提供的子智能体适配器接口（AgentToolChildAdapter）对外暴露标准操作：startAgentToolRun（幂等启动，已有终端运行的直接返回已有结果）、cancelAgentToolRun（幂等取消，不覆盖已终端的 completed/error 状态）、inspectAgentToolRun（用于恢复时查询 runId→requestId/streamId 映射）、getAgentToolChunks（按 sequence 获取已存储的 chunk，用于重放）。tailAgentToolRun 为可选接口，用于未来的延迟 live-tail 重连。

子 agent 的流式输出完全复用 Think/AIChatAgent 现有的 ResumableStream 机制。每个 chat turn 产生的 UIMessageChunk 同时完成三件事：通过 StreamAccumulator 增量构建 UIMessage、缓冲写入 SQLite（cf_ai_chat_stream_chunks 表，按 stream_id + chunk_index 索引）、广播给子 agent 自己的 WebSocket 客户端。父 agent 通过 RPC 读取子 agent 的流式 chunk，封装为 agent-tool-event 帧后转发，而非在父 DO 上创建第二条 ResumableStream。这保证了 chunk 的持久化权威数据源始终在子 agent 自身存储中。

### 智能体工具事件协议

父 agent 通过 WebSocket broadcast 通道向客户端发送 agent-tool-event 帧，帧结构为 { type: "agent-tool-event", parentToolCallId?: string, sequence: number, replay?: true, event: AgentToolEvent }。AgentToolEvent 包含六种类型：

- started：携带 runId、agentType、inputPreview、order、display 元数据，在子 agent 启动时由父 agent 合成发送，确保客户端在收到任何 chunk 前即可渲染子 agent 面板。
- chunk：携带 runId 和 body（JSON 编码的 UIMessageChunk），客户端使用与主 agent 聊天响应相同的 applyChunkToParts 原语重建子 agent 消息内容。
- finished：携带 runId 和 summary，在子 agent 正常完成时发送。
- error：携带 runId 和 error 字符串，表示子 agent 执行失败。
- aborted：携带 runId 和 reason，表示子 agent 被显式取消。
- interrupted：携带 runId 和 error，表示因父 agent 崩溃或观察者丢失而无法安全恢复的运行。

每个 agent-tool-event 帧携带父 agent 戳记的单调 sequence 序号。客户端去重键为 (parentToolCallId, runId, sequence)，因为同一 parentToolCallId 下多个并行子 agent 的 sequence 都从 0 开始。replay 标记用于区分来自重放路径（replay: true）和实时广播路径的帧。当重放与实时广播发生竞态时，客户端按 (parentToolCallId, runId, sequence) 去重，保证 UI 不出现重复内容。parentToolCallId 为空的后台执行采用 (null, runId, sequence) 去重。

### 状态管理与崩溃恢复

子 agent 运行是持久化工作（durable work），实时流式传输是观察运行的一种方式。runId 是贯穿父注册表、子运行映射、重放、钻入、取消和清理 API 的稳定 join key。运行的生命周期为：父 agent 在唤醒子 agent 前插入 status='starting' 行；父 agent 以 runId 和输入启动子 agent 运行；子 agent 持久化 runId→requestId→streamId 映射；父 agent 观察子 agent 流并转发事件帧；子 agent 达到终端态后父 agent 标记 completed/error/aborted。

父 agent 崩溃或休眠后的恢复流程：父 agent 重启后遍历 cf_agent_tool_runs 中所有非终端行（starting/running）。若找不到匹配的子 agent 或子运行记录，标记为 interrupted（子 agent 从未启动或已被删除）。若子 agent 报告 completed，从子 agent 获取存储 chunk 进行重放，获取最终输出，标记父行为 completed。若子 agent 报告 error 或 aborted，重放 chunk 并标记对应终端状态。若子 agent 报告 running（V1 限制），重放已存储 chunk 后标记为 interrupted。子 agent 侧映射表是恢复的权威数据源：若父 agent 在插入行后、获取 requestId/streamId 前崩溃，重启后通过 runId 从子 agent 的 inspectAgentToolRun 恢复这些 ID。

关键设计原则：观察与执行分离。观察者流断开（浏览器刷新、WebSocket 重连、父 agent 重启）不自动取消子 agent 运行。终端态一旦确定即为权威——延迟到达的取消请求不将 completed/error 改写为 aborted，延迟到达的协调请求不将 aborted 改写为 interrupted。多个浏览器标签页或重连可同时观察同一 runId——观察是扇出，执行只有一份。子 agent 调度（schedule/scheduleEvery）复用顶层父 agent 的物理 alarm，通过 owner path 路由回调到子 agent 内执行，使长时间运行的子 agent 获得恢复能力。

### 取消机制

取消沿 AbortSignal 自上而下传播：父 agent 的 chat/tool/request 被取消 → runAgentTool 通过 runId 取消子 agent 运行 → 子 agent 中止其 per-turn AbortController → 子 agent 将 signal 传入 saveMessages({ signal }) → 推理循环中止并返回 aborted 结果 → 父 agent 断开该运行的实时观察者流。区分观察取消与执行取消：浏览器断开或父 agent 重启应断开观察，不取消执行；只有父 agent 活动操作发出的显式 AbortSignal 才取消执行。取消是幂等的——子 agent 已达到 completed/error 终端态的，延迟取消不覆写为 aborted。

### 访问控制与钻入（Drill-in）

子 agent 通过现有子 agent 路由原语对外可寻址：useAgent({ agent: parentClass, name: parentName, sub: [{ agent: childClass, name: runId }] }) 生成嵌套 URL /agents/{parent}/sub/{child}/{runId}。访问控制通过父 agent 的 onBeforeSubAgent 中间件钩子实现：请求仅当父 agent 的 cf_agent_tool_runs 注册表中存在匹配 (childClass, runId) 行时才放行到子 agent。应用可自定义策略，但生产默认值阻止任意 runId 通过 URL 推测创建新 Facet。

钻入 URL 始终通过父 agent 的身份体系访问（useAgent({ agent: parent, name: user, sub: ... })），认证和租户信息继承自父 agent。runId 本身不作为 bearer token 使用。钻入观察者可自由读取子 agent 的聊天记录。当框架驱动的 agent tool turn 正在子 agent 上运行时，框架持有该子 agent 实例的排他执行权：对同一 runId 的并发 runAgentTool 调用返回已有检查结果（幂等启动），钻入用户在该期间发送聊天消息应被延迟或拒绝而非与进行中的 turn 交错执行。

### 保存与清理

默认保留子 agent 运行结果以支持完成后的刷新、重放、钻入和审计。清理为显式操作：clearAgentToolRuns() 删除父注册表行及对应的子 agent Facet；clearAgentToolRuns({ olderThan: timestamp }) 按时间窗口清理；clearAgentToolRuns({ status: [...] }) 按状态过滤清理。清理同时删除父注册表行和子 agent Facet，避免留下无法通过重放或钻入访问的孤儿聊天记录。正在运行（starting/running）的运行在清理前先执行取消，再删除注册行和 Facet，避免留下无观察者的孤立 LLM 工作。

### 多场景覆盖：工具调用、确定性流程与后台执行

本方案通过两条 API 覆盖三种调用场景。runAgentTool(Cls, { input, ...options }) 为命令式 API，用于服务端确定性多阶段工作流、通过 @callable 或 HTTP 触发的后台任务、非 LLM 编排场景，以及需要 Promise.allSettled 风格的扇出/扇入代码。调用者可选传入 parentToolCallId 将运行关联到特定工具调用，或留空使其成为无父工具调用的后台执行。agentTool(Cls, options) 为工具工厂，用于 LLM 自动决定调度场景：在 getTools() 中声明带有 inputSchema 和可选 outputSchema 的工具，框架自动将工具调用映射到 runAgentTool，并将子 agent 事件流关联到对应 tool part。

三种场景在客户端呈现上的差异：模型工具调用场景中，agent-tool-event 帧携带 parentToolCallId，客户端通过 useAgentToolEvents 的 getRunsForToolCall(toolCallId) 将子 agent 运行结果渲染在对应 tool-call part 下方。确定性流程调用场景中，若传入了 parentToolCallId 则同上；若未传入，运行出现在 unboundRuns 列表中，应用可直接渲染。后台执行场景中（完全无 parentToolCallId 且无关联 chat 消息），应用从 unboundRuns 拉取运行状态独立展示。所有场景共享同一套事件协议、去重、重放和恢复机制。

parentToolCallId 是可选字段。当 agentTool(...) 由 LLM 调用时，框架自动将 AI SDK 的 toolCallId 传入 runAgentTool 作为 parentToolCallId。当 runAgentTool 由应用代码调用时，调用者可显式传入以关联到特定 tool part，或省略以创建独立的后台运行。子 agent 运行不依赖 parentToolCallId 存在——无论是否传入，运行注册、事件转发、重放和恢复逻辑完全一致。

### 客户端状态管理

客户端通过 useAgentToolEvents hook 订阅主 agent 连接上的 agent-tool-event 帧。hook 内部维护一个纯函数 reducer（applyAgentToolEvent），处理以下逻辑：过滤 type==='agent-tool-event' 的消息帧；按 (parentToolCallId, runId, sequence) 去重（覆盖重放与实时广播竞态）；将 chunk 事件的 JSON body 通过 applyChunkToParts 应用于增量构建子 agent 消息内容（与主 agent 聊天响应使用同一原语）；按 parentToolCallId 分组运行，按 display_order 排序同组兄弟运行；将协议状态映射为 running/completed/error/aborted/interrupted 五种客户端状态；暴露 resetLocalState() 用于清理本地缓存。

### 技术效果

（1）子 agent 作为可调用能力的双重接口：agentTool 适合模型自动决定调用，runAgentTool 适合服务端确定性流程主动调用，且支持无父工具调用的后台执行，三种模式共享同一底层机制。（2）独立执行上下文与并行能力：每个子 agent 作为独立 DO Facet 拥有隔离的 SQLite 和单线程执行环境，多个子 agent 可并行运行而互不干扰，适合长任务和扇出场景。（3）实时可见性与统一会话视图：子 agent 执行进度、输出片段、完成、失败、取消等状态以 agent-tool-event 帧实时回传到主 agent 同一 WebSocket 连接，用户在单一界面中即可观察所有子 agent 活动。

（4）断连恢复与持久化：每个子 agent 的流式 chunk 持久化在其自身 SQLite 中，父 agent 的 cf_agent_tool_runs 注册表记录运行元数据。用户刷新页面或网络断开后重连时，父 agent 协调所有非终端运行状态，从子 agent 获取已存储 chunk 重放并合成终端事件，保证已发生的子 agent 执行过程不丢失。（5）重复请求安全与并发控制：runAgentTool 以 runId 为幂等键防止重复执行；maxConcurrentAgentTools 限制并行子 agent 数量；父 agent 对进行中的子 agent 持有排他执行权。（6）兼容现有体系：子 agent 复用 Think/AIChatAgent 的 ResumableStream、chat turn、工具系统和消息持久化，无需应用开发者重写 agent 框架。

### 风险与待确认问题

（1）延迟 live-tail 重连：V1 不支持在原观察者丢失后重新连接到正在运行的子 agent 的实时流——父 agent 协调后重放已存储 chunk 并标记 interrupted。协议设计（tailAgentToolRun 接口、稳定 runId、chunk sequence）为未来实现预留了扩展空间。（2）父 agent chat 恢复：若 agentTool 调用是父 agent LLM turn 的一部分，恢复子 agent 运行不足以恢复父 turn，除非父 agent chat 恢复机制能基于工具结果继续执行。命令式 runAgentTool 的恢复路径更简单——应用代码可在恢复后直接检查运行结果。（3）结构化输出：V1 将 text summary 作为基线输出，outputSchema 为可选；自动从散文中提取结构化输出暂不支持，应用若需要结构化输出应在子 agent 自身 prompt/tool 契约中明确。（4）子 agent 不拥有独立物理 alarm：调度和 fiber 恢复通过父 agent alarm 路由，依赖父 agent 存活。
