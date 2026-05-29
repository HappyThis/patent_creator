## 技术方案

本技术方案基于 Cloudflare Agents SDK 现有架构（Durable Object 风格的 Agent、子 Agent 路由、MCP 客户端管理、Think Agent、Workspace 虚拟文件系统、Session 消息树存储、调度与 Fiber 机制），提出一种用户级父 Agent + 会话级子 Agent 的两层组合架构，实现多会话助手系统中的会话隔离、共享资源代理、访问控制、实时同步和全局任务调度。

### 整体架构：用户级父 Agent 与会话级子 Agent

系统采用两层 Durable Object 组合架构：每个用户对应一个 UserAgent（父 Agent，作为 Durable Object 实例），用户下的每个聊天会话对应一个 SessionAgent（子 Agent，作为 UserAgent 的 facet 子对象）。UserAgent 持有用户级共享资源的真实状态，SessionAgent 持有会话级隔离状态，通过受控 RPC 代理访问共享资源。

UserAgent 的职责包括：维护会话索引（会话 ID、标题、创建/更新时间戳）；持有唯一的 Workspace 实例（虚拟文件系统）；持有 MCP 客户端管理器及其 OAuth 授权状态；通过 alarm 和 schedule 机制管理用户级定时任务；向所有已连接的浏览器标签页广播文件变更等实时信号；为会话创建和访问提供授权校验。

SessionAgent 的职责包括：独立维护本会话的消息历史、分支树、上下文块和配置；执行 Agentic Loop（LLM 调用、工具执行、流式输出）；通过 RemoteWorkspaceProxy 和 RemoteMCPProxy 间接访问共享资源；处理 WebSocket 连接、流式传输和断线恢复。

### 会话隔离机制

每个 SessionAgent 作为 UserAgent 的 facet 子对象运行，拥有独立的 Durable Object ID、SQLite 数据库和存储空间。通过 ctx.facets 机制创建，每个会话获得独立的 DO 实例，其消息历史、分支树、上下文块（Context Blocks）、压缩覆盖（Compaction Overlays）和会话级配置均存储在子 Agent 的 SQLite 中，与父 Agent 及其他会话完全隔离。

会话的创建流程：浏览器通过 WebSocket 连接到 UserAgent，调用 createSession RPC 方法；UserAgent 在校验用户身份后，生成唯一会话 ID，在会话索引表中写入记录，通过 this.subAgent(SessionAgent, sessionId) 创建子 Agent facet，返回会话元数据（ID、标题、创建时间）；浏览器随后通过子 Agent 路由 URL（/sub/SessionAgent/{sessionId}）建立到该会话的 WebSocket 连接。会话的删除流程：UserAgent 校验请求者身份后，将会话索引标记为软删除（deleted_at 时间戳），广播更新后的会话列表，然后通过 this.deleteSubAgent 永久移除子 Agent 及其存储。软删除窗口期内，重新创建相同 ID 的会话将清除旧记录并生成新的 generation 计数器，防止过期连接重新接入。

### 共享 Workspace 代理机制

Workspace 文件系统由 UserAgent 唯一持有，采用 SQLite + R2 混合存储架构（小文件内联在 SQLite 中，大文件溢出到 R2），提供 POSIX-like 文件操作（读、写、删除、目录创建、复制、移动、符号链接、glob 匹配、diff 比较）。所有会话共享同一批文件，但文件操作必须通过 RemoteWorkspaceProxy 间接执行。

RemoteWorkspaceProxy 是一个实现 Workspace 操作接口的代理对象，驻留在 SessionAgent 中。其核心机制如下：SessionAgent 通过 this.parentAgent(UserAgent) 获取到父 Agent 的 RPC 存根；RemoteWorkspaceProxy 的每个文件操作方法（readFile、writeFile、readDir、glob、stat 等）内部调用父 Agent 上对应的 @callable RPC 方法；UserAgent 在收到 RPC 调用后，在自己持有的 Workspace 实例上执行实际操作，将结果返回给子会话。这一代理模式确保了：所有文件操作经过单一写入路径（父 Agent），避免了多会话并发写入同一文件时的冲突问题（Durable Object 单线程执行保证串行化）；子会话无法绕过代理直接操作文件系统，保持了架构约束的强制力；Workspace 的 onChange 回调可统一触发广播逻辑。

### 共享 MCP 连接与 OAuth 代理机制

MCP（Model Context Protocol）服务器连接和 OAuth 授权由 UserAgent 集中管理。UserAgent 的 this.mcp（MCPClientManager 实例）负责：建立和维护 MCP 服务器连接（支持 SSE、streamable HTTP、WebSocket 等传输协议）；管理 OAuth 2.0 授权流程（DurableObjectOAuthClientProvider 持久化令牌）；发现和缓存远程工具列表、资源和提示；处理连接失败重试和状态恢复。

SessionAgent 不直接持有 MCP 连接，而是通过 RemoteMCPProxy 访问。RemoteMCPProxy 是一个工具代理层，在 SessionAgent 的 Agentic Loop 中以工具集（ToolSet）形式呈现：SessionAgent 调用父 Agent 的 listRemoteTools RPC 方法获取当前可用的 MCP 工具列表（带缓存和失效策略）；当 LLM 决定调用某个 MCP 工具时，工具执行函数内部通过父 Agent 的 callRemoteTool RPC 方法代理执行；父 Agent 在自己的 MCP 客户端上执行实际的 toolCall，将结果返回给子会话。该机制的技术效果包括：用户只需授权一次 OAuth，所有会话即共享授权结果；MCP 连接的生命周期与用户绑定而非会话绑定，避免每个会话重复建立连接；连接失败和重连逻辑集中于父 Agent，子会话无需感知连接拓扑变化。

### 访问控制机制

系统通过三层机制防止未授权的会话访问。第一层——路由级授权：浏览器不能直接访问 SessionAgent，必须通过 UserAgent 的路由转发。所有子会话请求的 URL 格式为 /sub/SessionAgent/{sessionId}，由 sub-routing 模块解析。在 UserAgent 的 onBeforeSubAgent 钩子中，校验请求者是否有权访问目标 sessionId：检查会话索引表中是否存在该 ID 且未被软删除；验证请求携带的用户身份与会话所属用户一致。校验失败时拒绝路由，返回 403。

第二层——会话创建授权：createSession RPC 调用必须在已认证的 UserAgent WebSocket 连接上发起。UserAgent 根据连接关联的用户身份创建会话，会话 ID 由服务端生成（crypto.randomUUID()），客户端无法指定或猜测。第三层——会话 ID 生成计数器：软删除后重新创建的会话携带递增的 generation，子 Agent 在收到连接请求时校验 generation 匹配，拒绝过期连接的访问。这三层机制确保用户只能访问自己创建的会话，无法通过猜测 ID 越权访问其他用户的会话数据。

### 实时同步机制

文件变化需要实时通知所有已连接的浏览器标签页或会话面板，系统采用"写入端广播 + 连接端推送"模式，避免轮询开销。具体机制：当 SessionAgent 通过 RemoteWorkspaceProxy 写入文件时，RPC 调用到达 UserAgent；UserAgent 的 Workspace 执行实际写入操作，Workspace 的 onChange 回调被触发（携带变更路径、操作类型和会话来源信息）；UserAgent 调用 this.broadcast() 向所有已连接的 WebSocket 客户端广播文件变更事件（事件类型为 cf_agent_file_changed，负载包含变更路径和操作类型）；各浏览器标签页的客户端收到广播后，根据当前打开的会话上下文决定是否刷新文件视图。

多标签页同步：同一用户可能在多个浏览器标签页中打开不同会话（或同一会话）。UserAgent 维护所有活跃连接列表，广播覆盖所有连接。当用户在标签页 A 的会话中修改文件后，标签页 B 打开的另一会话也能实时看到文件变化。对于同一会话内的流式消息同步，复用现有的 ResumableStream 和 BroadcastState 机制：新打开的标签页可以从 SQLite 中恢复未完成的流式输出，避免丢失中间状态。定时刷新方面，系统不需要轮询端点，所有状态变更均由服务端主动推送，减少网络开销和延迟。

### 全局定时任务调度机制

定时任务和跨会话全局任务由 UserAgent 统一调度，不在子会话中分散执行。系统使用 Agent SDK 的 schedule 和 alarm 机制实现。schedule 支持三种触发类型：scheduled（指定日期时间单次执行）、delayed（相对延迟秒数）、cron（周期性表达式）。所有 schedule 调用在 UserAgent 上执行。子 Agent 不支持直接调用 schedule（调用时将抛出错误），这保证了全局任务调度的集中性和可预测性。

典型全局任务场景：跨会话摘要——定时任务触发 UserAgent 遍历所有活跃会话的子 Agent，通过 RPC 获取各会话最近的消息摘要，聚合生成用户级日/周报；文件清理——定时清理 Workspace 中超过保留期的临时文件；会话归档——扫描超过 N 天未活跃的会话，执行软删除和存储回收。任务执行依托 Agent SDK 的 alarm 机制：UserAgent 根据最近待执行的 schedule 时间设置 alarm；alarm 触发时，UserAgent 遍历到期任务并按回调名称分发执行；任务执行状态（running 标志位）持久化在 cf_agents_schedules 表中，支持崩溃恢复和重试。

### 完整处理流程示例

以下描述用户发起一次带文件读写和 MCP 工具调用的聊天请求的完整处理流程，说明各组件如何协同工作。

步骤一：浏览器通过 useAgent hook（配置 sub: [{ agent: 'session-agent', name: sessionId }]）建立指向 UserAgent 的 WebSocket 连接，sub-routing 层将连接路由到目标 SessionAgent。步骤二：用户在聊天输入框中发送消息，消息经由 WebSocket 到达 SessionAgent 的 Agentic Loop。步骤三：LLM 判断需要读取工作区文件，生成 toolCall；SessionAgent 的工具执行器调用 RemoteWorkspaceProxy.readFile(path)，该调用通过 this.parentAgent(UserAgent).readFile(path) RPC 到达 UserAgent；UserAgent 在其 Workspace 实例上执行读取，返回文件内容。步骤四：LLM 判断需要调用 MCP 工具（如查询数据库），生成 toolCall；SessionAgent 的工具执行器调用 RemoteMCPProxy，内部通过 this.parentAgent(UserAgent).callRemoteTool(serverName, toolName, args) RPC 到达 UserAgent；UserAgent 通过 MCPClientManager 向目标 MCP 服务器发起工具调用，返回结果。步骤五：LLM 生成最终文本响应，SessionAgent 将其作为新的消息树节点持久化到子 Agent 的 SQLite 中，并通过 WebSocket 流式推送给浏览器。步骤六：如果 LLM 在步骤三/四中执行了文件写入操作，UserAgent 的 Workspace onChange 回调触发，UserAgent 广播文件变更事件到所有已连接浏览器标签页。

### 技术效果

本方案的技术效果源于两层架构中各机制的协同作用。会话隔离方面：每个会话作为独立 Durable Object 运行，消息历史和配置不相互污染；一个会话的 LLM 推理不会阻塞另一个会话（各子 Agent 有独立的 DO 执行线程），实现了用户级的多会话并行。资源共享方面：Workspace、MCP 连接和 OAuth 授权在父 Agent 中集中管理，避免了每个会话重复建立连接、重复授权的开销；用户授权一次即可在所有会话中使用相同工具集。实时同步方面：基于 WebSocket 广播而非轮询，文件变更通知延迟在毫秒级，多标签页状态一致性由服务端主动推送保证。安全约束方面：子会话无法绕过父 Agent 直接执行原始 MCP 工具调用或直接操作文件系统，架构约束是结构性的而非约定性的。全局任务方面：定时任务集中在父 Agent，避免了任务在多个会话中重复执行或遗漏执行的问题。

### 与现有项目架构的对应关系

UserAgent 直接继承 Agent 基类（packages/agents/src/index.ts 中的 Agent 类），复用其 Durable Object 状态管理、SQLite 存储、WebSocket 连接管理、alarm 调度、subAgent 子对象管理和 MCPClientManager（this.mcp）。SessionAgent 复用 Think Agent（packages/think/）的 Chat 生命周期处理，包括 Agentic Loop（streamText 调用）、消息持久化、客户端工具、可恢复流式传输和断线恢复，同时复用 Session 模块（packages/agents/src/experimental/memory/session/）的树形消息存储、分支、压缩覆盖和上下文块机制。Workspace 复用 packages/shell 中的 Workspace 实现（SQLite + R2 混合存储）。子 Agent 路由复用已实现的 sub-routing 模块（packages/agents/src/sub-routing.ts），包括 routeSubAgentRequest、parseSubAgentPath、SUB_PREFIX 常量和 parentPath/parentAgent 父子引用。广播机制复用 Agent 基类的 this.broadcast() 方法和 BroadcastState 状态机。RemoteContextProvider 模式参考 design/rfc-think-multi-session.md 中的 RemoteContextProvider/RemoteSearchProvider 设计（通过 DO RPC 代理读写远程上下文块），本方案中的 RemoteWorkspaceProxy 和 RemoteMCPProxy 遵循相同的代理模式。

### 风险与待确认问题

以下是需要在实施前确认和后续验证的技术点。共享 Workspace 的并发语义：当前 Workspace 基于单 DO 的 SQLite，写入操作由 DO 单线程串行化，不会产生底层数据竞争。但多个会话可能同时对同一文件执行读-改-写操作，上层语义是 last-writer-wins。是否需要文件锁或乐观锁机制，取决于实际使用场景。RemoteMCPProxy 的工具发现延迟：每次 Agentic Loop 启动时，SessionAgent 需要通过 RPC 向 UserAgent 获取当前可用的 MCP 工具列表。如果 MCP 服务器数量较多，应实现工具列表缓存（缓存键基于服务器连接状态，连接变化时失效）。跨会话消息搜索的扩展性：当前设计中对所有子会话的 FTS 搜索采用 RPC 扇出方式（并行调用每个子会话的 session.searchMessages），在活跃会话数超过约 50 个时性能可能下降，后续可引入父 Agent 侧的集中 FTS 索引。Durable Object 休眠与连接恢复：UserAgent 和 SessionAgent 都可能因 inactivity 而休眠（hibernate），休眠后 WebSocket 连接断开，客户端需要通过现有的 resumable stream 机制重新连接和恢复状态。需确认休眠时间窗口是否满足所有全局定时任务的触发需求（alarm 在休眠时仍可唤醒 DO）。
