## 技术方案

本技术方案提出一种基于父子 Agent 架构的多会话助理系统。系统将用户级共享资源与会话级隔离上下文分离到两个不同的 Durable Object 层级：一个用户级父 Agent（Assistant）负责管理聊天会话目录、共享工作空间文件、MCP 工具连接池、OAuth 授权凭据及文件变更广播；每个聊天会话由一个独立的子 Agent（ChatSession，作为 DO Facet 运行）承载，拥有独立的消息历史、会话配置、上下文块和推理执行上下文。子会话通过受控代理（Remote Provider）访问父 Agent 持有的共享资源，而不会绕过 Agent 生命周期直接执行原始 MCP 工具调用或直接读写共享文件。

### 整体架构

系统采用两层 DO 结构。顶层为 Assistant（用户级父 Agent），每个用户一个实例；底层为 ChatSession（会话级子 Agent），每个聊天会话一个 Facet 实例。两者均基于 Agents SDK 的 Agent 基类，子 Agent 通过 parent.subAgent(ChildClass, name) 创建，享有独立 SQLite 存储和推理隔离。

客户端通过 WebSocket 连接直接与 ChatSession 子 Agent 通信（URL 模式：/agents/assistant/{user-id}/sub/chat-session/{chat-id}），父 Agent 仅在连接建立时通过 onBeforeSubAgent 钩子执行访问控制，WebSocket 升级后帧直接路由到子 Facet。父 Agent 和子 Agent 之间的数据交互通过 DO RPC（callable 方法）完成，共享资源走 Remote Provider 代理模式。

### 父 Agent（Assistant）职责与内部结构

Assistant 是每个用户一个实例的顶层 Agent（extends Agent），持有以下持久化状态和职责：

- 聊天目录索引（SQLite 表 chats_index）：记录每个子会话的 id、标题、创建时间、更新时间、最后消息预览，支持增删改查和 FTS5 标题搜索。
- 共享工作空间（Workspace 实例）：基于 DO SQLite 的虚拟文件系统，存储用户项目文件，所有子会话通过 RemoteWorkspaceProvider 代理读写。
- MCP 连接池：管理与外部 MCP 服务器的 SSE/WebSocket 连接生命周期（建立、重连、心跳），维护可用的工具列表和连接状态。子会话不直接持有 MCP 连接，而是通过父 Agent 的 callable 方法代理工具调用。
- OAuth 凭据存储：缓存用户在某个会话中完成 OAuth 授权后的 access_token / refresh_token，按服务名索引；其他会话可直接使用已授权凭据，无需重新授权。
- 文件变更广播协调：工作空间发生变更时，父 Agent 通过遍历子 Facet 的 RPC 存根列表、调用各子 Agent 的转发方法，将变更通知（含变更路径和类型）中继到各自连接的客户端，前端据此刷新文件树或编辑器。具体机制详见「文件变更实时同步」章节。
- 全局定时任务：利用 Agent 基类的 schedule()/scheduleEvery() 能力执行跨会话摘要、定期清理、定时文件同步等后台任务。

### 子 Agent（ChatSession）职责与会话隔离

ChatSession 是每个聊天会话一个 Facet 实例的子 Agent（extends Think 或 Agent），拥有独立的 SQLite 存储和推理隔离。其内部状态与会话边界一一对应，不同会话之间不共享这些数据：

- 消息历史：树形结构消息表（assistant_messages），支持 parent_id 分支、压缩叠加层（compaction overlays）和 FTS5 全文搜索。每个会话完全独立。
- 会话级上下文块：通过 Session.withContext() 配置的上下文块（如 memory、todos 等），其中一部分是指向父 Agent 共享上下文的 RemoteContextProvider（如用户全局记忆），另一部分是本会话私有的。
- 会话配置：模型选择、系统提示词、最大步数、自定义工具等，通过 Think 的 getModel()、getSystemPrompt()、getTools() 等覆写点配置。
- 推理循环与流式处理：每个子 Agent 独立运行 streamText 推理循环，WebSocket 帧直接路由到子 Facet，不存在多会话串行化问题。

### 共享资源代理机制

子会话不直接持有共享资源的连接或凭据，而是通过代理机制调用父 Agent 暴露的 callable 方法。以下分别说明工作空间、MCP 工具和 OAuth 凭据三种共享资源的代理方案。

工作空间代理：父 Agent 持有一个 Workspace 实例（基于 DO SQLite 的虚拟文件系统）。子会话不直接创建 Workspace，而是通过 RemoteWorkspaceProvider 代理调用父 Agent 的 callable 方法 executeWorkspaceOperation(type, params)，支持 read、write、edit、list、find、grep、delete 七种操作。父 Agent 在 Workspace 构造时设置 onChange 回调，每次文件变更后触发文件变更广播协调流程（详见「文件变更实时同步」章节）：父 Agent 遍历已注册的子 Facet RPC 存根，调用各子 Agent 的中继方法，由子 Agent 通过自身的 broadcast() 将变更事件推送到各自连接的客户端。

MCP 工具连接代理：父 Agent 管理与外部 MCP 服务器的 SSE/WebSocket 连接生命周期（建立、心跳、重连），维护可用工具列表和连接状态。子会话不直接建立 MCP 连接，而是在其工具集中注册 mcp_proxy 工具。当大模型发起 MCP 工具调用时，子 Agent 通过 parentAgent(Assistant).executeMCPTool(serverName, toolName, args) 代理到父 Agent，父 Agent 找到对应连接并执行真正的 MCP 工具调用，结果返回子 Agent。该代理路径确保：MCP 连接只由父 Agent 建立一次、所有会话共享同一连接池、用户授权一次即可。

OAuth 凭据代理：父 Agent 维护一个 OAuth 凭据缓存（按服务名索引的 access_token / refresh_token 及过期时间）。当某个子会话完成 OAuth 授权流程后，凭据写入父 Agent 的 SQLite 表。其他子会话需要访问同一服务时，通过 RemoteCredentialProvider 调用 parentAgent(Assistant).getCredential(serviceName)，父 Agent 检查凭据有效性、必要时刷新 token、返回有效凭据。子会话不直接接触原始 token 字符串（仅用于 API 调用），父 Agent 可在 token 刷新后通知持有该凭据代理的子会话更新其上下文。

### 访问控制与会话生命周期

访问控制通过父 Agent 的 onBeforeSubAgent 钩子实现。当客户端请求连接 /agents/assistant/{user-id}/sub/chat-session/{chat-id} 时，框架先唤醒父 Agent，调用 onBeforeSubAgent(req, { className, name })，钩子执行以下检查：(1) className 是否为 ChatSession；(2) 通过 hasSubAgent(className, name) 判断该会话是否已在父 Agent 的注册表中存在（由 createChat 创建时写入）；(3) 通过 req 中的用户身份信息验证当前用户是否为该父 Agent 实例的合法所有者。任一检查失败返回 404 或 403 Response，阻止连接建立。检查通过后框架转发请求到子 Facet，WebSocket 升级后帧直接路由到子 Agent。

会话生命周期受父 Agent 统一管理。createChat(id, title) 在父 Agent 的 SQLite 注册表中写入行记录并通过 subAgent 创建子 Facet；deleteChat(id) 软删除注册表行（标记 deleted_at），广播新的聊天列表给所有连接的客户端，子 Facet 由其 GC 机制自动回收；renameChat(id, title) 更新注册表并通过 setState 广播。子 Agent 通过 this.parentPath 获取其在用户会话树中的完整路径，通过 this.parentAgent(Assistant) 获取父 Agent 的 RPC 存根。

### 文件变更实时同步

工作空间文件的变更通知由父 Agent 集中管理，避免各子会话独立轮询带来的延迟和资源浪费。机制如下：

- 父 Agent 在创建 Workspace 实例时注册 onChange 回调。每次文件创建、更新、删除操作完成后，回调被触发。
- 回调函数构造变更事件对象 { type: 'file-changed', path: string, operation: 'created'|'updated'|'deleted', timestamp: number }。
- 父 Agent 遍历已维护的子 Facet RPC 存根列表，通过 DO RPC 调用各子 Agent 的 notifyFileChange(event) callable 方法；各子 Agent 收到调用后，通过自身的 this.broadcast() 将事件推送到连接到该子 Agent 的所有客户端（包括同一会话的多个标签页）。
- 客户端收到事件后，根据所在会话的上下文决定处理方式：文件树组件刷新对应目录、打开该文件的编辑器提示文件已变更、正在进行的与文件相关的 LLM 操作可注入系统消息通知模型文件已变更。

由于子 Agent 的 WebSocket 客户端直接连接到子 Facet，父 Agent 自身的 broadcast() 仅能到达直接连接在父 Agent 上的客户端，无法覆盖子 Facet 的客户端。因此，跨 Facet 的文件变更广播需采用 RPC 中继方案：子 Agent 在 onStart 时通过 parentAgent(Assistant) 向父 Agent 注册自身（父 Agent 将子 Facet 的 RPC 存根存入列表）；父 Agent 检测到文件变更后，遍历存根列表，通过 DO RPC 调用各子 Agent 暴露的 notifyFileChange(event) callable 方法；子 Agent 收到调用后执行 this.broadcast()，将变更事件推送到连接到该子 Agent 的所有客户端。根据 Agents SDK 的 Facet 广播特性（子 Agent 的 broadcast() 覆盖连接到该 Facet 的 WebSocket 客户端），此方案确保所有会话窗口和标签页均能收到文件变更通知。

### 全局定时任务调度

跨会话的全局任务由父 Agent 通过 Agent 基类的调度能力统一执行，而非散落在各个子会话中。父 Agent 利用 schedule()（单次定时任务）、scheduleEvery()（周期性任务）、cancelSchedule()（取消任务）管理全局任务生命周期。典型场景包括：

- 跨会话摘要：定时遍历所有活跃子会话，调用各子 Agent 的消息历史摘要接口，汇总生成用户级别的日报或周报。
- 过期凭据刷新：定时检查 OAuth 凭据缓存中的 token 过期时间，提前刷新即将过期的 token。
- 空闲会话归档：定时标记超过 N 天无活动的子会话为已归档，触发子 Facet 的消息压缩和 DO 资源回收。
- 定时文件同步：定时从外部数据源拉取更新到工作空间文件，触发文件变更广播。

定时任务在父 Agent 的 onStart 中注册。由于子 Facet 不支持独立的 schedule()，所有需要定时执行的逻辑必须在父 Agent 中实现。对于需要操作子 Agent 数据的任务（如跨会话摘要），父 Agent 通过 this.subAgent(ChildClass, id) 获取子 Agent 的 RPC 存根并调用其 callable 方法。

### 技术效果与复用性

本方案利用 Agents SDK 已有的 Agent 基类、subAgent Facet 机制、sub-agent routing 外部寻址、Session 存储、Workspace 文件系统等能力，通过组合而非侵入式修改实现了多会话助理系统。与仅在单个 Agent 中增加多个 chat id 的方案相比，本方案的核心差异在于：(1) 用户级共享资源和会话级隔离上下文分别由不同的 DO 实例承载，职责边界清晰；(2) 子会话通过受控代理访问共享资源，而非直接持有连接或凭据；(3) 全局任务由父 Agent 统一调度，不依赖子 Facet 的 schedule 能力。

### 待确认风险点

(1) 子 Facet 的 keepAlive() 已被框架明确为空操作（workerd 不支持 SQLite 背衬 Facet 的独立 alarm），schedule()/scheduleEvery() 依赖相同的 alarm 机制，推断同样不可用；所有定时任务必须由父 Agent 执行，可能造成父 Agent 负载集中。(2) 文件变更广播需父 Agent 维护子 Facet 的 RPC 存根列表并逐一中继，在大量会话场景下广播开销需评估。(3) MCP 工具代理路径增加一次 DO RPC 往返延迟，对于高频工具调用场景需考虑缓存或批处理优化。(4) OAuth 凭据的跨会话共享需确保 token 刷新时的并发安全（父 Agent 单线程天然序列化，但父子 Agent 之间需注意刷新期间的竞态）。(5) ChatSession 基于 Think 或裸 Agent 的选择会影响可用的开箱即用能力（Think 提供 Session 存储、上下文块、压缩、FTS5 搜索等，裸 Agent 需自行实现），需根据具体需求权衡。
