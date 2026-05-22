## 技术方案

本技术方案基于 Cloudflare Workers Durable Object（DO）架构，在已有 Agent 基类和 chat/Think agent 体系之上，构建一套子 agent 作为可调用能力的协作系统。主 agent 在处理一次请求时，可按需调用一个或多个专门的子 agent 完成研究、规划、比较、总结等任务。子 agent 保持独立执行上下文，其执行进度、输出片段和生命周期事件以流式事件形式回到主 agent 所在同一会话视图中，支持刷新或断网后恢复。

### 技术问题

现有 agent 框架中，主 agent 调用子 agent 的能力通过 subAgent(cls, name) 实现，子 agent 由 ctx.facets 创建为同机 Durable Object，通过类型化 RPC 桩调用子 agent 上的 @callable 方法。该机制虽然提供了子 agent 的存储隔离和 RPC 调用，但存在以下不足：（1）子 agent 的执行过程对客户端不透明，用户只能看到主 agent 汇总后的最终文本；（2）子 agent 的流式输出、生命周期事件（启动、完成、失败、取消）没有标准化协议传递到主 agent 的会话视图；（3）缺少跨断线/刷新后的子 agent 执行过程恢复机制；（4）缺少将子 agent 包装为模型可调用工具、确定性流程可主动调用、以及无父工具调用关联的后台执行三种模式的统一抽象；（5）缺少重复请求防护、并发控制、访问控制、清理保留和取消传播的系统级支持。

### 系统架构

系统在已有 Agent/Think 基类之上增加 Agent Tool 编排层，由父 agent 的 DO 存储维护运行注册表（cf_agent_tool_runs），子 agent 的 DO 存储维护运行映射表（cf_agent_tool_child_runs）。整体数据流为：客户端通过 WebSocket 连接到父 agent；父 agent 在收到请求时，通过 runAgentTool（命令式）或 agentTool（模型工具工厂）派发子 agent 运行；子 agent 在执行过程中将聊天响应块通过父 agent 的广播通道以 agent-tool-event 帧推送到客户端；运行生命周期事件（started、chunk、finished、error、aborted、interrupted）经父 agent 统一发送。sub-agent routing 机制使子 agent 的外部 URL 可达，支持 drill-in 查看。

### 核心数据模型

（1）Agent Tool 运行注册表（cf_agent_tool_runs）：存储在父 agent 的 SQLite 中，每条记录包含 run_id、parent_tool_call_id（可选，关联父工具调用）、agent_type、input_preview（输入摘要，非原始输入）、status（starting/running/completed/error/aborted/interrupted）、summary、error_message、display_order、started_at、completed_at 等字段。该表是父 agent 恢复重放、排序和访问控制的权威来源。（2）子 agent 运行映射表（cf_agent_tool_child_runs）：存储在子 agent 的 SQLite 中，维护 run_id → request_id → stream_id 的映射关系。run_id 是编排层的唯一标识，request_id 是聊天轮次标识，stream_id 是可恢复流的持久化标识。该映射支持父 agent 崩溃后从子 agent 侧恢复已生成的 ID。（3）子 agent 自身：普通 chat-capable agent（Think 或 AIChatAgent 子类），拥有独立的 SQLite、消息历史、可恢复流和模型调用能力，不继承特殊基类。

### 事件协议与流式传输

定义 AgentToolEvent 协议消息类型，父 agent 通过 WebSocket 向客户端发送 agent-tool-event 帧，包含以下事件类型：

- started：包含 runId、agentType、inputPreview、order、display 字段，表示子 agent 启动；
- chunk：包含 runId、body 字段，body 为 JSON 编码的 UIMessageChunk，由子 agent 的流式输出逐块产生；
- finished：包含 runId、summary 字段，表示正常完成；
- error：包含 runId、error 字段，表示执行失败；
- aborted：包含 runId、reason 字段，表示显式取消；
- interrupted：包含 runId、error 字段，表示父 agent 恢复后无法继续观察的终止。

每条 agent-tool-event 消息包含 type: "agent-tool-event"、parentToolCallId（可选）、sequence（每 run 单调递增的序列号）和 replay 标志。客户端以 (parentToolCallId, runId, sequence) 为去重键，区分实时事件和重放事件，避免并行子 agent 的 sequence 冲突。chunk.body 保持与现有 UIMessageChunk 的兼容，客户端可使用已有的 applyChunkToParts 原语重建子 agent 消息部件。

### 三种调用模式

系统提供三种子 agent 调用模式，共享同一底层 runAgentTool 机制：

模式一：模型工具调用（agentTool）。通过 agentTool(Cls, { description, inputSchema, outputSchema? }) 工厂函数将子 agent 包装为 AI SDK 工具。父 agent 的 getTools() 返回该工具后，父 LLM 可根据工具描述自动决定调用。工具执行时，父 agent 获取模型的 toolCallId 和 abortSignal，调用 runAgentTool 并传入 parentToolCallId 关联。工具结果根据终端状态返回：completed 返回 output/summary；error/aborted/interrupted 返回结构化失败信息，确保 LLM 不会对静默空结果产生幻觉。

模式二：命令式调用（runAgentTool）。通过 this.runAgentTool(Cls, { input, runId?, parentToolCallId?, signal? }) 在服务端确定性流程中主动派发子 agent，返回 RunAgentToolResult { runId, status, output?, summary?, error? }。适用于多阶段报告生成、@callable 触发的后台分析、Promise.allSettled 并行扇出等场景。parentToolCallId 可选，不传则为无父工具调用的独立运行。

模式三：后台运行。当 runAgentTool 调用未传入 parentToolCallId 时，子 agent 作为独立的持久化运行存在。客户端通过 agentToolEventState.unboundRuns 获取此类运行列表，独立于聊天消息渲染。适用于从 HTTP handler 或定时任务触发的长时间分析任务。

### 状态管理与恢复机制

运行状态管理遵循以下生命周期：（1）父 agent 在启动子 agent 前，先在 cf_agent_tool_runs 中插入一行 status="starting"；（2）子 agent 启动聊天轮次后，父 agent 将状态更新为 "running"；（3）子 agent 正常终止后更新为 "completed"；（4）子 agent 抛出异常或流错误时更新为 "error"；（5）父 agent 的 AbortSignal 触发取消时更新为 "aborted"；（6）父 agent DO 崩溃或 eviction 后重启，对非终端行进行 reconciled：若子 agent 已正常完成则重放存储块并标记 completed；若子 agent 仍运行中但无法 live-tail 重连，则标记为 "interrupted"。终端状态一旦写入即为权威，后续取消或重 conciliation 不得覆写。

恢复机制的核心是 runId 作为稳定连接键。父 agent 重启时遍历所有 starting/running 行，通过子 agent 的 inspectAgentToolRun(runId) 查询实际状态。子 agent 的 startAgentToolRun 以 runId 为幂等键：若已存在该 runId 的记录，直接返回现有状态而不创建重复轮次。可恢复流（ResumableStream）将子 agent 的流式块持久化在子 agent 的 SQLite 中，父 agent 通过 getAgentToolChunks(runId, { afterSequence }) 按需重放。

客户端重连去重机制：客户端维护已收到的 sequence 集合，键为 (parentToolCallId, runId, sequence)。重连后父 agent 发送带有 replay: true 标志的事件帧，客户端据此区分首次接收和重放，避免 UI 中出现重复内容。父 agent 对同一 run 的 live 事件和重放事件使用相同的 sequence 编号空间，确保去重逻辑统一。

### 取消传播

取消传播路径：父 agent 的 AbortSignal（来自客户端取消请求、模型工具调用中止或父请求超时）→ runAgentTool 调用 cancelAgentToolRun(runId) → 子 agent 的轮次级 AbortController 触发 → saveMessages({ signal }) 中止推理循环 → 子 agent 报告 aborted 结果 → 父 agent 更新注册表为 aborted 并广播 aborted 事件。关键设计：取消观察者流不等同于取消运行。浏览器断开、父 agent 重启或重放连接失败只应分离观察者，不应取消子 agent 执行。仅显式的 abort 信号才触发运行取消。

### 重复请求与并发控制

重复请求防护：runAgentTool 以 runId 为幂等键。若调用方传入已存在的 runId：终端运行直接返回已有 RunAgentToolResult；非终端运行不创建重复工作。agentTool 由框架自动生成唯一 runId，每次 LLM 工具调用都是新运行。并发控制：父 agent 提供 maxConcurrentAgentTools 参数，限制同时运行的子 agent 数量；超出限制时快速失败并发送 error 事件。子 agent 实例级排他：框架驱动的 agent tool 运行在子 agent 上持有一个排他声明，并发 runAgentTool 调用同一 runId 返回已有 inspection 而不启动第二个轮次。drill-in 用户在框架驱动运行期间发送聊天消息时，应被延迟或返回明确错误。

### 访问控制与 Drill-in

子 agent 通过已有 sub-agent routing 机制保持外部可寻址：客户端可使用 useAgent({ agent: parentAgent, name: userId, sub: [{ agent: childAgent, name: runId }] }) 构造 drill-in URL，路径形如 /agents/{parent-class}/{parent-name}/sub/{child-class}/{runId}。访问控制：父 agent 的 onBeforeSubAgent 钩子在路由到子 agent 之前执行，可验证 cf_agent_tool_runs 中是否存在匹配的 runId 行，拒绝任意 runId 猜测。框架默认安装严格的注册表门控：仅当父 agent 的 agent tool 运行注册表中存在对应行时，才放行外部请求到达子 agent。runId 本身不作为持有令牌（bearer token），drill-in 必须经过父 agent 的身份验证和租户上下文。

### 保留与清理

运行完成后默认保留子 agent 的 DO 存储和父 agent 的注册表行，以支持事后刷新重放、drill-in 和调试。清理 API：this.clearAgentToolRuns() 支持按时间（olderThan）和状态过滤批量删除。删除操作同时移除父 agent 注册表行和对应的子 agent facet（调用 deleteSubAgent），避免孤立子 agent 残留在存储中。若清理时运行状态为 starting 或 running，先取消运行再删除，防止 LLM 调用无人观察地继续消耗资源。应用程序可在 clearHistory 等生命周期钩子中调用 clearAgentToolRuns 实现联动清理。

### 兼容性设计

方案完全兼容现有 chat/Think agent 体系：（1）子 agent 为普通 Think 或 AIChatAgent 子类，不要求继承特殊基类；通过内部 ChildAdapter 接口适配不同 chat base，首先支持 Think，AIChatAgent 通过后续适配器跟进。（2）父 agent 的 agentTool 作为 getTools() 返回的工具集的一部分，与现有工具（workspace、execute、browser、MCP 等）无冲突。（3）runAgentTool 可在任何 Agent 子类中调用，不限于 chat agent。（4）agent-tool-event 帧与现有 cf_agent_chat_* 协议帧在同一 WebSocket 连接上共存，客户端通过 type 字段区分。（5）子 agent 的 sub-agent routing URL 与现有路由规则无冲突，onBeforeSubAgent 钩子保持默认放行行为，仅在使用 agent tool 功能的父 agent 上需要安装注册表门控。

### 关键处理流程

流程一（模型工具调用）：客户端发送聊天消息 → 父 Think agent 的 onChatMessage 调用 streamText → LLM 决策调用 agentTool → agentTool.execute 调用 runAgentTool → 父 agent 在 cf_agent_tool_runs 插入 starting 行 → 通过 subAgent(Cls, runId) 获取子 agent → 子 agent 的 startAgentToolRun 在 cf_agent_tool_child_runs 记录映射 → 子 agent saveMessages 执行推理 → 子 agent 流式块经 ResumableStream 存储 → 父 agent 观察流并广播 agent-tool-event(started/chunk/finished) → 父 agent 更新注册表为 completed → agentTool 返回 summary/output 给父 LLM → 父 LLM 继续推理或输出最终回复。

流程二（恢复）：客户端重连 WebSocket → 父 agent 检测重连并发送 cf_agent_stream_resuming → 客户端发送 cf_agent_stream_resume_ack → 父 agent 遍历 cf_agent_tool_runs 中非终端行 → 对每个 runId 调用子 agent 的 inspectAgentToolRun → 若 completed 则调用 getAgentToolChunks 重放块并更新注册表 → 若仍 running 则重放已有块并标记 interrupted → 父 agent 以 replay:true 标志发送历史 agent-tool-event 帧 → 客户端去重后重建 UI 状态。

流程三（取消）：客户端发送 cf_agent_chat_request_cancel → 父 agent 的 AbortController 触发 → runAgentTool 的 signal 回调 cancelAgentToolRun(runId) → 子 agent 轮次 AbortController 中止 → saveMessages({ signal }) 中断推理 → 父 agent 更新注册表为 aborted → 广播 agent-tool-event(aborted)。

### 技术效果

（1）透明可观察：子 agent 执行过程以流式事件实时推送到同一会话视图，用户可看到每个子 agent 的思考过程、工具调用和输出，而非仅最终汇总文本。（2）持久可恢复：基于 Durable Object SQLite 的注册表和可恢复流机制，页面刷新或断网后能完整恢复已发生的子 agent 执行过程和结果。（3）多模式统一：同一套 runAgentTool 底层机制同时支持模型自动调用、服务端确定性调用和后台独立运行，避免三种场景各自实现。（4）隔离与排他：每个子 agent 拥有独立 SQLite 和执行上下文，并行子 agent 互不干扰；同一子 agent 实例的排他声明防止并发写入冲突。（5）结构化生命周期：六种运行状态（starting/running/completed/error/aborted/interrupted）覆盖正常终止、异常、取消和恢复受限场景。（6）兼容演进：无需重写现有 agent 框架，子 agent 保持为普通 Think/AIChatAgent 子类。

### 风险与待确认问题

（1）late live-tail reattach：V1 实现中，若父 agent 在子 agent 运行期间崩溃，重启后无法重新附着到子 agent 的实时流；当前方案重放已有块并标记 interrupted，需后续实现 tailAgentToolRun 和 detached 观察者状态。（2）跨机子 agent：当前 facets 限于同机部署，子 agent 与父 agent 在同一 workerd 隔离区内；跨机子 agent 需标准 DO 桩调用，失败模式和延迟特征显著不同。（3）父 LLM 轮次恢复：若父 LLM 在 agentTool 调用期间崩溃，恢复子 agent 转录不等于恢复父 LLM 轮次；需父 chat recovery 机制跟进。（4）AIChatAgent 适配器：首版以 Think 为子 agent 基类，AIChatAgent 父/子模式需后续适配器验证流式、恢复和取消契约一致性。（5）资源限制：无内置子 agent 数量、嵌套深度和总存储上限；workerd 平台限制需在实际使用中暴露和文档化。（6）成本可见性：多子 agent 并行可显著增加 LLM 调用量；需生命周期钩子辅助计量但不强制内置计费集成。
