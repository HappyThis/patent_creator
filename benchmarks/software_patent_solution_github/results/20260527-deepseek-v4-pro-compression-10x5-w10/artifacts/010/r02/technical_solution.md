## 技术方案

### 整体架构：用户级父实体与会话级子实体的双层 Agent 结构

本方案在现有 Durable Object（DO）风格 Agent 框架基础上，引入用户级父 Agent（UserAgent）与会话级子 Agent（ChatAgent）的双层结构。UserAgent 是每个用户账号对应的单一 DO 实例，负责管理该用户的所有聊天会话的索引、共享资源的真实状态以及全局定时任务。ChatAgent 是每个聊天会话对应的独立 DO 实例，作为 UserAgent 的子 Agent（通过 subAgent 机制创建），负责维护该会话独立的消息历史、分支、上下文块和会话配置。

### 父实体（UserAgent）的职责边界

UserAgent 和 ChatAgent 之间通过 Durable Object RPC 进行通信。客户端通过现有的子 Agent 路由机制（URL 形如 /agents/{user-agent}/sub/{chat-agent}/{chat-id}）直接连接到 ChatAgent 的 WebSocket，聊天消息和流式响应不经过 UserAgent 中转。UserAgent 仅在连接建立时的 onBeforeSubAgent 钩子中进行访问控制和路由分发，之后 WebSocket 帧直接在客户端与 ChatAgent 之间传输。

用户级父实体（UserAgent）承担以下职责：聊天会话生命周期管理、共享资源状态持有和受控访问代理、跨会话定时任务调度、以及向所有子会话广播资源变更通知。每个 UserAgent 实例对应于一个用户账号，内部维护一个 SQLite 持久化的聊天会话索引表。

聊天会话索引表记录每个 ChatAgent 子实例的唯一标识、标题、创建时间、最后活跃时间和最后消息预览。UserAgent 对外暴露 createChat、deleteChat、renameChat、listChats 和 searchChats 等 callable 方法，客户端通过 RPC 调用这些方法管理会话列表。deleteChat 采用软删除策略（标记 deleted_at 时间戳），避免在子会话尚有进行中写入时立即销毁 DO 实例。

共享资源方面，UserAgent 持有 Workspace 文件系统的唯一实例（基于 DO SQLite，大文件溢出到 R2）、MCP 服务器连接池和 OAuth 授权凭据的持久化存储。这些资源在 UserAgent 的 onStart 生命周期中初始化，子会话不直接持有这些资源的独立副本，而是通过远程代理（Remote Proxy）访问 UserAgent 上的真实资源。

UserAgent 还负责全局定时任务的调度。利用 Durable Object 的 Alarm 机制和框架的 schedule/scheduleEvery API，UserAgent 可以执行跨会话摘要生成、定时数据清理、定期通知等全局任务。这些任务在 UserAgent 的 SQLite 中持久化调度记录，确保 DO 休眠后能被 Alarm 唤醒并继续执行。

### 子实体（ChatAgent）的职责与会话隔离

每个 ChatAgent 是独立的 DO 实例，继承自 Think 基类，拥有自己的 Session 存储、消息树、分支、上下文块（Context Blocks）和压缩（Compaction）状态。会话之间完全隔离以下状态：

- 消息历史：每个 ChatAgent 通过 Session 的 getHistory 读取自己的树形消息结构，不同会话的消息互不可见。
- 会话配置：ChatAgent 通过 configure/getConfig 管理实例级配置（如模型选择、温度参数），不同会话独立。
- 上下文块与记忆：Session 的 Context Blocks（如 memory、soul）在会话级别独立存储和压缩。
- 推理循环与 Turn 队列：每个 ChatAgent 拥有独立的 TurnQueue，确保同一会话内的请求按序处理，不同会话的推理并行执行。

ChatAgent 通过 this.parentAgent(UserAgent) 方法获取对父实体的类型化引用（基于 Agent 框架的 parentAgent 单跳查找原语和 DO RPC）。子会话不直接持有 Workspace 文件系统实例或 MCP 客户端连接管理器，而是通过受控代理访问这些共享资源。

### 共享资源的受控代理访问机制

为确保子会话对共享资源的访问受控且不绕过 Agent 生命周期，本方案设计了三种远程代理机制：Workspace 代理、MCP 工具代理和共享上下文代理。所有代理均通过 DO RPC（callable 方法）在 UserAgent 端执行实际操作，子会话端仅持有轻量代理对象。

Workspace 代理：UserAgent 在 onStart 中初始化 Workspace 实例（基于 DO SQLite，大文件溢出到 R2），并暴露读文件、写文件、列目录、删除文件、文件搜索等 callable 方法。ChatAgent 通过 RemoteWorkspaceProxy 对象，将文件操作请求转为对 UserAgent 的 RPC 调用。代理对象实现与本地 Workspace 相同的接口，但在 ChatAgent 端不持有任何 SQLite 连接或文件内容缓存；每次读写在 UserAgent 端执行并通过 SQLite 的串行化写保证一致性。文件变更时，Workspace 的 onChange 回调触发 UserAgent 广播文件变更事件给所有已连接的 ChatAgent WebSocket 客户端。

MCP 工具代理：UserAgent 负责 MCP 服务器的连接生命周期管理（addMcpServer、removeMcpServer、OAuth 授权流程），MCP 客户端连接和 OAuth 令牌持久化在 UserAgent 的 SQLite 中。UserAgent 暴露 executeMcpTool 的 callable 方法，接收工具名称和参数，在 UserAgent 端通过 MCPClientManager 执行实际的工具调用。ChatAgent 的 getTools 方法不再直接调用 this.mcp.getAITools()，而是从 UserAgent 获取可用工具列表的代理描述，生成包装后的 ToolSet。当推理引擎决定调用某个 MCP 工具时，实际执行路由到 UserAgent 端完成，确保浏览器或子会话无法绕过 Agent 生命周期直接执行原始 MCP 工具调用。

共享上下文代理：基于已有的 RemoteContextProvider 和 RemoteSearchProvider 模式（定义于 agents/experimental/memory/session/providers/remote.ts）。UserAgent 暴露 getSharedContext、setSharedContext、appendSharedContext 等 callable 方法，ChatAgent 在 configureSession 中使用 RemoteContextProvider 指向 UserAgent 上的共享上下文块（如 user_memory）。每个 ChatAgent 的 Session 在冻结系统提示词时，通过 RPC 获取远程上下文内容并注入本地提示词缓存中。写入操作通过 appendSharedContext（追加）或 setSharedContext（替换）完成，避免子会话之间的读-修改-写竞态。

### 访问控制与会话 ID 安全

为防止用户通过猜测会话 ID 访问或创建未授权的子会话，本方案在子 Agent 路由层实现了多层访问控制。

第一层：UserAgent 的 onBeforeSubAgent 钩子。当客户端请求连接到 /agents/{user-agent}/sub/{chat-agent}/{chat-id} 时，框架在建立到子 Agent 的 WebSocket 连接之前，先在 UserAgent 上调用 onBeforeSubAgent。UserAgent 在此钩子中执行：验证请求是否来自认证用户（通过 cookie 或 token 检查），检查 chat-id 是否在聊天会话索引表中存在（通过 hasSubAgent），以及该会话是否已被删除。如果任一检查失败，直接返回 404 或 403 响应，不唤醒子 Agent。

第二层：会话创建必须通过 UserAgent 的 createChat callable 方法，该方法在 UserAgent 端生成会话 ID（crypto.randomUUID()），在索引表中插入记录，并广播更新后的会话列表。客户端不能自行指定会话 ID，也不能绕过 UserAgent 直接实例化 ChatAgent DO。这确保了所有活跃会话都在 UserAgent 的索引中有明确记录。

第三层：删除会话时的安全处理。当 UserAgent 的 deleteChat 被调用时，采用软删除策略：在索引表中设置 deleted_at 时间戳并广播更新后的会话列表。已连接的客户端收到更新后主动断开。在软删除窗口期内，该会话 ID 的重复创建会清理旧行并分配新的生成计数器，防止基于旧 ID 的重放攻击。子 Agent DO 在无活跃连接后自然休眠并被 GC，避免强制销毁导致进行中的数据写入丢失。

### 实时同步与多标签页文件变更通知机制

文件变更实时同步是本方案的关键技术特征。当任意 ChatAgent 通过 Workspace 代理写入文件时，实际写入在 UserAgent 端执行。Workspace 的 onChange 回调捕获此次变更（包含操作类型、文件路径、时间戳），UserAgent 随即通过两种机制通知所有相关客户端。

第一种机制：UserAgent 直接向其自身的 WebSocket 连接广播文件变更事件。客户端维护一个指向 UserAgent 的轻量 WebSocket 连接（用于接收全局通知，不承载聊天消息），当收到文件变更事件后，前端根据事件中的文件路径决定是否刷新文件列表或重新加载文件内容。这覆盖了侧边栏、文件浏览器等不特定于某个聊天会话的 UI 组件。

第二种机制：UserAgent 向所有活跃 ChatAgent 子实例发送文件变更 RPC 通知。每个 ChatAgent 在收到通知后，通过自身的 broadcast 方法将文件变更事件转发给其已连接的 WebSocket 客户端。这确保在某个聊天会话中打开的文件标签页能及时感知到来自其他会话的文件修改。为了减少不必要的唤醒，UserAgent 维护一个活跃子会话列表（有活跃 WebSocket 连接的 ChatAgent 在连接建立和关闭时向 UserAgent 注册/注销），仅向活跃子会话发送通知。

MCP 服务器状态变更的同步采用类似机制。当 UserAgent 上的 MCP 连接状态发生变化（新服务器连接成功、OAuth 授权完成、服务器断开），UserAgent 通过 broadcastMcpServers 协议消息通知所有已连接的客户端，客户端据此更新工具面板。同时，通过活跃子会话列表向各 ChatAgent 发送 MCP 工具列表变更通知，ChatAgent 在下一轮推理前刷新其工具集合。

### 定时任务与跨会话调度机制

定时任务和跨会话摘要等全局任务由 UserAgent 统一调度，不散落在各个 ChatAgent 子实例中。这利用了 DO Alarm 机制和框架的 schedule/scheduleEvery API。

调度存储：所有定时任务定义（任务名称、触发条件、回调方法名、参数载荷、重试选项）持久化在 UserAgent 的 SQLite 调度表中（cf_agents_schedules），与框架的调度基础设施完全兼容。UserAgent 的 alarm 方法在每次调度执行后重新计算下一次触发时间并设置 DO Alarm。

跨会话摘要：UserAgent 定义 generateDailySummary 回调方法。当定时触发时，该方法遍历聊天会话索引表中的活跃会话，通过 RPC 调用各 ChatAgent 的 session.getHistory 获取最近的对话摘要，聚合后写入 UserAgent 的共享上下文块中。UserAgent 还可以利用自身与 LLM 的集成能力，调用模型对聚合内容进行二次提炼。

闲时清理：UserAgent 定期扫描聊天会话索引表中软删除超过保留期的记录，清理对应的子 Agent DO 引用，并释放索引表空间。同时清理过期的 ResumableStream 缓存块和超时的 MCP OAuth 等待状态。

### 与现有系统的复用关系

本方案最大程度复用现有 Agent 框架的成熟基础设施，不引入新的底层存储、通信或调度机制。

Agent 与 DO 基础设施：UserAgent 和 ChatAgent 均继承自 Agent 基类，直接复用 SQLite 持久化、DO Alarm、WebSocket 连接管理、callable RPC、subAgent 子 Agent 管理、broadcast 广播和 state 状态管理等框架原语。ChatAgent 进一步继承 Think 基类，复用其 Session 存储、TurnQueue 推理队列、ResumableStream 可恢复流、StreamAccumulator 流式消息构建、sanitizeMessage 消息清洗和 chatRecovery 持久执行等能力。

子 Agent 路由：直接复用 rfc-sub-agent-routing 中定义的嵌套 URL 路由（/agents/{parent}/sub/{child}/{name}）、onBeforeSubAgent 中间件钩子、parentAgent 单跳查找、hasSubAgent 存在性检查和 useAgent 客户端的 sub 数组语法。客户端通过 useAgent({ agent: userAgentClass, name: userId, sub: [{ agent: chatAgentClass, name: chatId }] }) 建立到特定子会话的连接。

共享上下文：复用 RemoteContextProvider 和 RemoteSearchProvider 的 RPC 契约接口（getSharedContext、setSharedContext、appendSharedContext、searchShared、indexShared），ChatAgent 在 configureSession 中通过 withContext 配置指向 UserAgent 的远程提供者。UserAgent 端实现相同的 callable 方法签名。

MCP 客户端：复用 Agent 基类的 MCPClientManager（this.mcp）和 addMcpServer、getMcpServers、removeMcpServer 方法以及 OAuth 授权流程。差异仅在于 MCPClientManager 位于 UserAgent 而非 ChatAgent，子会话通过代理访问。McpAgent 和 createMcpHandler 等服务端 MCP 能力不受影响。

### 技术效果

本方案通过双层 Agent 结构带来以下技术效果。第一，并行推理：每个 ChatAgent 是独立的 DO 实例，不同会话的 LLM 推理完全并行执行，不受 DO 单线程限制。第二，资源复用：Workspace 文件、MCP 连接和 OAuth 凭据在用户维度共享，避免每个会话重复建立连接和重复授权，减少网络开销和存储冗余。第三，安全隔离：消息历史和会话配置完全隔离，共享资源通过受控代理访问，访问控制集中在 UserAgent 的 onBeforeSubAgent 钩子，防止会话 ID 猜测攻击。第四，实时同步：文件变更通过 Workspace onChange 回调驱动的事件广播链路，无需轮询即可通知多个标签页和会话面板。第五，集中调度：定时任务和跨会话操作由 UserAgent 统一管理，避免分散在各子会话中的调度冲突和重复执行。

### 风险与待确认问题

以下方面需要在实施前确认或在实际使用中持续观察。

- MCP 工具代理的性能开销：每次 MCP 工具调用需经过子 Agent 到父 Agent 的一次 RPC 往返。对于高频小参数工具调用（如文件读取），延迟叠加可能影响用户体验。可考虑在父 Agent 端实现工具调用的批量执行接口以减少往返次数。
- 活跃子会话列表的一致性：ChatAgent 在连接建立和关闭时向 UserAgent 注册和注销。如果 ChatAgent 异常崩溃（DO 休眠），UserAgent 可能持有过期的活跃列表项。需要设计心跳或租约机制来保证活跃列表的最终一致性。
- Workspace 文件写入的并发控制：当前 Workspace 依赖 DO SQLite 的串行化写保证单次写入原子性，但不提供文件级别的锁。多个 ChatAgent 同时修改同一文件时，可能出现后写覆盖先写的情况。在工作负载中出现频率高的场景下，可能需要引入乐观锁（基于文件版本号）或写入队列。
- 跨会话搜索的可扩展性：searchMessages 的扇出 RPC 模式在活跃会话数超过约 50 个时可能产生显著的延迟和开销。后续可能需要 UserAgent 端维护聚合 FTS 索引，通过 ChatAgent 的 onChatResponse 钩子推送增量索引更新。
- UserAgent 单点性能瓶颈：所有共享资源操作（文件读写、MCP 工具调用）都经过 UserAgent，在高并发场景下 UserAgent 的 DO 单线程可能成为瓶颈。需要监控 UserAgent 的 CPU 时间和请求排队延迟，必要时可考虑将高频只读操作（如文件读取）下放到 CDN 缓存层或使用 DO 的只读副本。
