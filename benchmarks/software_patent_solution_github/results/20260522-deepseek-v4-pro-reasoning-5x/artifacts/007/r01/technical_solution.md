## 技术方案

### 技术问题概述

本方案要解决的技术问题是：在基于 Durable Object 的有状态 agent 框架中，如何实现一种可复用的多 agent 协作机制，使得主 agent 在一次请求处理过程中能够按需调用一个或多个专门的子 agent 执行研究、规划、比较、总结等任务，同时将子 agent 的执行过程和结果以流式、可恢复的方式呈现给客户端，并在断网重连后仍能恢复已发生的子 agent 执行过程视图。

### 系统架构与组件

本方案的整体系统架构包含以下核心组件：主 Agent（Parent Agent）、子 Agent（Sub-Agent / Agent Tool）、子 Agent 运行注册表（Parent-side Agent Tool Run Registry）、子端运行映射表（Child-side Run Mapping）、流式事件协议层、以及客户端事件消费层。

主 Agent 和子 Agent 均基于 Durable Object（以下简称 DO）实现。每个主 Agent 和子 Agent 都是一个独立的 DO 实例，拥有各自的 SQLite 存储、WebSocket 连接管理和生命周期。子 Agent 通过 DO Facet 机制创建，与父 Agent 在同一 Worker 进程中托管（colocated），但拥有独立的存储边界。主 Agent 维护一个框架管理的 agent tool 运行注册表（cf_agent_tool_runs），用于记录每次子 Agent 调用的元数据；子 Agent 内部维护运行映射表（cf_agent_tool_child_runs），将编排层面的 runId 映射到内部 chat turn 的 requestId 和 streamId。

客户端通过 WebSocket 与主 Agent 保持长连接。子 Agent 的执行过程和结果以 agent-tool-event 帧的形式通过主 Agent 的 WebSocket 连接推送到客户端，而非要求客户端另行连接子 Agent。客户端通过 runId 和 sequence 序号对事件进行去重和排序。

### 子 Agent 运行身份与父子关联机制

每个子 Agent 运行由全局唯一的 runId 标识。runId 是跨重组、跨恢复的稳定标识符，同时作为子 Agent 的 DO 实例名称（facet name）、父端注册表的主键、子端映射表的主键、以及取消、清理、钻取（drill-in）等操作的句柄。

父端注册表（cf_agent_tool_runs）存储在父 Agent 的 SQLite 中，包含以下字段：run_id、parent_tool_call_id（关联的父工具调用 ID，可为空）、agent_type（子 Agent 类名）、input_preview（输入摘要，默认不存储完整原始输入）、status（starting / running / completed / error / aborted / interrupted）、summary（结果摘要）、error_message、display_order（同次父工具调用下的排序序号）、started_at、completed_at。注册表由框架维护，应用开发者只控制策略（保留期限、访问控制、展示元数据）。

子端映射表（cf_agent_tool_child_runs）存储在子 Agent 的 SQLite 中，将 runId 映射到内部 chat turn 的 requestId 和 streamId。这种设计使 runId 保持为编排层面的标识，而 chat turn 和 stream 的内部 ID 可以独立演化。映射表支持通过 runId 实现启动幂等性：如果子端已存在相同 runId 的记录，则不创建重复的 chat turn，而是返回已有运行状态。

父子关联链通过 parentPath 机制维护。每个子 Agent 实例保存其祖先链（从根 Agent 到直接父 Agent 的路径），支持递归嵌套（子 Agent 可进一步调用自己的子 Agent）。父 Agent 通过 hasSubAgent 和 listSubAgents API 进行子 Agent 实例的内省和枚举。

### 三种调用模式

本方案通过统一的底层机制 runAgentTool 支持三种调用模式，分别对应不同的发起方和关联关系：

模式一：模型作为工具调用（agentTool）。通过 agentTool(Cls, options) 工厂函数创建一个 AI SDK 工具条目，模型在推理过程中自主决定何时调用子 Agent。当模型触发该工具时，框架以模型工具调用的 toolCallId 作为 parentToolCallId，创建子 Agent 运行并流式返回结果。子 Agent 的事件帧携带 parentToolCallId，使客户端能将子 Agent 视图绑定到对应的父工具调用部件上。模型接收到的工具返回结果根据子 Agent 的终止状态不同：completed 状态返回结构化输出或文本摘要；error / aborted / interrupted 状态返回包含错误信息的结构化失败结果，避免模型在缺失数据时产生幻觉。

模式二：服务端确定性流程主动调用（runAgentTool 直接调用）。应用代码在 Agent 内部通过 this.runAgentTool(Cls, { input, parentToolCallId?, displayOrder?, signal? }) 直接发起子 Agent 运行。该模式适用于无需模型决策的确定性多阶段工作流、定时任务触发的分析、通过 @callable RPC 或 HTTP 端点发起的报告生成等场景。调用方可以选择不传递 parentToolCallId，此时子 Agent 运行不与任何父工具调用关联，客户端通过 unboundRuns 列表独立渲染这些运行。

模式三：无直接父工具调用的后台子运行。当 runAgentTool 的调用方既不是模型工具调用链、也不属于某个特定父消息的上下文时（例如通过 schedule 定时任务或外部 webhook 触发），子 Agent 以 parentToolCallId 为空的方式运行。该子 Agent 仍然具有完整的父子关联链（parentPath），其运行记录仍然写入父端注册表，客户端可以通过 drill-in URL 独立访问该子 Agent 的完整执行历史和聊天记录。

### 流式事件实时展示与重放去重机制

子 Agent 的执行进度、输出片段和生命周期事件通过主 Agent 的 WebSocket 连接以 agent-tool-event 帧的形式推送到客户端。事件帧类型定义如下：

- started：包含 runId、agentType、inputPreview、order、display 等字段，表示子 Agent 运行已启动。
- chunk：包含 runId、body（JSON 编码的 UIMessageChunk），承载子 Agent 的流式输出片段。body 采用与主聊天流相同的 UIMessageChunk 格式，客户端可复用同一套 applyChunkToParts 原语重建消息部件。
- finished：包含 runId、summary，表示子 Agent 正常完成。
- error：包含 runId、error，表示子 Agent 执行失败。
- aborted：包含 runId、reason，表示子 Agent 被显式取消。
- interrupted：包含 runId、error，表示父 Agent 在恢复时发现子 Agent 仍在运行但无法重新附加实时观察（V1 限制），该运行被标记为中断。

每条 agent-tool-event 帧包含单调递增的 sequence 序号和可选的 replay 标记。客户端去重键为 (parentToolCallId, runId, sequence) 三元组。因为同一父工具调用下可能并行运行多个子 Agent，每个子 Agent 的 sequence 都从 0 开始独立编号，所以仅靠 runId + sequence 无法去重，必须引入 parentToolCallId 维度。对于无 parentToolCallId 的命令式运行，去重键使用 (null, runId, sequence)。

去重场景覆盖两种数据到达路径：原始实时观察（original live observation）和持久化重放（durable replay）。当客户端刷新页面或网络断开后重连时，主 Agent 通过 getAgentToolChunks(runId, { afterSequence }) 从子 Agent 的存储中读取已持久化的 chunk 数据，以 replay: true 标记重新发送给客户端。客户端通过去重键检测已接收过的 chunk，避免重复渲染。sequence 在 V1 中将实时观察和持久重放的序号保持对齐（envelope.sequence == chunk.sequence），使客户端使用统一去重逻辑。

### 状态管理与恢复机制

子 Agent 运行的状态管理分布在父端和子端两层。父端注册表中的 status 字段是运行生命周期的权威记录，取值包括 starting、running、completed、error、aborted、interrupted。其中 completed、error、aborted、interrupted 为终态。interrupted 是父端独有的状态——子 Agent 不会声明自身为 interrupted，只有父 Agent 在失去观察者且无法实时续接时才会将该运行标记为 interrupted。

恢复机制的核心设计原则是：子 Agent 运行是持久化工作（durable work），实时流式观察只是观察运行的其中一种方式。具体恢复流程如下：

- 父 Agent 在启动子 Agent 运行前，先向 cf_agent_tool_runs 表插入一条 status='starting' 的记录，然后再唤醒子 Agent。
- 子 Agent 收到启动请求后，在 cf_agent_tool_child_runs 表中持久化 runId → requestId → streamId 的映射，然后开始执行 chat turn 并存储流式 chunk。
- 父 Agent 观察子 Agent 的流式输出，将 chunk 以 agent-tool-event 帧转发给客户端，同时更新父端注册表中的 status 为 running。
- 子 Agent 达到终态后，父 Agent 将注册表状态更新为 completed / error / aborted，并发送对应的终态事件帧。

当父 Agent 因 DO 休眠或被驱逐而在观察中途丢失上下文时，父 Agent 重新启动后会执行恢复（reconciliation）：

- 遍历父端注册表中所有非终态（starting / running）的记录。
- 对于每条记录，通过 runId 查找子 Agent 实例并调用 inspectAgentToolRun(runId) 获取子端的实际运行状态。
- 如果子端报告 completed / error / aborted，则重放已存储的 chunk 数据并更新父端注册表为对应终态。
- 如果子端报告 running（仍在执行），V1 版本重放已存储的 chunk 并将父端记录标记为 interrupted，同时携带明确的错误信息说明实时续接尚不支持。未来版本通过 tailAgentToolRun 可附加新的观察者。
- 如果找不到子 Agent 实例或运行记录，则标记为 interrupted。

该设计保证了即使父 Agent 崩溃，已在执行的子 Agent 也不会被自动取消；父 Agent 重启后能够诚实地向客户端报告每个子 Agent 运行的最终状态或中断原因。

### 取消、清理保留与访问控制

取消机制：子 Agent 运行的取消通过 AbortSignal 从父 Agent 向子 Agent 传播。取消流程为：(1) 父 Agent 的 chat turn / 工具调用被取消；(2) runAgentTool 通过 runId 显式取消子 Agent 运行；(3) 子 Agent 的 AgentToolChildAdapter 中止当前 chat turn 的 AbortController；(4) 该 signal 传递到 saveMessages 和 LLM 推理循环，中止推理并返回 aborted 结果；(5) 父 Agent 分离该运行的实时观察者流，发送 aborted 事件帧。关键设计区分：断开观察者流（如浏览器断开、父 Agent 重启）不等于取消运行；只有显式的 abort signal 才触发执行取消。

取消操作的幂等性：如果子 Agent 已到达终态（completed / error），后续的取消请求不得将已完成的运行改写为 aborted。同样，恢复过程中的 reconciliation 不得将已 aborted 的运行改写为 interrupted。终态一旦写入即不可覆盖。

清理与保留：子 Agent 运行完成后默认保留，不自动删除。保留策略原因为：支持运行后刷新查看、钻取详情、失败调试和审计追踪。框架提供显式清理 API：clearAgentToolRuns({ olderThan?, status? })，该 API 同时删除父端注册表行和对应的子 Agent facet 实例。清理操作在存在 starting / running 状态的运行时，先执行取消再清理，避免遗留无观察者且无恢复路径的孤立 LLM 工作。

钻取（drill-in）与访问控制：子 Agent 通过现有的子 Agent 路由原语（嵌套 URL：/agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}）对外可寻址。框架安装严格的 onBeforeSubAgent 守卫：仅当父端注册表中存在匹配的 agent tool 运行记录时，钻取请求才能到达子 Agent 实例。runId 本身不作为能力令牌：钻取 URL 始终通过父 Agent 的已有身份（useAgent({ agent: parent, name: userId, sub: [...] })）访问，认证和租户隔离由父 Agent 继承而来。在框架驱动的 agent tool turn 运行期间，子 Agent 实例持有排他性声明：对同一 runId 的并发 runAgentTool 调用返回已有运行状态（幂等启动）而非启动第二个 turn；钻取用户在框架运行期间发送聊天消息的行为被拒绝或排队，不会与进行中的 turn 交织。

### 异常处理与并发控制

重复请求处理：runAgentTool 和子 Agent 端的 startAgentToolRun 均以 runId 为键实现幂等性。如果调用方传入已存在的 runId：(1) 终态运行直接返回已有的 RunAgentToolResult，不重新执行；(2) 非终态运行不启动重复工作——V1 中，非原始实时观察者的调用方重放已存储 chunk 并收到 interrupted 状态。这使得 runAgentTool 可以安全地从重试路径、定时告警和重连恢复中调用，不会意外重复 LLM 工作。

并发控制：方案提供粗粒度并发保护——父 Agent 级别选项 maxConcurrentAgentTools 限制同时运行的子 Agent 数量，超过限制时快速失败并返回明确的 error 事件。更细粒度的配额、基于 Token 的预算和计费集成可通过生命周期钩子（onAgentToolStart / onAgentToolFinish）在应用层实现。子 Agent 调度利用现有 Agent 调度 API：子 Agent 本身不拥有独立的物理告警槽位，但可通过顶级父 Agent 的告警路由拥有子 Agent 的逻辑回调。

错误边界与传播：(1) 子 Agent 执行过程中的错误被捕获为 error 终态，存储到子端映射表和父端注册表，并通过 agent-tool-event 帧的 error 事件通知客户端。(2) 当子 Agent 作为模型工具被调用时，非 completed 的运行结果以结构化失败形式返回给父 LLM，使模型能基于实际失败原因决定重试或向用户说明。(3) 父 Agent 在启动子 Agent 前插入注册表行的设计确保即使父 Agent 在启动后立即崩溃，恢复路径也能发现并处理该运行记录。

与现有 chat / Think agent 体系的兼容性：子 Agent 本质上是普通的 chat-capable agent 子类（Think 或 AIChatAgent）。一个类之所以成为 agent tool，是因为父 Agent 通过 runAgentTool 或 agentTool 调度它，而非因为它继承特殊的基类。框架通过内部的 ChatCapableAgentClass 结构化合约和 AgentToolChildAdapter 适配器支持 Think 和 AIChatAgent 两类子 Agent。这种设计意味着应用开发者不需要重写现有 agent 类即可将其作为子 Agent 使用。

### 客户端事件消费层

客户端通过 React hook useAgentToolEvents 消费 agent-tool-event 帧。该 hook 从已有的 useAgent 连接中订阅非聊天 WebSocket 帧，负责：(1) 过滤 agent-tool-event 类型的消息；(2) 通过 (parentToolCallId, runId, sequence) 去重实时和重放数据；(3) 调用 applyChunkToParts 将 JSON 编码的 UIMessageChunk 应用到消息部件；(4) 按 parentToolCallId 分组运行；(5) 按 displayOrder 排序同组运行；(6) 将协议状态映射为 running / completed / error / aborted / interrupted；(7) 暴露 subAgent 信息用于钻取（drill-in）URL 构造。

该 hook 是无头（headless）组件——只管理状态和逻辑，不包含 UI 渲染。应用开发者自行决定子 Agent 面板的视觉呈现、位置和钻取交互方式。对于无 parentToolCallId 的命令式运行，hook 提供 unboundRuns 列表供非聊天 UI 渲染。

重置与清理协调：hook 暴露 resetLocalState() 方法清除客户端状态。应用开发者在清除聊天历史之前应先调用服务端的 clearAgentToolRuns 清理服务端运行记录，再调用 resetLocalState() 清理客户端状态，保证两端一致性。

### 技术效果总结

技术效果方面，本方案实现了以下目标：(1) 主 Agent 以统一机制支持模型自主调用、服务端确定性流程调用和后台子运行三种模式，覆盖了多 agent 协作的主要场景；(2) 子 Agent 拥有独立的 DO 存储和生命周期，天然支持长任务、并行任务和离线查看；(3) 子 Agent 的流式执行过程对客户端实时可见，且通过三元组去重键实现断网重连后的无缝恢复；(4) 父端注册表与子端映射表的双层状态管理保证了崩溃恢复的诚实性和数据一致性。

### 风险与待确认问题

以下为当前方案中需要后续确认或迭代的技术风险点：

- 实时续接（live-tail reattach）限制：V1 版本不支持在父 Agent 丢失观察者后重新附加到仍在运行的子 Agent 的实时流——此时该运行被标记为 interrupted。完整的 detached → 重新附加 live-tail 需要 tailAgentToolRun 机制，列为后续迭代。
- 跨 DO 的祖先 RPC：子 Agent 可通过 parentPath 获取祖先链，但向上跨 DO 的 RPC 调用需要应用层通过父 Agent 的显式桥接方法实现，框架不提供透明代理。
- 观测与追踪：当前 agent tool 运行不自动继承追踪上下文（trace ID）。跨子 Agent 调用的分布式追踪需要应用层通过生命周期钩子手动注入。
- 嵌套子 Agent 的向上事件冒泡：子 Agent 进一步调用的孙 Agent 事件不会自动向上冒泡到根 Agent 的客户端——每层父子关系的事件观察是独立的。
- 跨 Worker / 跨账户子 Agent：当前方案子 Agent 运行在与父 Agent 同一 Worker 进程中，通过 DO Facet 实现托管。跨 Worker 或跨账户的子 Agent 调度不属于 V1 范围。

### 关键处理流程

完整的子 Agent 调用处理流程如下：

第一步——启动前记录：父 Agent 生成或接收 runId，向 cf_agent_tool_runs 表插入 status='starting' 的记录，包含 agent_type、input_preview、parent_tool_call_id（可为空）、display_order 等元数据。此步骤确保即使后续任何步骤失败，恢复路径都能感知到该运行的存在。

第二步——唤醒子 Agent：父 Agent 通过 this.subAgent(ChildClass, runId) 获取或创建子 Agent 的 DO 实例（facet）。子 Agent 的 DO 与父 Agent 托管在同一 Worker 进程中，但拥有独立的 SQLite 存储。

第三步——子端启动与映射：父 Agent 调用子 Agent 的 startAgentToolRun(input, { runId, signal })。子 Agent 在 cf_agent_tool_child_runs 表中持久化 runId → requestId → streamId 映射，然后启动 chat turn。启动是幂等的：若该 runId 已有记录，直接返回已有状态。

第四步——流式观察与转发：父 Agent 通过子 Agent 的流式输出接口获取 UIMessageChunk 序列。每收到一个 chunk，父 Agent：(a) 将 chunk 以 agent-tool-event 帧（kind='chunk'，带递增 sequence）广播给客户端；(b) 更新父端注册表 status 为 running。chunk 同时被子 Agent 自身的 ResumableStream 持久化到其 SQLite 中。

第五步——终态处理：子 Agent 达到终态后，父 Agent：(a) 更新注册表状态为 completed / error / aborted；(b) 发送对应的终态事件帧（finished / error / aborted）；(c) 如果该运行属于模型工具调用（有 parentToolCallId），父 Agent 将结果返回给父 LLM 的工具调用处理逻辑。运行记录和子 Agent 实例默认保留，支持后续钻取和审计。

### 关键数据模型

本方案涉及以下关键数据表（均为框架维护，应用开发者通过 API 间接操作）：

父端 agent tool 运行注册表（位于父 Agent SQLite）：cf_agent_tool_runs (run_id TEXT PRIMARY KEY, parent_tool_call_id TEXT, agent_type TEXT NOT NULL, input_preview TEXT, input_redacted INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, summary TEXT, error_message TEXT, display_metadata TEXT, display_order INTEGER NOT NULL DEFAULT 0, started_at INTEGER NOT NULL, completed_at INTEGER)。该表记录每次子 Agent 调用的编排元数据，是恢复、重放、钻取和清理的权威数据源。

子端运行映射表（位于子 Agent SQLite）：cf_agent_tool_child_runs (run_id TEXT PRIMARY KEY, request_id TEXT, stream_id TEXT, status TEXT NOT NULL, summary TEXT, error_message TEXT, started_at INTEGER NOT NULL, completed_at INTEGER)。该表将编排层面的 runId 映射到内部 chat turn 的具体执行标识，支持启动幂等性和状态查询。

子 Agent 注册表（位于父 Agent SQLite）：cf_agents_sub_agents (class TEXT NOT NULL, name TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (class, name))。该表由 subAgent() / deleteSubAgent() 的副作用维护，支持 hasSubAgent 和 listSubAgents 内省。

流式 chunk 存储（位于子 Agent SQLite）：cf_ai_chat_stream_metadata 和 cf_ai_chat_stream_chunks。子 Agent 复用 Think / AIChatAgent 已有的 ResumableStream 机制，将流式 chunk 持久化到 SQLite 中，支持重连后的 chunk 重放。
