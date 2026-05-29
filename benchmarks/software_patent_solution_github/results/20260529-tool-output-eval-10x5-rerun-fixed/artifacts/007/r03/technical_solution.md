## 技术方案

本技术方案在基于 Durable Object 的 agent 框架之上，构建了一套 agent 工具编排系统（Agent Tool Orchestration），使主 agent 能够将其他 chat-capable agent 作为可调用的工具来使用，同时将这些子 agent 的完整执行过程以流式事件的方式回传到主 agent 所在的会话视图中。方案核心包括：（1）将子 agent 封装为可被模型自动调用或被服务端确定性流程主动调用的能力单元；（2）父 agent 侧维护 agent 工具运行注册表，记录每次子 agent 调用的生命周期与关联关系；（3）子 agent 侧维护运行映射表，作为崩溃恢复的权威数据源；（4）通过父 agent 的 WebSocket 连接复用已有的流式通道，将子 agent 的对话片段以多路复用的 agent 工具事件协议实时推送至客户端；（5）基于注册表实现断开重连后的完整回放与去重。

### 整体架构

系统的核心运行单元是 Durable Object（DO），每个 agent 实例为一个 DO，拥有独立的 SQLite 持久存储和独立的计算上下文。父 agent 和子 agent 之间通过 DO Facet 机制实现共址部署——子 agent 作为父 agent 的 facet 子对象运行在同一 Worker 进程中，但拥有隔离的 SQLite 数据库。

方案复用已有的 subAgent(Cls, name) 原语创建子 agent facet。任何继承自 Think 或 AIChatAgent 的 chat-capable agent 均可作为 agent 工具被父 agent 调度，无需继承特殊的基类。子 agent 拥有自己的模型、系统提示词、工具集、消息历史和可恢复流（resumable stream），是完整的独立对话代理。

客户端始终连接父 agent 的 WebSocket。子 agent 的执行进度、输出片段、完成、失败、取消等状态以 agent 工具事件（agent-tool-event）的形式，通过父 agent 的 WebSocket 通道进行多路复用传输。客户端按 parentToolCallId + runId 对事件进行分组和渲染。

### 父侧 Agent 工具运行注册表

父 agent 在其自身的 SQLite 中维护一个框架管理的 agent 工具运行注册表（cf_agent_tool_runs），用于记录每次子 agent 调用的全生命周期信息。该表作为父 agent 侧的权威数据源，支撑事件回放、访问控制和清理操作。

注册表的每条记录以 runId 为主键，包含以下关键字段：parent_tool_call_id（关联的父工具调用标识，允许为 null 以支持无父工具调用的后台执行）、agent_type（子 agent 的类名）、input_preview（输入摘要，默认不持久化完整输入以保护敏感数据）、status（运行状态）、summary / error_message（终态摘要或错误信息）、display_order（同一次父工具调用中多个子 agent 的显示顺序）、started_at 和 completed_at（时间戳）。

运行状态包括：starting（行已插入但子 agent 尚未确认运行）、running（子 agent 已启动对话轮次）、completed（正常完成）、error（子 agent 抛出异常或流错误）、aborted（被显式取消）、interrupted（父 agent 重启后无法安全恢复观察而标记的父侧终态）。其中 completed、error、aborted、interrupted 为终态，一旦到达终态即不可被后续取消或重写覆盖。interrupted 是父侧专属状态——子 agent 不声明自身为 interrupted，只有失去观察者的父 agent 在恢复时记录此状态。

应用开发者通过框架暴露的 runAgentTool 和 agentTool 接口触发子 agent 调用，框架负责自动插入、更新和查询注册表行，应用无需直接操作该表。应用可控制保留策略、访问控制决策和显示元数据。

### 子侧运行映射

子 agent 自身维护一个子侧运行映射表（cf_agent_tool_child_runs），将编排层的 runId 映射到对话引擎内部的 requestId 和 streamId。该映射表是崩溃恢复的权威数据源。

关键设计包括：（1）runId 是公开的编排标识，requestId 是对话轮次标识，streamId 是可恢复流的持久化标识，三者解耦避免了"一次 agent 工具运行等于一次对话轮次"的固化假设，为未来多轮次 agent 工具运行预留空间；（2）启动操作按 runId 幂等——如果子 agent 已存在该 runId 的记录，返回已有运行状态而不创建重复的对话轮次；（3）子侧映射必须在父 agent 可恢复的时机点之前持久化：如果父 agent 在子 agent 启动后但尚未获取到 requestId/streamId 时崩溃，父 agent 重启后可通过 runId 从子 agent 处恢复这些内部标识。

### Agent 工具事件协议

父 agent 通过 WebSocket 向客户端发送 agent 工具事件消息（agent-tool-event message），每条消息包含以下字段：

- type：固定为 "agent-tool-event"，供客户端过滤。
- parentToolCallId：关联的父工具调用标识，可为 null（用于无直接父工具调用的命令式后台执行）。
- sequence：per-run 单调递增序号，由父 agent 在发出每条事件时递增。客户端以 (parentToolCallId, runId, sequence) 三元组作为去重键。
- replay：布尔标记，为 true 时表示该事件是重连回放事件——客户端按相同逻辑渲染，仅用于区分来源。
- event：具体事件体，包含 runId 及事件类型特定字段。

事件体支持六种类型：（1）started——包含 runId、agentType、inputPreview、order、display 元数据等，在子 agent 创建并插入注册表行后立即合成，使 UI 在收到任何对话片段前即可渲染面板；（2）chunk——携带 JSON 编码的 UIMessageChunk 体（body），与父 agent 自身的对话响应使用相同的 AI SDK 消息片段词汇表，客户端通过 applyChunkToParts 原语重建子 agent 的消息部件；（3）finished——包含 runId 和 summary，表示子 agent 正常完成；（4）error——包含 runId 和 error 字符串，表示子 agent 执行失败；（5）aborted——包含 runId 和可选 reason，表示运行被显式取消；（6）interrupted——父侧特有，表示运行因父 agent 重启而中断观察。

terminal 事件相互独立：error 表示子 agent 自身失败，aborted 表示显式取消，interrupted 表示观察丢失。三者分别对应不同的 UI 渲染策略。

### 流式桥接与事件转发

子 agent 执行对话轮次时，通过 DO RPC 返回一个 ReadableStream<Uint8Array> 给父 agent。该流以 NDJSON（换行分隔 JSON）格式传输帧，每帧为 { sequence, body }，其中 body 是 JSON 编码的 UIMessageChunk。

子 agent 侧的关键机制：（1）broadcast 拦截——子 agent 重写 broadcast 方法，在对话轮次进行中将 Think 引擎产生的 MSG_CHAT_RESPONSE 类型帧旁路写入 RPC 流的 active forwarder 回调中；（2）并发保护——子 agent 实例上维护一个同步的 _runInProgress 标志，防止同一实例被并发调用导致 forwarder 状态污染；（3）取消传播——子 agent 维护 per-turn 的 AbortController，当父 agent 取消 RPC reader 时，workerd RPC 桥接层触发流的 cancel 回调，该回调 abort per-turn 的 AbortController，其 signal 已通过 saveMessages({ signal }) 线程化到 Think 推理循环中，实现同步终止推理；（4）流标识捕获——每次 turn 完成后捕获 _resumableStream 分配的 stream_id，供父 agent 存储到注册表行中，确保未来回放时读取的是该轮次的对话片段而非后续 drill-in 产生的片段。

父 agent 侧读取 RPC 流时，按行解析 NDJSON 帧，从帧中提取 body 字段，重新包装为 agent-tool-event 的 chunk 事件并通过 broadcast 发送到所有连接的客户端。父 agent 维护自己的 per-run 序号，将子 agent 的内部序号替换为统一的包含 started/finished 生命周期事件在内的单调序号。

### 恢复与重连机制

系统区分"执行"与"观察"两个关注点。agent 工具运行是持久工作（durable work），实时流式传输只是观察方式之一。丧失观察者不应自动取消执行。

父 agent 启动（或重启）时的恢复流程如下：（1）在 onStart 中将所有 running 状态的注册表行标记为 interrupted，并设置 completed_at，因为转发循环已随父 agent 崩溃而消失；（2）在 onConnect 中遍历所有注册表行，为每个行合成 started 事件，获取子 agent 的存储对话片段并通过 chunk 事件回放，根据行的终态合成对应的 finished/error/aborted/interrupted 事件。回放事件标记 replay: true。客户端使用 (parentToolCallId, runId, sequence) 去重，使得回放与实时事件无缝衔接。

父 agent 崩溃恢复的边界情况处理：（1）父 agent 可能在插入注册表行后、子 agent 启动前崩溃——恢复时"无匹配子运行"将标记为 interrupted，默认不重新运行以避免不确定的副作用；（2）父 agent 可能在子 agent 启动后、尚未持久化 requestId/streamId 前崩溃——子侧映射表作为权威数据源，父 agent 通过 runId 向子 agent 查询恢复这些内部标识；（3）多个浏览器标签页或重连可能同时观察同一运行——观察是扇出模式，每个 runId 只有一次执行；（4）显式取消是幂等的——如果子 agent 已到达终态，后续取消操作不覆写已完成的终态。

V1 实现不支持延迟实时重连（late live-tail reattach）：如果原始观察者消失且子 agent 仍在运行，父 agent 恢复时回放已存储的片段后标记 interrupted。runId、sequence 和 tailAgentToolRun 接口设计保持向前兼容，未来可在不改变公开 API 前提下实现实时重连。

### 取消传播机制

取消操作沿 AbortSignal 链从父 agent 向子 agent 传播，形成完整闭环：（1）父 agent 的对话轮次或请求被取消；（2）runAgentTool 通过 runId 显式取消子 agent 运行；（3）子 agent 的 per-turn AbortController 被 abort；（4）该 signal 被传递到 saveMessages({ signal }) 内部；（5）Think 推理循环同步终止；（6）父 agent 分离该运行的实时观察者流。

关键设计原则：（1）取消观察者流不自动取消运行——浏览器断开、父 agent 重启或回放连接失败仅分离观察，只有来自父 agent 活跃操作的显式 AbortSignal 才触发执行取消；（2）取消是幂等的——在子 agent 已到达 completed/error 终态后，后续取消操作不将终态覆写为 aborted；（3）清理历史时先取消再删除——在运行处于 starting/running 状态时，先执行取消操作终止推理，再删除注册表行和子 agent facet，避免留下无观察者的孤立推理工作。

### 双重 API 接口

方案提供两种对等的 API 形态，底层共享同一编排机制：

（1）agentTool(Cls, options)——工具工厂函数，返回 AI SDK Tool 条目，插入父 agent 的 getTools() 返回值中。当父 agent 的 LLM 决定调用该工具时，框架自动调用 runAgentTool，传入 model 分配的 toolCallId 和 AbortSignal。适用于模型自主决定调度子 agent 的场景。inputSchema 为必填项，供模型进行工具选择和参数验证；outputSchema 为可选，若提供则框架验证子 agent 返回的结构化输出，若未提供则以子 agent 的文本摘要（summary）作为返回结果。

（2）runAgentTool(Cls, options)——命令式 API，接收子 agent 类、输入参数、可选的 runId、parentToolCallId、displayOrder 和 AbortSignal。返回 RunAgentToolResult，包含 runId、status、output/summary、error 等字段。适用于：服务端确定性多阶段工作流、通过 @callable 或 HTTP 触发的非对话任务、扩展 Agent（非 Think）基类的父 agent、以及使用 Promise.allSettled 的并行扇出/扇入代码。

agentTool 内部调用 runAgentTool，两者返回统一的 tool result 给父 LLM：completed 返回结构化 output 或文本 summary；error 返回 { ok: false, error } 结构；aborted 返回取消提示；interrupted 返回中断提示。LLM 不会收到非 completed 运行的静默空结果。

两种 API 均支持 runId 幂等：传入已存在的 runId 时，终态运行直接返回已有结果而不重新执行；非终态运行不启动重复工作（V1 返回 interrupted）。这使得 runAgentTool 可安全用于重试路径、定时告警和重连恢复场景。

### Drill-in 与访问控制

子 agent 通过已有的子代理路由原语对外可寻址：URL 格式为 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。客户端可通过 useAgent({ agent: parent, name: userId, sub: [{ agent: childClass, name: runId }] }) 直接连接到子 agent，对其进行独立的对话交互（drill-in）。

访问控制通过父 agent 的 onBeforeSubAgent 中间件钩子实现。框架默认安装严格的注册表门控：仅当父 agent 的 cf_agent_tool_runs 表中存在匹配的 (agentType, runId) 行时，才允许外部 HTTP/WebSocket 请求路由到子 agent。防止通过 URL 猜测任意 runId 来创建新的 facet 实例。内部 subAgent(...) 调用绕过此钩子（与 getAgentByName 绕过 onBeforeConnect 的模式一致）。

认证和租户隔离继承自父 agent 的身份——drill-in URL 始终通过父 agent 的现有身份（userID）到达，框架不鼓励将 runId 作为 bearer token 分发。同时，在框架驱动的 agent 工具运行期间，子 agent 实例持有排他声明，并发的 runAgentTool 调用按 runId 返回已有检查结果，drill-in 用户在框架驱动运行期间发送对话消息应被延迟或拒绝，避免与进行中的推理交错。

### 保留与清理策略

agent 工具运行默认保留而非自动删除，因为完成时刻正是事后检查（回放、drill-in、调试、审计）的起点。提供显式清理 API：clearAgentToolRuns() 无参数清理所有运行；支持按 olderThan（时间阈值）或 status（状态过滤）条件清理。

清理操作同时删除父侧注册表行和对应的子 agent facet（通过 deleteSubAgent）。仅删除注册表行而保留子 agent facet 将造成无法通过回放或 drill-in 访问的孤立对话片段，框架不默认允许此行为。应用开发者通过该 API 与已有的 clearHistory 等生命周期方法组合，实现完整的会话清理。

### 并发执行与并发控制

并行扇出通过 Promise.allSettled 实现：父 agent 在一次工具调用中同时启动多个子 agent，每个子 agent 运行在独立的 DO facet 上，拥有隔离的 SQLite 和计算上下文，互不干扰。父 agent 为每个子 agent 分配不同的 display_order，确保客户端按确定顺序（而非竞态到达顺序）渲染面板。使用 allSettled 而非 all，使得单个分支失败不影响其他分支的正常完成。

系统在父 agent 级别提供 maxConcurrentAgentTools 并发上限选项，超过限制时快速失败并返回 error 事件。子 agent 实例级别通过同步标志 _runInProgress 防止并发调用同一实例导致 forwarder 状态污染。

### 与现有 agent 体系的兼容性

方案完全兼容现有的 chat/Think agent 体系。子 agent 就是普通的 chat-capable agent 子类，是否成为 agent 工具取决于父 agent 是否通过 runAgentTool 或 agentTool 调度它，而非继承特殊基类。框架通过内部 ChatCapableAgentClass 结构契约和子适配器（AgentToolChildAdapter）为 Think 和 AIChatAgent 提供支持，应用开发者无需重写 agent 框架。

子 agent 可嵌套调度自己的 agent 工具（子 agent 的 runAgentTool 创建更深层的 facet），底层通过 Facet 嵌套自然支持。嵌套运行的观察默认不向上桥接——子 agent 的运行仅对其直接父 agent 的客户端可见。跨链追踪通过 runId 作为 join key 实现。

### 客户端状态管理

方案提供头部无关（headless）的客户端状态管理原语，不强制 UI 样式。核心包括：（1）纯函数 reducer applyAgentToolEvent(state, message)，负责过滤 agent-tool-event 消息、按 (parentToolCallId, runId, sequence) 去重、应用 JSON UIMessageChunk 体重建消息部件、按 parentToolCallId 分组、按 order 排序；（2）React hook useAgentToolEvents({ agent })，订阅现有 useAgent 连接，返回 runsById、runsByToolCallId、unboundRuns（无 parentToolCallId 的命令式运行列表）以及 resetLocalState 方法。

与 useAgentChat 的集成方式：应用在渲染每条消息的工具调用部件时，通过 agentTools.getRunsForToolCall(part.toolCallId) 获取关联的 agent 工具运行列表并渲染面板。仅使用命令式 runAgentTool 的应用可直接渲染 unboundRuns。

### 技术效果与风险点

基于上述技术方案，系统实现了以下技术效果：

- 子 agent 执行过程对用户可见——通过将子 agent 的对话片段以 agent 工具事件形式实时多路复用到父 agent WebSocket，用户可在同一会话视图中观察每个子 agent 的推理过程、工具调用和输出片段，而不仅仅看到最终汇总结果。
- 断网重连后可恢复全部子 agent 执行历史——通过父侧注册表和子侧存储对话片段的双重持久化机制，页面刷新或网络短暂断开后，客户端重连时通过回放路径完整恢复所有已发生的子 agent 时间线。
- 模型自主调用与服务端确定性调用的统一——agentTool 和 runAgentTool 共享同一底层编排机制，LLM 可以在对话中自行决定何时调用子 agent，应用代码也可以通过 callable、HTTP handler 或定时任务主动调度子 agent，两种路径在事件流、状态管理和恢复机制上完全一致。
- 执行与观察的解耦——将"正在运行"和"正在被观察"两个概念分离，浏览器关闭或父 agent 崩溃不会取消子 agent 执行，子 agent 完成的对话片段仍可通过恢复路径获取，避免丢失已完成的推理工作。
- 状态安全——runId 幂等启动防止重复推理，终态不可覆写防止竞态条件导致的状态错误，取消-然后-清理的顺序防止孤立推理工作，注册表门控防止未授权访问。

待确认的风险点包括：（1）子 agent 的 alarm/schedule 机制依赖顶层父 agent 的物理 alarm 路由回调，在父 agent 被回收时子 agent 的定时任务能否正常触发需要在生产环境中验证；（2）跨 AIChatAgent/Think 混合场景的适配器尚处于规划阶段，当前以 Think-Think 组合为第一阶段实现目标；（3）延迟实时重连（late live-tail reattach）在 V1 中不支持——父 agent 崩溃后子 agent 仍在运行的情况下，恢复后只能回放已存储片段并标记 interrupted，完整的实时重连需要后续版本实现 tailAgentToolRun 机制。
