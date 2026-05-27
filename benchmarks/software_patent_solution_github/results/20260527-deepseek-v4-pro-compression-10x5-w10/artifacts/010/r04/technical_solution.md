## 技术方案

本技术方案提出一种基于父子 Durable Object（DO）分层架构的多会话助手系统。系统将一个用户的所有聊天会话组织为一个用户级父 Agent 和多个会话级子 Agent 的树状拓扑：父 Agent 持有一个 Durable Object，负责管理会话目录、共享工作区文件、共享 MCP（Model Context Protocol）工具连接、共享 OAuth 授权凭据、实时文件变更通知广播以及跨会话定时任务调度；每个子 Agent 持有一个独立的 Durable Object，作为单个聊天会话的执行上下文，拥有独立的消息历史、分支树、上下文块、扩展配置和推理循环状态。子 Agent 不能绕过父 Agent 直接执行原始 MCP 工具调用或直接操作 MCP 服务端连接；所有对共享资源的访问均通过父 Agent 提供的受控代理接口完成。客户端通过嵌套子 Agent 路由机制直接与目标子 Agent 建立 WebSocket 连接，父 Agent 仅在连接建立和 HTTP 请求时执行访问控制校验，不在消息热路径上成为瓶颈。

### 一、整体架构

系统由三类核心实体组成：（1）UserAssistant（用户级父 Agent），（2）ChatSession（会话级子 Agent），（3）浏览器客户端。一个用户对应一个 UserAssistant DO 实例，该实例通过 Durable Object 的 SQLite 存储维护会话目录索引和共享资源状态。用户的每个聊天会话对应一个 ChatSession DO 实例，该实例由 UserAssistant 通过 subAgent() 方法创建并注册到子 Agent 注册表中。

客户端通过嵌套 URL 路由直接访问子 Agent：路径格式为 /agents/{父类名}/{父实例名}/sub/{子类名}/{子实例名}。框架在父 DO 收到请求时执行 onBeforeSubAgent 钩子进行访问控制校验，校验通过后将请求转发至子 DO 的 Facet。WebSocket 升级完成后，后续数据帧直接路由到子 DO，父 DO 不再参与消息转发。父 DO 与子 DO 之间的 RPC 调用通过框架的 _cf_invokeSubAgent 桥接方法完成，子 Agent 可通过 parentAgent(Cls) 方法获取指向父 Agent 的类型化 RPC 存根。

### 二、父子 Agent 职责边界与状态隔离

父 Agent（UserAssistant）持有以下用户级共享状态：（1）会话目录索引，以 SQLite 表 cf_agents_sub_agents 维护所有子会话的 id、标题、创建时间、更新时间、最近消息摘要等元数据；（2）共享工作区文件系统，基于 Workspace 抽象提供跨会话的文件读写、目录遍历、glob 搜索和变更通知；（3）共享 MCP 客户端连接管理器（MCPClientManager），统一管理所有 MCP 服务端的连接生命周期、工具发现和 OAuth 授权流程；（4）共享 OAuth 授权凭据存储，用户授权一次后所有子会话可复用相同的访问令牌；（5）文件变更通知广播通道，当任一子会话修改工作区文件后，父 Agent 通过 setState() 向所有连接的客户端推送更新状态。（6）跨会话定时任务调度表，记录 cron 表达式、延迟秒数或具体执行时间，由父 DO 的 alarm 机制驱动执行。

子 Agent（ChatSession）持有以下会话级隔离状态：（1）消息历史与分支树，以 SQLite 的 assistant_messages 表存储，每条消息具有 parent_id 形成树状结构，支持分支和重新生成；（2）上下文块（Context Blocks），通过 Session 框架注入系统提示的持久化键值对，如用户偏好、项目约定、会话记忆等；（3）压缩叠加层（Compaction Overlays），以 assistant_compactions 表存储消息范围的摘要，读时非破坏性地替换原始消息；（4）Think 专用配置（think_config 表），存储会话级模型参数、工具开关、扩展配置等；（5）推理循环状态，包括当前流式响应、中止控制器、待执行工具调用队列；（6）WebSocket 连接集合，维护与本会话相关的所有浏览器标签页或面板的连接。

关键隔离原则：子 Agent 不直接持有 MCPClientManager 实例、不直接管理 MCP 传输层连接、不直接发起 OAuth 授权流程、不直接拥有 Workspace 的文件存储后端。子 Agent 可缓存从父 Agent 获取的工具列表和文件内容快照，但所有写操作和状态变更必须通过父 Agent 的受控接口进行。父 Agent 也不持有任何子会话的消息历史或推理状态，保证不同会话之间的推理执行完全并行而不受 DO 单线程模型限制。

### 三、共享工作区文件代理

共享工作区文件系统由父 Agent 持有 Workspace 实例。Workspace 基于内存文件系统和可选的 R2 持久化后端，提供 readFile、writeFile、readDir、stat、glob、mkdir、rm 等文件操作接口。子 Agent 通过以下受控代理方式访问工作区：

- 子 Agent 调用 parentAgent(UserAssistant).readFile(path) 通过 DO RPC 获取文件内容；父 Agent 在执行读取前可校验子 Agent 的身份和路径合法性。
- 子 Agent 调用 parentAgent(UserAssistant).writeFile(path, content) 写入文件；父 Agent 在写入完成后触发文件变更事件，通过广播状态机制通知所有连接的客户端。
- 子 Agent 通过 parentAgent(UserAssistant).readDir(dir) 和 parentAgent(UserAssistant).glob(pattern) 进行文件浏览和搜索。
- 子 Agent 在每轮推理开始时可通过一次 RPC 获取工作区快照；推理过程中文件变更不自动失效缓存，子 Agent 在下轮开始时重新获取。

### 四、共享 MCP 连接与 OAuth 授权代理

MCP 工具连接和 OAuth 授权由父 Agent 的 MCPClientManager 统一管理，子 Agent 不拥有独立的 MCPClientManager 实例。MCPClientManager 维护一个 mcpConnections 映射表，每个表项包含一个 MCPClientConnection 实例，管理到外部 MCP 服务端的传输层连接（支持 SSE、Streamable HTTP 和 RPC 三种传输类型）、工具发现（tools/list）、资源发现（resources/list）和提示模板发现（prompts/list），以及连接状态机（AUTHENTICATING → CONNECTING → DISCOVERING → READY）。

子 Agent 获取可用 MCP 工具的流程如下：（1）子 Agent 的推理循环在准备工具集时调用 parentAgent(UserAssistant).getTools() RPC 方法；（2）父 Agent 的 MCPClientManager 遍历所有处于 READY 状态的连接，聚合各服务的 tools 列表，为每个工具添加服务来源标识前缀；（3）返回给子 Agent 的工具列表是 AI SDK ToolSet 格式，可直接注入推理循环的 streamText 调用；（4）当子 Agent 的模型决定调用某个 MCP 工具时，工具调用请求再次通过父 Agent RPC 代理：parentAgent(UserAssistant).callTool(serverId, toolName, args)；（5）父 Agent 定位对应的 MCPClientConnection，通过其底层 Client 实例发起 tools/call 请求，将结果返回子 Agent。子 Agent 在此过程中不持有 MCP 服务端的传输层连接，不直接构造 MCP JSON-RPC 消息。

OAuth 授权流程同样由父 Agent 集中处理：（1）用户通过浏览器向父 Agent 发起 addMcpServer 请求，指定 MCP 服务端 URL 和传输类型；（2）父 Agent 创建 DurableObjectOAuthClientProvider 实例，发起 OAuth 2.0 授权码流程，生成 authUrl 并返回给浏览器；（3）用户在浏览器中完成授权后，OAuth 回调到达父 Agent，父 Agent 完成令牌交换并将刷新令牌持久化到 DO 存储中；（4）后续所有子会话的工具调用自动复用已授权的访问令牌，无需用户重复授权；（5）令牌过期时父 Agent 自动使用刷新令牌获取新令牌。子 Agent 完全不参与 OAuth 流程，不存储任何凭据。

### 五、访问控制与会话路由安全

系统通过三层访问控制防止未经授权的会话访问和创建：

第一层：Worker 入口层。在 fetch 处理函数中，通过 OAuth 认证中间件校验请求的会话 Cookie 或 Bearer 令牌，拒绝未认证请求。通过认证的用户标识（如 GitHub 登录名）作为父 Agent 的 DO 实例名。

第二层：父 Agent 的 onBeforeSubAgent 钩子。该钩子在父 DO 收到指向子 Agent 的请求时触发，接收原始 Request 和子 Agent 的 {className, name} 信息。实现严格的注册表门控：通过 hasSubAgent(className, name) 检查目标子会话是否存在于父 Agent 的注册表中；不存在则返回 404，阻止通过猜测会话 ID 访问未登记的子会话。同时校验子 Agent 类名是否为预期的 ChatSession 类型，拒绝未知类名的路由请求。

第三层：子 Agent 自身的 onBeforeConnect 钩子。子 Agent 信任父 Agent 已完成的访问控制决策，可在此基础上执行额外的会话级权限检查，如读写权限、速率限制等。子 Agent 的 onBeforeRequest 钩子同样可用于 HTTP 请求的细粒度控制。

此外，系统通过 DO 实例命名约定实现租户隔离：父 Agent 的 DO 实例名即为用户标识，子 Agent 的 DO 实例名由父 Agent 在 createChat 时生成（crypto.randomUUID()），子 Agent 的 DO 存储天然与父 Agent 的用户绑定。框架在客户端通过 retry 硬化机制处理访问拒绝：HTTP 4xx 响应和 WebSocket 1008/4000-4999 关闭码被视为终态错误，停止重连。

### 六、实时文件变更同步

系统通过父 Agent 的广播状态机制实现文件变化的实时同步，减少轮询开销。核心流程如下：

（1）文件变更检测与事件生成。当子 Agent 通过父 Agent 的 writeFile RPC 方法修改工作区文件后，父 Agent 执行实际的写入操作，然后触发文件变更事件。父 Agent 的 Workspace 层可在写入后生成变更摘要（包含变更文件路径、操作类型和时间戳）。

（2）跨标签页广播。父 Agent 调用 this.setState(state) 或 this.broadcast(message) 方法，将文件变更通知推送到所有当前连接到父 Agent 的 WebSocket 客户端。框架的 broadcast-state 机制维护一个 BroadcastStreamState 状态机，管理广播流的生命周期，处理客户端重连后的流恢复。父 Agent 的 ChatsState 结构中包含文件版本号或变更计数器，客户端通过比对版本号判断是否需要刷新文件视图。

（3）子会话内广播。子 Agent 自身的 setState 和 broadcast 方法仅向连接到该子 Agent 的 WebSocket 客户端发送消息。当文件变更仅影响当前会话的工作视图时，子会话内广播即可满足需求。多标签页打开同一会话的场景下，一个标签页的文件操作可通过子 Agent 广播同步到其他标签页。

（4）客户端接收与合并。客户端 useAgent 钩子监听 WebSocket 消息流中的 cf_agent_state 消息，更新本地状态缓存。useChats 钩子同时维护对父 Agent 目录状态和活跃子 Agent 聊天状态的订阅，确保侧边栏文件树和聊天面板中的文件视图保持同步。文件变更不触发聊天消息的重新渲染，仅刷新文件浏览组件。

### 七、跨会话定时任务调度

跨会话定时任务和全局后台任务由父 Agent 统一调度，不散落在各子会话中。系统复用框架的 Schedule 机制，支持三种调度类型：

（1）Scheduled（定时执行）：指定具体日期时间的一次性任务，如“明天下午2点总结所有会话的待办事项”。父 Agent 解析自然语言调度请求后，将任务描述和触发时间存储到调度表中，设置 DO alarm 在目标时间唤醒父 Agent 执行任务。

（2）Delayed（延迟执行）：指定相对延迟秒数的一次性任务。父 Agent 计算触发时间并设置 alarm。适用于“30分钟后提醒我检查邮件”等场景。

（3）Cron（周期执行）：基于 cron 表达式的重复任务。父 Agent 使用 cron-schedule 库解析 cron 表达式，计算下一次触发时间并设置 alarm。每次执行完成后重新计算下一次触发时间。适用于“每天早上9点生成跨会话摘要”等场景。

父 Agent 执行定时任务时：（a）从 alarm 处理函数中读取任务定义；（b）根据任务类型执行相应逻辑——跨会话摘要任务遍历所有子会话的 session.searchMessages 接口收集摘要信息，清理任务软删除过期的子会话，通知任务向所有连接的客户端广播；（c）任务执行结果可存储到父 Agent 的共享上下文块中供子会话查询。子 Agent 不持有 alarm，不独立调度定时任务，保证全局任务不会因为子会话的休眠/销毁而丢失。

### 八、技术效果

本技术方案的技术效果包括：

- 并行推理隔离：每个聊天会话运行在独立的 Durable Object 中，不同会话的推理执行完全并行，不受 DO 单线程模型限制。用户切换会话时无需等待其他会话的推理完成。
- 共享资源复用：MCP 服务端连接和 OAuth 授权凭据在用户级别共享，避免每个会话重复建立连接和重复授权，减少连接数和授权流程次数。
- 安全访问控制：子 Agent 不能绕过父 Agent 直接访问 MCP 服务端或工作区存储，三层访问控制防止会话 ID 猜测攻击和未授权访问。
- 实时同步：基于 WebSocket 广播的文件变更通知替代轮询，减少网络开销和延迟，多标签页/多面板场景下文件视图保持一致。
- 全局任务可靠性：定时任务由父 Agent 统一调度，利用 DO alarm 机制保证即使子会话休眠或销毁，定时任务也不会丢失。
- 复用现有体系：方案完全基于已落地的子 Agent 路由原语（subAgent、parentAgent、onBeforeSubAgent、hasSubAgent、listSubAgents）、MCPClientManager、Workspace、Session 和 Schedule 框架构建，无需重新设计路由层或存储层。

### 九、风险与待确认问题

以下为当前方案中需要后续确认和关注的风险点：

- MCP 工具调用的 RPC 延迟：子 Agent 每次调用 MCP 工具需要通过父 Agent RPC 代理（双跳：子→父→MCP 服务端→父→子）。对于高频工具调用场景，可考虑在工具发现阶段将只读工具的描述和参数模式缓存到子 Agent 本地，仅在实际调用时通过父代理。
- 父 Agent 单点瓶颈：所有子会话的共享资源访问和定时任务都汇聚到父 DO。当用户拥有大量活跃会话时，父 DO 的请求吞吐可能成为瓶颈。可通过按资源类型拆分父 DO（如单独的工作区 DO、MCP 连接 DO）缓解。
- 跨会话消息搜索的扇出成本：searchMessages 需要向所有子会话发起并行 RPC，会话数超过 50 时延迟显著。后续可引入父 Agent 侧的 FTS5 倒排索引，由子 Agent 的消息写入钩子增量更新。
- 共享内存的读写竞争：两个子会话同时读取并修改共享上下文块时，可能出现丢失更新（read-modify-write 竞争）。建议优先使用 appendSharedContext 方法进行追加式写入，避免全量替换。
- 子 Agent 删除时的清理一致性：软删除的会话在删除窗口期内仍可能收到进行中的 RPC 请求。当前方案采用软删除+生成计数器方式防止过期重连，但需确保 DO 的 TTL 回收机制与软删除窗口匹配。
