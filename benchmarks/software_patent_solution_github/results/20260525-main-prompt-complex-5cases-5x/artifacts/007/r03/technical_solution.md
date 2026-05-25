## 技术方案

### 系统架构总述

本方案在现有基于 Durable Object 的 Agent 框架之上，增加一层代理工具（Agent Tool）编排层。框架已有的子代理（Sub-Agent）原语提供了按名称创建、获取、终止和删除共置子 Durable Object 的能力，每个子代理具有独立的 SQLite 存储和 RPC 调用接口；框架已有的聊天代理（Think / AIChatAgent）提供了消息持久化、可恢复流式输出、WebSocket 协议、客户端工具执行和会话恢复能力。代理工具编排层在不重写上述基础能力的前提下，将两者连接为新的协作模式：主代理将子代理作为一种可调度能力，子代理的执行进度、输出片段、完成、失败、取消等状态以流式事件形式回到主代理所在的同一会话视图中。

核心系统由以下组件构成：(1) 主代理（Parent Agent）：用户通过 WebSocket 直接连接的顶层聊天代理，负责接收用户消息、运行推理循环、调度代理工具并将流式事件广播至客户端。(2) 代理工具子代理（Agent Tool Sub-Agent）：由主代理按需创建的共置子 Durable Object，自身也是一个完整的聊天代理实例，具有独立的模型调用、工具集、消息历史和可恢复流。

(3) 代理工具运行注册表（Agent Tool Run Registry）：主代理维护的持久化表（cf_agent_helper_runs），记录每一次代理工具调度的运行标识、父工具调用标识、代理类型、输入摘要、状态、完成摘要、错误信息和显示顺序。(4) 代理工具运行器（Agent Tool Runner）：框架提供的协议桥接层，负责创建子代理、驱动聊天轮次、将子代理的聊天响应块转发为主代理的代理工具事件、维护子代理自身可恢复流作为持久事实源。(5) 子代理路由层（Sub-Agent Routing）：将客户端对子代理的直接访问（钻入视图）通过 URL 路径 /agents/{parent}/sub/{child-type}/{child-name} 路由至子代理实例，主代理在连接建立时执行访问控制检查。

数据流向：用户通过 WebSocket 连接主代理发生消息；主代理在推理循环中调用工具（LLM 决定或确定性流程）；工具执行通过 runAgentTool 创建子代理并驱动其聊天轮次；子代理的每个聊天响应块通过重写的 broadcast 钩子截获，经 RPC ReadableStream 回传主代理；主代理包装为 helper-event 帧通过主 WebSocket 广播至客户端；客户端的 onConnect 触发重放流程，从注册表行重建完整时间线。

### 代理工具调度机制

本方案提供两种调度形状，共用同一底层机制：(1) runAgentTool(Cls, { input, ...options })：命令式 API，供服务端确定性流程、后台任务、分阶段报告、非 LLM 编排场景直接调用。(2) agentTool(Cls, options)：工具工厂，为 AI SDK 工具集生成一个工具条目，使主代理的大语言模型可自主决定何时调度代理工具。两者均使用现有 subAgent(Cls, name) 语义创建子代理，不引入新的子代理基类。

一次代理工具调度（runId）的生命周期如下：主代理在调度前先在 cf_agent_helper_runs 表中插入一行，标记状态为 running，记录父工具调用标识（parentToolCallId）、代理类型（agentType）和显示顺序（displayOrder）。然后通过 subAgent(Cls, runId) 获取或创建子代理实例，调用子代理的 chat() 或 saveMessages() 方法驱动聊天轮次。子代理在其自身 SQLite 中持久化消息和可恢复流块，主代理作为观察者读取这些块并包装为代理工具事件广播至客户端。

### 流式事件实时展示与重放去重

代理工具的执行进度和结果通过四类事件传递至客户端：started（启动）、chunk（流式输出块）、finished（完成）、error（错误）。started 事件由主代理在子代理开始执行前合成，携带 helperId、helperType、query 和 displayOrder，使得客户端在尚未收到任何流式块时即可渲染子代理面板占位。chunk 事件直接转发子代理自身的聊天响应块（UIMessageChunk），不做解析和再编码。finished 和 error 事件由主代理在子代理轮次结束后合成，携带最终摘要或错误信息。

每个代理工具事件在线路上携带 (parentToolCallId, sequence) 二元组：parentToolCallId 将事件归属到主代理消息中的特定工具调用部件，sequence 为逐事件递增的 0 基序号。在重放场景中，事件帧额外携带 replay: true 标记，客户端通过 (parentToolCallId, sequence) 进行去重——同一序号的事件仅处理一次，无论其来自实时广播还是重放通道。重放时事件按 started（sequence 0）、chunk（sequence 1..N）、finished/error（sequence N+1）的顺序严格输出，与实时广播路径的序号分配完全一致。

### 状态持久化与崩溃恢复

状态持久化分两层：(1) 子代理层：子代理自身是一个完整的聊天代理实例，其消息历史、可恢复流块和流元数据持久化在子代理独立的 SQLite 中。即使主代理崩溃或休眠，子代理的已生成内容不会丢失。(2) 主代理层：主代理维护 cf_agent_helper_runs 注册表，记录每次代理工具调度的运行标识、父工具调用标识、代理类型、查询文本、状态、摘要、错误信息和流标识（stream_id）。

崩溃恢复流程：主代理在 onStart 生命周期中检查 cf_agent_helper_runs 表，将所有状态为 running 的行标记为 interrupted 并写入完成时间戳——这是因为主代理重启意味着原先读取子代理 RPC 流的转发循环已不存在。在客户端重连时（onConnect），主代理遍历 cf_agent_helper_runs 的所有行，对每一行：合成 started 事件，通过子代理 RPC 获取已存储的聊天流块并转发为 chunk 事件（使用行中的 stream_id 精确定位对应轮次的流，避免钻入用户的后续轮次产生的流覆盖原始内容），最后根据行状态合成 finished 或 error 终端事件。若行状态为 interrupted，则合成一个 error 事件说明中断原因。

### 取消传播机制

取消传播链路从用户操作到子代理推理循环全程贯通。主代理的聊天轮次由一个 AbortController 控制，该控制器的信号传递给 AI SDK 工具执行的 abortSignal。当主代理轮次被取消时：(1) 主代理的工具执行收到 abort 信号，(2) 工具执行取消子代理 RPC 流的 reader，(3) workerd 的 DO RPC 桥接层将取消传播至子代理侧，触发 ReadableStream 的 cancel 回调，(4) 子代理的 cancel 回调中止其轮次专有的 AbortController，(5) 该控制器的信号通过 saveMessages({ signal }) 传递至 Think 推理循环，使 LLM 调用同步终止，不存在竞态窗口。

并行子代理的取消处理：当主代理的一个工具调用同时调度多个子代理（如 compare 工具并行启动两个 Researcher）时，所有子代理共享同一个父级 abortSignal。取消任一分支不影响另一分支——采用 Promise.allSettled 而非 Promise.all 收集结果，即使一个子代理被取消或失败，其余子代理继续在其独立 DO 上运行至完成。已存储的流块在取消后仍持久化在子代理 SQLite 中，供重连重放。

### 访问控制与钻入隔离

子代理通过嵌套 URL /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name} 对外可寻址，客户端可通过 useAgent({ sub: [...] }) 建立对子代理的直接 WebSocket 连接（钻入视图）。钻入访问的访问控制通过主代理的 onBeforeSubAgent 钩子实现：当外部请求到达子代理 URL 时，框架先将请求路由至主代理，调用 onBeforeSubAgent(req, { className, name })，主代理在此检查 cf_agent_helper_runs 注册表中是否存在对应的 (className, name) 行。

若注册表中不存在对应行，返回 404 响应阻止连接——这意味着攻击者无法通过猜测子代理名称来创建任意子代理实例。由于 _runHelperTurn 在广播 started 事件之前已插入注册表行，合法客户端在收到 started 事件并获知 helperId 时，注册表行必定已存在，不存在时序缺口。内部 subAgent() 调用绕过 onBeforeSubAgent 钩子（与 getAgentByName 绕过 onBeforeConnect 的语义一致），因此主代理自身调度子代理不受自身访问控制检查影响。钻入连接的子代理本身也是一个完整聊天代理实例，可通过 useAgentChat 进行正常对话交互。

### 清理与保留策略

代理工具子代理在完成后默认保留而非立即删除。这是因为客户端在页面刷新后需要通过子代理的持久化存储重放已完成子代理的时间线——若子代理已被删除，重放将无法获取已存储的流块。清理策略由应用层通过策略回调控制：(1) Clear 操作遍历 cf_agent_helper_runs 所有行，对每行调用 deleteSubAgent 删除子代理的 Durable Object 存储（含 SQLite 和所有消息），然后清空注册表。(2) 基于时间或数量的 GC 策略由应用在注册表查询中通过 WHERE 条件限制保留范围（如保留最近 N 条记录或过去 T 小时内的记录）。(3) deleteSubAgent 操作会先终止子代理（abortSubAgent）再永久删除存储，且递归删除子代理的子孙代理，不可逆。

清理操作在处理每行时使用 helperClassFor(helper_type) 查找正确的子代理类，确保 Researcher 和 Planner 等不同类型的子代理被路由至正确的 deleteSubAgent 调用。并发控制方面，子代理实例通过 _runInProgress 同步标志防止同一实例上的并发 runTurnAndStream 调用，主代理为每次工具调用创建新的子代理实例（唯一 runId），因此不同工具调用的子代理天然隔离。

### 子代理身份与父子关联

每个代理工具子代理在创建时获得唯一 runId 作为其 Durable Object 名称。父子关联通过两层机制建立：(1) 在 cf_agent_helper_runs 注册表中，parentToolCallId 字段将子代理运行关联到主代理消息中的特定工具调用部件——同一 parentToolCallId 下可有多个子代理（如 compare 工具的并行调度），通过 displayOrder 区分左右顺序。(2) 在子代理路由层，子代理的嵌套 URL 包含完整祖先路径，客户端和框架均可通过此路径定位子代理的归属。

方案覆盖三种调用模式：(A) 模型驱动工具调用：主代理的 LLM 通过 agentTool 工厂生成的工具条目自主决定调度子代理，parentToolCallId 为 AI SDK 分配的工具调用标识，子代理结果作为工具输出返回给 LLM 继续推理。(B) 服务端确定性流程调用：应用代码直接调用 runAgentTool，此时无对应 LLM 工具调用，parentToolCallId 由调用方指定（如流程步骤标识），子代理结果直接用于后续逻辑。(C) 无直接父工具调用的后台子运行：通过 runAgentTool 在后台启动子代理后不等待结果，或通过子代理自身的调度 API 在子代理内部启动定时或延续任务——此时子代理的 parentToolCallId 设为空或约定的后台标记，子代理事件仍广播至主代理连接但不在特定工具部件下渲染。

### 数据流与关键协议

子代理的流式输出通过广播钩子截获机制回传主代理。子代理重写 broadcast 方法：当检测到输出帧类型为 MSG_CHAT_RESPONSE 时，提取其中的 UIMessageChunk JSON 正文，通过 RPC ReadableStream 的 enqueue 操作写入 NDJSON 行，主代理通过 reader.read() 逐行消费。主代理为每个 NDJSON 行分配自身的单调递增 sequence，包装为 helper-event 帧通过主代理的 broadcast 广播至所有已连接客户端。

关键设计决策：(1) 使用 Uint8Array 字节流而非对象块传输——workerd 的 DO RPC 层仅支持 ReadableStream<Uint8Array>，对象块在传输中会触发 Network connection lost 错误。(2) 子代理的 RPC 流主体逻辑（包括 saveMessages 调用）放在 ReadableStream 的 start(controller) 回调中执行——workerd 将 start 回调视为活跃 I/O，保持子代理 facet 在推理循环暂停期间不被休眠。(3) 子代理完成后的摘要通过 getFinalTurnText RPC 方法获取，该方法通过对比轮次前后的 assistant 消息 ID 差集精确定位本轮产生的消息，不受钻入用户后续追加轮次的影响。同时捕获子代理的 stream_id 写入注册表行，确保重放时读取的是原始轮次的流块而非钻入用户追加轮次的流块。

### 技术效果

(1) 状态边界清晰：每个子代理的聊天流和消息持久化在其自身 SQLite 中，主代理仅维护轻量注册表，不存在双流冲突或状态镜像不一致问题。子代理崩溃或主代理崩溃的恢复路径相互独立。

(2) 流式事件与重放一致：通过 (parentToolCallId, sequence) 二元组实现事件去重，重放路径使用与实时广播相同的序号分配逻辑，客户端无需区分事件来源即可正确重建子代理消息。

(3) 取消传播完整：从用户操作经主代理 AbortController、RPC 流 cancel、子代理轮次 AbortController 到 LLM 推理循环，形成无竞态窗口的同步终止链路。

(4) 与现有 Agent 体系兼容：代理工具子代理复用现有 Agent 基类的 subAgent、SQLite、调度、RPC 和 WebSocket 能力，复用 Think/AIChatAgent 的聊天生命周期、流式输出、可恢复流和消息持久化，不要求应用开发者重写 Agent 框架。

(5) 三种调度模式统一：模型驱动工具调用、服务端确定性流程调用和后台子运行共用同一底层机制（subAgent + cf_agent_helper_runs + helper-event 协议），仅在 parentToolCallId 的语义和结果消费方式上有区别。

(6) 钻入访问安全：onBeforeSubAgent 注册表门控确保只有经主代理合法调度的子代理可通过外部 URL 访问，内部 subAgent 调用不受影响，防止未授权子代理创建。

### 重复请求与并发控制

重复请求处理：主代理的 saveMessages 和子代理的 runTurnAndStream 均通过同步标志（_runInProgress）防止同一实例的并发执行，重复调用抛出明确错误而非静默覆盖。主代理为每次工具调用生成唯一 runId，确保每次调度创建独立子代理实例，重复工具调用不会复用旧子代理。消息持久化使用 INSERT OR IGNORE（幂等）和 INSERT ON CONFLICT DO UPDATE（增量更新），相同 ID 的消息不会重复存储。

异常处理：子代理轮次中的错误通过多层捕获保证系统稳定。(1) 子代理在推理循环中通过 _streamResult 的错误广播机制将错误信息写入 _lastStreamError，主代理在获取最终摘要时读取此错误并构造包含真实原因的错误消息。(2) 主代理的 _runHelperTurn 外层的 catch 块捕获所有异常后，将注册表行状态更新为 error 并记录 error_message，同时广播 error 事件至客户端。(3) 重放路径对每个子代理行使用独立 try-catch，单行重放失败不影响其余行的重放。(4) 清理路径对每个子代理的 deleteSubAgent 使用独立 try-catch，单个删除失败不影响其余清理。

### 风险与待确认问题

(1) 跨机子代理：当前子代理通过 ctx.facets 实现，与主代理共置于同一机器。未来扩展到远程子代理（通过标准 DO Stub 跨机调用）需重新评估故障模式、延迟和 RPC 语义。(2) 实时尾部订阅：若主代理在子代理执行期间崩溃，子代理独立恢复运行并持久化流块，但主代理侧没有重新建立实时广播循环的机制——当前通过 interrupted 标记和后续重放弥补。未来可增加主代理恢复后重新订阅子代理 RPC 流的实时尾部机制。(3) 资源限制：当前框架未对子代理数量、嵌套深度或总存储量设置上限——workerd 可能施加自身限制，但 SDK 未显式暴露或强制执行。生产部署需应用层自行管理资源配额。(4) 跨代理跟踪：当父代理调用子代理、子代理调用 LLM、LLM 触发工具、工具调用另一个子代理时，缺乏连接式跟踪——需通过 runId 传播实现跟踪 ID 在整个代理树中的连通。
