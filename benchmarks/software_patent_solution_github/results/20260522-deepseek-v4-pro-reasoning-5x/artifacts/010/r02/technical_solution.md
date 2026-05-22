## 技术方案

本技术方案提出一种基于父子代理（Parent-Child Agent）结构的多会话智能助手系统。系统将代理拆分为用户级父实体（Assistant Directory）和会话级子实体（Chat Agent）两个层级：父实体负责会话目录管理、共享资源生命周期维护、跨会话全局任务调度和访问控制；子实体负责单会话的消息历史、个性化配置、上下文记忆和推理执行。子会话通过父实体的受控代理（controlled proxy）访问 workspace 文件、MCP 工具连接和 OAuth 授权凭据等用户级共享资源，浏览器或子会话不能绕过代理生命周期直接执行原始 MCP 工具调用。整体架构复用了已有的 Durable Object agent 基类、Think agent 框架、workspace 虚拟文件系统、MCP 客户端连接管理、子代理路由原语和 chat recovery 体系。

### 整体架构

系统采用两层代理拓扑结构。每个经过认证的用户拥有一个唯一的用户级父 Durable Object（Assistant Directory），该父 DO 维护该用户的所有聊天会话目录、共享资源和全局调度。每个聊天会话对应一个独立的会话级子 Durable Object（Chat Agent），作为父 DO 的 facet 子代理存在。父 DO 和子 DO 各自拥有独立的 SQLite 存储，通过 Durable Object RPC 进行受控通信。

客户端连接路径：浏览器通过 WebSocket 分别连接父 DO（获取侧边栏会话列表状态）和当前活跃子 DO（进行聊天交互）。子 DO 的 URL 采用嵌套路由形式 /agents/{父类名}/{用户名}/sub/{子类名}/{会话id}。父 DO 通过 onBeforeSubAgent 钩子对所有进入子 DO 的请求进行访问控制判决。子 DO 通过 parentAgent(Cls) 方法获取父 DO 的 RPC 存根，在需要时调用父 DO 暴露的 @callable 方法访问共享资源。

这种两层结构的关键优势在于：会话间的并行推理天然隔离（每个子 DO 独立单线程调度），共享资源的状态由一个统一位置管理避免竞争，定时任务和跨会话操作由父实体集中负责而不散落在各子会话中。

### 用户级父实体（Assistant Directory）

用户级父实体（Assistant Directory）是一个继承自 Agent 基类的 Durable Object，每个认证用户持有一个实例。其核心职责包括：

- 会话目录管理：维护会话索引表（chat_meta），记录每个子会话的 id、标题、创建时间、最近更新时间、最后消息预览。目录状态通过 agent.setState() 广播到所有连接的客户端侧边栏。
- 子代理注册表维护：通过 subAgent(ChildClass, id) 创建子会话 DO 时，框架自动在父实体的 SQLite 中写入 cf_agents_sub_agents 注册表行；deleteSubAgent() 则移除对应行。hasSubAgent() 和 listSubAgents() 基于该注册表提供存在性检查和枚举能力。
- 访问控制：通过 onBeforeSubAgent(req, {className, name}) 钩子对所有进入子 DO 的 HTTP/WebSocket 请求进行判决。默认实现为严格注册表门控——仅当子会话已在注册表中存在时才放行，否则返回 404。这防止了用户通过猜测会话 id 访问未登记的子会话。
- 共享资源管理：作为 workspace 文件系统实例、MCP 服务器连接池和 OAuth 授权凭据的宿主。这些资源在父 DO 的 SQLite 存储中按用户维度持久化，子会话通过 RPC 调用父 DO 暴露的 @callable 方法受控访问。
- 全局任务调度：拥有定时任务（scheduled/delayed/cron）的生命周期管理。父 DO 的 alarm 机制触发后执行跨会话摘要、清理过期会话等全局任务，必要时通过 subAgent() 获取子 DO 存根并向子 DO 发起 RPC 调用。

### 会话级子实体（Chat Agent）

会话级子实体（Chat Agent）是每个聊天会话的独立 Durable Object，作为父 DO 的 facet 子代理运行。其核心职责和隔离边界如下：

- 消息历史隔离：每个子 DO 拥有独立的 SQLite 存储，消息以树结构存储（支持分支和 fork），不同会话的消息历史完全隔离。
- Session 上下文隔离：每个子 DO 维护自己的 Session 实例，包括系统提示缓存、上下文块（如 'soul' 身份定义、'memory' 事实记忆）、消息压缩（compaction）状态和全文搜索（FTS5）索引。不同会话的配置和记忆互不干扰。
- 推理执行：子 DO 内部运行完整的 agentic loop（ Think agent 框架），包括工具调用、多步推理、流式输出、断点续传（resumable stream）和中止处理（abort）。TurnQueue 保证同一会话内的请求串行化。
- 个性化配置：模型选择、系统提示词、扩展加载、工具注册等配置按会话维度隔离。会话可以通过 sessionId 级别的配置表存储个性化设置。
- 通过 RPC 访问父资源：子 DO 在需要读写共享资源时，通过 this.parentAgent(ParentClass) 获取父 DO 的类型化 RPC 存根，调用父 DO 的 @callable 方法。子 DO 和浏览器客户端均不能绕过父 DO 直接执行原始 MCP 工具调用或直接操作 workspace 文件——这些操作必须通过父 DO 的受控代理接口完成。

### 父子路由与访问控制

系统通过嵌套 URL 路由和父实体中间件钩子实现子代理的外部可寻址性和受控访问。默认 URL 模式为：

/agents/{父类名-kebab-case}/{父实例名}/sub/{子类名-kebab-case}/{子实例名}

该模式支持递归嵌套（子代理可以再有子代理）。路由解析流程如下：

1. Worker 入口的 routeAgentRequest 解析顶层 URL，定位父 DO 实例并转发请求。
2. 父 DO 基类的 fetch 方法检测 URL 中的 /sub/ 段，提取子代理的类名和实例名。
3. 触发 onBeforeSubAgent(req, {className, name}) 钩子。钩子可返回 void（放行原始请求）、Request（转发修改后的请求）或 Response（短路返回，不唤醒子 DO）。
4. 若钩子放行，框架解析剩余路径，通过 ctx.facets.get() 获取子 DO 的 Fetcher，重写请求 URL 后转发。WebSocket 升级在此时完成，之后帧直接路由到子 DO，父 DO 退出热路径。

访问控制的关键机制：父实体的 onBeforeSubAgent 实现严格注册表门控——调用 this.hasSubAgent(className, name) 检查子会话是否已在注册表中。未注册的请求在父 DO 层面返回 404，框架不会唤醒子 DO。客户端 useAgent hook 在收到 HTTP 4xx 或 WebSocket close code 1008/4000-4999 时停止重连，将其视为终端错误。

客户端通过 useAgent({ agent, name, sub: [{ agent, name }] }) 的扁平 sub 数组指定目标路径。父代理名和子代理名均可包含 URL 编码的任意字符（斜杠、空格、Unicode 等），空字符（\0）被保留用于框架内部的 facet 组合键，在注册时被运行时检查拒绝。

### 共享资源代理层

子会话需要访问 workspace 文件、MCP 工具和 OAuth 授权结果等资源时，不能直接操作这些资源，而必须通过父 DO 的受控代理接口。父 DO 作为这些资源的唯一宿主和管理者，暴露类型安全的 @callable RPC 方法供子 DO 调用。

Workspace 文件共享：父 DO 持有一个 Workspace 实例（基于 DO SQLite + 可选 R2 溢出的虚拟文件系统）。所有子会话看到和修改的是同一份文件。Workspace 实例通过 onChange 回调在文件创建、更新或删除时触发 agent.broadcast()，将变更事件推送到所有连接的浏览器标签页。子 DO 在工具执行中需要读写文件时，通过父 DO 暴露的 getWorkspace() 等 RPC 方法获取文件内容或写入变更。

MCP 服务器连接共享：父 DO 负责 MCP 客户端连接的生命周期管理——连接建立、保活、重连和关闭。MCP 服务器列表存储在父 DO 的 SQLite 中（mcpservers 表），连接状态通过 MCPClientConnection 管理。子 DO 在推理循环中需要调用 MCP 工具时，通过父 DO 的 RPC 接口获取可用工具列表（listTools）并执行工具调用（callTool），而不是自己建立独立的 MCP 连接。这避免了每个子会话重复建立连接带来的资源浪费，并确保工具调用的统一鉴权和审计。

OAuth 授权凭据共享：MCP 服务器的 OAuth 授权流程由父 DO 统一处理。父 DO 使用 DurableObjectOAuthClientProvider 管理 OAuth 令牌的获取、刷新和持久化。用户完成一次 OAuth 授权后，所有子会话都可以通过父 DO 使用相同的授权凭据调用该 MCP 服务器的工具。授权回调 URL 通过 callbackPath 配置路由到父 DO。

共享记忆上下文：父 DO 维护一个 inbox_memory 表（按 label 索引的键值对），存储跨会话共享的用户记忆。子 DO 通过 RemoteContextProvider 将父 DO 的共享记忆块挂载到 Session 的上下文块中。RemoteContextProvider 封装了到父 DO 的 RPC 调用：get() 读取共享记忆、set() 全量替换、append() 增量追加。读取采用 fail-soft 策略——父 DO 不可达时返回 null，子会话继续工作但无共享记忆。记忆更新采用 appendSharedContext 优先策略避免 read-modify-write 丢失更新。

### 实时同步机制

系统通过以下机制实现文件变化、会话列表变化等共享状态向多个浏览器标签页或会话面板的实时推送，减少轮询开销：

- WebSocket 广播：父 DO 和子 DO 均通过 agent.broadcast() 向所有连接的 WebSocket 客户端推送状态变更。父 DO 在会话创建、删除、重命名或元数据更新后调用 this.setState() 触发广播，侧边栏收到新的 chats 数组后更新 UI。子 DO 在推理过程中通过广播推送流式消息块。
- Workspace 变更事件：Workspace 实例的 onChange 回调在文件创建、更新、删除操作后触发。父 DO 注册该回调，在回调中调用 agent.broadcast() 将文件变更事件（包含路径、操作类型）推送到所有连接的浏览器标签页，前端收到后可按需刷新文件树或触发编辑器热重载。
- 广播流状态机：客户端 useAgentChat 通过 broadcastTransition 状态机管理从 WebSocket 广播/恢复路径接收的流式消息。该状态机处理 chunk 应用、重放抑制（replay suppression）、完成/错误清理和 continuation 上下文追踪，确保多标签页同时打开同一会话时不会出现重复消息。

由于父 DO 的 Durable Object 单线程特性，对共享状态的写入天然串行化，不存在并发写入导致的广播乱序问题。子 DO 的 TurnQueue 确保同一会话内的请求按序处理，进一步保证消息顺序的正确性。

### 定时任务调度机制

定时任务和跨会话全局操作由父 DO 统一负责，不散落在各子会话中。调度机制基于 Durable Object alarm 和 scheduleSchema 实现三类调度模式：

- scheduled（定时执行）：指定 ISO 8601 日期时间，父 DO 在该时刻通过 alarm 触发指定任务。
- delayed（延迟执行）：指定相对延迟秒数，父 DO 在延迟后执行任务。
- cron（周期性执行）：标准 cron 表达式，父 DO 按周期重复触发任务，每次执行后自动设置下一次 alarm。

父 DO 的 alarm 处理流程：alarm 触发后，父 DO 根据调度配置执行对应任务。对于跨会话操作（如每日摘要），父 DO 通过 listSubAgents() 枚举所有子会话，依次通过 getSubAgentByName() 或 subAgent() 获取子 DO 的 RPC 存根，调用子 DO 的方法收集摘要信息，汇总后写入共享存储。子 DO 自身不使用独立 alarm（当前 Durable Object facet 不支持独立 alarm），所有依赖时间触发的操作都通过父 DO 协调。

scheduleSchema 提供自然语言到调度配置的解析能力，配合 getSchedulePrompt 生成 LLM 可理解的调度解析提示词，支持用户通过自然语言创建定时任务（如 '每天上午 9 点总结所有会话'）。

### 技术效果

本方案在以下方面产生可验证的技术效果：

- 会话并行隔离：每个子会话运行在独立 Durable Object 中，推理计算天然并行。一个会话的长时间工具调用不会阻塞其他会话的消息处理，因为每个子 DO 拥有独立的单线程调度。
- 共享资源一致性：workspace 文件、MCP 连接和 OAuth 凭据由父 DO 统一管理，子会话通过受控 RPC 访问。避免了多会话独立管理资源时的一致性问题（如重复连接、凭据过期不同步）和资源浪费。
- 安全访问控制：onBeforeSubAgent 注册表门控确保子 DO 仅在父 DO 显式创建后才可被外部访问。用户无法通过猜测会话 id 访问他人会话或创建未登记的影子会话。攻击面限定在父 DO 的钩子层面，子 DO 在被唤醒前即被拦截。
- 实时同步低延迟：Workspace 文件变更通过 onChange 回调 → agent.broadcast() → WebSocket 的路径推送到所有标签页，消除轮询延迟。会话列表变更同理通过 setState 广播。
- 全局任务集中调度：父 DO 的 alarm 机制统一管理定时任务生命周期，子会话无需各自维护 alarm，降低了调度逻辑的复杂度和 alarm 的资源开销。
- 架构复用：方案全部构建在已有的 Agent 基类、子代理路由、Think agent、Workspace、MCP 客户端和 chat recovery 体系之上，不引入新的基础设施依赖。

### 风险与待确认问题

以下为需要后续验证和确认的技术风险点：

- 父 DO 单线程瓶颈：所有子会话的共享资源访问都经过父 DO 的 RPC 调用。在高并发场景下（大量子会话同时请求 workspace 读写或共享记忆更新），父 DO 可能成为吞吐瓶颈。缓解方案包括：共享记忆读取采用 fail-soft 策略不影响子会话运行；workspace 读取可以考虑在子 DO 侧缓存文件元数据。
- 子 DO 不支持独立 alarm：当前 Durable Object facet 不支持独立 alarm，所有定时任务必须由父 DO 统一调度。这意味着子会话无法设定独立于父 DO 的定时行为（如单会话的消息定时清理），需要父 DO 通过枚举子会话并逐个 RPC 调用的方式模拟。
- 跨会话搜索的可扩展性：基于父 DO 向各子 DO 发出 RPC 调用并合并结果的 fanout 搜索模式，在会话数量超过约 50 个后性能下降明显。需要评估是否需要引入父 DO 侧的集中式 FTS 索引，由子 DO 在消息持久化时向父 DO 同步增量索引。
- Workspace 并发写入冲突：虽然父 DO 单线程保证了写入串行化，但两个不同子会话对同一文件路径的先后写入仍然是 last-write-wins。对于需要并发协作编辑的场景，需要额外的冲突解决机制（如 OT 或 CRDT）。
- 扩展（Extensions）在子代理中的兼容性：Think agent 的扩展加载机制需要在子代理（facet）环境中验证是否完全兼容，包括扩展的 hook 注册、生命周期和工具注入是否正常工作。
- MCP OAuth 回调路径配置：当子 DO 启用 sendIdentityOnConnect 时，addMcpServer 的 callbackPath 默认值可能指向不存在的路径，需要显式配置或框架层面修复回调 URL 的生成逻辑。
- 父 DO 冷启动延迟：子 DO 首次通过 RemoteContextProvider 访问父 DO 的共享记忆时，若父 DO 处于休眠状态，需要等待其冷启动，增加首轮推理延迟。可通过 Session 的 withCachedPrompt 缓存冻结点以减少跨轮次唤醒次数。
