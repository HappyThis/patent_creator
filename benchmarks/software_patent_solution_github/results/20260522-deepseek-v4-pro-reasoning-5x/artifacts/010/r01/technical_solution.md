## 技术方案

本方案基于 Cloudflare Agents SDK 的 Durable Object 代理框架，构建一种双层 DO（Durable Object）拓扑结构的多会话智能助手系统。系统将一个用户的所有聊天会话组织为一个用户级父 DO 和多个会话级子 DO 的树形结构：父 DO 持有用户维度的共享资源（Workspace 文件系统、MCP 工具连接、OAuth 授权凭据、共享上下文记忆），并作为聊天目录和全局调度中心；每个子 DO 是一个独立的 AI 对话执行单元，拥有隔离的消息历史、分支结构、个性化配置和会话内记忆，通过父 DO 提供的受控代理接口访问共享资源。以下从整体架构、会话隔离、共享资源代理、访问控制、实时同步和定时任务调度六个方面阐述具体技术方案。

上述方案通过双层 DO 拓扑和受控代理机制，实现了以下技术效果：（1）会话级并行推理——每个子 DO 是独立的 Durable Object 实例，多会话可被 workerd 调度到不同机器并行执行 agentic loop，互不阻塞；（2）共享资源复用——MCP 连接和 OAuth 授权在用户维度建立一次即可被所有子会话复用，避免每会话重复建连的开销和凭据管理复杂度；（3）安全隔离——子 DO 不持有原始凭据和连接，MCP 工具调用和文件操作均经过父 DO 校验，恶意或异常的会话级 prompt 注入无法越权操作共享资源；（4）实时同步——基于 WebSocket broadcast 和 Workspace onChange 的推送机制替代客户端轮询，减少网络开销和延迟；（5）可维护性——全局定时任务集中在父 DO 管理，调度配置持久化且支持重试，单个子 DO 的销毁不影响父 DO 和其他子 DO 的正常运行。

### 整体架构与父子 DO 拓扑

系统采用两层 DO 拓扑结构：一个用户对应一个父 DO 实例（基于 Chats Agent 基类），该父 DO 下管理多个子 DO 实例（基于 Think Agent 基类），每个子 DO 对应一个独立的聊天会话。

父 DO 的职责包括：（1）维护聊天目录索引——在自身 SQLite 中建立 chats_index 表，记录每个子会话的 id、标题、创建时间、最近活跃时间和软删除标记，并通过 FTS5 虚拟表支持标题搜索；（2）持有用户维度的共享资源——包括 Workspace 虚拟文件系统实例、外部 MCP 服务器连接池及其 OAuth 授权凭据、跨会话共享上下文块（shared_context 表）；（3）作为子 DO 的工厂——通过 this.subAgent(ThinkSubclass, chatId) 创建或获取子会话 Durable Object，框架底层使用 workerd 的 ctx.facets 机制在同一台机器上共置子 DO；（4）跨聊天搜索——通过向所有活跃子 DO 并发发起 RPC 调用 session.searchMessages(query)，聚合搜索结果；（5）全局定时任务调度——使用 Agent 基类内置的 schedule/scheduleEvery 注册跨会话摘要、闲置清理、Token 刷新等任务；（6）WebSocket 连接管理和广播——维护所有客户端 WebSocket 连接，通过 this.broadcast() 向多标签页推送状态变更。

子 DO 的职责包括：（1）独立的消息历史管理——使用 Session 层的树形消息结构（parent_id 链路 + 分支），支持 compaction overlays 压缩旧消息为摘要而非直接删除；（2）agentic loop 执行——调用 AI SDK 的 streamText 进行多轮工具调用推理，最大步数可配置，通过 TurnQueue 保证同一会话内请求串行化；（3）对话级配置——包括 system prompt、模型选择、工具白名单等，存储在子 DO 自身的 assistant_config 表中；（4）通过 RemoteContextProvider 读写父 DO 的共享上下文块；（5）通过父 DO 的 @callable RPC 方法代理访问 MCP 工具和 Workspace 文件。子 DO 不直接持有任何外部 MCP 连接、OAuth 凭据或文件系统实例。

寻址方式：父 DO 以用户标识命名（如 user_{userId}），子 DO 以会话标识命名（如 chat_{uuid}）。客户端通过嵌套 URL 路径 /agents/Chats/user_{userId}/sub/Think/chat_{uuid} 直接连接到目标子 DO 的 WebSocket，框架的 routeAgentRequest 和 routeSubAgentRequest 负责解析路径并将连接升级请求逐跳转发至叶子 DO。连接建立后，WebSocket 帧直接路由到子 DO，父 DO 不在热路径上。

### 会话隔离机制

系统从消息数据、对话配置、流式状态和生命周期四个维度实现会话间的严格隔离：

消息历史隔离。每个子 DO 拥有独立的 Session 存储层，消息以树形结构组织在 SQLite 的 assistant_messages 表中，每条消息记录 parent_id 指向父消息，支持通过 getHistory(leafId) 沿根到叶的递归 CTE 查询获取完整对话链，同时 getBranches(messageId) 可返回同一条用户消息下的多个分支回复，实现消息重新生成功能。compaction overlay 表存储压缩摘要，在读取时通过 applyCompactions 算法将指定范围的消息替换为摘要，原始消息不删除。FTS5 虚拟表为每一条消息建立全文索引，子 DO 内的搜索仅限当前会话。

对话配置隔离。每个子 DO 维护独立的 assistant_config 表，以配置键为复合主键存储 system prompt、模型标识、最大工具调用步数、温度参数、用户自定义工具白名单等。子 DO 的 getSystemPrompt() 返回的冻结提示词仅从本会话的 Session 上下文块和经 RemoteContextProvider 引用的父 DO 共享块组装，不同子 DO 的 system prompt 互不干扰。

流式状态隔离。每个子 DO 内部持有独立的 TurnQueue 实例，通过 generation 计数器保证同一个会话内请求按 FIFO 顺序串行执行，generation 变更时自动跳过过期排队项。ResumableStream 将流式输出的中间分块缓冲在子 DO 自身的 SQLite 中，客户端断开重连后可从上次中断位置继续接收。所有流式状态——包括 AbortController 注册表、StreamAccumulator、活跃请求 ID——完全在子 DO 内存和存储中，不与其他子 DO 共享。

生命周期隔离。子 DO 可按会话粒度独立创建和销毁。父 DO 的 deleteChat(id) 方法先软删除 chats_index 中的目录条目（设置 deleted_at 时间戳），再调用 this.deleteSubAgent(ThinkSubclass, chatId) 销毁子 DO 及其全部存储（SQLite 表、Session 数据、FTS5 索引），该操作不可逆。子 DO 闲置超时后可由父 DO 的定时清理任务调用 ctx.storage.deleteAll() 销毁，父 DO 的存续不受单个子 DO 影响。

### 共享资源代理机制

为解决子会话需要访问 Workspace 文件、MCP 工具和共享记忆但又不能直接持有底层连接或凭据的问题，系统建立了一套基于父 DO @callable RPC 的受控代理机制。子 DO 通过类型化的 parentAgent() 方法获取父 DO 的 RPC stub，所有对共享资源的操作均经由父 DO 代理执行，子 DO 不直接接触原始 transport、文件系统实例或 OAuth token。

Workspace 文件代理。父 DO 在自身 SQLite 上初始化一个 Workspace 实例（基于 @cloudflare/shell 的虚拟文件系统），使用 namespace="shared" 隔离用户级文件空间。子 DO 不创建自己的 Workspace 实例，而是通过父 DO 暴露的 @callable 文件操作方法（如 readSharedFile、writeSharedFile、listSharedDir）进行文件操作。父 DO 在执行前校验路径前缀：子会话写入操作强制限制在 /chats/{chatId}/ 子目录内，读取操作允许 /shared/ 公共目录和本会话专属子目录，禁止访问其他会话的文件空间。Workspace 自身提供 SQLite 内联存储（小于 1.5MB 的文件直接存于 content 列）和 R2 溢出存储（大文件），对子 DO 透明。

MCP 工具连接代理。父 DO 通过 Agent 基类的 addMcpServer 或 registerServer 方法建立与外部 MCP 服务器的连接，连接配置（服务器 URL、认证方式、OAuth 客户端凭据、重试策略）持久化在父 DO SQLite 的 mcp_servers 表中。MCP 服务器的 transport（SSE / Streamable HTTP / RPC）由父 DO 建立的 McpAgent 子代理管理，OAuth 授权流程和 token 刷新均在父 DO 侧完成。子 DO 在 agentic loop 中遇到需要调用 MCP 工具时，不直接建立 MCP 连接或持有工具 stub，而是通过父 DO 的 @callable callMcpTool(serverName, toolName, args) 方法发起调用。父 DO 接收后执行两步校验：通过子 DO 的 DO ID 反查 chats_index 确认该子会话确实属于当前用户；检查工具白名单确认该 MCP 工具对该子会话可用。校验通过后，父 DO 代理执行工具调用并将结果返回子 DO。

共享上下文记忆代理。父 DO 维护 shared_context 表（label TEXT, content TEXT），存储用户级共享记忆。子 DO 通过配置 RemoteContextProvider 实例引用父 DO 的共享上下文：RemoteContextProvider 是 ContextProvider 接口的远程实现，其 get/set/append 方法通过 RPC 调用父 DO 的 getSharedContext、setSharedContext、appendSharedContext 方法。写入采用追加优先策略——子 DO 优先调用 appendSharedContext 做增量追加以避免读-改-写竞争丢失，仅在需要完全替换时才使用 setSharedContext。共享上下文内容被注入到子 DO 的冻结系统提示词中，当前 turn 内使用快照版本，不因其他会话的并行修改而中断。

Loopback 绑定的技术实现。子 DO 通过 this.parentAgent(ParentClass) 获取父 DO 的类型化 RPC stub——这是 Agent 基类提供的单跳父代理查找原语，基于 workerd 的 ctx.facets 和 ctx.exports 机制在运行时解析父 DO 引用。父 DO 和子 DO 共置于同一台 workerd 机器上，RPC 调用为零网络延迟的进程内通信。当子 DO 内部还有更深层子代理（如 Researcher 子代理）需要访问共享资源时，采用显式传递模式：Think 子 DO 将 Chats 父 stub 作为参数传递给 Researcher，避免依赖隐式的全局上下文。

### 访问控制机制

系统从会话 ID 防猜测、父代理中间件鉴权、MCP 工具调用校验和路径边界限制四个层面实现访问控制，防止用户通过猜测会话 ID 访问未登记的子会话，也防止子会话绕过代理直接执行原始 MCP 工具调用。

会话 ID 防猜测。所有 chatId 使用 crypto.randomUUID() 生成，熵空间约 2^122，在计算上不可预测。父 DO 的 chats_index 表是会话是否存在的唯一权威来源——即使攻击者构造出合法格式的 UUID，若该 ID 不在 chats_index 中或已软删除，父 DO 的 onBeforeSubAgent 中间件会返回 404 响应，子 DO 不会被唤醒。

onBeforeSubAgent 鉴权中间件。父 DO 覆写 onBeforeSubAgent(req, child) 方法，在每次子 DO 被访问前执行鉴权逻辑：从请求 URL 路径中解析出父 DO 名称（userId）和子 DO 名称（chatId），查询 SQLite 的 chats_index 表验证该 chatId 确实属于当前用户且未被删除。若验证失败，直接返回 403 或 404 Response，子 DO 不会被创建或唤醒，实现了零 DO 唤醒成本的鉴权。此外，onBeforeSubAgent 还可注入身份头（如 x-inbox-id）供子 DO 读取，或接入外部认证系统（如 Clerk、Auth0）校验 JWT 中的 user ID 与父 DO 名称一致性。

MCP 工具调用的双重校验。子 DO 的 agentic loop 无法直接访问 MCP transport 或持有工具 stub——其 getTools() 方法返回的工具集中，所有远程 MCP 工具均封装为向父 DO 发起 callMcpTool RPC 的代理工具函数。父 DO 的 callMcpTool 方法在代理执行前执行身份校验：通过调用方的 DO ID 反查确认该子 DO 是当前父 DO 的合法子会话；同时检查该子会话的工具白名单配置，确认被调用的 MCP 工具在允许范围内。浏览器客户端或子会话的任何代码路径均无法绕过此代理层直接执行原始 MCP 工具调用。

路径边界限制。子 DO 通过父 DO 代理进行文件操作时，父 DO 在 Workspace 方法调用前强制校验路径前缀：写入操作路径必须匹配 /chats/{chatId}/ 前缀，读取操作允许 /shared/ 或 /chats/{chatId}/。共享上下文块写入限定 scope="shared"，子 DO 无法通过 RemoteContextProvider 访问或修改其他会话的 session-scoped 上下文块。

### 实时同步机制

系统通过父 DO 的 WebSocket 连接管理和 Workspace 变更回调实现文件变化、聊天列表变化等状态的多标签页实时同步，减少客户端轮询开销。

文件变化广播。父 DO 初始化 Workspace 时注册 onChange 回调，回调函数接收变更事件（包含操作类型 create/update/delete 和受影响的文件路径）。当任一子会话通过代理修改文件时，Workspace 在 SQLite 写入完成后同步触发 onChange，回调内部调用父 DO 的 this.broadcast() 方法向所有已连接的 WebSocket 客户端广播结构化消息，消息类型为 workspace:changed，携带操作类型和路径信息。不同浏览器标签页的客户端收到消息后，按需刷新文件树视图或重新读取变更文件的内容。

聊天列表同步。父 DO 的 createChat、deleteChat、renameChat 方法在更新 chats_index 表后，通过 this.setState() 更新父 DO 的广播状态对象（包含聊天摘要列表），框架自动将状态变更推送到所有连接的客户端。客户端使用 useChats() React hook 订阅该状态，当侧边栏聊天列表发生变化时自动重渲染。软删除机制确保正在使用的会话不会因并行删除而丢失数据——deleteChat 先标记 deleted_at，广播更新后的列表，客户端检测到当前活动聊天被删除后主动断开连接，子 DO 延迟销毁。

消息同步。子 DO 完成一个 agentic turn 后，通过父 DO 的 @callable 方法通知父 DO 更新 chats_index 中该会话的 updated_at 和 last_message_preview 字段，父 DO 随之广播聊天列表更新。其他标签页中显示的同一会话可通过 Think 的 ResumableStream 机制在切换回来时从缓冲分块恢复流式内容。

连接管理。父 DO 维护所有客户端 WebSocket 连接的注册表，通过 onClose 和 onError 回调清理断开的连接。每个连接可通过 connection.setState() 记录当前活跃的聊天 ID，父 DO 广播时可按需过滤目标连接。使用 TurnQueue 保证状态更新和广播的有序性，避免多个并发的聊天操作导致状态不一致。

### 定时任务与全局调度

系统将所有定时任务和全局后台任务统一注册在父 DO 中，利用 Agent 基类内置的 schedule / scheduleEvery / queue 机制执行，子 DO 不持有任何独立的定时器或调度配置。

调度基础设施。Agent 基类提供三种调度原语：schedule(date, callback) 在指定时间执行一次；scheduleEvery(cron, callback) 按 cron 表达式周期性执行；queue(payload, callback) 入队后尽快执行。所有调度回调享有内置重试机制——默认最多重试 3 次，采用 Full Jitter 指数退避（baseDelayMs=100ms, maxDelayMs=3s），重试配置可按任务粒度覆盖。调度配置（cron 表达式、重试参数）序列化为 JSON 持久化在父 DO SQLite 的 schedules 表中，确保 DO 休眠恢复后调度不丢失。

跨会话摘要任务。父 DO 注册 scheduleEvery("0 */6 * * *", ...) 周期性任务，每 6 小时遍历 chats_index 中近 6 小时有活动的子会话列表，通过 fanout RPC 调用每个子 DO 的 session.getHistory() 获取最近消息，聚合生成用户级活动摘要存储于 shared_context 中。此任务完全在父 DO 侧编排，不需要子 DO 持有定时器。

闲置会话清理任务。父 DO 注册 scheduleEvery("0 3 * * *", ...) 每日凌晨 3 点执行：查询 chats_index 中 updated_at 早于 N 天（默认 30 天）且未被 pinned 的会话，依次调用 this.deleteSubAgent(ThinkSubclass, chatId) 销毁子 DO 及其全部存储（包括 Session 消息历史、FTS5 索引、compaction 数据），随后从 chats_index 中删除对应记录。销毁操作不可逆，但闲置阈值可配置。

MCP Token 刷新任务。父 DO 维护每个 MCP 服务器的 OAuth token 过期时间，注册 schedule 任务在 token 过期前 5 分钟触发刷新流程，使用存储的 refresh_token 获取新凭据并更新 mcp_servers 表中的配置。刷新失败时触发重试机制，连续失败达到上限后通过 broadcast 通知客户端 MCP 连接异常。

共享上下文压缩任务。父 DO 注册周期性任务对 shared_context 表中的内容执行 token 预算检查，当某 label 下的内容超过配置的 maxTokens 阈值时，调用 LLM 对历史内容进行摘要压缩，保留关键信息的同时控制存储和提示词长度。所有定时任务在父 DO 首次初始化（onStart）时注册，通过检查 schedules 表避免重复注册。
