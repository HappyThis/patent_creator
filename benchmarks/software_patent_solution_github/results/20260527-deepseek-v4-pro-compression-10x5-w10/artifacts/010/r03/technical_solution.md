## 技术方案

### 技术问题概述

当前系统提供了基于 Durable Object 的单体 Agent、Think Agent、Workspace 文件系统、MCP 工具连接、子 Agent 路由等基础能力，但缺乏面向多会话场景的统一 assistant 架构。用户在同一账号下需要管理多个独立聊天会话，每个会话应有独立的消息历史、分支、配置和记忆；同时又需要跨会话共享 workspace 文件、MCP 工具连接和 OAuth 授权结果，并需要在文件变更时将刷新信号实时推送到多个浏览器标签页。现有架构中，将多会话状态全部压缩到单个 Durable Object 内部会导致单线程串行化瓶颈和职责混乱；简单地为每个 chat_id 创建独立的顶层 Agent 则无法实现共享资源的高效管理和访问控制。

### 整体架构：父子 Agent 双层结构

本方案引入用户级父实体（UserAssistant）与会话级子实体（ChatSession）的双层 Agent 结构，两者均基于现有 Agent 基类，通过 subAgent 机制构建 parent-child 拓扑。

UserAssistant 为一个 Durable Object，对应一个用户的全局命名空间，是所有 ChatSession 子 Agent 的父节点。它持有并管理三类按用户维度共享的资源：Workspace 文件系统实例、MCP 服务器连接池及 OAuth 授权凭据、以及文件变更广播通道。ChatSession 为 UserAssistant 通过 this.subAgent(ChatSession, chatId) 创建的子 Durable Object（facet），每个 ChatSession 拥有自己隔离的 SQLite 存储、独立的消息树（Session）、独立的上下文块和配置。

子会话不直接持有 Workspace 实例、不直接发起 MCP 连接、不直接管理 OAuth 令牌。子会话通过受控代理接口（SharedResourceProxy）向父实体请求共享资源。父实体的 @callable RPC 方法构成子会话访问共享资源的唯一合法路径，浏览器客户端或子会话无法绕过 Agent 生命周期直接执行原始 MCP 工具调用或直接操作文件。

### 用户级父实体（UserAssistant）

UserAssistant 继承自 Agent 基类，在一个 Durable Object 内聚合以下职责：

- 会话目录管理：维护 cf_chats_index 表，记录每个 ChatSession 的 id、标题、创建时间、更新时间、最后消息预览。通过 @callable 方法暴露 createChat、listChats、deleteChat、renameChat、searchChats 接口。创建会话时，UserAssistant 调用 this.subAgent(ChatSession, chatId) 创建子 facet，并将记录写入目录表。删除会话时调用 this.deleteSubAgent(ChatSession, chatId) 同时清理子 DO 和目录记录。
- Workspace 文件系统：持有一个 Workspace 实例（基于 DO SQLite + 可选 R2），所有 ChatSession 通过父实体的 RPC 接口读写同一文件树。Workspace 的 onChange 回调被连接至 broadcast() 机制，实现文件变更的实时推送。
- MCP 连接池：通过 Agent 基类的 addMcpServer / getMcpServers 管理 MCP 服务器连接。连接状态和 OAuth 令牌持久化在父实体的 SQLite 中。暴露 getMcpTools(sessionId) RPC 方法，子会话通过此方法获取工具列表，父实体在此处实施访问控制——验证 sessionId 确实属于调用方子 Agent。
- OAuth 凭据管理：MCP 服务器所需的 OAuth 授权流程（重定向、令牌交换、刷新）全部在父实体中完成。授权结果（access_token、refresh_token、过期时间）存储在父实体的 SQLite 中，子会话无需感知 OAuth 流程，仅通过代理接口使用已授权的工具。

### 会话级子实体（ChatSession）

ChatSession 基于 Think Agent 或 AIChatAgent 构建，每个实例为一个独立的 Durable Object facet。其隔离范围包括：

- 消息历史：每个 ChatSession 拥有自己的 Session 实例（树形消息存储），消息通过 parent_id 组织为分支结构，支持消息再生（regeneration）和会话分叉（fork）。消息存储在子 DO 的独立 SQLite 中，与其他会话完全隔离。
- 上下文块与记忆：每个 ChatSession 有自己私有的上下文块（如 soul、memory、todos），其中 session 局部记忆（如当前会话的讨论焦点）存储在本地 SQLite 的 AgentContextProvider 中。跨会话共享记忆通过 RemoteContextProvider 代理至父实体的共享上下文块。
- 会话配置：通过 Think 的 configure() 持久化的动态配置（如模型选择、温度参数）存储在子 DO 私有的 think_config 表中。
- 扩展和工具：每个 ChatSession 可注册自己的扩展（ExtensionManager）和服务端工具（getTools），这些不与其他会话共享。客户端工具（client tools）通过 WebSocket 协议按连接传递，天然隔离。
- WebSocket 连接：每个 ChatSession 通过子 Agent 路由直接接收客户端的 WebSocket 连接（路径为 /agents/user-assistant/{userId}/sub/chat-session/{chatId}），父实体的 onBeforeSubAgent 钩子在此处实施访问控制。

### 会话隔离机制

本方案通过以下机制实现会话间隔离，确保任何子会话无法访问其他会话的私有数据：

- 存储级隔离：每个 ChatSession 作为独立 Durable Object facet，拥有独立的 SQLite 数据库文件。子 DO 的 assistant_messages、assistant_config、think_config 等表在物理上与其他子 DO 隔离。
- 路由级隔离：客户端的 WebSocket 连接直接路由至目标子 Agent（通过 /sub/chat-session/{chatId} 路径段），父实体仅在连接建立时通过 onBeforeSubAgent 执行一次访问控制检查，后续帧流量直接到达子 Agent，不经过父实体中转。
- RPC 隔离：子 Agent 的 @callable 方法仅对其直接连接的 WebSocket 客户端和父实体可见。外部无法通过猜测 chatId 直接调用子 Agent 的 RPC 方法——所有外部入口必须先经过父实体的 onBeforeSubAgent 钩子，该钩子通过 hasSubAgent 检查子会话是否已在父实体的注册表中登记。
- 身份链传递：父实体在创建子 Agent 时通过 _cf_initAsFacet(name, parentPath) 传递祖先身份链。子 Agent 通过 this.parentPath 知晓自身在用户命名空间中的位置，可通过 this.parentAgent(UserAssistant) 反查父实体。该身份链由框架在 facet 初始化时注入，不可伪造。
- 消息与上下文隔离：每个 ChatSession 的 Session 实例使用独立的 session_id 命名空间，FTS5 全文索引按 session_id 隔离。子会话无法枚举或搜索其他会话的消息内容。

### 共享资源代理机制

子会话不直接持有共享资源，而是通过 SharedResourceProxy 接口向父实体发起受控请求。该代理是一个封装了 parentAgent RPC 调用的薄层，在 ChatSession 的 onStart 生命周期中初始化。代理提供三类共享资源访问：

一、Workspace 文件共享代理。子会话调用 proxy.readFile(path)、proxy.writeFile(path, content)、proxy.listDir(path) 等方法时，代理将调用转发至父实体的 @callable workspaceRead / workspaceWrite / workspaceList 方法。父实体在执行写操作后，通过 Workspace 的 onChange 回调触发 broadcast({ type: 'file-changed', path, operation })，所有连接到父实体或子会话的 WebSocket 客户端均收到文件变更通知。该 broadcast 利用 Agent 基类的广播机制，自动覆盖连接到同一父实体的多个浏览器标签页。

二、MCP 工具共享代理。子会话调用 proxy.getMcpTools() 获取可用 MCP 工具列表时，代理向父实体发起 RPC 调用。父实体从已连接的 MCP 服务器汇总工具定义并返回。子会话在 LLM 推理过程中需要调用 MCP 工具时，调用 proxy.invokeMcpTool(toolName, args)，代理将请求转发至父实体的 @callable mcpToolCall 方法。父实体在此方法中：(1) 验证调用方 sessionId 是否合法；(2) 查找对应的 MCP 客户端连接；(3) 调用 MCP 服务器的 toolCall；(4) 将结果返回子会话。此流程确保 MCP 连接的认证令牌和传输层状态始终由父实体持有，子会话无法绕过父实体直接访问 MCP 服务器。

三、OAuth 凭据共享代理。当 MCP 服务器要求 OAuth 授权时，父实体的 addMcpServer 返回 { state: 'authenticating', authUrl }。用户通过浏览器完成授权后，OAuth 回调到达父实体，父实体完成令牌交换并将凭据持久化。后续所有子会话通过 MCP 工具代理间接使用该凭据，无需各自维护授权状态。代理不暴露原始令牌内容给子会话的 LLM 上下文。

### 访问控制机制

系统通过三层访问控制防止未经授权的会话访问和资源操作：

1. 连接层控制（onBeforeSubAgent）：父实体在收到指向 /sub/chat-session/{chatId} 的 WebSocket 升级请求或 HTTP 请求时，触发 onBeforeSubAgent(req, { className, name })。在此钩子中，父实体验证：(1) 请求是否来自已认证用户（通过 cookie 或 Authorization 头）；(2) chatId 是否在父实体的子 Agent 注册表中存在（通过 hasSubAgent 检查）；(3) 该 chatId 是否属于当前用户。验证失败时返回 404 或 403 响应，阻止连接建立和子 DO 唤醒。该方法与 Agent 基类的 onBeforeConnect / onBeforeRequest 形成互补——前者处理跨会话的通用认证，后者处理父实体特有的子 Agent 访问授权。
2. 注册表强制：父实体维护 cf_agents_sub_agents 注册表作为子 Agent 是否存在的事实来源。subAgent() 调用自动插入记录，deleteSubAgent() 删除记录。外部请求只能访问已在注册表中登记的子会话，用户无法通过猜测 chatId 访问不存在的或他人的会话。
3. RPC 级验证：共享资源代理的每个 RPC 方法（workspaceRead、mcpToolCall 等）在父实体侧验证调用来源。父实体通过检查调用方的 Durable Object 标识与传入的 sessionId 参数是否匹配来防止子会话冒充其他会话。跨会话共享上下文（RemoteContextProvider）采用 fail-soft 策略：父实体不可达时返回空值而非抛出异常，保证子会话在共享资源暂时不可用时仍可继续推理。

### 实时同步机制

本方案利用现有 WebSocket 广播和状态同步机制实现文件变更的实时推送，避免浏览器轮询：

- 文件变更广播链路：当任一 ChatSession 通过共享代理写入文件时，父实体的 Workspace.onChange 回调被触发。父实体调用 this.broadcast({ type: 'cf_agent_state', state: { files: { [path]: { operation, timestamp } } } })，该消息通过 Agent 基类的广播机制发送至所有连接到父实体和所有子会话 WebSocket 的客户端。连接到同一用户的不同浏览器标签页——无论当前活跃的是哪个 ChatSession——均收到相同的文件变更事件。
- 多标签页同步：前端 useAgent / useChats hook 通过 WebSocket 连接持续接收 cf_agent_state 消息。当收到文件变更状态时，前端根据 operation 类型（create/update/delete）和 path 更新本地文件树视图。对于当前正在编辑变更文件的标签页，可采用乐观更新策略避免覆盖。
- 子会话状态同步：setState() 在子 Agent（facet）中向其自身 WebSocket 客户端广播状态更新。父实体的状态广播独立于子会话。当父实体的共享状态（如会话列表、文件树摘要）变更时，所有已连接的客户端均收到更新。此机制复用了 Agent 基类现有的状态同步基础设施。
- 消息推送与恢复：对于已关闭浏览器标签页的用户，可结合 Push API 在重要文件变更时发送浏览器推送通知。父实体通过 schedule() 或 scheduleEvery() 注册定时检查任务，检测到特定文件变更后使用 Web Push 发送通知，用户点击通知后重新建立 WebSocket 连接并通过 ResumableStream 恢复流式消息。

### 定时任务与全局调度

全局定时任务和跨会话批量操作由父实体 UserAssistant 统一调度，而非分散在各个 ChatSession 中：

- 定时任务注册：父实体在 onStart 中通过 this.schedule(cronExpression, callbackName, payload) 注册全局定时任务。例如，每日跨会话摘要生成、过期会话清理、MCP 令牌刷新等。所有定时任务存储在父实体的 cf_agents_schedules 表中，由 Durable Object 的 Alarm 机制统一唤醒执行。
- 跨会话搜索与摘要：当用户发起跨会话搜索时，父实体的 searchMessages(query) RPC 方法遍历所有子会话，对每个子会话调用其 session.search(query)（通过 subAgent RPC），收集结果后按 FTS5 排名合并返回。类似地，跨会话摘要任务由父实体的定时回调触发，父实体从各子会话拉取近期消息摘要并通过 LLM 合成全局摘要。
- 调度与子会话生命周期协调：父实体的定时任务不阻塞子会话的正常推理。定时任务在父实体自身的执行上下文中运行，子会话仅在需要拉取数据时才被 RPC 调用唤醒。对于长时间未活跃的子会话，父实体可通过 deleteChat 方法触发 deleteSubAgent，清理其存储和注册表记录。
- 故障恢复：定时任务失败时，Agent 基类的 retry 机制自动重试（可配置最大重试次数和退避策略）。Alarm 机制确保即使父实体在任务执行前被 evict，任务也会在 Alarm 触发时被正确唤醒和执行。

### 与现有体系的复用关系

本方案最大化复用现有系统组件，不引入新的基础抽象：

- subAgent 机制：复用 Agent 基类的 subAgent(Cls, name) / deleteSubAgent(Cls, name) API，子会话作为 Durable Object facet 创建，拥有独立的 SQLite 存储。子 Agent 路由（/sub/{class}/{name}）和 onBeforeSubAgent 钩子直接使用已实现的地址和访问控制基础设施。
- Session / SessionManager：ChatSession 的会话存储复用现有 Session API 的树形消息、上下文块、压缩、FTS5 搜索。父实体的跨会话搜索复用 SessionManager.search() 或通过直接 RPC 调用的 fan-out 模式。RemoteContextProvider / RemoteSearchProvider 复用现有 ContextProvider 接口，子会话通过配置 session.withContext('shared_memory', { provider: new RemoteContextProvider(parentStub, 'user_memory') }) 即可接入共享记忆。
- Workspace：父实体直接实例化 Workspace({ sql: this.ctx.storage.sql, onChange: ... })，子会话通过 RPC 代理间接使用。Workspace 的 SQLite + R2 混合存储、流式 I/O、符号链接、glob 匹配等能力无需修改。
- MCP 客户端：复用 Agent 基类的 addMcpServer / getMcpServers / removeMcpServer API。MCP 连接的重试、断线重连、传输层协商（streamable-http / SSE）由现有 MCP 客户端处理。OAuth 授权流程复用 @cloudflare/workers-oauth-provider。
- chat 共享层：复用 agents/chat 中的 TurnQueue（串行化推理）、ResumableStream（流恢复）、StreamAccumulator（消息组装）、sanitizeMessage / enforceRowSizeLimit（消息消毒）等原语。ChatSession 可选择基于 Think 或 AIChatAgent 构建，均复用这些共享组件。
- 调度系统：复用 Agent 基类的 schedule / scheduleEvery / cancelSchedule API 和 Alarm 唤醒机制。定时任务的持久化、幂等去重、重叠防护、错误重试均利用现有调度基础设施。

### 技术效果

本方案取得的整体技术效果包括：

- 并行推理：不同 ChatSession 运行在独立的 Durable Object 中，用户的多个会话可以并行执行 LLM 推理，不受单 DO 单线程的限制。一个会话的长时间工具调用不会阻塞另一个会话的消息处理。
- 资源高效共享：Workspace 文件、MCP 连接和 OAuth 令牌在父实体中集中管理，避免每个会话各自建立连接和重复授权。MCP 连接数从 O(N) 降为 O(1)（N 为会话数），OAuth 授权从 N 次降为 1 次。
- 安全隔离：子会话无法访问其他会话的消息、配置或上下文块；无法绕过父实体直接操作文件或调用 MCP 工具；无法通过猜测 ID 访问未登记的子会话。三层访问控制（连接层、注册表层、RPC 层）形成纵深防御。
- 实时同步：Workspace 文件变更通过 Agent 广播机制实时推送到所有浏览器标签页，无需轮询。文件变更事件与 WebSocket 连接生命周期解耦——即使用户切换了活跃的 ChatSession，也能收到文件更新。
- 可扩展调度：全局定时任务由父实体统一管理，支持 cron 表达式和固定间隔。跨会话搜索和摘要通过 fan-out RPC 模式实现，可线性扩展至数十个活跃会话。

### 风险与待确认问题

以下事项需要在后续实施中进一步确认和细化：

- 子 Agent 的 Alarm 独立性问题：当前 Durable Object facet 不支持独立的 schedule() / Alarm 机制。如果 ChatSession 需要自身的定时任务（如会话内定时提醒），目前只能通过父实体代理调度。需确认 workerd 运行时后续是否支持 facet 级别的 Alarm。
- 跨会话 fan-out 性能边界：searchMessages 的 fan-out RPC 模式在会话数超过 ~50 时延迟可能显著增加。当用户拥有大量会话时，需考虑在父实体侧维护 FTS5 聚合索引（由子会话的 onChatResponse 钩子异步更新），替代实时 fan-out。当前方案预留了该优化路径，父实体的 @callable indexShared 方法已提供索引写入接口。
- 共享内存并发写冲突：当两个 ChatSession 同时通过 RemoteContextProvider 写入共享记忆时，由于父实体 DO 单线程，实际不会发生真正的并发冲突。但 read-modify-write 模式可能导致后写入覆盖先写入。建议文档中明确推荐使用 appendSharedContext（追加模式）作为安全的增量更新方式，并说明 setSharedContext 是完整替换模式。
- 文件并发编辑冲突：多个会话同时修改同一文件时，Workspace 的 last-write-wins 策略可能丢失中间更新。对于需要冲突解决的场景，可考虑在 Workspace 代理层增加基于版本号的乐观锁（compare-and-swap），这是一项可选优化而非 v1 必需。
- 父实体单点性能：所有跨会话操作（共享文件读写、MCP 工具调用）都需要 RPC 往返父实体，每次调用都会唤醒父 DO。在极端高频场景下（如大量会话同时读写文件），父实体可能成为瓶颈。缓释措施包括：父实体可对文件内容实施短期内存缓存、MCP 工具列表可被各子会话缓存一个 turn 的周期。
