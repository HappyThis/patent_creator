## 技术方案

本方案在 Durable Object agent 框架中设计了一种主 agent 可调用多个子 agent 进行协同工作的系统。核心思路是：将每个子 agent 实现为独立的 Durable Object Facet，拥有独立的持久化存储和生命周期，父子 agent 之间通过类型化 RPC 和流式事件协议进行通信，子 agent 的执行进度、输出和状态通过统一的事件通道回流到主 agent 同一会话视图中，由客户端实时渲染。

系统由三个核心层面构成：第一层是子 agent 的创建与管理，通过 ctx.facets 机制将子 agent 注册为父 agent 的持久化附属单元，维护父子关联注册表；第二层是外部寻址与流式路由，通过层级化 URL 路径使得每个子 agent 可被外部客户端直接寻址和访问，同时通过中间件实现门禁控制；第三层是 agent 工具编排系统，父 agent 将子 agent 包装为可调用的工具（agent tool），LLM 可自主选择调用，子 agent 的执行过程以结构化流式事件实时回流到父 agent 会话中，支持中断恢复、取消传播和运行清理。

在 agent 工具编排层面，本方案设计了父子双端注册表机制：父端维护 cf_agent_tool_runs 表记录每次子 agent 调用的运行元数据（运行标识、状态、输入预览、输出摘要、显示顺序等），子端维护 cf_agent_tool_child_runs 映射表将运行标识关联到具体的请求和流标识。父子两端通过 agent-tool-event 协议通信，包含 started/chunk/finished/error/aborted/interrupted 六种结构化事件，每个事件携带父工具调用标识、运行标识和序列号，支持客户端按序渲染、去重和断线重连后的精确重放。系统同时支持三种调用模式：LLM 通过函数调用自主选择子 agent（模型驱动）、服务端通过 runAgentTool API 确定性调用（命令式）、以及无父工具调用的后台运行。

### 整体架构

整体架构分为服务端和客户端两层。服务端基于 Durable Object 运行时，每个 agent 实例是一个持久化对象。父 agent 通过 ctx.facets 创建子 agent，每个子 agent 作为独立的 Facet 运行在同一 Durable Object 实例内，拥有独立的 SQLite 存储空间，与父 agent 的存储完全隔离。父子 agent 之间通过框架提供的 SubAgentStub 类型化 RPC 桩进行方法调用，调用在同一个 JavaScript 隔离区内执行，不经过网络序列化。

客户端通过 WebSocket 连接到父 agent，父 agent 在单个会话连接上多路复用所有子 agent 的输出。子 agent 的输出以结构化事件的形式通过父 agent 的会话通道推送到客户端。客户端维护 runsById 和 runsByToolCallId 两个索引，以支持按运行标识和按工具调用标识两种查询模式。父子 agent 之间的祖先链通过 parentPath 和 selfPath 维护，子 agent 可通过 parentAgent(Cls) 方法沿链向上查找任意祖先类型的父节点。

### 子 agent 创建与管理

子 agent 通过 ctx.facets 机制创建。父 agent 调用 subAgent(ChildClass, name) 方法，框架在父 agent 的 Durable Object 实例内创建一个新的 Facet 作为子 agent，返回 SubAgentStub 类型 RPC 桩。子 agent 拥有独立的 SQLite 存储，其表空间与父 agent 完全隔离，互不干扰。每个子 agent 通过唯一的 name 标识，在同一父 agent 下 name 不可重复。

父 agent 维护 cf_agent_sub_agents 注册表，记录所有已创建的子 agent 的类名和名称。该注册表支持 hasSubAgent(Cls, name) 存在性查询和 listSubAgents() 列表查询。子 agent 的生命周期与父 agent 绑定：父 agent 被销毁时，所有子 agent 也被级联删除。父 agent 可通过 deleteSubAgent(Cls, name) 主动删除指定子 agent，同时清理其存储和注册表项。

### 外部寻址与流式路由

每个子 agent 拥有层级化的外部 URL 寻址路径：/agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。该路径完整编码了父子关系链，使得客户端无需额外的服务发现即可直接定位到任意深度的子 agent。在父 agent 的路由层，onBeforeSubAgent 中间件在请求到达子 agent 之前执行门禁检查，验证子 agent 是否存在于 cf_agent_sub_agents 注册表中，以及请求方是否具备访问权限。

父子 agent 之间通过 parentPath 和 selfPath 维护完整的祖先链。parentPath 记录从根 agent 到当前 agent 的所有祖先引用，selfPath 在 parentPath 基础上追加当前 agent 自身。子 agent 通过 parentAgent(Cls) 方法，按类型沿祖先链向上查找最近的匹配父节点，实现跨层级的类型安全引用。该机制支持任意深度的嵌套，父 agent 可以是另一个更上层 agent 的子 agent。

### agent 工具编排系统

agent 工具编排系统是本方案的核心机制，它将子 agent 包装为父 agent 可调用的工具，支持 LLM 自主选择和程序化确定性调用两种方式。系统由父端运行注册表、子端运行映射表、结构化事件协议、恢复/取消/清理机制和并发控制五个部分组成，以下分别阐述。

系统提供两种子 agent 调用入口。其一是 agentTool(Cls, { description, inputSchema, outputSchema })，它将子 agent 包装为 LLM 可识别的工具定义，LLM 在对话中根据 description 和 schema 自主决定是否调用、何时调用以及传入什么参数。其二是 runAgentTool(Cls, { input, parentToolCallId, displayOrder, signal })，它允许服务端代码以命令式方式直接调用子 agent，适用于预设工作流和确定性编排场景。当 parentToolCallId 为空时，该运行不绑定任何父工具调用，在客户端以 unboundRuns 独立展示，实现后台异步运行。

### 编排——父端运行注册表

父 agent 维护 cf_agent_tool_runs 注册表，记录每次子 agent 调用的完整运行元数据。每条记录包含以下字段：run_id（运行唯一标识）、parent_tool_call_id（触发该运行的父工具调用标识，命令式调用时可空）、agent_type（子 agent 类名）、status（运行状态，取值为 starting/running/completed/error/aborted/interrupted）、input_preview（输入摘要，用于 UI 展示）、summary（输出摘要）、error_message（错误信息）、display_order（显示顺序）、started_at / completed_at（起止时间戳）、stream_id（关联的流标识）。注册表支持按 parent_tool_call_id 查询关联运行、按状态过滤、按 display_order 排序。当 LLM 发起工具调用时，父 agent 在注册表中创建一条状态为 starting 的记录，在子 agent 实际开始执行时更新为 running，执行结束后更新为 completed 或 error。

### 编排——子端运行映射表

子 agent 端维护 cf_agent_tool_child_runs 映射表，结构为 run_id → {request_id, stream_id}。当父 agent 通过 runAgentTool 向子 agent 发起运行时，子端在映射表中记录该运行对应的内部请求标识和流标识。此映射表用于恢复场景：父端崩溃重启后，通过子端的 inspectAgentToolRun(run_id) 接口查询运行的真实状态，获取已存储的流标识，从而重放已输出的内容。

### 编排——事件协议

父子 agent 之间通过 agent-tool-event 结构化事件协议通信，定义六种事件类型：started 表示子 agent 开始执行，携带 run_id 和 parent_tool_call_id；chunk 表示流式输出片段，其 body 为 JSON 编码的 UIMessageChunk，客户端用 applyChunkToParts 重建消息部件，同时携带 sequence 序号用于去重和排序；finished 表示正常完成，携带 summary 摘要；error 表示执行出错，携带 error_message；aborted 表示被主动取消；interrupted 表示因父端崩溃等异常中断。每个事件均包含 parent_tool_call_id、run_id 和 sequence 三元组，客户端按 (parent_tool_call_id, run_id, sequence) 进行幂等去重，确保断线重连后的事件重放不会产生重复内容。

### 编排——恢复机制

父 agent 崩溃后重新启动时，对 cf_agent_tool_runs 注册表中所有状态为 starting 或 running 的记录执行和解（reconcile）流程：逐条向对应子 agent 的 inspectAgentToolRun 接口查询运行的真实状态。若子端返回 completed，则将父端记录更新为 completed 并重放已存储的 chunks；若子端返回 error 或 aborted，则同步更新父端状态；若子端返回 running 且框架版本为 V1，则标记为 interrupted。重放时使用存储的 stream_id 读取历史 chunks，按 sequence 排序后通过 WebSocket 推送到客户端，客户端按三元组去重。

### 编排——取消与清理

取消机制通过 AbortSignal 链式传播实现。父 agent 调用 runAgentTool 时传入 AbortSignal，当用户或系统触发取消时，signal 被标记为 aborted。子 agent 在每个 turn 的边界检查 signal.aborted，若已取消则通过 saveMessages({ signal }) 将取消状态持久化，并停止继续执行。子 agent 的每轮执行（per-turn）内部维护独立的 AbortController，与父端 signal 级联。取消后，子 agent 在注册表中的状态更新为 aborted。

清理机制通过 clearAgentToolRuns(olderThan?, status?) 方法实现。该方法接受时间阈值和状态过滤器作为参数，删除符合条件的注册表记录，并同步删除对应的子 agent facet 实例以释放存储资源。清理支持按状态选择性清理（如仅清理 aborted 和 error 状态的运行），也支持按时间保留最近的运行记录。oldThan 参数确保正在进行的运行不被误删。

### 编排——并发控制与生命周期

系统通过 maxConcurrentAgentTools 配置项限制同时运行的 agent tool 数量。当 LLM 在一次响应中发起多个工具调用时，若当前正在运行的 agent tool 数量已达上限，后续调用进入等待队列，待正在运行的完成后再启动。此机制防止子 agent 数量膨胀导致的资源耗尽。并发控制作用于父 agent 级别，即每个父 agent 实例独立计算其当前并发数。

此外，系统提供 AgentToolLifecycleHooks 生命周期钩子（onAgentToolStart / onAgentToolFinish），允许在子 agent 运行开始和结束时执行自定义逻辑，如日志记录、指标采集或通知推送。

### 客户端集成与流式渲染

客户端通过 useAgentToolEvents hook 订阅 parent agent WebSocket 上的 agent-tool-event 流。该 hook 内部使用 applyAgentToolEvent reducer 管理两个索引结构：runsById（按 run_id 索引所有运行的当前状态和已接收的 chunks）和 runsByToolCallId（按 parent_tool_call_id 索引关联的运行列表）。当收到 started 事件时创建运行条目，收到 chunk 事件时按 sequence 追加并排序，收到 finished/error/aborted/interrupted 事件时更新终结状态。

对于没有 parent_tool_call_id 的命令式运行（通过 runAgentTool 直接调用），客户端将其归入 unboundRuns 集合，以 run_id 为键独立管理。流式重放时，客户端对每个 chunk 以 (parent_tool_call_id, run_id, sequence) 三元组去重，确保在 onConnect 重连时重放历史事件不会产生重复 UI 内容。chunk.body 为 JSON 编码的 UIMessageChunk，客户端使用 applyChunkToParts 将其解码并增量重建消息部件，实现流式打字效果。

### 访问控制与安全边界

系统通过两层机制实现访问控制。第一层是 onBeforeSubAgent 路由中间件：当外部请求通过 /agents/{parent}/sub/{child}/{name} 路径访问子 agent 时，中间件在请求到达子 agent 之前执行，检查目标子 agent 是否在父 agent 的 cf_agent_sub_agents 注册表中存在。若不存在，请求被拒绝。第二层是 drill-in 访问控制：客户端通过 useAgent({ sub: [...] }) 声明需要访问的子 agent 列表，父 agent 验证请求方是否有权访问指定的子 agent。

子 agent 的 parentPath 祖先链也参与访问控制：子 agent 可通过 parentAgent(Cls) 按类型查找祖先，获取祖先的配置和权限上下文，从而在自身逻辑中实施细粒度授权。此设计确保即使子 agent 被外部 URL 直接寻址，访问仍受父 agent 的门禁和祖先链约束，不会产生越权访问。

### 技术效果

本方案的技术效果体现在以下几个方面：（1）状态一致性：父子双端注册表加事件协议的幂等去重设计，确保在崩溃恢复、断线重连等场景下客户端视图与服务器状态最终一致。（2）实时可观测：流式事件协议使子 agent 的执行进度、中间输出和最终结果实时回流到同一会话视图，用户无需切换上下文即可观察所有子 agent 的并行工作状态。（3）资源隔离与安全：每个子 agent 拥有独立 SQLite 存储，父子 agent 之间及子 agent 之间数据完全隔离；外部寻址通过中间件门禁和祖先链实现访问控制。（4）灵活的调用模式：同时支持 LLM 自主选择、服务端命令式调用和后台运行三种模式，适应不同业务场景。（5）弹性与可恢复性：基于 Durable Object 的持久化特性，父子 agent 的状态在崩溃后可精确恢复，已输出的内容通过重放机制不丢失。（6）并发安全：maxConcurrentAgentTools 限制和 per-turn AbortController 隔离确保并发场景下资源可控、取消传播可靠。

### 风险与待确认问题

当前方案存在以下边界条件和待确认点：（1）子 agent 与父 agent 运行在同一 Durable Object 实例内，受限于单实例的 CPU 和内存上限，大量并发子 agent 可能触发资源瓶颈，跨实例分布式子 agent 调用机制尚未在设计中覆盖。（2）嵌套深度增加时，祖先链 parentPath 的序列化开销和寻址 URL 长度线性增长，深嵌套场景下的性能特征待验证。（3）长时间运行的子 agent 可能超出 Durable Object 的单次执行时限，需确认框架层面的续期或分段执行策略。（4）多个子 agent 并行输出大量 chunk 事件时，客户端渲染性能和事件缓冲区管理需要进一步评估。（5）子 agent 的输出作为 LLM 工具调用结果注入父 agent 上下文窗口时，长输出的截断策略和上下文预算管理待明确。
