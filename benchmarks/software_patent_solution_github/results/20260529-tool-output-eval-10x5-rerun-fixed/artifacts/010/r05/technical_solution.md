## 技术方案

### 总体架构

本方案基于 Durable Object（DO）架构，构建一种双层 Agent 组合模型：每个用户拥有一个独立的用户级父 Agent（User Agent），在该父 Agent 下管理多个会话级子 Agent（Chat Agent）。父 Agent 负责用户维度的共享资源管理、会话目录维护、访问控制和全局定时任务调度；子 Agent 负责单一会话内的消息持久化、LLM 推理循环、流式输出、工具调用和会话级上下文管理。

架构采用“一个会话对应一个独立 DO 实例”的组合模式，而非在单个 DO 内托管多个会话。这保证了不同会话的推理执行天然并行，避免了单线程 DO 的串行化瓶颈。父 Agent 与子 Agent 之间通过 DO RPC（Remote Procedure Call）进行通信，WebSocket 连接在认证后直接路由到目标子 Agent，父 Agent 仅在连接建立和 HTTP 请求路径中介入，不在消息热路径上产生额外开销。

### 用户级父 Agent 结构

用户级父 Agent 是一个继承自 Agent 基类的 DO 实例，每个用户对应一个独立实例。其核心职责包括：（1）维护会话目录索引，记录每个子会话的标识符、标题、创建时间、最后活跃时间和最后一条消息预览，支持软删除；（2）管理用户级共享资源，包括 Workspace 文件系统实例、MCP 服务器连接池和 OAuth 授权凭据；（3）提供共享上下文存储，子会话通过 RemoteContextProvider 和 RemoteSearchProvider 以 RPC 方式读写共享记忆块；（4）执行跨会话的全局定时任务，如定时摘要生成、会话归档清理、跨会话全文搜索聚合；（5）作为子 Agent 的访问网关，在 onBeforeSubAgent 钩子中执行认证和授权检查。

父 Agent 的内部 SQLite 存储包含以下关键表：chats_index 表记录所有子会话元数据（id、title、created_at、updated_at、last_message_preview、deleted_at），支持基于 FTS5 的标题全文搜索；shared_context 表存储键值对形式的共享上下文块，支持按标签（label）读写和追加写入；mcp_connections 表持久化 MCP 服务器连接配置（服务器地址、传输类型、连接状态）；oauth_credentials 表存储 OAuth 授权令牌及其刷新令牌，按用户维度加密持久化。

### 会话级子 Agent 结构

会话级子 Agent 是继承自 Think 基类的 DO 实例，每个聊天会话对应一个独立实例。其核心职责包括：（1）管理该会话的消息历史，采用 Session 存储层的树形消息结构（parent_id 关联），支持分支对话和消息回退再生；（2）执行 agentic 推理循环，包括系统提示词组装、上下文块注入、工具集合并、LLM 调用和流式输出；（3）通过受控代理方式访问用户级共享资源，子会话不直接持有 MCP 客户端连接或 OAuth 凭据，而是通过 RPC 调用父 Agent 暴露的 callable 方法间接使用；（4）管理会话级配置，包括模型选择、系统提示词、最大推理步数、上下文压缩阈值等。

子 Agent 通过 configureSession 方法配置会话级上下文块。对于需要跨会话共享的上下文（如用户偏好、长期记忆），子 Agent 使用 RemoteContextProvider 实例，该 provider 的 get/set 操作通过 DO RPC 转发到父 Agent 的 getSharedContext/setSharedContext 方法，数据实际存储在父 Agent 的 SQLite 中。对于会话私有的上下文（如当前任务状态、临时偏好），则使用本地 AgentContextProvider 存储在本会话的 SQLite 中。子 Agent 通过 parentAgent 方法获取父 Agent 的类型化 RPC 存根，实现对共享资源的受控访问。

### 职责边界与隔离机制

本方案的核心隔离原则为：消息历史、会话配置、会话级上下文块、推理状态（包括当前流式输出的缓冲区和中断控制器）按会话隔离，每个子 Agent 的 SQLite 仅存储本会话数据；Workspace 文件系统、MCP 服务器连接池、OAuth 授权令牌和共享上下文块按用户共享，由父 Agent 统一持有。子会话通过受控代理访问共享资源，不能绕过 agent 生命周期直接执行原始 MCP 工具调用或直接读写 OAuth 凭据。

| 资源类型 | 归属 | 持有者 | 子会话访问方式 |
| --- | --- | --- | --- |
| 消息历史 | 会话隔离 | 子 Agent | 本地 SQLite 直接读写 |
| 会话配置（模型、步数等） | 会话隔离 | 子 Agent | 本地 think_config 表 |
| 会话级上下文块 | 会话隔离 | 子 Agent | AgentContextProvider 本地读写 |
| Workspace 文件 | 用户共享 | 父 Agent | 通过父 Agent RPC 操作的受控代理 |
| MCP 工具连接 | 用户共享 | 父 Agent（MCPClientManager） | 通过父 Agent 的 callable 方法代理执行 |
| OAuth 授权凭据 | 用户共享 | 父 Agent（加密存储） | 子 Agent 不直接访问，由父 Agent 注入工具调用 |
| 共享记忆（user_memory） | 用户共享 | 父 Agent | RemoteContextProvider 通过 RPC 读写 |
| 前端刷新信号（broadcast） | 用户共享 | 父 Agent 广播 | 通过 WebSocket broadcast 推送状态变更 |

### 共享资源代理机制——Workspace 共享

Workspace 文件系统是父 Agent 持有的虚拟文件系统实例，基于 Durable Object SQLite 实现文件元数据和内容存储，大文件溢出到 R2 对象存储。Workspace 实例在父 Agent 初始化时创建，通过命名空间（namespace）隔离不同用途的文件区域。子会话不持有独立的 Workspace 实例，而是通过父 Agent 暴露的 callable 方法间接操作文件。Workspace 的 onChange 回调连接到父 Agent 的 broadcast 方法，当文件发生创建、更新或删除时，父 Agent 向所有连接的客户端广播状态变更消息，实现多标签页的实时文件同步。

### 共享资源代理机制——MCP 连接与 OAuth 共享

MCP 服务器连接和 OAuth 授权按用户维度由父 Agent 统一管理。父 Agent 内部维护一个 MCPClientManager 实例，负责与外部 MCP 服务器的连接生命周期管理，包括连接建立、心跳维持、工具列表拉取、断线重连和连接状态监控。OAuth 授权流程由父 Agent 的 DurableObjectOAuthClientProvider 处理：用户在任一子会话中发起的 OAuth 授权请求被路由到父 Agent，父 Agent 完成授权流程后将令牌持久化到其 SQLite 的 oauth_credentials 表中，后续所有子会话的工具调用均可复用该授权。

子会话不直接持有 MCP 客户端连接。当子 Agent 执行推理循环需要调用外部工具时，合并后的工具集（ToolSet）中包含通过父 Agent RPC 代理生成的工具定义。工具的实际执行通过以下路径：子 Agent 的 streamText 调用触发工具调用请求 → Think 基类的 beforeToolCall 钩子识别该工具属于远程 MCP 工具 → 通过父 Agent 的 callable 方法代理执行 → 父 Agent 使用已建立的 MCP 连接发送 CallToolRequest → MCP 服务器返回结果 → 结果沿 RPC 链路返回子 Agent。浏览器前端和子会话均不能绕过此代理路径直接发起原始 MCP 工具调用。

### 访问控制机制

访问控制分为三个层次。第一层为跨域认证层，在 Worker 入口的 onBeforeConnect/onBeforeRequest 钩子中执行，验证请求是否携带有效的用户身份凭证（如 Cookie/Session Token），拒绝未认证请求。第二层为父 Agent 网关层，在父 Agent 的 onBeforeSubAgent 钩子中执行。该钩子接收子 Agent 的类名和实例名，通过检查父 Agent 内部的 hasSubAgent 注册表验证目标子 Agent 是否属于当前用户。对于未注册的子 Agent 访问请求（如攻击者猜测会话 ID），直接返回 404 响应，不唤醒子 Agent DO。第三层为子 Agent 自身的业务授权层，在子 Agent 的 onBeforeConnect 等钩子中执行，检查注入的身份头信息并进行业务级权限判断。

子 Agent 的访问采用严格的注册表模式：父 Agent 的 chats_index 表是子 Agent 的权威目录。当用户通过 createChat 创建新会话时，父 Agent 先在 chats_index 中插入记录，再通过 subAgent 方法懒创建子 Agent DO 实例。WebSocket 连接通过 URL 路径 /agents/{user-agent-class}/{user-id}/sub/{chat-class}/{chat-id} 直接路由到子 Agent，但 URL 解析后必须经过父 Agent 的 onBeforeSubAgent 验证。即使攻击者构造了合法的 URL 格式，若对应的子 Agent 未在父 Agent 注册表中登记，请求也会在父 Agent 层被拦截。软删除的会话通过 deleted_at 字段标记，在恢复宽限期内允许重新激活，过期后由定时清理任务移除。

### 实时同步机制

实时同步采用基于 Durable Object WebSocket broadcast 的推送机制，避免客户端轮询。父 Agent 作为共享状态的唯一权威来源，在以下事件发生时通过 broadcast 方法向所有连接的客户端推送状态变更消息：（1）Workspace 文件变更——Workspace 的 onChange 回调触发父 Agent 向所有标签页广播 file-changed 事件，包含变更文件的路径和操作类型；（2）会话目录变更——createChat、deleteChat、renameChat 操作完成后，父 Agent 广播更新后的 chats 数组，客户端侧边栏实时更新；（3）共享上下文变更——当子会话通过 RemoteContextProvider 写入共享记忆时，父 Agent 可选择性地广播 memory-updated 信号，通知其他活跃会话刷新其缓存的系统提示词。

父 Agent 维护一个 broadcast 状态机（BroadcastTransition），管理连接状态和消息序列化。每个 WebSocket 连接在建立时被注册到父 Agent 的连接池中。对于子 Agent 直连的 WebSocket（聊天消息流），其连接归属于子 Agent 自身，父 Agent 的状态广播通过独立的控制连接或复用父 Agent 的 WebSocket 连接下发。客户端 useChats React Hook 封装了目录状态订阅和活跃会话连接管理，自动处理侧边栏状态同步和会话切换时的连接生命周期。

### 定时任务调度机制

全局定时任务和周期性任务统一由父 Agent 负责调度和执行，不散落在各个子会话中。Durable Object 的 alarm 机制提供了可靠的定时触发能力：父 Agent 通过 setAlarm 设置下次触发时间，在 alarm 回调中执行任务逻辑，执行完毕后根据任务周期设置下一次 alarm。支持的调度模式包括：（1）延迟任务——指定延迟秒数后执行一次性任务；（2）cron 定时任务——使用 cron-schedule 库解析 cron 表达式，计算下次触发时间并设置 alarm；（3）基于自然语言的任务调度——利用 LLM 的 generateObject 能力将自然语言描述的调度意图解析为结构化调度参数（类型、日期、延迟秒数或 cron 表达式）。

典型全局任务场景包括：跨会话摘要——父 Agent 定时遍历所有活跃会话，调用各子 Agent 的消息历史接口获取最近消息，聚合生成摘要并存储到共享上下文中；会话归档——扫描 chats_index 表中超过 N 天未活跃的会话，执行软删除并清理关联的 DO 存储；OAuth 令牌刷新——检查 oauth_credentials 表中即将过期的令牌，使用刷新令牌获取新的访问令牌；MCP 连接健康检查——定期向已注册的 MCP 服务器发送心跳请求，更新连接状态。父 Agent 在执行全局任务时通过 subAgent RPC 与子 Agent 通信，而非直接操作子 Agent 的 SQLite 存储，保持了职责边界的清晰。

### 关键处理流程——会话创建

用户创建新会话的流程如下：（1）客户端调用父 Agent 的 createChat RPC 方法，传入可选的标题参数；（2）父 Agent 生成唯一的会话 ID（默认使用 crypto.randomUUID()），在 chats_index 表中插入新记录，通过 subAgent 方法懒创建子 Agent 的 DO 实例；（3）父 Agent 生成默认标题（格式为"Chat — ISO日期"），将新的 ChatSummary 条目追加到状态中，通过 broadcast 向所有已连接客户端推送更新后的 chats 数组；（4）客户端侧边栏实时显示新会话条目，用户点击即可发起 WebSocket 连接到对应的子 Agent。

### 关键处理流程——消息推理

用户在活跃会话中发送消息的处理流程如下：（1）客户端通过 WebSocket 向子 Agent 发送 cf_agent_use_chat_request 消息，包含用户消息和可选的客户端工具定义；（2）子 Agent 的 Think 基类接收消息，通过 Session.appendMessage 将用户消息幂等写入本地 SQLite（使用 INSERT OR IGNORE 基于消息 ID 防重复）；（3）子 Agent 组装推理上下文：通过 Session.getHistory 获取树形消息历史、通过 ContextBlocks 渲染系统提示词（其中共享记忆块通过 RemoteContextProvider.get 从父 Agent RPC 获取）、合并工具集（workspace 工具 + 会话本地工具 + MCP 代理工具 + 客户端工具）；（4）子 Agent 调用 LLM 的 streamText 执行推理循环，流式输出通过 WebSocket 推送到客户端，同时通过 StreamAccumulator 累积为完整消息；（5）推理完成后，助手消息通过 Session.appendMessage 持久化到本地 SQLite，触发 compaction 检查（若 token 量超过阈值则执行非破坏性压缩）。若推理过程中发生工具调用，工具的实际执行根据工具来源走不同路径：workspace 工具和本地工具在子 Agent 内执行，MCP 工具通过父 Agent RPC 代理执行。

### 与项目环境的对应关系

本方案直接复用和扩展现有项目中的以下组件：（1）Agent 基类（packages/agents/src/index.ts）——提供 subAgent、broadcast、setAlarm、runFiber 等 DO 原语，是父 Agent 和子 Agent 的共同基类；（2）Think 基类（packages/think/src/think.ts）——提供完整的 chat 生命周期管理，包括 WebSocket 协议处理、Session 存储、StreamAccumulator 流式累积、AbortRegistry 中断管理、ResumableStream 断线重连，子 Agent 直接继承 Think；（3）Session 存储层（packages/agents/src/experimental/memory/session/）——提供树形消息存储、上下文块管理、非破坏性 compaction、FTS5 全文搜索；（4）Chats 父类设计（design/rfc-think-multi-session.md）——提供会话目录管理、共享上下文存储、跨会话搜索的原型设计；（5）Workspace（packages/shell/）——虚拟文件系统，带 onChange 回调支持广播；（6）MCP Client（packages/agents/src/mcp/）——MCPClientManager 管理连接生命周期，DurableObjectOAuthClientProvider 处理 OAuth 流程；（7）Sub-agent Routing（packages/agents/src/sub-routing.ts）——子 Agent URL 路由和 onBeforeSubAgent 访问控制钩子；（8）Chat Shared Layer（packages/agents/src/chat/）——消息构建、清洗、流式累积、广播状态机等共享基础设施。

需要新增或增强的组件包括：（1）RemoteContextProvider / RemoteSearchProvider——跨 DO 的上下文块 RPC 代理，数据实际存储在父 Agent SQLite 中；（2）MCP 工具代理层——在父 Agent 侧将 MCPClientManager 的工具列表转换为可通过 RPC 调用的代理工具定义；（3）Workspace 代理接口——父 Agent 暴露文件操作的 callable 方法，子 Agent 通过 RPC 存根调用；（4）父 Agent 的定时任务框架——基于 alarm 的调度循环和任务注册机制。

### 技术效果

本方案的技术效果包括：（1）会话级并行推理——每个子 Agent 是独立的 DO 实例，不同会话的 LLM 推理可以并行执行，不受单线程 DO 的串行化限制，显著提升多会话场景下的响应性能；（2）资源高效共享——MCP 连接和 OAuth 授权在用户维度统一管理，避免每个会话重复建立连接和重复授权，减少网络开销和令牌管理复杂度；（3）安全隔离——子会话通过受控代理访问共享资源，攻击者无法通过猜测会话 ID 访问未授权会话（父 Agent 网关拦截），也无法绕过 agent 生命周期直接执行外部工具调用；（4）实时多端同步——基于 DO broadcast 的推送机制实现文件变化和会话目录变更的实时通知，无需客户端轮询，延迟低至毫秒级；（5）故障容错——子 Agent 崩溃不影响其他会话，共享资源的父 Agent 独立存活；RemoteContextProvider 在 RPC 失败时返回空值而非抛出异常，保证子会话在共享记忆不可达时仍可继续工作；（6）存储可扩展——树形消息结构支持分支对话，非破坏性 compaction 自动压缩历史消息控制 token 消耗，FTS5 全文搜索跨会话检索历史消息。

### 风险与待确认问题

以下风险点和待确认事项需要在实施前进一步评估：（1）共享 Workspace 的并发写入——若两个子会话同时通过父 Agent 代理修改同一文件，由于父 Agent 是单线程 DO，实际上不会出现真正的并发写入冲突，但需要确认 Workspace 是否需要在文件级别提供乐观锁或版本号机制防止后写覆盖先写的结果；（2）MCP 工具代理的性能开销——每次外部工具调用需要通过两次 DO RPC（子 Agent → 父 Agent → MCP 服务器 → 父 Agent → 子 Agent），对于高频工具调用场景可能引入额外延迟，可考虑引入工具结果缓存机制；（3）父 Agent 单点瓶颈——所有共享资源操作和定时任务都集中在父 Agent，在高并发场景下父 Agent 可能成为瓶颈，需要评估是否需要将不同类别的共享资源拆分到独立的辅助 DO 实例中；（4）跨会话 FTS 搜索的扩展性——当前设计的 searchMessages 对 N 个子会话发起 N 个并行 RPC，在会话数超过 50 时可能存在性能问题，需要在父 Agent 侧建立聚合 FTS 索引；（5）软删除会话的存储回收——子 Agent DO 在软删除后依赖 DO 自身的 TTL 机制回收存储，回收时间不可控，需要确认是否需要主动清理机制；（6）共享上下文写入的最终一致性——子会话 A 写入共享记忆后，子会话 B 在当前推理回合中使用的是冻结的系统提示词快照，需到下一轮才能看到更新，这在多会话协作场景中可能导致短期不一致。
