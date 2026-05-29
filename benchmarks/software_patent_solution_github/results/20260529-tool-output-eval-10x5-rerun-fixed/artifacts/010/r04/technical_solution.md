## 技术方案

### 整体架构

本方案采用用户级父实体与会话级子实体的双层 Durable Object 架构。一个用户对应一个父 Durable Object（以下称 UserHub），负责管理该用户的所有聊天会话列表、共享资源状态、全局定时任务和跨会话统一的访问控制。每个聊天会话对应一个独立的子 Durable Object（基于 Think 的 ChatSession），拥有自己的 SQLite 存储、消息历史、会话配置和 agentic 推理循环。

UserHub 与 ChatSession 之间通过云端 Durable Object RPC 通信，客户端通过嵌套 URL 路径 /agents/{user-hub-class}/{user-id}/sub/{chat-class}/{chat-id} 直接连接子 DO，父 DO 在连接建立阶段执行访问控制。WebSocket 升级后，客户端帧直接到达子 DO，父 DO 不处于通信热路径。

父 DO 承载三类核心职责：(1) 维护 chats_index 表和全文搜索索引，提供会话的创建、列举、重命名、删除和搜索操作；(2) 作为共享资源的唯一真实源（single source of truth），管理 Workspace 文件系统、MCP 服务器连接池和 OAuth 授权凭据；(3) 运行定时任务调度器，负责跨会话摘要、文件清理等全局周期性任务。

### 用户级父实体（UserHub）

UserHub 继承 Agent 基类，是用户维度的顶层 Durable Object。每个用户拥有一个 UserHub 实例，实例名通常为用户唯一标识（如 userId）。UserHub 内部维护以下持久化状态：

- chats_index 表：记录每个子会话的 id、title、created_at、updated_at、last_message_preview 和 soft-delete 标记 deleted_at。支持按更新时间分页查询和标题 FTS5 全文搜索。
- 共享 Workspace 实例：基于 Durable Object SQLite 与可选 R2 的虚拟文件系统，以用户维度命名空间隔离。所有子会话通过 RPC 代理访问同一 Workspace 实例。
- MCP 连接注册表：存储用户配置的 MCP 服务器地址、传输类型和连接参数。父 DO 持有 MCPClientConnection 实例池，管理连接生命周期、OAuth 令牌刷新和服务健康检查。
- OAuth 凭据存储：持久化用户在 MCP 服务器上的 OAuth 授权结果（access token、refresh token、过期时间），由 DurableObjectOAuthClientProvider 管理自动刷新。
- 定时任务注册表：记录全局周期性任务的 cron 表达式、回调方法名和参数，由 DO Alarm 机制驱动执行。

UserHub 通过 @callable 装饰器暴露 RPC 方法，子 DO 通过 this.parentAgent(UserHubClass) 获取类型化 RPC 存根调用这些方法。客户端通过 useAgent 或 useChats React hook 连接 UserHub 以获取会话列表和共享状态广播。

### 会话级子实体（ChatSession）

每个 ChatSession 是一个独立的 Think Durable Object，拥有自己的 SQLite 数据库和 Session 实例。ChatSession 通过 subAgent 机制由 UserHub 惰性创建——首次访问时由框架自动实例化，后续访问复用已有实例。ChatSession 的职责边界严格限定为单次聊天对话的上下文管理：

- 消息历史：Session 提供树形消息存储（parent_id 链），支持分支对话、消息回溯和 FTS5 全文搜索。消息通过 appendMessage 追加，自动触发父节点关联和可选自动压缩。
- 会话配置：通过 configureSession 方法注册 ContextProvider，包括系统角色（soul）、可写记忆（memory）、技能（skill）和搜索（search）上下文块。配置数据持久化在 ChatSession 自身的 SQLite 中。
- Agentic 推理循环：runAgentLoop 方法驱动 streamText 调用，管理工具调用步骤计数、AbortController 取消、流式区块广播和部分消息持久化。
- 工具集：getTools 返回的工具仅服务于当前会话，可包含会话特定的代码执行工具和浏览器工具。需要访问共享 MCP 工具时，通过 RemoteMCPToolProvider 向父 DO 发起 RPC 调用。

ChatSession 不直接持有 MCP 客户端连接、不直接管理 OAuth 令牌、不直接操作底层 Workspace 文件系统。当推理循环需要调用外部 MCP 工具或读写文件时，ChatSession 通过 this.parentAgent() 获取 UserHub 的 RPC 存根，调用 UserHub 暴露的代理方法。UserHub 在代理层执行权限校验、连接复用和审计日志记录。

### 会话隔离机制

会话隔离是整个方案的核心设计原则。每个 ChatSession DO 拥有独立的 SQLite 数据库（Durable Object 存储按 DO 实例隔离），消息历史、Session 配置、ContextProvider 实例和推理循环状态均不跨会话共享。隔离在以下层面实现：

存储隔离：Durable Object 框架保证每个 DO 实例拥有独立的 SQLite 数据库文件。ChatSession A 和 ChatSession B 是不同的 DO 实例（由不同的 DurableObjectId 标识），其 Session 表（assistant_messages、assistant_compactions、context_blocks 等）天然物理隔离。UserHub 的 chats_index 表仅存储会话元数据（标题、时间戳），不包含消息内容。

配置隔离：每个 ChatSession 的 getSystemPrompt、getTools、getMaxSteps 等覆盖方法以及 configureSession 注册的 ContextProvider 仅影响当前 DO 实例。会话 A 的 System Prompt 修改不会影响会话 B。通过 Session.forSession(sessionId) 可为同一 DO 内的多个 Session 提供命名空间隔离（用于高级场景），但本方案的多会话架构已将不同会话映射到不同 DO，无需此机制。

运行时隔离：每个 DO 实例在独立的 isolate 中运行，拥有独立的内存空间。ChatSession A 的 agentic 推理循环执行时不会阻塞 ChatSession B。WebSocket 连接直接绑定到子 DO，连接断开、重连和恢复流均只影响对应会话。TurnQueue 排队机制在单个 DO 内部串行化推理请求，不同 DO 之间完全并行。

### 共享资源代理机制

共享资源（Workspace 文件、MCP 连接、OAuth 凭据）均由 UserHub 父 DO 统一持有，子 ChatSession 不直接访问底层资源，而是通过受控的 RPC 代理接口进行操作。这一设计确保资源状态的唯一真实源在父 DO，避免多子 DO 之间的状态不一致和竞态条件。

一、Workspace 文件共享。UserHub 在初始化时创建一个 Workspace 实例，绑定到父 DO 的 SQLite 存储。Workspace 的 onChange 回调被注册为向所有已连接的 ChatSession 广播文件变更事件。当 ChatSession 需要读写文件时，调用父 DO 的代理方法（如 readFileProxy(path)、writeFileProxy(path, content)），父 DO 在 Workspace 上执行实际操作并返回结果。这种代理模式保证了文件系统的单写者语义——所有写操作在父 DO 的单线程中串行化，消除了并发写入冲突。

二、MCP 工具连接共享。UserHub 维护一个 MCPClientConnection 连接池，按 MCP 服务器地址索引。当用户通过 OAuth 授权一个 MCP 服务器后，父 DO 创建连接并完成工具发现（listTools），将可用工具列表缓存。ChatSession 的 getTools 方法通过 RemoteMCPToolProvider 获取工具列表——该 Provider 内部通过 this.parentAgent() 向父 DO 发起 RPC 请求，父 DO 返回缓存的工具 schema 列表。当 LLM 决定调用某个 MCP 工具时，ChatSession 再次通过 RPC 代理实际调用——父 DO 的 MCPClient 执行 callTool，结果返回子 DO。子 DO 不持有原始 MCP 连接句柄，不能绕过代理直接调用。

三、OAuth 凭据共享。MCP 服务器的 OAuth 授权流程由 UserHub 统一处理。DurableObjectOAuthClientProvider 在父 DO 中管理 OAuth 令牌的生命周期——access token 的存储、refresh token 的自动轮换和过期检测。子 ChatSession 完全无感知 OAuth 流程，它看到的只是可用的 MCP 工具列表。当工具调用因令牌过期失败时，父 DO 自动执行令牌刷新并重试，子 DO 仅感知到最终的调用结果。用户在一个会话中完成 OAuth 授权后，所有现有和新建的会话立即可使用该 MCP 服务器的工具。

### 访问控制与安全

系统通过三层访问控制防止未授权访问和会话 ID 猜测攻击。这三层控制分别部署在 Worker 入口、父 DO 的 onBeforeSubAgent 钩子和子 DO 自身的请求处理器上。

第一层——跨域认证（onBeforeConnect / onBeforeRequest）：在 Worker 的 fetch 入口，routeAgentRequest 处理所有 /agents/... 请求前，通过 onBeforeConnect 或 onBeforeRequest 钩子验证请求是否携带有效的用户身份凭证（Cookie、Bearer Token 等）。未通过认证的请求在这一层即被拒绝，父 DO 和子 DO 均不会被唤醒。

第二层——父 DO 子路由守卫（onBeforeSubAgent）：当请求 URL 包含 /sub/ 段指向某个 ChatSession 时，父 DO 的 onBeforeSubAgent 钩子被触发。该钩子可执行以下检查：(1) 验证请求的目标 className 是否为合法的 ChatSession 类；(2) 通过 hasSubAgent 检查目标会话是否已在 chats_index 中注册（严格模式），拒绝未登记会话 ID 的访问；(3) 注入身份标头（如 x-user-id、x-request-id），供子 DO 使用而无需再次查询。如果检查失败，钩子返回 404 或 403 Response，子 DO 不会被唤醒。

第三层——子 DO 自身处理器：ChatSession 可在自己的 onBeforeConnect / onBeforeRequest 中进一步校验。例如，读取父 DO 注入的 x-user-id 标头，与本地记录的会话所有者比对，或执行更细粒度的操作级权限检查（如只读连接 vs 读写连接）。

会话 ID 不可猜测性：ChatSession 的 ID 由 crypto.randomUUID() 生成，熵值足够大使暴力枚举不可行。hasSubAgent 检查进一步确保即使攻击者猜中一个有效格式的 UUID，如果该 ID 不在当前用户的 chats_index 中，请求仍被拒绝。客户端重试加固机制将 4xx HTTP 响应和特定 WS 关闭码（1008 策略违规、4000-4999 应用级永久错误）视为终端错误，停止无限重连，避免对已删除会话或未授权访问的持续探测。

### 实时同步与广播通知

实时同步机制确保用户在多个浏览器标签页或设备上看到一致的共享资源状态。系统提供两类同步通道：父 DO 的目录状态广播（会话列表变更）和文件变更的跨标签页实时通知。

目录状态广播：UserHub 使用 Agent 基类的 setState 方法更新 ChatsState（包含所有非删除会话的 ChatSummary 列表）。每次 createChat、deleteChat、renameChat 操作后，父 DO 调用 this.setState({ chats: updatedList })，框架自动通过 WebSocket 向所有连接到 UserHub 的客户端广播最新状态。客户端 useChats hook 订阅此广播，React 状态自动更新，侧边栏实时反映会话列表变化。

文件变更通知：UserHub 创建 Workspace 时注册 onChange 回调。当任一 ChatSession 通过代理写入文件时，父 DO 的 Workspace 触发该回调。回调内部调用父 DO 的 broadcast 方法，向所有连接到 UserHub 的客户端广播文件变更事件（包含变更路径、操作类型和时间戳）。客户端接收到事件后，可触发文件树 UI 刷新、文件内容重新加载或依赖该文件的前端状态更新。此机制避免了浏览器轮询 Workspace 状态，实现近实时的多标签页文件同步。

跨会话共享上下文同步：ChatSession 通过 RemoteContextProvider 访问父 DO 上的共享记忆块（如 user_memory）。当 ChatSession A 更新共享记忆后，ChatSession B 在当前推理回合内使用的是更新前的冻结快照（frozen prompt）。ChatSession B 在下一回合开始时调用 session.refreshSystemPrompt() 拉取最新内容。父 DO 可在共享记忆被更新时广播通知，触发所有活跃子会话在下一回合自动刷新，保证长期一致性。

### 定时任务与全局调度

子 Durable Object（ChatSession）当前不支持独立的 DO Alarm，定时任务和全局调度必须在父 DO（UserHub）上实现。UserHub 利用 Agent 基类的 this.schedule 和 this.scheduleEvery 方法管理全局任务。

调度架构：UserHub 维护一个定时任务注册表，每个任务包含：cron 表达式或延迟秒数、回调方法名、任务参数。当 Alarm 触发时，父 DO 的 alarm 方法被调用，从注册表中查询到期任务并按序执行。对于跨会话摘要任务，父 DO 遍历 chats_index 中的活跃会话列表，通过 this.subAgent(ChatSessionClass, chatId) 获取每个子 DO 的 RPC 存根，并行调用子 DO 的摘要生成方法，汇总结果后存入共享记忆或通过通知渠道发送给用户。

任务类型包括：(1) 跨会话摘要：每日定时遍历所有活跃会话，为每个会话生成摘要并汇总为每日简报；(2) 会话清理：按 TTL 策略（如 90 天未活跃）软删除过期会话，标记 deleted_at 后由延迟 GC 清理子 DO 存储；(3) MCP 令牌预刷新：在 OAuth access token 过期前主动刷新，避免子会话工具调用时因令牌过期而增加延迟；(4) 共享文件索引更新：定时扫描 Workspace 变更，更新全文搜索索引。所有任务在父 DO 的单线程中串行执行，DO Alarm 机制保证任务在 DO 休眠时也能准时唤醒执行。

### 技术效果

本方案通过用户级父 DO 与会话级子 DO 的分层架构，在复用现有 Think、Session、Workspace、MCP Client 和 Sub-Agent Routing 等基础设施的基础上，实现了多会话 assistant 的完整能力，达到以下技术效果：

- 并发会话执行：每个 ChatSession 是独立的 DO 实例，多个会话的 agentic 推理循环可在不同 isolate 中并行执行，互不阻塞。避免了单 DO 多会话方案（如 SessionManager 方案）的串行化瓶颈。
- 资源共享与一致性：Workspace 文件、MCP 连接和 OAuth 凭据在父 DO 中集中管理，所有子会话通过受控 RPC 代理访问，确保单写者语义和状态一致性。用户一次授权，所有会话受益。
- 安全的多会话访问：三层访问控制（Worker 入口认证、父 DO 子路由守卫、子 DO 自身校验）结合不可猜测的会话 ID 和客户端重试加固，有效防止会话 ID 枚举和跨用户访问。
- 实时多标签页同步：基于 WebSocket 的广播机制实现文件变更的推模式通知，替代轮询，降低延迟和带宽消耗。目录状态自动广播保证侧边栏实时性。
- 全局任务的统一调度：定时任务集中在父 DO 执行，避免了子 DO 不支持独立 Alarm 的限制，同时确保跨会话摘要、清理和维护任务的统一入口和可观测性。
- 架构可扩展性：子 DO 内部可通过 subAgent 机制进一步创建自己的子 Agent（如专用 Researcher Agent），父 DO 也可创建非聊天类的工具子 Agent（如 SearchAggregator），递归嵌套路由支持任意深度的 Agent 树。

### 典型交互流程

以下以"用户创建新会话并发起一次需要 MCP 工具调用的对话"为例，说明各组件协作的完整流程。

步骤 1 — 创建会话：客户端调用 useChats hook 的 createChat 方法，向 UserHub DO 发起 RPC 调用。父 DO 生成 UUID 作为 chatId，在 chats_index 表中插入新行（包含默认标题和时间戳），通过 setState 广播更新后的会话列表。客户端侧边栏实时展示新会话。

步骤 2 — 连接会话：客户端通过 useAgent({ agent: 'user-hub', name: userId, sub: [{ agent: 'chat-session', name: chatId }] }) 发起 WebSocket 连接。Worker 的 onBeforeConnect 验证用户身份。请求路由到 UserHub DO，触发 onBeforeSubAgent 钩子，验证 chatId 在 chats_index 中存在且未被软删除。验证通过后，子 ChatSession DO 被惰性创建（首次）或唤醒，WebSocket 升级完成，客户端与子 DO 直接通信。

步骤 3 — 发送消息和推理：用户通过 WebSocket 发送 cf_agent_use_chat_request 消息。ChatSession 的 _handleChatRequest 方法将用户消息通过 Session.appendMessage 持久化，组装 TurnInput（包含消息历史、客户端工具 schema 和 abort signal），进入 TurnQueue 排队。轮到当前回合时，runAgentLoop 调用 getTools 收集工具集——此时 RemoteMCPToolProvider 通过 this.parentAgent() 向 UserHub 发起 RPC 请求，UserHub 返回缓存的 MCP 工具 schema 列表。完整工具集传递给 streamText。

步骤 4 — MCP 工具调用：LLM 在推理过程中决定调用某个 MCP 工具（如文件搜索工具）。streamText 触发工具执行回调，ChatSession 的对应工具实现内部调用 this.parentAgent().callMCPTool(serverName, toolName, args)。UserHub 从连接池中获取对应的 MCPClientConnection，执行 callTool，将结果返回给子 DO。如果 OAuth 令牌已过期，父 DO 的 OAuthClientProvider 自动刷新令牌后重试。子 DO 将工具结果作为 tool-result 部分追加到流式响应中。

步骤 5 — 响应完成与同步：推理循环结束后，assistant 消息被持久化到子 DO 的 Session 中，通过 _broadcastMessages 向所有连接到该子 DO 的客户端广播完整消息列表。如果推理过程中涉及文件写入（通过父 DO 的 writeFileProxy），父 DO 的 Workspace onChange 回调触发，父 DO 向所有连接到 UserHub 的客户端广播文件变更事件。其他打开同一用户的浏览器标签页接收到事件后刷新文件视图。

### 现有基础设施复用

本方案最大程度复用现有 SDK 基础设施，避免重新实现已有能力，具体复用关系如下：

- Agent 基类：UserHub 和 ChatSession 均继承 Agent，直接获得 DO SQLite 存储、WebSocket 连接管理、RPC 调用（callable）、状态广播（setState/broadcast）、调度（schedule/scheduleEvery）和 Fiber 持久化执行能力。
- Think 基类：ChatSession 继承 Think，获得完整的 chat 生命周期管理——消息持久化（通过 Session）、agentic 推理循环（streamText + 工具调用）、流式区块广播、取消支持（AbortRegistry）、可恢复流（ResumableStream）、TurnQueue 排队和客户端工具动态注册。
- Session API：ChatSession 使用 Session 管理消息树、上下文块和自动压缩，无需自行实现消息存储逻辑。RemoteContextProvider 和 RemoteSearchProvider 通过 RPC 向父 DO 读写共享上下文。
- Sub-Agent Routing：嵌套 URL 路由（/agents/.../sub/.../）由已实现的 parseSubAgentPath、forwardToFacet 和 routeSubAgentRequest 原语处理。客户端 useAgent({ sub: [...] }) 自动构造正确 URL。
- Workspace：UserHub 直接使用 @cloudflare/shell 的 Workspace 类管理虚拟文件系统，获得内联 SQLite 存储 + R2 大文件溢出、symlink、glob、diff 和流式 I/O 能力。
- MCP Client：UserHub 复用 agents/mcp 的 MCPClient 和 MCPClientConnection 管理外部 MCP 服务器连接，包括多种传输协议支持、OAuth 令牌管理和工具发现。
- Chat Shared Layer：Think 和前端共享的流式区块解析（applyChunkToParts）、消息清理（sanitizeMessage）、流式累加器（StreamAccumulator）和协议常量（CHAT_MESSAGE_TYPES）均来自 agents/chat 共享层。

### 风险与待确认问题

以下为当前方案中需后续确认和关注的技术风险点：

- 子 DO 不可独立设置 Alarm：当前 workerd 平台限制，子 DO 的 schedule/scheduleEvery 为 no-op。所有定时任务必须落在父 DO 上，父 DO 成为调度单点。平台承诺即将支持子 DO Alarm，后续可下放部分周期性任务到子 DO 以减轻父 DO 负载。
- 父 DO 单线程瓶颈：UserHub 作为用户维度的单 DO，所有共享资源的读写操作在该 DO 中串行化。对于高频文件写入或大量并发子会话同时调用 MCP 工具的场景，父 DO 可能成为吞吐瓶颈。缓解措施包括：MCP 工具调用结果缓存、文件读写批处理，以及将只读操作（如文件读取）短路到本地缓存。
- 跨会话搜索的扇出开销：searchMessages 需向所有活跃子 DO 发起并行 RPC 调用，会话数超过约 50 时延迟可能不可接受。方案预留的优化路径是在父 DO 侧维护一个聚合的 FTS5 索引，由子 DO 的 onChatResponse 钩子异步更新。
- 共享 Workspace 的并发写入冲突：虽然父 DO 单线程避免了真正竞态，但多个子会话可能对同一文件进行读-改-写操作导致逻辑层面的丢失更新（子 A 读，子 B 写，子 A 写回）。建议在 Workspace 代理层引入基于版本号或 ETag 的乐观锁。
- RemoteContextProvider 的 RPC 延迟：ChatSession 每次推理回合开始时需通过 RPC 从父 DO 拉取共享上下文块，增加回合启动延迟。Session 的 withCachedPrompt 机制已缓存冻结的系统提示词，仅在回合边界刷新，将 RPC 调用分摊到每个对话回合仅一次。
- 子 DO 无对应目录行：如果父 DO 的 chats_index 被意外清空但子 DO 仍存在，子 DO 将成为孤立的 Durable Object。需实现定期巡检（父 DO 定时任务对比目录行与子 DO 注册表）或延迟 GC 机制来清理。
