## 技术方案

本方案在基于 Durable Object 的 Agent 框架之上，构建了一套子 Agent 协作系统，使主 Agent 能够以工具调用、服务端确定性流程、后台执行等多种方式按需调度子 Agent，并将子 Agent 的执行进度、输出片段、完成、失败、取消等状态实时回传到主 Agent 所在会话视图中。

系统由以下核心机制构成：基于 Durable Object 面（facet）的子 Agent 创建与寻址机制；父方运行注册表与子方运行映射的双侧状态管理；统一流式事件协议与重放去重机制；基于 alarm 路由的子 Agent 恢复与崩溃协调；基于 AbortSignal 的跨 DO 取消传播；以及基于 onBeforeSubAgent 钩子的严格 drill-in 访问控制。以下逐一说明。

### 系统架构总览

系统由三层组件构成：父 Agent（主 Agent）、子 Agent（Agent 工具实例）、以及框架级的编排层。父 Agent 与子 Agent 均为运行在同一 Worker 内的 Durable Object（DO），子 Agent 通过 workerd 的 ctx.facets 机制以面（facet）形式与父 Agent 共址部署，拥有独立的 SQLite 存储、内存状态和 WebSocket 客户端集合，但共享父 Agent 的物理 alarm 时隙。子 Agent 在框架内保持与普通 Agent 完全一致的编程模型（继承 Agent 基类，可使用 SQL、RPC、调度、流式输出等全部基座能力），其"工具"角色仅由父 Agent 的调用方式决定，不依赖特殊基类。

编排层是框架在父 Agent 内部提供的运行管理设施，包括：运行注册表（cf_agent_tool_runs）用于记录每次子 Agent 运行的标识、状态、与父工具调用的关联关系、输入预览、排序元数据等；子方运行映射表（cf_agent_tool_child_runs）用于建立编排级 runId 到对话级 requestId/streamId 的映射；事件转发器负责将子 Agent 产生的聊天响应区块转换为统一的 agent-tool-event 帧并通过父方的 WebSocket 广播到客户端；以及恢复协调器负责在父 Agent 重启或客户端重连后重建运行状态。

### 子 Agent 标识与父子关联

每次子 Agent 运行由一个全局唯一的 runId 标识。runId 是跨父方注册表、子方运行映射、事件流、重放、取消和清理的统一连接键。父方在启动子运行前将 runId 写入 cf_agent_tool_runs 表；子方在收到启动请求后，将 runId 到内部 requestId（对话轮次标识）和 streamId（可恢复流标识）的映射写入 cf_agent_tool_child_runs 表。两表分离的设计使编排语义与对话语义解耦：runId 是产品的编排标识，requestId 是对话引擎的中止注册标识，streamId 是持久流的存储标识。

父子关联通过两种路径建立。模型驱动路径：父 Agent 的 LLM 选择调用 agentTool 工具时，框架自动捕获工具调用的 toolCallId 作为 parentToolCallId，与子运行的 runId 关联存入父方注册表。服务端确定性路径：应用代码直接调用 runAgentTool(Cls, options)，可选择性传入 parentToolCallId 以实现 UI 分组，也可不传入（此时子运行作为无父工具调用的独立运行，在客户端通过 unboundRuns 列表渲染）。后台执行路径：通过 Agent 的 @callable RPC 方法、HTTP 处理器或 schedule 回调触发的子运行同样使用 runAgentTool，不依赖 WS 连接存在。

子 Agent 的身份链通过 parentPath（根优先的祖先链）传递。每个子 Agent 在初始化时接收父方传入的 selfPath（祖先链+自身），使子 Agent 可以感知自己在 facet 树中的位置。parentAgent(Cls) 方法提供向直接父 Agent 的类型化 RPC 引用，用于子 Agent 需要回调父方的场景。递归嵌套是原生支持的：子 Agent 可以再调用 subAgent 创建孙 Agent，形成任意深度的 agent 工具树。

### 流式事件协议与实时展示

父 Agent 向客户端广播统一的 agent-tool-event 消息帧，帧类型包括：started（含 runId、agentType、inputPreview、排序序号 order、显示元数据）、chunk（含 runId 和 JSON 编码的 UIMessageChunk 体）、finished（含 runId 和总结文本 summary）、error（含 runId 和错误信息）、aborted（含 runId 和取消原因）、interrupted（含 runId 和中断原因，仅由父方在恢复时判定）。每条帧携带单调递增的 per-run 序列号 sequence 和可选的 replay 标记。

客户端去重以 (parentToolCallId, runId, sequence) 三元组为键。由于同一父工具调用下的多个并行子运行各自从 sequence 0 开始编号，仅用 sequence 不足以区分；加上 parentToolCallId 和 runId 可精确去重。对于无 parentToolCallId 的独立运行，以 (null, runId, sequence) 为键。重放帧携带 replay: true 标记，客户端据此区分原始实时观察帧与恢复重放帧，但两者共享同一去重键空间，保证重放不会产生重复条目。

子 Agent 的 chunk.body 直接使用与父 Agent 聊天响应相同的 UIMessageChunk 格式（文本、推理、工具调用、工具结果、来源等部件），客户端可以使用同一个 applyChunkToParts 原语重建子 Agent 的消息部件，无需为子 Agent 发明第二套渲染词汇。子 Agent 的输出在 UI 中可内联展示在父消息的工具调用部件下方，也可通过 drill-in 连接打开独立面板查看完整对话历史。

### 状态管理与持久化

状态管理采用双侧分工模式。父方 SQLite 中的 cf_agent_tool_runs 表记录编排级信息：run_id（主键）、parent_tool_call_id（可空）、agent_type、input_preview（默认仅保存预览而非完整输入，避免敏感数据扩散）、status（starting/running/completed/error/aborted/interrupted）、summary、error_message、display_order、started_at、completed_at。该表由框架维护，应用只控制策略（如保留规则、访问控制、显示元数据）。

子方 SQLite 中的 cf_agent_tool_child_runs 表记录对话级映射：run_id（主键）、request_id、stream_id、status、summary、error_message、started_at、completed_at。子方还通过已有的可恢复流（ResumableStream）机制将每个 UIMessageChunk 逐块持久化到 SQLite，序列号与父方事件帧中的 sequence 对齐。子 Agent 自身的消息历史和工具调用结果同样持久化在其独立的 SQLite 中。

状态生命周期遵循以下规则：插入运行行（status=starting）先于子 Agent 启动，确保即使父方在启动后立即崩溃也能在恢复时发现此行；子方启动后 status 更新为 running；子方到达终态后更新为 completed/error/aborted；父方恢复时对无法安全恢复的非终态运行标记为 interrupted。一旦运行到达终态，该状态即为权威状态——延迟的取消请求不得将 completed 改写为 aborted；延迟的恢复协调不得将 aborted 改写为 interrupted。运行完成后，父方注册行和子方面均默认保留（不自动删除），以支持事后刷新回放、drill-in 查看和调试审计。清理通过显式的 clearAgentToolRuns 接口执行，该接口同时删除父方注册行和对应的子方面，避免留下孤儿数据。

### 恢复机制

恢复机制覆盖三种场景：父 Agent DO 因休眠/崩溃被驱逐后重启、客户端网络断开后重连、浏览器页面刷新。

父 Agent 重启时，恢复协调器遍历 cf_agent_tool_runs 中所有非终态行（starting/running）进行协调：若子方面已不存在（子 DO 被删除或从未启动），标记为 interrupted；若子方报告 completed，重放已存储区块并将父方行标记为 completed；若子方报告 error 或 aborted，重放区块并标记对应终态；若子方报告 running（仍在执行但父方观测者已丢失），重放已存储区块后标记为 interrupted，同时附明确原因。此设计保证即使父方观测者丢失，已产生的子 Agent 输出仍然可恢复查看，不丢失已发生的执行过程。

关键的设计决策是观测与执行分离：断开观测者流（如浏览器关闭 WebSocket）不会自动取消子 Agent 执行。子 Agent 继续运行直到自然完成或被显式取消。runAgentTool 以 runId 为键具有幂等性：若传入已存在的 runId，终态运行返回已有结果而不重新执行，非终态运行不启动重复工作。这使 runAgentTool 可以安全地从重试路径、alarm 回调和重连恢复中调用，不会意外重复 LLM 推理工作。

子 Agent 的调度恢复通过父方的物理 alarm 多路复用实现。子 Agent 虽然不拥有独立的物理 alarm 时隙，但可以通过 schedule/scheduleEvery 注册逻辑回调，回调行存储在父方的调度表中并携带所有者路径。父方 alarm 触发时，框架按所有者路径将回调路由回对应的子 Agent 并以该子 Agent 为 this 执行。Think agent 的聊天恢复和 runFiber 机制同样在 facet 内工作：恢复的 continuation 可以从子方内部调度，并通过顶层父方的 alarm 路由回子方。

### 取消与清理机制

取消机制遵循 AbortSignal 链式传播路径：父方聊天/工具/请求被取消 → runAgentTool 通过 runId 显式取消子运行 → 子 Agent 中止当前轮次的 AbortController → 子 Agent 将 signal 传入 saveMessages（进而传入 streamText 的 LLM 调用）→ 父方断开该运行的观测者流 → 子 Agent 中止推理循环并报告 aborted 结果。子运行取消是幂等的：对已到达终态的运行发送取消请求不会改写其状态。

框架区分"断开观测"与"请求取消"两个操作。浏览器断开、父方重启或重放连接失败仅断开观测者流，不触发执行取消。只有来自父方活跃操作的显式 abort 信号才会传播到子方取消执行。deleteSubAgent 先执行 abortSubAgent（强制停止运行中的子 Agent 并级联取消其子孙），然后永久删除子 DO 存储。clearAgentToolRuns 支持按时间范围（olderThan）和状态过滤（status）删除，默认同时删除父方注册行和子方面。

### 访问控制与 Drill-in 安全

子 Agent 通过嵌套 URL 对外可寻址：/agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。客户端使用 useAgent({ agent: parent, name: userId, sub: [{ agent: childClass, name: runId }] }) 建立到子 Agent 的直接 WebSocket 连接（drill-in）。连接建立时，HTTP 请求先到达父 Agent DO，触发 onBeforeSubAgent(req, { className, name }) 钩子。

框架推荐的访问控制策略基于父方运行注册表实现严格门控：onBeforeSubAgent 中检查 this.hasSubAgent(className, name) 是否成立；若不成立则返回 404。由于只有通过 runAgentTool/agentTool 创建的子运行才会写入注册表，此机制天然阻止通过猜测 runId 的 URL 访问不存在的子 Agent。对于更精细的控制，钩子可返回修改后的 Request（注入身份头）或直接返回 Response（短路拒绝）。身份和租户信息继承自父 Agent：drill-in URL 总是通过父 Agent 的身份到达，runId 本身不是独立的访问凭证。子 Agent 上正在进行的框架驱动运行时，框架持有排他声明，并发的 runAgentTool 调用返回已有检查结果而不启动第二个轮次；drill-in 用户在框架驱动运行期间发送聊天消息将被推迟或明确拒绝，避免与运行中的轮次交错。

### 并发控制与可观测性

子 Agent 工具调用可能显著放大 LLM 成本：父 LLM 一次 fan-out 5 个 research 调用意味着 5 个并行的子 Think 轮次，每个都有独立的模型调用。系统在父 Agent 层面提供 maxConcurrentAgentTools 并发上限，超过上限的新请求快速失败并产生 error 事件，防止资源耗尽。

父方提供 AgentToolLifecycleHooks 生命周期钩子（onAgentToolStart/onAgentToolFinish），使应用代码可以在不修改 runAgentTool 的前提下记录、计量和审计每次子运行。runId 充当父方注册表、子方对话记录、日志和追踪系统之间的统一连接键。

### 与现有系统兼容性

本方案完全兼容现有 chat agent 和 Think agent 体系。子 Agent 就是普通的 Agent 子类，不需要继承特殊的 AgentTool 或 HelperAgent 基类；一个类是因为被 runAgentTool 调用才成为"agent 工具"。框架通过内部 ChatCapableAgentClass 结构契约和适配器支持 Think 和 AIChatAgent 作为子 Agent：适配器封装了 startAgentToolRun（按 runId 幂等启动）、cancelAgentToolRun（按 runId 幂等取消）、inspectAgentToolRun（按 runId 返回运行检查结果，是恢复的权威数据源）、getAgentToolChunks（按 runId 返回已存储的 UIMessageChunk 序列）等操作。

Think 和 AIChatAgent 均可作为父 Agent 或子 Agent，形成四种组合。第一阶段以 Think 为优先实现目标（因其已有最强的程序化轮次 API 和 session 树形消息存储），AIChatAgent 的适配在后续里程碑中补齐。runAgentTool 和 agentTool 是框架在已有 subAgent(Cls, name) 基元之上新增的编排 API，不改变 subAgent 的语义或路由。已有使用 subAgent 进行 fan-out/fan-in、多房间 chat、隔离数据库、门控访问等模式的应用代码无需修改。

### 关键处理流程

以下描述三种典型调用模式的关键处理流程。

流程一：模型驱动子 Agent 工具调用。1）父 Agent 的 LLM 决定调用 agentTool 工具（如 research）。2）框架工具执行函数内部调用 runAgentTool(Cls, { input, parentToolCallId: toolCallId, signal })。3）生成 runId，在 cf_agent_tool_runs 中插入 status=starting 行。4）通过 subAgent(Cls, runId) 获取子 Agent 面，调用 startAgentToolRun 启动 Think 聊天轮次，子方建立 runId→requestId→streamId 映射。5）父方观察子方 UIMessageChunk 输出，包装为 agent-tool-event/chunk 帧广播，sequence 递增。6）子方完成后父方更新 status=completed，广播 finished，返回 summary 给父 LLM。7）失败时广播 error，取消时广播 aborted，均返回结构化失败结果给父 LLM。

流程二：服务端确定性流程主动调用。1）应用代码在 Agent 的 @callable 方法、HTTP 处理器或 schedule 回调中直接调用 this.runAgentTool(Cls, { input })。2）不依赖 WebSocket 连接存在——调用路径是 DO 内部 RPC，而非 WS 驱动。3）若调用时存在活跃的客户端 WS 连接（如用户正在查看仪表盘），产生的 agent-tool-event 帧仍通过父方 WS 广播到客户端。4）若调用时无活跃连接（后台执行），子运行照常进行，状态和输出持久化在注册表和子方存储中。5）用户后续打开页面或刷新时，通过恢复机制重放已完成的子运行结果和正在运行的子运行已产生的输出。6）parentToolCallId 可选：传入时子运行渲染在对应工具调用部件下，不传入时作为 unboundRuns 渲染在独立区域。

流程三：无直接父工具调用的后台子运行。1）通过 Agent 的 schedule 回调、外部 HTTP 触发、或 MCP 工具间接触发 runAgentTool。2）此类运行在 cf_agent_tool_runs 中 parentToolCallId 为 null。3）客户端通过 unboundRuns 列表独立渲染这些运行的状态和输出。4）后台运行同样享受完整的幂等启动、取消、恢复、清理生命周期管理。5）子运行可以嵌套：子 Agent 内部可进一步调用 runAgentTool 创建孙 Agent，嵌套运行仅对直接父方的客户端可见，不自动向上桥接事件。

### 技术效果

本方案在基于 Durable Object 的 Agent 框架上实现了以下技术效果：（1）多模式子 Agent 调度——统一支持 LLM 工具调用、服务端确定性流程和后台执行三种调度模式，覆盖对话式 AI、批处理工作流和定时任务等场景；（2）双侧持久化与崩溃恢复——父方运行注册表与子方对话映射的双侧状态管理，配合基于 alarm 路由的恢复协调，确保即使父 Agent DO 被驱逐或客户端断连，已发生的子 Agent 执行过程和结果不丢失；（3）流式事件实时可见——子 Agent 的每个输出区块通过统一事件帧实时广播到客户端，与父 Agent 聊天流在同一视图中展示，支持并行多子运行的独立进度追踪；（4）重连重放去重——以 (parentToolCallId, runId, sequence) 为去重键，重放帧与实时帧共享键空间，客户端重连后无缝恢复已发生事件；（5）严格访问控制——基于父方运行注册表的 onBeforeSubAgent 门控，防止通过 URL 猜测访问不存在的子 Agent，drill-in 身份继承父 Agent 的认证和租户信息；（6）与现有体系零破坏兼容——不引入特殊子类、不改动 subAgent 语义、不要求应用重写 Agent 框架。

### 风险与待确认问题

以下为当前方案中已识别但待后续确认的风险点和技术边界：（1）V1 不支持延迟实时追踪重挂接（late live-tail reattach）：如果父方观测者在子运行仍在执行时消失，V1 重放已存储的区块后将运行标记为 interrupted，而非重新挂接实时流。协议层已预留 tailAgentToolRun 接口和 detached 观测者状态以支持未来实现。（2）子 Agent 不拥有独立物理 alarm 时隙：子方的 schedule/scheduleEvery 通过父方的物理 alarm 多路复用，极端情况下大量子方的并发调度可能造成父方 alarm 队列拥塞，需在实际负载下验证。（3）跨机子 Agent：当前子 Agent 以 facet 形式与父 Agent 共址部署，不支持跨机分布。未来若需支持远程子 Agent，API 和故障模式将有本质差异。（4）父 LLM 轮次恢复：若 agentTool 调用是父 LLM 轮次的一部分，恢复子运行本身不一定能恢复父 LLM 轮次——因为父 LLM 可能在拿到工具结果之前已崩溃。命令式 runAgentTool 的恢复路径更清晰，因为应用代码可以在事后检查运行结果而无需重建一个飞行中的 LLM 轮次。（5）自动重试：V1 不自动重试失败/中断的子运行，重试是调用方的显式决策。（6）嵌套子运行的观测不自动向上桥接：孙 Agent 的事件仅对直接父方（子 Agent）的客户端可见，跨层追踪依赖 runId 作为连接键。若需要全局追踪视图需额外实现。（7）结构化输出提取：V1 不在子 Agent 散文输出中自动提取结构化 JSON，若子方需返回结构化输出应在自身 prompt/工具契约中实现。
