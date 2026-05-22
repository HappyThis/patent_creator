## 技术方案

### 整体架构

系统采用双层 Durable Object（DO）代理架构：每个用户拥有一个用户级父实体（UserAssistant），在该父实体下，每个聊天会话作为一个独立的会话级子实体（ChatSession）运行。父实体与子实体均基于 Agents SDK 的 Agent 基类实现，子实体通过 subAgent() 机制作为父实体的 facet 子 DO 被创建和管理。

父实体（UserAssistant）负责维护用户维度的共享资源，包括：Workspace 文件系统（SQLite + R2 混合存储）、MCP 服务器连接注册表、OAuth 授权凭据存储、聊天会话索引（chat index）、文件变更广播通道、定时任务调度器。每个 UserAssistant 实例对应一个用户，通过 DO 名称（name）与用户标识绑定。

子实体（ChatSession）基于 Think 基类实现，每个实例对应一个独立的聊天会话，拥有隔离的 SQLite 存储、消息历史树（Session）、上下文块（context blocks）、压缩覆盖（compaction overlays）和代理执行循环（agentic loop）。子实体不直接持有 Workspace、MCP 连接或 OAuth 凭据，而是通过父实体的受控 RPC 接口代理访问共享资源。

### 用户级父实体（UserAssistant）

UserAssistant 是一个扩展自 Agent 基类的父代理实体，一个用户对应一个 UserAssistant DO 实例。其核心职责是管理用户维度共享资源，提供子会话的创建、列举、删除、重命名操作，并作为共享资源的唯一权威来源。

UserAssistant 维护以下关键数据：(1) 聊天索引表（chats_index），存储在父实体的 SQLite 中，记录每个子会话的 id、title、created_at、updated_at 等元数据，支持 FTS5 全文搜索；(2) 子代理注册表（cf_agents_sub_agents），由框架在 subAgent()/deleteSubAgent() 调用时自动维护，用于访问控制；(3) Workspace 实例，基于 @cloudflare/shell 的 Workspace 类，采用 SQLite 内联存储 + R2 溢出存储的混合模式，文件按用户维度共享；(4) MCP 连接注册表，记录已建立的 MCP 服务器连接及其工具清单，按用户维度共享；(5) OAuth 凭据存储，保存用户授权的 OAuth token，供所有子会话通过代理接口使用。

UserAssistant 通过 @callable 方法暴露共享资源访问接口给子会话，包括：getWorkspaceStub() 返回 Workspace 操作句柄、listMcpTools() 列出可用 MCP 工具、executeMcpTool(name, args) 代理执行 MCP 工具调用、getOAuthToken(provider) 获取 OAuth 凭据、broadcastFileChange(path, op) 广播文件变更事件。

### 会话级子实体（ChatSession）

ChatSession 是基于 Think 基类的会话级子实体，每个聊天会话对应一个独立的 DO 实例，拥有隔离的 SQLite 存储和完整的代理生命周期。ChatSession 通过父实体的 subAgent(ChatSession, chatId) 创建，客户端通过嵌套路由 /agents/{user-assistant}/{userId}/sub/{chat-session}/{chatId} 直接建立 WebSocket 连接。

每个 ChatSession 独立维护：(1) 消息历史树——基于 Session 的树形消息结构（assistant_messages 表含 parent_id），支持分支、再生（regeneration via branching）和压缩覆盖（compaction overlays）；(2) 个性化配置——通过 Think 的 configure()/getConfig() 持久化模型选择、系统提示词、工具集、最大步数等参数；(3) 上下文块（context blocks）——通过 configureSession() 配置，支持 writable memory、R2 skills provider、search provider 等，其中跨会话共享的 memory 块通过 RemoteContextProvider 指向父实体；(4) 扩展系统（ExtensionManager）——会话级沙箱化 Worker 工具扩展。

ChatSession 通过 this.parentAgent(UserAssistant) 获取父实体引用，调用父实体的 @callable 方法访问共享资源。关键区别在于：ChatSession 不直接持有 MCP 连接或 OAuth 凭据，不直接执行原始 MCP 工具调用，所有对外部资源的访问必须经过父实体的受控代理接口。

### 会话隔离机制

会话隔离通过 Durable Object 的 facet 机制实现，而非在单个 DO 内通过 session_id 列进行软隔离。每个 ChatSession 是一个独立的 DO facet，拥有独立的 SQLite 数据库、独立的消息表、独立的 Session 实例和独立的代理执行上下文。父实体 UserAssistant 仅维护子会话的索引元数据，不参与子会话的消息存储或代理推理。

隔离边界如下：(1) 消息历史完全隔离——每个 ChatSession 在各自的 assistant_messages 表中存储消息树，不同会话的消息不会混入同一 LLM 上下文；(2) 会话配置隔离——每个 ChatSession 的 think_config 独立存储模型选择、系统提示词、工具集等参数；(3) 上下文块部分隔离——每个会话可配置私有上下文块（如会话级 soul），同时通过 RemoteContextProvider 引用父实体中的共享 memory 块；(4) 扩展隔离——每个会话的 ExtensionManager 独立加载和管理沙箱化扩展；(5) WebSocket 连接直连子 DO——客户端通过嵌套路由与特定 ChatSession 建立 WebSocket，消息帧直接路由到该子 DO，父实体仅在连接建立时参与 onBeforeSubAgent 鉴权。

这种隔离设计的关键优势是并行性：不同会话的 agentic loop 可以在不同的 DO 实例中并行执行，不受 Durable Object 单线程模型的串行化限制。

### 共享资源代理机制

共享资源的真实状态由父实体 UserAssistant 统一管理，子会话通过受控代理接口访问。子会话不直接持有 Workspace 实例、MCP 连接或 OAuth 凭据，浏览器或子会话不能绕过代理生命周期直接执行原始 MCP 工具调用。

Workspace 代理：父实体持有一个 Workspace 实例（基于 @cloudflare/shell），所有子会话共享同一文件系统。子会话通过父实体的 RPC 接口操作文件：readFile(path)、writeFile(path, content)、listDir(path)、grep(pattern) 等。父实体的 Workspace 实例配置 onChange 回调，在文件创建/更新/删除时触发广播。父实体的 DO 单线程特性自然提供了文件操作的序列化，避免并发写入冲突。

MCP 代理：父实体维护 MCP 连接注册表，记录每个 MCP 服务器的连接状态和工具清单。子会话通过 listMcpTools() 获取可用工具列表（合并到 agentic loop 的 tool set 中），当 LLM 选择调用 MCP 工具时，子会话调用父实体的 executeMcpTool(name, args) 方法，由父实体代理执行真实的 MCP 工具调用并返回结果。此设计确保：(1) MCP 连接在用户级别建立一次，多个会话复用；(2) 子会话无法绕过父实体直接访问 MCP 服务器；(3) 父实体可以实施调用频率限制、参数校验和审计日志。

OAuth 代理：父实体存储 OAuth 授权结果（access token、refresh token、过期时间）。子会话通过 getOAuthToken(provider) 获取有效的访问凭据。父实体在凭据即将过期时自动刷新 token。子会话不接触原始 OAuth 流程和密钥材料。

### 访问控制机制

系统通过三层访问控制防止未授权访问子会话和共享资源。第一层在 Worker 入口，由 routeAgentRequest 解析嵌套 URL 路径 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}，将请求路由到父实体；第二层在父实体的 onBeforeSubAgent 钩子中，通过 hasSubAgent(className, name) 检查子会话是否已在父实体的子代理注册表中登记；第三层在子实体的 onBeforeConnect/onBeforeRequest 钩子中实施会话级鉴权。

访问控制的关键规则：(1) 子会话只能通过父实体注册表登记的方式创建——即只能通过父实体的 createChat() 方法创建，不能通过猜测 session id 直接访问或创建未登记的子会话；(2) 父实体的 onBeforeSubAgent 默认返回 404 拒绝未登记的子会话请求，防止 URL 枚举攻击；(3) 子会话通过 this.parentAgent(UserAssistant) 获取父实体引用，RPC 调用受限于父实体暴露的 @callable 方法签名，无法访问父实体的内部状态；(4) MCP 工具调用必须经过父实体的 executeMcpTool 代理方法，父实体可在此实施白名单校验、参数合法性和频率限制。

客户端重连行为也受访问控制约束：当子会话被删除后，客户端尝试重连时 onBeforeSubAgent 返回 404，useAgent 客户端钩子检测到 HTTP 4xx 响应后标记为终端错误并停止重试，区别于网络瞬断的 5xx/WebSocket 关闭重试策略。

### 实时文件变更同步

文件变更的实时通知通过父实体的广播机制实现，而非轮询。当子会话通过代理接口修改 Workspace 文件时，父实体的 Workspace 实例触发 onChange 回调，该回调执行两个动作：(1) 调用 this.setState() 更新父实体的广播状态，将文件变更事件（path、operation、timestamp）推送到所有连接到父实体的客户端；(2) 调用 this.broadcast() 向所有已连接的 WebSocket 客户端发送 file-changed 事件。

前端通过 useAgent 钩子的 state 订阅机制接收文件变更。当多个浏览器标签页或会话面板同时打开时，每个标签页通过独立的 WebSocket 连接（直连各自的 ChatSession 子 DO）进行聊天交互，但文件变更广播通过父 DO 的广播通道统一推送。具体流程如下：(1) ChatSession A 的 agentic loop 调用父实体的 writeFile 修改文件；(2) 父实体 Workspace 写入完成后触发 onChange；(3) 父实体调用 setState({ fileChanges: [...] }) 和 broadcast({ type: 'file-changed', path, op })；(4) 所有连接到父实体（或子实体）的客户端通过状态同步收到变更通知；(5) 前端 UI 根据文件变更事件刷新文件树或编辑器内容。

该机制使用 Durable Object 内置的 WebSocket 广播基础设施，无需额外的 Pub/Sub 服务或轮询端点。文件变更的广播延迟为毫秒级，取决于 DO 的写入和广播执行时间。

### 定时任务调度机制

定时任务和全局任务由父实体 UserAssistant 统一调度，而非分散在每个子会话中。父实体通过 Agent 基类继承的调度能力（基于 DO Alarms）注册和管理定时任务。主要场景包括：(1) 跨会话摘要——定时扫描所有活跃子会话，生成跨会话的工作摘要或上下文摘要；(2) 过期会话清理——根据 last_active_at 时间戳软删除长期不活跃的子会话及其 DO 存储；(3) OAuth 凭据刷新——在凭据过期前主动刷新，确保子会话调用时始终获得有效凭据；(4) MCP 连接健康检查——定期检测 MCP 服务器连接状态，对断开的连接尝试重连。

定时任务通过 this.schedule(interval, callback) 注册，任务执行在父 DO 的上下文中。由于父 DO 是单线程的，定时任务与子会话的资源代理请求共享同一执行线程，不会产生并发竞争。任务执行结果可通过 setState 广播到所有连接的客户端。调度器在 DO 休眠（hibernation）后通过 DO Alarm 机制自动唤醒，无需外部 cron 服务。

### 技术效果

本方案通过在 Durable Object 框架上构建双层代理架构，实现了以下技术效果：

(1) 会话并行性：每个聊天会话作为独立 DO 实例运行，不同会话的 LLM 推理、工具调用和流式输出可并行执行，避免单 DO 串行化的性能瓶颈。这与 Durable Object 的隔离性模型天然契合。

(2) 资源共享与复用：MCP 连接和 OAuth 授权在用户级别建立一次，所有会话通过代理接口复用，减少连接开销和重复授权流程。Workspace 文件在所有会话间即时共享，无需文件同步协议。

(3) 安全边界清晰：子会话无法绕过父实体的受控代理直接访问外部资源，MCP 工具调用必须经过父实体的校验和代理执行，构成结构性的安全边界而非约定性约束。

(4) 实时同步低延迟：文件变更通过 DO 内置广播机制推送，延迟为毫秒级，无需轮询或外部消息队列。

(5) 架构复用：方案复用现有的 Agent 基类、Think 基类、subAgent facet 机制、Workspace、Session、McpAgent、RemoteContextProvider、嵌套路由（routeSubAgentRequest）和 useAgent 客户端钩子，无需引入新的基础设施。

### 风险与待确认点

以下为本方案中需要后续确认或存在已知局限的风险点：

(1) 父实体单线程瓶颈：虽然子会话的推理可并行，但所有共享资源访问（Workspace 读写、MCP 工具调用）都经过父实体，在高并发场景下父 DO 可能成为瓶颈。缓解措施：父实体的 Workspace 操作和 MCP 代理调用通常为轻量级 RPC 转发，实际瓶颈概率低；如有需要可引入读写分离或缓存层。

(2) 跨会话 FTS 搜索性能：searchMessages 跨 N 个会话需要 N 次并行 RPC 调用，在活跃会话数超过 50 时性能下降。后续可引入父实体侧 FTS 索引来优化。

(3) 共享 memory 的 read-modify-write 竞争：子会话 A 读取共享 memory、子会话 B 写入、子会话 A 写回可能导致丢失更新。缓解措施：推荐使用 appendSharedContext 进行增量追加，预留 setSharedContext 用于全量替换场景并文档化风险。

(4) 父实体冷启动延迟：子会话首次访问共享资源时需要唤醒父实体（如果父 DO 处于休眠状态），增加一次 RPC 往返延迟。通常每个会话每回合仅触发一次，且 Think 的 withCachedPrompt 缓存策略会冻结系统提示词减少调用频率。

(5) ChatSession 类与顶层 DO binding 命名冲突：如果 ChatSession 类名同时出现在 wrangler.jsonc 的顶层绑定和 facet 子代理中，URL 路由可能混淆。解决方案：文档化约定——作为子代理使用的类不应同时暴露为顶层 DO 绑定。

(6) 定时任务执行时长限制：DO Alarm 回调有执行时间限制，长时间运行的跨会话摘要任务需要拆分为多次调度或使用 fiber（runFiber）进行断点续跑。
