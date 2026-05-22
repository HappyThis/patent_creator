## 技术方案

本方案提出一种基于父-子 Agent 结构的多会话 assistant 系统。系统为每个用户创建一个用户级父 Agent（User Agent），负责管理共享资源和子会话生命周期；为用户的每个聊天会话创建一个会话级子 Agent（Chat Agent），作为独立的 agent 执行上下文。父 Agent 与子 Agent 之间通过 Durable Object 的 facet 机制建立父子关系，子 Agent 通过受控代理（controlled proxy）访问父 Agent 持有的共享资源，而非直接持有 MCP 连接、OAuth 凭据或 workspace 文件句柄。

### 父-子 Agent 架构

系统采用两级 Agent 层次结构：用户级父 Agent（UserAgent）与会话级子 Agent（ChatAgent）。UserAgent 继承自 Agent 基类（基于 Cloudflare Durable Objects），ChatAgent 继承自 Think 基类（即已有的单会话聊天 Agent），二者通过 subAgent()/parentAgent() 机制建立父子关系。每个 UserAgent 实例对应一个用户，每个 ChatAgent 实例对应一个聊天会话。

- UserAgent（父）：负责用户维度的共享资源管理，包括 MCP 服务器连接池、OAuth 授权状态、workspace 文件存储、共享上下文块（shared context blocks），以及子会话的创建、列举、重命名、删除和跨会话搜索。
- ChatAgent（子）：负责单个会话的独立执行上下文，包括消息历史、会话内配置（configure()）、扩展（extensions）、会话内 memory 上下文块、agentic loop 执行流程和流式响应。ChatAgent 不直接持有 MCP 连接或 OAuth 凭据。

### 会话隔离与资源共享边界

系统在父 Agent 和子 Agent 之间划分了清晰的职责边界，明确哪些状态按会话隔离、哪些资源按用户共享。

按会话隔离的状态（由 ChatAgent 独立持有）：消息历史（Session 的 assistant_messages 表，含树形分支和 compact 覆盖层）、会话内配置（think_config 表）、会话内上下文块（如 memory block，通过 Session 的 withContext 配置）、扩展工具（ExtensionManager 管理的沙箱化 Worker 工具）、agentic loop 执行状态（TurnQueue 序列化队列、ResumableStream 缓冲块、abort 控制器）。每个 ChatAgent 拥有独立的 Durable Object 实例和 SQLite 存储，消息历史和会话配置不会在不同会话间串扰。

按用户共享的资源（由 UserAgent 持有并代理）：workspace 文件存储（基于 @cloudflare/shell 的 Workspace，SQLite 内联小文件 + R2 溢出大文件，按用户维度一个 UserAgent 一个 Workspace 实例）、MCP 服务器连接（UserAgent 通过 MCPClientManager 维护与外部 MCP 服务器的 HTTP/SSE/RPC 连接，连接状态和 OAuth token 持久化在 UserAgent 的 DO 存储中）、OAuth 授权结果（通过 DurableObjectOAuthClientProvider 持久化 token，用户授权一次后所有子会话可用）、共享上下文块（如跨会话的 user_memory，存储在 UserAgent 的 SQLite 中，通过 @callable 方法暴露访问接口）。

### 共享资源的受控代理访问

子 Agent（ChatAgent）不直接持有 MCP 客户端连接、OAuth token 或 workspace 文件句柄。所有对共享资源的访问都通过父 Agent（UserAgent）的受控代理完成。具体机制如下：

MCP 工具调用代理：当 ChatAgent 的 agentic loop 需要调用外部 MCP 工具时，ChatAgent 的工具集（ToolSet）中不直接包含 MCP 连接产生的工具。相反，ChatAgent 通过 parentAgent(UserAgent) 获取父 Agent 的 RPC 引用，调用父 Agent 上暴露的 @callable 方法（如 executeMcpTool(serverId, toolName, args)），由父 Agent 在其自身的 MCPClientManager 中定位对应连接、执行工具调用并返回结果。这确保 MCP 连接的生命周期由父 Agent 统一管理，子会话无法绕过父 Agent 直接与外部 MCP 服务器通信。

Workspace 文件访问代理：Workspace 实例由 UserAgent 持有。ChatAgent 通过父 Agent 的 @callable 方法（如 readFile(path)、writeFile(path, content)、listDirectory(path)、glob(pattern)）进行文件操作。UserAgent 的 Workspace 实例配置了 onChange 回调，当文件发生变更时触发实时通知（见“实时同步机制”）。文件内容按用户维度共享，不同会话看到的始终是同一份文件状态。

共享上下文块代理：UserAgent 维护跨会话的共享上下文块（如 user_memory），ChatAgent 通过 RemoteContextProvider 访问。RemoteContextProvider 是一个实现了 Session 的 ContextProvider 接口的远程代理：其 get()/set() 方法通过 DO RPC 调用 UserAgent 的 getSharedContext(label)/setSharedContext(label, content) 方法。UserAgent 上的共享上下文块读写在单个 DO 线程内串行化，避免并发写冲突。对追加型记忆更新，提供 appendSharedContext(label, delta) 方法，由父 Agent 原子执行“读取-追加-写入”，避免子会话间的丢失更新。

### 访问控制与安全机制

系统通过多层访问控制确保子会话不被越权创建或访问。

第一层——URL 路由级隔离：子 Agent（ChatAgent）通过嵌套 URL 模式对外可寻址：/agents/{UserAgent-class}/{user-id}/sub/{ChatAgent-class}/{session-id}。父 Agent 的 onBeforeSubAgent 中间件钩子在请求到达子 Agent 之前执行，可进行身份验证、会话存在性检查和请求改写。该钩子支持三种返回模式：返回 void（默认放行）、返回修改后的 Request（转发改写后的请求）、返回 Response（直接应答，不唤醒子 Agent）。

第二层——子 Agent 注册表：UserAgent 维护一个 cf_agents_sub_agents 注册表（SQLite 表），记录每个子 Agent 的类名、名称和创建时间。subAgent() 调用时自动写入注册表，deleteSubAgent() 调用时移除。在 onBeforeSubAgent 中通过 hasSubAgent(className, name) 检查：未登记的会话 id 直接返回 404，防止通过猜测会话 id 访问或创建未授权的子会话。父 Agent 还可通过 listSubAgents(className) 枚举所有子会话，用于侧边栏会话列表展示。

第三层——客户端重试硬化：客户端 hook（useAgent / useChats）在遭遇 HTTP 4xx 响应或 WebSocket 关闭码 1008/4000-4999 时判定为终端错误，停止自动重连。这防止了被拒绝的客户端无限重试。

第四层——禁止绕过 Agent 生命周期：MCP 工具调用必须经过父 Agent 的 @callable 代理方法，浏览器端或子会话无法直接获取 MCP 服务器的连接凭据或发起原始 MCP 协议请求。子 Agent 持有的 RPC stub 只能调用父 Agent 显式暴露的 @callable 方法，且参数和返回值受 Durable Object RPC 的结构化克隆约束。

### 实时同步机制

系统利用 Agent 框架的 WebSocket 广播能力和 Workspace 的 onChange 回调，实现文件变更的实时多标签同步，减少轮询。

文件变更通知路径：当任一 ChatAgent 通过父 Agent 的代理方法修改 workspace 文件时，UserAgent 的 Workspace 实例触发 onChange 回调。该回调调用 this.broadcast()（Agent 基类的 WebSocket 广播方法），向所有连接到 UserAgent 的客户端发送文件变更事件，包含变更类型（create/update/delete）、文件路径和可选的文件列表摘要。同时，UserAgent 调用 setState() 更新其广播状态，触发所有连接客户端的 onStateUpdate 回调。前端通过 useAgent 的 onStateUpdate 接收状态变更并刷新文件视图。

会话状态同步：UserAgent 维护 chats 状态数组（通过 initialState 和 setState），包含所有子会话的摘要信息（id、标题、创建/更新时间、最后消息预览）。当发生会话创建、重命名、删除时，setState 自动广播到所有连接到 UserAgent 的客户端。侧边栏通过 useAgent 的 onStateUpdate 实时更新会话列表。

消息级别的实时同步：每个 ChatAgent 继承自 Think，已具备 WebSocket 连接管理和消息广播能力。当用户在标签页 A 中与 ChatAgent 交互时，同一 ChatAgent 的其他连接（如标签页 B）通过 cf_agent_chat_* 协议实时收到消息更新。ChatAgent 的 ResumableStream 机制确保断线重连后流式响应可从缓冲块恢复。

### 定时任务与全局调度

定时任务和跨会话全局任务由 UserAgent 统一调度，不散落在子会话中。这避免每个子 Agent 独立维护定时器导致的资源浪费和协调困难。

UserAgent 利用 Agent 基类的 schedule() 和 scheduleEvery() 方法注册定时任务。schedule() 支持四种调度类型：scheduled（指定时间单次执行）、delayed（延迟秒数后单次执行）、cron（基于 cron 表达式的周期性执行）、interval（固定秒数间隔的周期性执行）。scheduleEvery() 对周期性任务提供幂等保证：相同 callback、intervalSeconds 和 payload 的多次调用只创建一条调度记录，适合在 onStart() 中声明式注册。

全局任务示例：跨会话摘要——UserAgent 注册一个 cron 调度（如每日凌晨），在回调中通过 listSubAgents() 枚举所有 ChatAgent，对每个子会话调用其 RPC 方法获取近期消息摘要，汇总后写入共享上下文块或通过邮件发送。空闲会话清理——UserAgent 注册 interval 调度，定期扫描子会话的最后活动时间，对超过阈值的会话执行软删除（标记 deleted_at 并由后续 TTL 回收 DO 存储）。

调度执行保证：Agent 基类的 alarm 机制确保 DO 在休眠期间能被定时唤醒执行调度任务。keepAlive() 机制可防止长时间运行的全局任务中途被 evict。调度任务的回调执行失败时，通过配置的 retry 选项自动重试，并触发 schedule:error 观察事件。

### 关键处理流程

以下描述多会话 assistant 系统中几个核心操作的端到端处理流程。

创建新会话：用户在前端侧边栏点击“新建会话”。前端通过 useChats() hook 调用 UserAgent 的 createChat() @callable 方法。UserAgent 生成唯一会话 id（默认 crypto.randomUUID()），在 chats_index 表中写入记录，调用 subAgent(ChatAgent, id) 创建子 Agent facet（实际 DO 延迟创建，直到首次请求到达），通过 setState() 广播更新后的 chats 列表到所有连接客户端。前端收到状态更新后，通过 useAgent({ agent, name, sub: [{ agent: chatAgent, name: id }] }) 建立到子 Agent 的 WebSocket 连接。

用户发送消息：前端通过已建立的 WebSocket 连接向 ChatAgent 发送 cf_agent_chat_* 协议消息。ChatAgent 的 TurnQueue 确保单会话内消息串行处理。ChatAgent 通过 assembleContext() 组装上下文（从 Session 获取冻结的系统提示词块、获取消息历史、应用 compact 覆盖层和截断），合并工具集（基础工具 + 扩展工具 + 会话上下文工具 + MCP 代理工具），调用 streamText() 执行 agentic loop。tool-call 步骤中如遇到 MCP 工具调用，ChatAgent 通过 parentAgent(UserAgent) 的 RPC 代理方法将调用转发到 UserAgent 的 MCPClientManager。流式结果通过 WebSocket 逐块广播到所有连接到该 ChatAgent 的客户端。完成后持久化 assistant 消息到 Session，触发 onChatResponse 钩子。

文件修改同步：ChatAgent 在执行 tool-call 过程中通过父 Agent 代理修改 workspace 文件。UserAgent 的 Workspace.onChange 回调被触发，调用 this.broadcast() 向所有连接到 UserAgent 的客户端发送文件变更事件。同时 setState() 更新状态。前端各标签页通过 onStateUpdate 或自定义消息处理接收变更并刷新。

跨会话搜索：前端通过 useChats() hook 调用 UserAgent 的 searchMessages(query) 方法。UserAgent 通过 listSubAgents() 获取所有活跃子会话，并行向每个 ChatAgent 发起 RPC 调用其 session.searchMessages(query)，收集结果并按相关度合并排序后返回。

### 技术效果

本方案基于已有 Agent 框架基础设施，通过父-子 Agent 架构实现多会话 assistant，带来以下技术效果：

- 会话级并行：每个 ChatAgent 是独立的 Durable Object 实例，不同会话的消息处理在不同 DO 中并行执行，不会因单 DO 单线程限制而串行化。
- 资源共享复用：MCP 连接和 OAuth token 在 UserAgent 中集中管理，用户授权一次后所有会话复用，避免重复建立连接和重复授权。
- 存储隔离与共享并存：消息历史按会话隔离（每个 ChatAgent 独立 SQLite），workspace 文件按用户共享（UserAgent 的 Workspace 实例），互不干扰。
- 实时无轮询同步：利用 WebSocket 广播和 setState 机制实现文件变更和会话列表的推模式同步，无需客户端轮询。
- 安全访问控制：子会话通过受控代理访问共享资源，onBeforeSubAgent 钩子和子 Agent 注册表防止越权访问，客户端无法绕过 Agent 生命周期直接执行 MCP 工具调用。
- 全局任务集中调度：定时任务由 UserAgent 统一管理，利用 alarm 机制在休眠期间自动唤醒，避免每个子会话独立维护定时器。
- 复用已有体系：方案充分复用 Think 的 agentic loop、Session 的消息树和 compact、ResumableStream 的流恢复、TurnQueue 的串行化、Workspace 的文件存储、MCPClientManager 的连接管理等已有组件。

### 风险与待确认问题

以下为本方案在当前项目环境下需要后续确认的风险点和开放问题：

- Chats 父 Agent 基类尚未落地：设计文档 rfc-think-multi-session.md 中的 Chats 基类为 Proposed 状态，尚未在 @cloudflare/think 包中实现。当前需自行基于 Agent 基类编写 UserAgent，或等待 Chats 基类正式发布后再迁移。
- RemoteContextProvider 尚未实现：设计文档 rfc-think-multi-session.md 中提出的 RemoteContextProvider / RemoteSearchProvider 类尚未在 agents/experimental/memory/session/providers/ 中实现。当前需自行实现跨 DO 的上下文代理。
- MCP 工具代理的 ToolSet 集成：将父 Agent 的 MCP 工具以代理工具的形式注入子 Agent 的 ToolSet 需要自定义适配层——需将父 Agent 的 @callable executeMcpTool 包装为符合 AI SDK Tool 接口的工具定义（含 name、description、parameters schema 和 execute 函数）。该适配层的具体实现方式（复用 MCPClientManager 的工具 schema 动态生成 vs. 手动声明）待确定。
- 跨会话搜索的扩展性：searchMessages 的扇出 RPC 方案在活跃会话数超过约 50 个时性能下降。长期方案需要父 Agent 维护集中式 FTS 索引，由子 Agent 的 onChatResponse 钩子增量更新。这在 v1 中可暂不实现。
- workspace 并发写冲突：多个会话同时修改同一 workspace 文件时，UserAgent 的 DO 单线程串行化保证了写入顺序，但存在后写覆盖先写的丢失更新风险。对于代码文件场景，可接受的方案是最后一次写入生效；对于需要合并的场景，未来可引入文件级锁或操作队列。
- 父 Agent 成为单点瓶颈：所有子会话的 MCP 工具调用和 workspace 文件访问都经过父 Agent，父 Agent 的 DO 单线程模型在高并发场景下可能成为瓶颈。缓解措施包括：MCP 连接本身是持久化的（连接复用而非每次建立）、workspace 文件操作通常很快（内联小文件在 SQLite 中直接读写）、以及利用 keepAlive 防止频繁冷启动。
- 子 Agent 对外寻址的类名冲突：如 ChatAgent 类同时注册为顶层 DO binding 和子 Agent facet，URL /agents/chat/abc 和 /agents/inbox/alice/sub/chat/abc 将解析到不同 DO 实例。实现时需确保 ChatAgent 不作为顶层 binding 暴露。
