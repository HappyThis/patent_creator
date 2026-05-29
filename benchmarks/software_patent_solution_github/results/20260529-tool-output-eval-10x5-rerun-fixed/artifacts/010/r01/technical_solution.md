## 技术方案

本技术方案基于 Cloudflare Agents SDK 的 Durable Object 架构，提出一种父子 Agent 双层结构的多会话智能助手系统。系统将一个用户的多个聊天会话组织为：一个用户级父 Agent（Chats）持有共享资源并管理会话目录，每个聊天会话由一个独立的会话级子 Agent（Think）承载，两者通过 Durable Object 子代理路由机制进行受控通信。

### 要解决的技术问题

在现有基于 Durable Object 的单体 Agent 架构中，每个 Agent 实例维护一份消息历史、工具连接和会话配置。当用户需要同时进行多个独立对话（例如分别讨论不同项目），仅靠单一 Agent 实例无法实现会话隔离。若在单个 DO 中维护多组消息历史，则所有对话的推理请求将串行化到同一 DO 线程上，丧失并行性；若为每个对话创建完全独立的 DO 实例，则 workspace 文件、MCP 工具连接、OAuth 授权凭据等用户级资源将在各实例间重复维护，既浪费存储空间，又导致授权状态不一致和文件变更通知无法跨会话同步。

### 整体架构：用户级父Agent与会话级子Agent

系统采用用户级父 Agent 与多个会话级子 Agent 的双层 Durable Object 架构。父 Agent（Chats）作为一个用户维度的 Durable Object，负责维护会话目录、持有共享资源（workspace 文件系统、MCP 服务器连接池、OAuth 授权凭据）并执行跨会话的定时任务。每个聊天会话由一个独立的子 Agent（Think 实例）承载，拥有自己隔离的 SQLite 存储，独立维护消息历史、上下文块、会话配置和推理状态。父与子之间通过 Durable Object 子代理路由（sub-agent routing）进行 RPC 通信，WebSocket 连接直接指向子 Agent 以承载实时聊天流。

架构层次如下：用户/租户层（可选，已有认证实体）→ Chats 父 Agent（每个用户一个 DO 实例）→ Think 子 Agent（每个会话一个 DO 实例，例如 "chat-abc"、"chat-def"）。父 Agent 通过框架内置的 subAgent(ChildClass, name) 方法按需创建子 Agent，子 Agent 作为 workerd facet 与父 Agent 共址部署在同一机器上，拥有独立的 SQLite 存储和内存空间，但不支持独立闹钟（schedule 不可用于 facet）。

### 父Agent（Chats）的职责边界

Chats 父 Agent 继承自 Agent 基类，维护以下核心职责：

- 会话目录管理：父 Agent 在 SQLite 中维护 chats_index 表（字段：id、title、created_at、updated_at、last_message_preview、deleted_at），并通过 FTS5 全文索引支持标题搜索。通过 @callable 装饰的 createChat、listChats、deleteChat、renameChat、searchChats 方法暴露给前端。删除采用软删除策略，标记 deleted_at，子 DO 随后进入休眠并通过 TTL 回收。
- 共享 Workspace 代理：父 Agent 持有唯一的 Workspace 实例，基于 SQLite+R2 混合存储的虚拟文件系统。所有子会话通过父 Agent 的 RPC 接口间接访问同一套文件，而非各自持有独立 workspace。文件变更通过父 Agent 的 onChange 回调触发广播，通知所有已连接的 WebSocket 客户端。
- MCP 连接与 OAuth 共享：父 Agent 通过内置的 MCPClientManager 统一管理所有外部 MCP 服务器的连接。用户只需在任一子会话中配置并授权一次 MCP 服务器，父 Agent 即可将连接状态持久化到 cf_agents_mcp_servers 表，通过 OAuth 回调流程完成授权后，所有子会话均可通过父 Agent 的受控代理接口调用 MCP 工具。
- 定时任务与全局调度：父 Agent 作为非 facet DO 完整支持 schedule() 和 scheduleEvery() 闹钟机制，负责执行跨会话摘要生成、定时清理、索引重建等全局后台任务。子 Agent 不支持独立闹钟，避免散落调度。
- 访问控制与安全边界：父 Agent 通过 onBeforeSubAgent 钩子实现严格的子会话注册表门控——仅当子会话 ID 已在父 Agent 的注册表中登记（通过 hasSubAgent 检查）时，才允许外部请求路由到子 Agent，防止通过猜测会话 ID 访问或创建未授权的子会话。

### 子Agent（Think会话）的职责与隔离边界

每个会话级子 Agent 是一个独立的 Think 实例，通过 Session 模块管理消息存储。其隔离与共享的边界如下：

- 消息历史隔离：每个子 Agent 的 Session 持有独立的 assistant_messages 表（树形结构，支持 parent_id 分支）、assistant_compactions 表（非破坏性压缩叠加层）和 FTS5 全文搜索索引。不同会话的消息历史完全隔离，不可互相访问。
- 会话配置隔离：每个子 Agent 的 think_config 表独立存储会话级配置（模型选择、系统提示词、工具集、最大步数等），不同会话可拥有不同的个性化设置。
- Context Block 分离与会话内记忆：每个子 Agent 通过 configureSession() 配置本地上下文块（如 "soul" 性格块），其中写权限的上下文块由 Session 的 WritableContextProvider 在子 Agent 本地 SQLite 中管理，用于会话内记忆。
- 受控共享资源访问：子 Agent 通过 RemoteContextProvider 和 RemoteSearchProvider 访问父 Agent 上的共享上下文块（如 "user_memory" 跨会话记忆）。RemoteContextProvider 通过 parentAgent(Cls) 获取父 Agent 的 RPC 存根，将 get/set/append 操作转发到父 Agent 的 getSharedContext/setSharedContext/appendSharedContext 方法。读取失败时采用软失败策略（返回 null），保证子 Agent 在父 Agent 暂时不可达时仍可正常工作。
- 工具调用代理：子 Agent 在执行推理循环中需要调用外部工具时，不直接持有 MCP 客户端连接。子 Agent 通过父 Agent 暴露的 RPC 工具代理接口发起工具调用，父 Agent 的 MCPClientManager 负责实际的 MCP 协议交互。这确保工具调用经过父 Agent 的统一生命周期管理，子 Agent 不能绕过父 Agent 直接执行原始 MCP 工具。
- 分支与再生支持：Session 的树形消息结构支持基于 parent_id 的消息分支。用户在同一会话中对历史消息进行重新生成时，系统创建新分支而非覆盖原消息，通过 getBranches(messageId) 可获取兄弟响应分支。

### 共享Workspace文件系统机制

Workspace 是一个基于 Durable Object SQLite 的持久化虚拟文件系统，辅以 R2 大文件溢出存储。文件以路径为主键存储在 SQLite 表（cf_workspace_{namespace}）中，小文件内容内联在 content 列中，超过阈值（默认 1.5MB）的文件将内容溢出到 R2，SQLite 仅存储元数据和 R2 键。

在本方案中，Workspace 实例由父 Agent 持有，按用户维度共享。子 Agent 不创建独立的 Workspace 实例，而是通过父 Agent 提供的 RPC 文件操作接口进行文件的读写、目录遍历和 glob 搜索。父 Agent 包装 Workspace 的文件操作，在每次变更（创建、更新、删除）时通过 onChange 回调触发 agent.broadcast()，将文件变更事件推送到所有已连接的 WebSocket 客户端。不同浏览器标签页或会话面板订阅父 Agent 的状态广播即可收到实时文件变更通知，无需轮询。

Workspace 的安全边界包括：路径规范化防止目录遍历攻击（禁止 .. 和双斜杠）、命名空间名称校验（仅允许字母数字和下划线，用于 SQL 表名安全插值）、符号链接深度限制（最大 40 层，检测循环）。文件操作不经过子 Agent 的推理上下文直接执行，而是作为工具调用通过子 Agent 的推理循环中的工具执行步骤完成。

### MCP连接与OAuth的共享与代理机制

父 Agent 在构造函数中初始化 MCPClientManager，该管理器负责所有外部 MCP 服务器的连接生命周期。关键流程如下：

- 服务器注册与持久化：用户通过前端界面添加 MCP 服务器时，调用父 Agent 的 addMcpServer @callable 方法。服务器配置（id、name、server_url、client_id、auth_url、callback_url、server_options）持久化到父 Agent 的 cf_agents_mcp_servers 表中。
- OAuth 授权流程：若服务器需要 OAuth 认证，父 Agent 使用 DurableObjectOAuthClientProvider 管理授权流程。授权完成后，OAuth 凭据持久化在父 Agent 的 Durable Object Storage 中，通过 callbackUrl 回调完成令牌交换。
- 连接恢复：父 Agent 在 onStart 生命周期中调用 mcp.restoreConnectionsFromStorage() 恢复所有已持久化的 MCP 连接。恢复完成后通过 broadcastMcpServers() 将当前 MCP 服务器状态广播给所有客户端。
- 子 Agent 的工具访问路径：子 Agent 在推理循环中需要调用 MCP 工具时，通过 parentAgent(Chats) 获取父 Agent 的 RPC 存根，调用父 Agent 暴露的工具代理方法。父 Agent 的 MCPClientManager 根据工具名称路由到对应的 MCPClientConnection 执行实际的 callTool 操作，将结果返回子 Agent。子 Agent 不持有任何 MCP 客户端连接对象，不直接执行 MCP 协议交互。
- 状态变更广播：每当 MCP 服务器状态发生变化（注册、连接、断开、OAuth 授权完成），MCPClientManager 的 onServerStateChanged 事件触发父 Agent 执行 broadcastMcpServers()，将更新后的服务器列表通过 WebSocket 广播给所有已连接的客户端。

### 实时同步与文件变更通知

系统的实时同步基于 Durable Object 的 WebSocket 广播机制和父 Agent 的状态变更事件传播。

父 Agent 维护其自身的可序列化状态（ChatsState），包含会话摘要列表。当创建、删除或重命名会话时，父 Agent 调用 setState() 更新内部状态，框架自动将新状态通过 WebSocket 广播给所有连接到父 Agent 的客户端。前端 useChats() React Hook 订阅该广播状态，驱动侧边栏的实时更新。

文件变更通知通过 Workspace 的 onChange 回调机制实现。父 Agent 在创建 Workspace 时注册 onChange 回调，回调中调用 agent.broadcast() 将文件变更事件（包含变更路径和操作类型）推送到所有已连接的 WebSocket 客户端。由于所有子会话共享同一 Workspace 实例（位于父 Agent），任何子会话中的文件操作最终都经过父 Agent 的 Workspace，确保变更事件统一发出。

MCP 服务器状态同步类似——父 Agent 的 broadcastMcpServers() 在每次 MCP 连接状态变化时广播最新的服务器列表，前端据此更新可用工具面板。所有广播通过 _broadcastProtocol 方法发送符合 cf_agent_chat_* 协议的 JSON 消息帧，与现有 chat 协议兼容。

子 Agent 独立的 WebSocket 连接用于实时聊天流：每个子 Agent 拥有自己的 WebSocket 客户端集合，通过 ResumableStream 机制（SQLite 块缓冲区）在网络断开重连后恢复未完成的流式输出，无需重新开始推理。

### 访问控制与安全边界

系统通过多层机制确保会话访问的安全边界：

- 子 Agent 路由门控：父 Agent 的 onBeforeSubAgent 钩子拦截所有指向 /sub/{className}/{childName} 的 HTTP/WebSocket 请求。钩子中通过 hasSubAgent(className, childName) 检查子 Agent 是否已在父 Agent 的注册表（SQLite 中的 cf_agents_sub_registry 表）中登记。未登记的访问请求返回 404 响应，防止通过猜测会话 ID 访问或创建未授权的子会话。
- 子 Agent 注册表管理：subAgent(Cls, name) 调用在父 Agent 的注册表中执行 INSERT OR IGNORE，deleteSubAgent(Cls, name) 执行删除。注册表是子 Agent 存在性的权威来源。应用程序可在注册表基础上维护自己的元数据表（标题、预览等）。
- 租户边界：父 Agent 的 DO ID 即为租户边界——每个用户拥有独立的 Chats DO 实例。多租户共用一个目录不在本方案范围内。
- 只读连接控制：系统通过 shouldConnectionBeReadonly 钩子和 setConnectionReadonly() 方法支持将特定 WebSocket 连接标记为只读。只读连接不能调用 setState() 修改状态，其状态变更尝试在框架层被拦截并返回错误，无需在每个 @callable 方法中手工检查权限。
- OAuth 凭据隔离：MCP OAuth 凭据持久化在父 Agent 的 Durable Object Storage 中（通过 DurableObjectOAuthClientProvider），子 Agent 不持有凭据的直接访问权限，仅能通过父 Agent 的 RPC 代理间接使用已授权的工具。

### 典型处理流程

以下描述一个典型的用户操作流程，涵盖会话创建、消息发送、MCP 工具调用和文件操作：

1. 用户通过前端侧边栏调用父 Agent 的 createChat RPC 方法。父 Agent 生成唯一会话 ID（crypto.randomUUID()），在 chats_index 表中插入记录，调用 subAgent(ThinkClass, chatId) 在注册表中登记并返回 ChatSummary。父 Agent 通过 setState() 更新 ChatsState 并广播。
2. 用户点击会话进入聊天界面。前端通过 useAgent({ agent: 'chats', name: userId, sub: [{ agent: 'think-chat', name: chatId }] }) 建立指向子 Agent 的 WebSocket 连接。请求经父 Agent 的 onBeforeSubAgent 钩子验证子 Agent 已注册后，路由到子 Agent。
3. 用户在聊天框中发送消息。消息通过 WebSocket 以 cf_agent_use_chat_request 类型到达子 Agent。子 Agent 的 Think._handleChatRequest 方法将消息通过 INSERT OR IGNORE 写入 Session 的 assistant_messages 表（基于消息 ID 幂等），然后启动推理循环。
4. 推理循环中，子 Agent 调用 getModel() 获取语言模型，调用 assembleContext() 组装上下文（包含来自 Session 的本地上下文块和通过 RemoteContextProvider 从父 Agent 获取的跨会话记忆块）。
5. 若推理循环需要调用外部工具（如文件读写或 MCP 工具），子 Agent 根据工具类型路由：文件操作工具调用父 Agent 的 Workspace RPC 代理接口，MCP 工具调用父 Agent 的 MCP 工具代理接口。父 Agent 执行实际操作后返回结果。
6. 工具执行结果返回子 Agent 后继续推理循环，直到达到最大步数或模型返回最终响应。流式输出通过 StreamAccumulator 累积为 UIMessage，通过 WebSocket 实时推送到客户端。最终消息持久化到 Session 并触发可选的压缩检查。
7. 用户切换到另一会话时，前端关闭当前 WebSocket 连接（code 1000），使用新 chatId 建立新连接。原子 Agent 可能进入休眠状态，其状态由 Durable Object 持久化保证下次唤醒时完整恢复。

### 技术效果

本方案通过父子 Agent 双层架构，取得了以下技术效果：

- 并行推理能力：每个会话由独立 Durable Object 承载，多个会话的推理请求可并行执行，互不阻塞。避免了单 DO 多会话方案中的串行化瓶颈。
- 资源高效共享：Workspace 文件、MCP 连接和 OAuth 凭据在父 Agent 中集中维护一份，所有子 Agent 通过受控 RPC 代理访问，消除了重复存储和重复授权开销。用户只需授权一次 OAuth，所有会话即可使用相同工具。
- 会话隔离完整：消息历史、会话配置、上下文块严格按子 Agent 隔离，不同会话之间无消息泄漏风险。子 Agent 通过 RemoteContextProvider 的显式配置才能访问父 Agent 上的共享记忆，非默认行为。
- 实时同步低延迟：文件变更和 MCP 状态变化通过父 Agent 的状态广播机制推送到所有 WebSocket 连接，无需客户端轮询。子 Agent 的 ResumableStream 机制保证断网重连不丢失流式输出。
- 安全边界清晰：onBeforeSubAgent 注册表门控防止未授权会话访问，只读连接控制防止低权限客户端修改状态，子 Agent 不能绕过父 Agent 直接执行 MCP 工具调用，OAuth 凭据不暴露到子 Agent 层。
- 复用现有体系：方案完全基于当前 Agents SDK 已有的 Agent 基类、Think 会话基类、Session 消息存储、子代理路由、MCPClientManager、Workspace 和广播机制构建，无需引入新的运行时依赖。

### 与项目环境的对应关系

本方案的技术实现与项目环境有如下对应关系：

- Agent 基类（packages/agents/src/index.ts）：提供 Durable Object 生命周期、SQLite 存储、WebSocket 连接管理、子代理路由（subAgent/onBeforeSubAgent/hasSubAgent）、MCPClientManager 集成、broadcast 广播和 schedule 定时机制。
- Think 类（packages/think/src/think.ts）：提供 chat 生命周期、Session 消息存储（树形消息、上下文块、压缩、FTS5）、流式输出（StreamAccumulator）、可恢复流（ResumableStream）、客户端工具和推理循环。
- Session 模块（packages/agents/src/experimental/memory/session/）：提供 assistant_messages 表（含 parent_id 分支结构）、ContextProvider/WritableContextProvider/SearchProvider 上下文块体系、非破坏性压缩叠加层、FTS5 搜索。
- Chats 父 Agent（设计文档 design/rfc-think-multi-session.md）：已提出完整的 Chats 基类设计，包含会话目录 CRUD、RemoteContextProvider/RemoteSearchProvider 跨 DO 上下文块、useChats() React Hook。当前状态为 proposed RFC，尚未在源码中实现。
- Workspace（design/workspace.md）：基于 SQLite+R2 的虚拟文件系统，支持文件、目录、符号链接、glob、diff、流式 I/O 和 onChange 回调。
- MCP 客户端（packages/agents/src/mcp/client.ts）：MCPClientManager 提供服务器注册、连接管理、OAuth 流程、状态广播，通过 DurableObjectOAuthClientProvider 管理凭据持久化。

### 风险与待确认问题

以下为当前方案中需要后续确认的技术风险点：

- Chats 基类实现状态：Chats 类目前为 RFC 提议状态（design/rfc-think-multi-session.md，status: proposed），尚未在 @cloudflare/think 包中实现。RemoteContextProvider/RemoteSearchProvider 同样处于设计阶段。方案的落地依赖于这些组件的实际开发。
- Facet 闹钟限制：子 Agent 作为 workerd facet 不支持独立 schedule()/scheduleEvery() 调用，全局定时任务必须全部集中在父 Agent 中。这是 Durable Object facet 的已知限制，可能影响需要子会话级别定时任务的场景。
- 共享 Workspace 并发：多个子会话同时修改同一文件可能导致后写覆盖。当前方案依赖 Workspace 的 SQLite 事务级别保证单次写入的原子性，但不提供跨会话的文件锁或冲突解决机制。对需要协同编辑的场景，可能需要额外的冲突检测层。
- 跨会话搜索扩展性：searchMessages 跨会话搜索采用扇出模式（并行 RPC 到每个子 Agent 的 Session.searchMessages），在活跃会话数超过约 50 个时可能产生性能瓶颈。设计文档提出后续可引入父 Agent 侧的 FTS 聚合索引来优化。
- 共享内存的读取-修改-写入竞争：当子 Agent A 读取共享记忆后，子 Agent B 写入更新，然后子 Agent A 回写时可能覆盖 B 的更新。当前方案建议优先使用 appendSharedContext（追加模式）降低丢失更新风险，但完全的并发控制仍需后续完善。
- 软删除窗口内的 ID 复用：deleteChat 为软删除，同一 ID 在软删除窗口内重新创建时需要清理旧行并递增代数计数器，防止旧 WebSocket 连接误重连到新会话。此机制需要在实现中仔细处理。
