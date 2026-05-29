## 技术方案

本方案提出一种基于父子 Durable Object 架构的多会话智能助手系统，通过将用户级共享资源与会话级执行上下文分离到不同的 Durable Object 实例中，解决单一账号下多会话并行执行时的资源隔离与共享矛盾。系统由用户级父实体（Chats Durable Object）和会话级子实体（Think Durable Object）两层构成，父实体集中管理用户维度的共享资源——包括虚拟文件系统（Workspace）、外部 MCP 工具连接、OAuth 授权凭据以及文件变更广播信号——子实体通过受控代理（RemoteContextProvider、RemoteSearchProvider 及共享资源 RPC 代理）间接访问这些资源，每个子实体持有独立的会话历史、系统提示配置和 agent 执行循环状态。

### 整体架构

系统采用两层 Durable Object 组合架构。每个用户对应一个用户级父 Durable Object（Chats 实例），该实例维护该用户下的会话目录索引、共享上下文存储和共享资源连接池。每个聊天会话对应一个独立的会话级子 Durable Object（Think 实例），作为该用户父实体的子代理（sub-agent）存在。WebSocket 连接直接指向子实体进行实时聊天通信；父实体仅在列表操作（创建、删除、重命名会话）和共享资源访问时介入。父实体与子实体之间通过 Durable Object RPC 通信，子实体可通过 parentAgent() 方法获取父实体的类型化 RPC 引用。

### 用户级父实体与资源集中管理

用户级父实体（Chats）是继承自 Agent 基类的 Durable Object，每个用户拥有一个独立实例。父实体的 SQLite 数据库维护两张核心表：chats_index 表记录所有子会话的元信息（会话 ID、标题、创建时间、更新时间、最后消息预览），chats_fts 虚拟表提供标题全文搜索能力。父实体通过 @callable 装饰器暴露以下 RPC 方法供客户端和子实体调用：createChat 创建新会话并同步创建子 Durable Object（通过 subAgent 惰性创建）；listChats 返回会话列表（支持分页游标和时间范围过滤）；deleteChat 执行软删除（设置 deleted_at 时间戳，保留子 DO 一段时间以允许进行中的写入完成）；renameChat 更新会话标题并触发状态广播；searchChats 和 searchMessages 分别提供目录级标题搜索和跨会话全文搜索。

父实体还作为共享资源的集中管理者。Workspace 虚拟文件系统实例在父实体级别创建，所有子会话通过父实体 RPC 间接读写文件。MCP 服务器连接也由父实体统一管理：addMcpServer 方法在父实体上执行，连接、工具发现和 OAuth 授权流程均在父实体中完成，连接状态和工具列表持久化到父实体的 SQLite。

OAuth 授权凭据（access token、refresh token）也存储在父实体的持久化存储中。子会话需要调用外部工具时，不直接持有 MCP 连接或 OAuth 凭据，而是通过父实体暴露的受控 RPC 接口发起工具调用，由父实体代理执行。这种设计确保用户只需授权一次，所有子会话即可共享同一组外部工具，同时防止子会话绕过 agent 生命周期直接执行原始 MCP 工具调用。

### 会话级子实体与会话隔离

每个聊天会话对应一个独立的 Think Durable Object 实例，作为用户父实体的子代理运行。每个子实体拥有独立的 SQLite 数据库，其中存储该会话的完整消息历史、会话配置（系统提示、模型选择、最大步骤数等）、请求上下文和客户端工具注册信息。消息历史采用树形结构存储，通过 parent_id 字段支持消息分支（regeneration），允许用户回溯到历史消息节点重新生成回复，而原始消息链不受影响。子实体通过 Session 模块管理消息的压缩叠层（compaction overlay），当历史消息超过令牌阈值时自动生成摘要，原始消息保留在 SQLite 中不丢失。

子实体的 agent 执行循环（agentic loop）完全独立。每个子实体通过 getModel、getSystemPrompt、getTools 等可重写方法定义自身的行为配置。客户端工具（ClientToolSchema）由浏览器端通过 WebSocket 协议动态注册到当前活跃的子实体上，不会跨会话泄漏。每个 WebSocket 请求分配独立的 AbortController，用户可以取消当前子实体的正在进行的推理而不影响其他会话。

子实体之间通过 Durable Object 的天然隔离实现数据边界：SQLite 数据库完全独立，内存状态在 DO 休眠（hibernation）后由运行平台恢复但不会跨实例共享。子实体通过两种路径接收消息——WebSocket 直连路径（浏览器客户端通过 cf_agent_chat_* 协议直接与子实体通信）和 RPC 路径（父实体或其他子实体通过 chat() 方法调用）——两种路径共享相同的内部生命周期和持久化机制。

### 共享资源的受控代理访问

子会话不直接持有共享资源（Workspace 文件系统、MCP 连接、OAuth 凭据），而是通过父实体暴露的受控 RPC 代理接口进行访问。这一代理模式确保：共享资源的真实状态由父实体这一统一位置管理；所有操作经过父实体的权限检查（通过 onBeforeSubAgent 中间件钩子）；子会话的崩溃或异常不会影响共享资源的连接状态。

跨会话共享记忆通过 RemoteContextProvider 和 RemoteSearchProvider 两个通用 RPC 代理提供者实现。子会话在 configureSession 中声明记忆上下文块，其 provider 设置为指向父实体的 RemoteContextProvider 实例。当子会话需要读取共享记忆时，RemoteContextProvider.get() 通过 DO RPC 调用父实体的 getSharedContext(label) 方法；写入时通过 appendSharedContext 实现追加式更新，避免读-改-写的竞态条件。父实体的 DO 单线程特性保证写操作原子性。子会话的冻结系统提示可能包含稍旧的内存快照——这在本轮推理中被接受，下一轮边界通过 session.refreshSystemPrompt() 拉取最新内容。

Workspace 文件系统按用户维度共享——父实体持有唯一的 Workspace 实例，基于 Durable Object SQLite 实现虚拟文件系统，小文件内容直接存储在 SQLite 的 content 列中，超过 1.5MB 阈值的大文件溢出到 R2 对象存储。子会话通过父实体暴露的 RPC 方法执行文件读写、目录操作和 glob 搜索，所有文件变更事件由父实体的 onChange 回调统一触发。MCP 工具连接同样由父实体集中管理：父实体通过 addMcpServer 建立与外部 MCP 服务器的连接，管理 OAuth 授权流程，持久化连接状态和工具列表。子会话通过父实体的 RPC 接口获取可用工具列表并代理执行工具调用，父实体在转发调用前后可注入审计日志、速率限制和参数校验。

### 访问控制与会话安全

系统通过三层访问控制防止未授权的会话访问和资源操作。第一层为 Worker 入口层交叉认证，由 onBeforeConnect / onBeforeRequest 中间件在请求到达任何 Durable Object 之前验证请求的身份合法性。第二层为父实体级访问控制，通过 onBeforeSubAgent 中间件钩子在父实体转发请求到子实体之前执行：检查请求中的目标会话 ID 是否存在于父实体的 chats_index 表中，若不存在则返回 404 阻止请求到达子实体，防止通过猜测会话 ID 进行未授权访问。第三层为子实体自身访问控制，子实体的 onBeforeConnect 等处理器可执行额外的按操作权限检查。

子会话通过嵌套 URL 路径寻址：/agents/{父类名}/{用户标识}/sub/{子类名}/{会话ID}。URL 中的子代理名称经过 URL 编码/解码处理，支持包含空格和 Unicode 字符的名称。父实体在 onBeforeSubAgent 中通过 hasSubAgent(className, name) 检查子代理是否已在注册表中登记，实现严格的注册表门控访问。WebSocket 升级后，父实体退出热路径，后续消息帧直接路由到子实体，避免父实体成为吞吐瓶颈。

### 实时文件变更同步

系统采用 Durable Object 的 broadcast() 机制实现文件变更的实时跨标签页通知，无需客户端轮询。当 Workspace 文件发生创建、修改或删除操作时，Workspace 的 onChange 回调被触发，回调中调用父实体的 broadcast() 方法将变更事件以 WebSocket 消息帧广播给所有连接到该父实体的客户端。连接不同子会话的多个浏览器标签页通过维持与父实体（或其目录状态广播）的 WebSocket 连接接收文件变更信号。

对于会话目录状态的同步（如创建、删除、重命名会话），父实体在每次目录写操作后自动通过广播状态（broadcast state）机制将更新后的 chats 数组推送给所有连接的客户端。前端的 useChats React hook 订阅该广播状态，当收到更新后自动刷新侧边栏的会话列表。对于跨会话共享记忆的变更同步，父实体可采用独立的 _notifySharedChange 广播事件通知所有子会话标签页，使前端在无需刷新页面的情况下更新共享记忆的显示内容。Workspace 文件变更的广播采用诊断通道（node:diagnostics_channel）发布结构化事件，与 broadcast() 机制解耦，仅在订阅者存在时才产生开销。

### 全局定时任务调度

全局定时任务和跨会话批处理任务由父实体（Chats DO）统一调度，而非散落在各个子会话中各自执行。父实体继承自 Agent 基类的调度能力，支持四种调度模式：延迟执行（schedule(seconds, callback, payload)）、定时执行（schedule(Date, callback, payload)）、Cron 循环（schedule(cronExpression, callback, payload)）和固定间隔（scheduleEvery(intervalSeconds, callback)）。所有调度任务持久化到父实体的 SQLite 表中，基于 Durable Object Alarm 机制唤醒执行，在 DO 休眠和重启后仍能按计划触发。

典型全局任务包括：跨会话摘要生成——父实体通过 scheduleEvery 注册定时摘要任务，在回调中遍历所有活跃子会话，通过 RPC 调用每个子会话的 getRecentActivity 方法获取最近对话内容，汇总生成全局摘要并通过 RemoteContextProvider 写回共享记忆；会话清理——父实体通过 Cron 表达式注册定期清理任务，扫描 chats_index 中 deleted_at 超过软删除窗口的记录，调用 deleteSubAgent 永久清除对应的子 DO 及其存储；全局通知——父实体通过 schedule 在指定时间点向所有连接的 WebSocket 客户端广播公告消息。

### 与现有基础设施的复用关系

本方案充分复用现有 Agent SDK 的以下基础设施，不引入新的存储或通信层：子代理机制（Agent.subAgent、SubAgentStub 类型化 RPC）提供父实体到子实体的创建和管理能力，每个子 DO 自动获得独立的 SQLite 存储；子代理路由（routeAgentRequest 扩展、onBeforeSubAgent 中间件、嵌套 URL 路径解析）实现客户端直连子实体的网络寻址；Session 模块（树形消息存储、压缩叠层、上下文块提供者系统）为每个子实体提供消息管理能力；Think 基类（agent 执行循环、流式输出、可恢复流、客户端工具）被每个子实体继承复用；Workspace 模块（混合 SQLite+R2 虚拟文件系统）整合到父实体中；MCP 客户端基础设施（addMcpServer、OAuth 流程、连接持久化和重连）由父实体调用和持有。

### 技术效果

（1）并行会话执行隔离：每个子会话对应独立的 Durable Object 实例，并行推理请求由运行平台调度到不同的 DO 实例执行，消除了单 DO 多会话方案中会话间串行等待的瓶颈。一个会话的长时间推理或工具调用不会阻塞其他会话。会话间的消息历史、配置和 agent 执行状态完全隔离，不会因 session_id 列混淆导致上下文泄漏。（2）资源共享与安全控制的统一：通过将 Workspace、MCP 连接和 OAuth 凭据集中到父实体，用户只需授权一次即可在所有会话中使用外部工具，同时父实体通过 onBeforeSubAgent 中间件对所有子会话的资源访问进行统一鉴权。子会话不直接持有原始 MCP 连接句柄，防止 LLM 绕过安全策略直接执行敏感工具调用。

（3）实时同步减少轮询开销：利用 DO WebSocket 广播机制推送文件变更、目录更新和共享记忆变更信号，前端通过事件驱动更新界面，避免了传统基于 HTTP 轮询方案的延迟和带宽浪费。（4）全局任务集中调度：定时摘要、会话清理等跨会话任务由父实体统一管理，调度任务持久化到 SQLite 并通过 DO Alarm 唤醒执行，即使父实体休眠后也能按计划触发；子实体无需各自维护定时器，减少资源浪费和调度冲突。

### 风险与待确认问题

（1）父实体单线程瓶颈：父 Durable Object 的单线程特性意味着来自多个子会话的并发共享资源访问（如同时读写 Workspace 或调用共享 MCP 工具）将在父实体上序列化。对于 Workspace 读写，由于文件操作通常较短暂，此瓶颈影响有限；但对于涉及外部网络调用的 MCP 工具代理，父实体转发会导致工具调用延迟叠加。缓解方案包括：为高频只读操作（如工具列表查询）增加父实体侧缓存；对耗时的 MCP 工具调用考虑异步回调模式而非同步代理。（2）RemoteContextProvider 的 RPC 延迟：子会话每次读取共享记忆时需要一次跨 DO 的 RPC 调用。Session 的 withCachedPrompt 机制将冻结的系统提示缓存到每个推理轮次，因此该开销每个轮次仅发生一次，影响可控。

（3）共享大脑更新的写冲突：当多个子会话同时通过 appendSharedContext 追加记忆片断时，由于父 DO 单线程，追加操作天然串行化，不存在写冲突。但若子会话使用 setSharedContext（全量替换模式），可能出现后写覆盖先写的丢失更新问题。方案已明确推荐使用 appendSharedContext 作为安全的追加原语，setSharedContext 仅用于全量替换场景。（4）Workspace 并发写入：多会话同时写入同一文件路径时，由于 Workspace 未提供文件锁机制，后写入者覆盖先写入者。对于典型的多会话使用场景（不同会话通常操作不同文件或不同项目），此冲突概率低；对于需要并发协作编辑的场景，需在应用层实现合并策略。（5）父实体冷启动延迟：父实体首次被访问时可能处于休眠状态，首次 RPC 调用将触发冷启动。此延迟仅在用户首次打开应用或长时间不活动后出现，通过 DO 的 keepAlive 机制可减少休眠频率。
