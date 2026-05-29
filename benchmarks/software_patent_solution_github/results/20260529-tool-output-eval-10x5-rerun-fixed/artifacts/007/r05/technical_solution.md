## 技术方案

本方案在现有基于 Durable Object（DO）的 Agent 框架基础上，扩展一种子 agent 编排机制，使主 agent 能够将子 agent 作为可调用能力使用。子 agent 保持独立的 DO 执行上下文（独立 SQLite、独立会话流），主 agent 通过框架管理的运行注册表将子 agent 的流式输出、生命周期事件转发到同一会话视图中。方案同时覆盖模型将子 agent 作为工具调用（agentTool）、服务端确定性流程主动调用（runAgentTool）以及无直接父工具调用的后台子运行三种场景。

### 系统架构与核心组件

系统在现有 subAgent(Cls, name) 基础设施之上引入三层编排结构。第一层：子 agent 基础设施。通过 workerd 运行时的 ctx.facets 机制，父 DO 可创建与自身同机部署的子 DO（facet），每个子 DO 拥有独立的 SQLite 存储和 DO 身份。子 agent 与父 agent 均为 Agent 基类的子类，通过 SubAgentStub<T> 进行类型化 RPC 调用。复合键 ${className}\0${name} 确保不同类可共享同名子 agent。子 agent 的祖先链通过 parentPath 传递，支持任意深度递归嵌套。

第二层：父端运行注册表（cf_agent_tool_runs）。父 DO 的 SQLite 中维护框架管理的运行注册表，包含 run_id、parent_tool_call_id、agent_type、input_preview、status、summary、display_order 等字段。该表记录每次子 agent 运行的元数据：运行由谁发起、属于哪个父工具调用、当前状态及结果摘要。注册表是父 DO 恢复和重放的权威数据源——只有父端知道哪个父工具调用触发了子运行、兄弟运行的渲染顺序、以及重连后应合成哪个生命周期事件。默认仅持久化 inputPreview 而非完整 input，避免将敏感数据复制到编排表中。

第三层：子端运行映射表（cf_agent_tool_child_runs）。每个子 agent 的 SQLite 中维护 runId 到内部 chat turn 标识的映射：run_id → request_id → stream_id。该映射是崩溃恢复的权威数据源——当父 DO 在子 agent 启动后、记录内部标识前崩溃时，父 DO 重连后可通过 runId 向子 agent 查询恢复 requestId 和 streamId。启动操作基于 runId 幂等：若子 agent 已存在该 runId 的运行记录，则返回现有运行状态，不会创建重复的 chat turn。

第四层：子 agent 适配器契约（AgentToolChildAdapter）。框架为 Think 和 AIChatAgent 两种 chat 基类提供内部适配器，无需应用开发者继承新的公共基类。适配器核心接口包括：startAgentToolRun（启动运行，幂等）、cancelAgentToolRun（取消运行，幂等）、inspectAgentToolRun（查询运行状态，恢复时获取内部标识映射）、getAgentToolChunks（获取持久化流 chunks 用于重放）、tailAgentToolRun（可选，未来支持实时 tail 重连）。适配器将 runId 映射到 Think 的 saveMessages({ signal }) 或 AIChatAgent 的等效 chat turn 接口。

### 处理流程

子 agent 运行的完整生命周期分为调度、执行、观察和终止四个阶段。

调度阶段。父 agent 调用 runAgentTool(Cls, { input, parentToolCallId, displayOrder, signal }) 或通过 agentTool(Cls, options) 工厂生成 AI SDK 工具定义。两者均先向 cf_agent_tool_runs 表插入一行 status='starting' 的记录，再通过 subAgent(Cls, runId) 获取或创建子 DO facet。runId 默认由框架生成，也可由调用方指定以实现跨重启的幂等恢复。若 runId 已存在终端态记录，则直接返回已有结果不重新执行；若存在非终端态记录，V1 重放已存储 chunks 并返回 interrupted 状态。

执行与观察阶段。父 agent 通过子适配器调用 startAgentToolRun，子 agent 在其独立 DO 中启动 chat turn（Think 通过 saveMessages，AIChatAgent 通过等效接口），并将 runId → requestId → streamId 映射持久化到 cf_agent_tool_child_runs 表。父 agent 将父端注册表状态更新为 running。子 agent 产生的每个 UIMessageChunk 通过 ResumableStream 持久化到子 agent 自身的 SQLite（表 cf_ai_chat_stream_chunks），父 agent 观察这些 chunks 并包装为 agent-tool-event 帧，通过 WebSocket broadcast 发送给所有连接的客户端。执行与观察分离：断开观察者流不会自动取消运行，只有显式的 AbortSignal 才会触发取消。

终止与取消阶段。子 agent chat turn 完成后，父 agent 将注册表状态更新为 completed/error/aborted。取消链路为：父 agent 的请求/工具调用被取消 → runAgentTool 通过 AbortSignal 显式取消子运行 → 子适配器的 cancelAgentToolRun 触发 Think 的 per-turn AbortController → saveMessages({ signal }) 终止推理循环。已到达终端态（completed/error）的运行，迟到取消请求不会覆写为 aborted。清理历史时，先取消 running 态的运行，再删除注册表行和子 facet，最后清除 chat 历史——该顺序防止孤儿 LLM 工作继续运行而无人观察。

### 恢复机制

父 DO 可能因 eviction、崩溃或正常休眠而丢失内存态。恢复机制基于持久化的注册表和子 agent 侧映射实现状态重建。

父 DO 重启后，遍历 cf_agent_tool_runs 表中所有非终端态（starting/running）的行进行调和。调和逻辑：若无匹配子 agent 或子运行记录，子 agent 从未启动或已被删除，标记为 interrupted；若子 agent 报告 completed，重放存储的 chunks 并标记 completed；若子 agent 报告 error 或 aborted，重放 chunks 并标记对应终端态；若子 agent 报告 running，V1 重放已存储 chunks 并标记 interrupted，因为 V1 不支持实时 tail 重连。子端 cf_agent_tool_child_runs 映射是调和过程中的权威数据源——父 DO 在存储生成的 requestId/streamId 前崩溃时，可通过 runId 向子 agent 查询恢复。

客户端重连恢复：父 DO 的 onConnect 处理中遍历注册表中所有运行行，为每个运行合成 started 事件，然后从子 agent 获取存储的 chat chunks 通过 getAgentToolChunks(runId) 重放，最后根据终端状态合成 finished/error/aborted/interrupted 事件。重放帧标记 replay:true，但客户端以相同方式渲染——刷新页面后子 agent 时间线表现为短暂停顿后追上。去重键为 (parentToolCallId, runId, sequence)，防止重放帧与正在进行的实时 broadcast 帧因竞态而重复渲染。parentToolCallId 为 null 时（无父工具调用的后台运行），去重键使用 (null, runId, sequence)。

### 事件协议与实时展示

子 agent 的输出通过六种事件类型传递到客户端：started（运行已创建，携带 runId、agentType、inputPreview、order 和 display 元数据）、chunk（JSON 编码的 UIMessageChunk，由子 agent 的 ResumableStream 产生）、finished（正常完成，携带 summary）、error（运行异常）、aborted（显式取消）、interrupted（父端调和时发现无法安全恢复）。每个 agent-tool-event 帧包含 type、parentToolCallId（可选）、sequence 和 replay 标记。sequence 按每个 runId 单调递增，chunk 事件的 body 为不透明 JSON 字符串，客户端通过 applyChunkToParts 原语重建 UIMessage.parts，复用与主 chat 流相同的渲染逻辑。

客户端通过 useAgentToolEvents hook 订阅父 WebSocket 连接中的 agent-tool-event 帧。hook 内部维护纯函数 reducer（applyAgentToolEvent），负责：过滤 agent-tool-event 消息、按 (parentToolCallId, runId, sequence) 去重、通过 applyChunkToParts 累积 chunk 为 UIMessage.parts、按 parentToolCallId 分组运行、按 order 排序兄弟运行、映射协议状态为 running/completed/error/aborted/interrupted。输出 runsByToolCallId（按父工具调用分组）、unboundRuns（无 parentToolCallId 的后台运行）和 subAgent 路由信息（用于 drill-in URL 构造）。同时暴露 resetLocalState 供清理 chat 历史时同步重置。

### Drill-in 与访问控制

子 agent 通过现有外部寻址机制可被客户端直接访问，URL 形状为 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。客户端通过 useAgent({ agent: parentClass, name: parentName, sub: [{ agent: childClass, name: runId }] }) 构造 drill-in 连接。框架在父 DO 上安装 onBeforeSubAgent 守卫：仅当父端 cf_agent_tool_runs 表中存在匹配 (agentType, runId) 的运行记录时，才将请求转发到子 agent。这防止通过猜测 runId 的 URL 创建新 facet。runId 本身不作为 bearer token——drill-in 始终通过父 agent 的已有身份和认证链路，租户隔离由父 agent 继承。在框架驱动的 agent tool turn 运行期间，框架持有对子 agent 实例的排他声明：并发的 runAgentTool 对同一 runId 返回已有检查结果（幂等启动），drill-in 用户在框架驱动运行期间发送 chat 消息应被拒绝或排队，不破坏进行中的 turn。

### 清理与保留策略

子 agent 运行完成后默认保留（子 DO facet 和父端注册表行均保留），以支持刷新重放、drill-in 和事后检查。清理通过显式 API clearAgentToolRuns(option?) 执行，可选参数包括 olderThan（时间阈值）、status（按状态筛选）。该 API 同时删除父端注册表行和对应的子 agent facet，防止遗留无法访问的孤儿子 agent。不允许仅删除注册表行而保留子 facet——若未来需要，该行为需显式标注为孤儿化操作。V1 不包含自动 TTL 或基于计数的 GC，应用可通过子 agent 调度机制在自身生命周期代码中调用清理。

### 并发控制与幂等设计

父端 maxConcurrentAgentTools 限制同时运行的子 agent 数量，超限时快速失败并产生 error 事件。启动操作基于 runId 幂等：同一 runId 的重复调用不会创建重复 chat turn。cancelAgentToolRun 和状态写入均为幂等：终端态不被迟到取消覆写，迟到调和也不覆写已有终端态。

### 三种调度模式

方案覆盖三种子 agent 调度模式。模式一：模型作为工具调用（agentTool）。父 agent 的 getTools 返回 agentTool(Cls, { description, inputSchema, outputSchema? }) 生成的工具定义，父 LLM 决定何时调用。生成的工具接收 AI SDK 的 toolCallId 和 abortSignal，调用 runAgentTool 并将 parentToolCallId 设为 toolCallId。子 agent 事件携带该 parentToolCallId，客户端将其绑定到对应工具调用组件下。模式二：服务端确定性流程（runAgentTool）。父 agent 的 @callable 方法、HTTP 处理器或非 chat 逻辑直接调用 runAgentTool(Cls, { input, signal })，无需经过 LLM 工具选择。子 agent 事件不带 parentToolCallId（或设为 null），客户端通过 unboundRuns 列表独立渲染。模式三：无父工具调用的后台运行。与模式二机制相同，适用于后台报告、定时触发的分析任务等场景。

### 与现有体系兼容性

本方案建立在现有 subAgent 基础设施之上，不要求重写 Agent 框架。任何 Agent 子类均可作为子 agent 使用，无需继承特殊基类。Chat 能力通过内部适配器契约接入：Think 和 AIChatAgent 子类自动符合 ChatCapableAgentClass 结构约束。适配器在框架内部实现，应用开发者无需了解适配器细节。agentTool 和 runAgentTool 的 API 表面与现有 getTools、@callable、onConnect 模式兼容。子 agent 调度复用了现有 Agent 调度机制：子 agent facet 虽无物理 alarm 槽位，但可通过顶层父 DO 的 alarm 注册逻辑回调，支持子 agent 内的恢复续执行。

### 技术效果

第一，子 agent 保持独立 DO 执行上下文，拥有独立 SQLite，适合长任务和并行任务，各子 agent 的 chat 历史、工具调用、模型交互互不干扰。第二，执行与观察分离：子 agent 的运行是持久化工作，实时流式输出是一种观察方式而非运行本身。浏览器断开、父 DO 重启或重放连接失败仅断开观察，不取消运行。第三，基于 runId 的全链路关联：同一 runId 贯通父端注册表、子端映射、重放、drill-in、取消和清理，保证各环节的数据一致性。第四，崩溃后可恢复：父 DO 重启后通过注册表调和恢复所有运行状态，客户端重连后通过重放追上已发生的执行过程。第五，与现有 chat/Think agent 体系兼容：任何 Think 或 AIChatAgent 子类自动成为可调度的子 agent，无需应用开发者重写框架。

### 风险与待确认点

以下为当前方案的风险或待确认点。第一，V1 不支持实时 tail 重连——若父 DO 在子 agent 运行期间崩溃，恢复后子 agent 仍在运行但父端无实时流，V1 将其标记为 interrupted 并仅重放已存储 chunks。完整的 detached 观察者状态和 tailAgentToolRun 实时重连需后续版本实现。第二，父 chat 恢复的边界：若 agentTool 调用是父 LLM turn 的一部分，父 DO 在工具调用期间崩溃，恢复子 agent 的 transcript 但父端工具调用可能仍标记为 interrupted，因为恢复进行中的 LLM turn 需父 chat 恢复机制同时支持从工具结果续执行。runAgentTool 调用的恢复路径更清晰。第三，AIChatAgent 适配器尚未实现——当前原型基于 Think，AIChatAgent 支持是后续适配器里程碑。第四，子 agent 的嵌套调度（子 agent 再调用孙 agent）在基础设施层面支持（facet 可嵌套），但嵌套运行的观察仅向直接父端透传、不向上桥接，链式追踪依赖 runId 作为 join 键。
