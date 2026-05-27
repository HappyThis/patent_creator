## 技术方案

本技术方案提出一种基于父子 Agent 结构的多会话助理系统。系统引入一个用户级父 Agent（Assistant）作为共享资源的管理实体，以及多个会话级子 Agent（Chat）作为独立会话执行上下文。父 Agent 集中持有 Workspace 文件系统、MCP 服务器连接、OAuth 授权凭据和文件变更通知通道等用户级共享资源；子 Agent 维护各自独立的消息历史、分支树、会话配置和上下文块等会话级隔离状态，并通过受控代理机制访问父 Agent 上的共享资源。

### 整体架构

系统采用两层 Agent 拓扑：一个 Assistant（用户级父 Agent）管理零至多个 Chat（会话级子 Agent）。Assistant 作为用户维度的持久化 Durable Object，持有用户全局状态；每个 Chat 是一个独立 Durable Object，通过 Durable Object Facet 机制挂载为 Assistant 的子 Agent。客户端通过嵌套 URL 路由直接连接到目标 Chat，WebSocket 升级后的帧直接路由到子 Agent，父 Agent 仅在连接建立时参与鉴权和路由决策。

路由 URL 采用 /agents/{assistant-class}/{user-id}/sub/{chat-class}/{chat-id} 格式。客户端通过 useAgent({ agent: "assistant", name: userId, sub: [{ agent: "chat", name: chatId }] }) 构造连接，框架自动解析嵌套路径并完成父到子的请求转发。Assistant 的 onBeforeSubAgent 钩子在每次连接建立时执行，可在此进行会话存在性校验和访问控制。

### 用户级父 Agent（Assistant）

Assistant 是继承自 Agent 基类的用户级父 Agent，一个 Assistant 实例对应一个用户。其核心职责是持有和管理所有跨会话共享资源，不直接参与单会话的 LLM 推理。Assistant 在 onStart 生命周期中初始化以下组件：

- Workspace 文件系统实例：基于 SQLite + R2 的虚拟文件系统，使用用户级命名空间。所有子会话通过受控代理读写同一文件树。Workspace 的 onChange 回调接入 Agent 的广播机制，文件变更时向所有已连接客户端推送增量通知。
- MCP 客户端管理器（MCPClientManager）：管理所有外部 MCP 服务器连接及其 OAuth 授权状态。连接建立、工具发现、令牌刷新和连接恢复均在父 Agent 的生命周期内执行。子会话通过 RPC 代理调用工具，不持有原始连接对象。
- OAuth 凭据存储（DurableObjectOAuthClientProvider）：将 OAuth 令牌、客户端信息和状态 nonce 持久化在父 Agent 的 Durable Object Storage 中。一次授权后，所有子会话共享同一令牌。
- 会话索引表（cf_agents_chats_index）：SQLite 表，记录所有已创建 Chat 的 ID、标题、创建时间、更新时间、最后消息预览和软删除标记。通过 FTS5 全文索引支持标题搜索。
- 共享上下文块（Shared Context）：跨会话长期记忆，如用户偏好、项目约定等。存储在父 Agent SQLite 中，子会话通过 RemoteContextProvider 以 RPC 方式读取和追加。
- 定时任务调度：父 Agent 利用 Agent 基类的 schedule() 方法注册跨会话的定时任务（如每日摘要、过期会话清理），由 Durable Object Alarm 机制触发执行。

### 会话级子 Agent（Chat）

Chat 是继承自 Think 基类的会话级子 Agent，每个 Chat 实例对应一个独立的聊天会话。Chat 作为 Assistant 的子 Agent（通过 ctx.facets 创建），拥有独立的 SQLite 数据库，维护以下会话隔离状态：

- 消息历史与会话树（Session）：Think 的 Session 模块在 Chat 自有的 SQLite 中维护树形消息结构（parent_id 链）、分支（regeneration 时创建兄弟节点）和压缩叠加层（compaction overlays）。不同 Chat 的消息完全物理隔离。
- 会话配置：每个 Chat 通过 Think 的 configureSession 方法独立定义系统提示、上下文块（本会话内的短期记忆）、压缩阈值和工具集。配置存储在各自的 think_config 表中。
- LLM 推理上下文：每个 Chat 的 assembleContext 方法组装本会话的冻结系统提示和消息历史，独立调用 LLM。不同 Chat 的推理互不干扰，天然并行——因为它们运行在不同的 Durable Object 实例上。
- 子会话工具集：Chat 的 getTools 方法返回本会话特有的工具（如会话内搜索、上下文更新），再加上通过受控代理从父 Agent 获取的共享工具（MCP 工具、工作区文件工具等）。

### 共享 Workspace 代理机制

Workspace 文件系统实例仅存在于父 Agent 中。子会话通过以下两种受控通道访问共享文件，不直接持有 Workspace 实例引用：

方式一：通过 parentAgent 获取父 Agent 的 RPC 桩，调用父 Agent 上暴露的 @callable 方法（如 readSharedFile、writeSharedFile、listSharedDir、globShared）。这些方法在父 Agent 内部操作 Workspace 实例，返回结果给子会话。

方式二：将 Workspace 操作封装为 MCP 工具。子会话通过共享 MCP 代理（见下一节）调用文件读写工具，无需感知底层 Workspace API。LLM 在子会话中直接使用 list_directory、read_file、write_file 等工具，工具执行在父 Agent 侧完成。

两种方式均保证文件操作发生在父 Agent 的隔离环境中。子会话不能绕过 Agent 生命周期直接执行原始文件 I/O，也不能访问其他用户的 Workspace。Workspace 的 onChange 回调在每次写入、删除或目录变更时触发，父 Agent 通过 this.broadcast() 将变更事件（路径、操作类型、条目类型）推送到所有已连接的 WebSocket 客户端，实现多标签页的实时文件同步。

### 共享 MCP 连接代理机制

MCP 服务器连接在父 Agent 中通过 MCPClientManager 集中管理。父 Agent 的 onStart 中调用 mcp.restoreConnectionsFromStorage 恢复持久化的连接，并注册 onServerStateChanged 回调以在连接状态变更时广播给客户端。

子会话通过以下受控路径使用 MCP 工具：（1）父 Agent 暴露一个 @callable 方法 getSharedMcpTools，子会话通过 this.parentAgent(Assistant).getSharedMcpTools() 调用，返回当前所有已连接 MCP 服务器的工具列表及其输入模式；（2）父 Agent 暴露 callSharedMcpTool(serverId, toolName, args) 方法，子会话的工具调用经此 RPC 方法代理到实际 MCP 连接；（3）工具执行结果和错误通过 RPC 返回值返回给子会话，子会话将其注入 LLM 推理循环。

关键约束：子会话不直接持有 MCPClientConnection 实例或底层传输对象（SSEClientTransport、StreamableHTTPClientTransport）。MCP 连接的完整生命周期——包括传输建立、协议握手、能力发现、工具/资源/提示列表拉取、断线重连、OAuth 令牌刷新——均在父 Agent 的 MCPConnectionState 状态机中管理。子会话仅通过 RPC 接口获取工具列表快照和发起工具调用。这保证了：（1）MCP 服务器只需维护一条到父 Agent 的连接，不因子会话数量倍增；（2）OAuth 授权一次后所有子会话自动可用；（3）连接故障恢复由父 Agent 统一处理，子会话不受影响。

### 共享 OAuth 授权机制

OAuth 授权凭据存储在父 Agent 的 DurableObjectStorage 中，使用 DurableObjectOAuthClientProvider 实现。该 Provider 利用 DO Storage 的持久化能力存储 OAuth 客户端信息、访问令牌和刷新令牌，键名前缀为 /{clientName}/{serverId}/{clientId}/，天然按服务器隔离。

OAuth 授权流程为：（1）用户通过任意 Chat 子会话触发 MCP 服务器注册，指定 authUrl；（2）父 Agent 创建 DurableObjectOAuthClientProvider 实例，生成 state nonce（含 10 分钟过期时间），启动 OAuth 授权流程，返回授权 URL 给客户端；（3）用户在浏览器中完成授权，回调到父 Agent 的 OAuth 回调端点；（4）父 Agent 校验 state nonce 有效性，通过授权码换取令牌，持久化到 DO Storage；（5）MCPClientManager 检测到授权完成，自动建立 MCP 连接；（6）所有子会话的后续工具调用自动使用已授权的连接。令牌刷新由 MCPClientManager 在父 Agent 中自动执行，子会话无感知。

### 访问控制机制

系统通过父 Agent 的 onBeforeSubAgent 钩子和子 Agent 注册表实现三层访问控制，防止会话 ID 猜测、越权访问和未授权资源操作。

第一层——子 Agent 存在性校验：父 Agent 重写 onBeforeSubAgent 钩子，在每次客户端请求路由到子 Agent 前，查询 cf_agents_sub_agents 注册表检查目标子 Agent 是否已登记。未登记的会话 ID 返回 404 响应，阻止通过猜测 ID 访问不存在的会话。结合 useAgent 客户端的 4xx 终端重试策略，猜测失败的客户端连接被永久终止而非无限重试。

第二层——用户隔离：父 Agent 的 DO 实例名即为用户标识（如用户 ID），不同用户的 Assistant 运行在不同的 DO 实例上，物理隔离各自的子 Agent 树、Workspace 和 MCP 连接。客户端路由 URL 中的父 Agent 名称由鉴权系统注入（如从 JWT 中提取用户 ID 作为父 Agent 的 DO 名称），客户端无法伪造其他用户的父 Agent 实例名。

第三层——共享资源操作的只读/读写控制：父 Agent 的共享资源 @callable 方法在实现中区分操作类型。对于只读操作（列出工具、读取文件、获取共享上下文），直接执行并返回结果。对于写操作（写入文件、删除文件、修改共享上下文），可结合 Agent 框架的 readonly connections 机制，通过 shouldConnectionBeReadonly 钩子对特定连接标记只读，框架自动在 setState 和客户端状态写入路径上拦截。

### 实时文件同步机制

系统利用 Agent 框架的广播机制实现文件变化的实时多标签页同步，避免客户端轮询。

实现方案为：（1）父 Agent 在初始化 Workspace 时传入 onChange 回调函数；（2）Workspace 在每次文件/目录的创建、更新、删除操作后调用 emit 方法触发 onChange，传递包含操作类型（create/update/delete）、路径和条目类型的 WorkspaceChangeEvent；（3）onChange 回调调用父 Agent 的 this.broadcast() 方法，向所有已连接的 WebSocket 客户端发送 CF_AGENT_STATE 类型消息，消息负载中包含变更文件和操作类型；（4）客户端 useAgent 钩子接收到状态变更后触发 React 重新渲染，前端文件树组件根据变更类型增量更新显示，无需全量刷新。

同步范围说明：文件变更广播到连接到同一父 Agent 的所有 WebSocket 客户端，无论客户端当前活跃的子会话是哪个。这意味着用户在 Chat A 中通过 Agent 修改了文件，打开 Chat B 的另一个浏览器标签页也会实时看到文件树更新。Chat 子 Agent 自身不参与广播——文件状态由父 Agent 集中管理，广播由父 Agent 统一发出。

### 定时任务调度机制

跨会话的定时任务（如每日对话摘要、过期会话清理、共享上下文整理）由父 Agent 的内置调度机制统一管理，而非分散到各个子会话中。

实现方案基于 Agent 基类已有的 schedule 方法和 cf_agents_schedules 表。父 Agent 在 onStart 中注册定时任务：（1）使用 this.schedule({ type: 'cron', cron: '0 6 * * *' }, 'daily-summary', { idempotent: true }) 注册每日摘要任务（早 6 点执行）；（2）使用 this.schedule({ type: 'interval', intervalSeconds: 3600 }, 'cleanup-expired', { idempotent: true }) 注册每小时过期清理任务。idempotent: true 确保 onStart 多次执行（DO 唤醒/迁移）不会创建重复的调度条目——框架通过回调名去重。

定时回调执行时，父 Agent 通过 Durable Object Alarm 机制被唤醒。回调方法（如 dailySummary、cleanupExpired）在父 Agent 的上下文中执行，可以：（1）遍历 cf_agents_sub_agents 注册表获取所有子 Agent 列表；（2）通过 this.subAgent(ChatClass, chatId) 逐一获取子 Agent 的 RPC 桩；（3）调用子 Agent 的 @callable 方法（如 chat.getHistory、chat.getConfig）收集信息；（4）调用 LLM 生成摘要并存入共享上下文块。框架的 schedule 方法自动将任务信息持久化到 cf_agents_schedules 表，支持 scheduled（定时）、delayed（延迟）、cron（周期）和 interval（间隔）四种调度类型，并内置重试机制。

### 与现有基础设施的集成

本方案通过复用现有框架原语实现，不引入新的基础设施概念。

- Agent 基类的 subAgent / deleteSubAgent / hasSubAgent / listSubAgents / parentAgent / parentPath 全套子 Agent 管理原语，直接用于构建父子拓扑。
- onBeforeSubAgent 中间件钩子，用于子会话访问控制和存在性校验。
- sub-agent routing 嵌套 URL 路由机制（/agents/parent/name/sub/child/name），客户端通过 useAgent({ sub: [...] }) 数组形式声明目标链。
- Think 的 Session 模块提供树形消息存储、分支、压缩、上下文块和 FTS5 全文搜索，直接作为 Chat 子 Agent 的会话存储层。
- MCPClientManager + DurableObjectOAuthClientProvider 提供完整的 MCP 连接管理和 OAuth 授权生命周期，集中在父 Agent 中运行。
- Workspace 的 onChange 回调 + Agent 的 broadcast 方法组合实现文件变更通知。
- Agent 的 schedule 方法 + cf_agents_schedules 表提供定时任务调度和 Durable Object Alarm 触发。
- Agent 的 readonly connections 机制提供连接级只读控制。
- TurnQueue、ResumableStream、StreamAccumulator 等 chat 共享层模块在 Chat 子 Agent 中继续使用，不受父 Agent 架构影响。

### 风险与待确认问题

以下为实施中需关注的风险点和待确认问题：

- 父 Agent 成为单点瓶颈：由于 Durable Object 单线程执行，所有子会话的共享资源访问（MCP 工具调用、文件读写）都经过父 Agent 的 RPC 方法。对于高并发场景，父 Agent 可能成为吞吐瓶颈。缓解措施：（1）共享内存读操作使用快照模式，子会话缓存结果；（2）MCP 连接可考虑按需从父 Agent 迁移到独立 DO 的连接池模式。
- MCP 连接数量限制：当前架构下所有 MCP 服务器连接集中在父 Agent 一个 DO 实例上。如果用户注册了大量 MCP 服务器（如 50+），连接建立和保活的开销将集中在单一 DO 上。需评估实际场景中 MCP 服务器的典型数量。
- 子会话间共享内存的并发写入：多个子会话同时更新共享上下文块时，可能发生读-修改-写丢失。当前缓解策略为提供 appendSharedContext 原子追加操作（additive write），避免全量替换。对于需要强一致性的场景，需引入乐观锁或版本号机制。
- 跨会话全文搜索的性能：searchMessages 需要对所有子会话进行扇出 RPC 调用，子会话数量增长时延迟线性增加。当前建议适用上限约为 50 个子会话，超出后需引入父 Agent 侧的聚合 FTS 索引。
- Workspace 大文件的并发写入：Workspace 本身无文件锁机制，多个子会话并发写入同一文件时为 last-write-wins。对于协作编辑场景，需在应用层引入 Operational Transformation 或 CRDT 机制。
