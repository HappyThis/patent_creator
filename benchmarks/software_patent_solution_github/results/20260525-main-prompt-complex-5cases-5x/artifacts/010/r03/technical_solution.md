## 技术方案

本方案提出一种基于父子 Durable Object 架构的多会话助手系统。系统将用户级共享资源与会话级隔离上下文分离到不同粒度的 Durable Object 实例中：一个用户级父实体负责管理会话目录、共享工作区、MCP 工具连接、OAuth 授权凭据、实时同步信号和定时任务调度；多个会话级子实体各自维护独立的消息历史、分支、个性化配置和会话级上下文。子会话通过受控的 RPC 代理访问共享资源，浏览器客户端和子会话均不绕过父实体的生命周期直接操作原始 MCP 工具或工作区文件。

### 整体架构

系统由两类 Durable Object 实例组成：一个 UserAgent（用户级父实体）和多个 ChatSession（会话级子实体）。UserAgent 按用户维度创建，一个认证用户对应一个 UserAgent 实例。ChatSession 按会话维度创建，每个聊天会话对应一个 ChatSession 实例，作为 UserAgent 的子代理（sub-agent / facet）存在。

客户端与系统的连接分为两条路径：（1）侧边栏连接——客户端通过 WebSocket 连接到 UserAgent，获取会话列表和共享状态广播；（2）活跃会话连接——客户端通过嵌套子代理路由（URL 模式 /agents/{user-agent}/{user-id}/sub/{chat-session}/{session-id}）直接连接到目标 ChatSession，消息流和推理执行在该子 DO 内完成。

父实体与子实体之间通过 Durable Object RPC 通信。子实体通过 parentAgent(Cls) 获取父实体的类型化 RPC 桩，调用父实体的 @callable 方法访问共享资源；父实体通过 subAgent(Cls, name) 获取子实体桩，进行跨会话操作（如全局搜索、汇总）。

### 父子实体职责边界

UserAgent（用户级父实体）的职责包括：（1）会话目录管理——维护 chat_index 表，记录每个 ChatSession 的 id、标题、创建时间、更新时间、最后消息预览，提供 createChat、deleteChat、renameChat、listChats 等 @callable 方法，操作结果通过 setState 广播到所有连接的侧边栏客户端；（2）共享工作区——持有一个 Workspace 实例（基于 DO SQLite 的虚拟文件系统，可选 R2 溢出），所有 ChatSession 通过父实体代理进行文件读写，文件变更通过 onChange 回调触发广播；（3）共享 MCP 连接池——父实体负责调用 addMcpServer 建立和维护 MCP 服务器连接、处理 OAuth 授权回调，子会话不直接持有 MCP 连接，工具调用请求通过父实体转发；（4）共享 OAuth 凭据——用户在任意会话中完成的 OAuth 授权结果存储在父实体的 SQLite 中，所有子会话共享同一套授权状态；（5）实时同步中枢——文件变更、MCP 连接状态变更通过父实体的 broadcast 通道推送到所有连接的浏览器标签页；（6）定时任务调度——利用 Agent 基类的 schedule() 机制（基于 DO Alarm），在父实体上注册定时任务（如每日摘要、跨会话汇总），父实体在触发时按需驱动子会话执行；（7）访问控制网守——通过 onBeforeSubAgent 钩子拦截所有指向子会话的路由请求，仅允许已在 chat_index 中登记的子会话被访问，阻止通过猜测会话 id 的未授权访问。

ChatSession（会话级子实体）的职责包括：（1）消息历史——通过 Session 存储层维护树形结构消息（支持分支和再生），使用 SQLite 持久化，每个会话拥有独立的 assistant_messages 表；（2）会话级上下文——通过 Session 的上下文块（Context Blocks）维护会话级记忆（如用户偏好、项目上下文），支持 LLM 可写的持久化记忆，通过 configureSession 配置自动压缩（compaction）和全文搜索（FTS5）；（3）会话级配置——通过 think_config 表存储模型层级、persona 等会话级配置，与会话 id 绑定；（4）扩展管理——每个会话独立加载和管理 Sandboxed Worker 扩展；（5）推理执行——基于 Think agent 的 agentic loop（streamText + 工具调用），支持可恢复流（ResumableStream）、中止、自动续接；（6）会话级搜索——通过 Session 的 FTS5 索引在本会话范围内搜索消息。

### 会话隔离机制

每个 ChatSession 作为独立的 Durable Object facet 运行，享有框架级隔离：（1）独立 SQLite 存储——每个 facet 拥有自己的 SQLite 实例，消息表、配置表、上下文块表在物理上不互通；（2）独立内存状态——每个 facet 的 JavaScript 堆隔离，消息缓存、工具状态、流式缓冲区互不干扰；（3）独立 WebSocket 连接——客户端连接到特定 ChatSession 的 WebSocket 直接路由到对应 facet，消息广播仅发送到该 facet 的客户端集合，不会泄漏到其他会话；（4）并行执行——不同 ChatSession 位于不同 facet，可在同一台机器上并行处理各自的推理请求，不会因为单个 DO 的单线程特性而互相阻塞。

会话的创建通过父实体的 createChat 方法完成。该方法首先生成会话 id，调用 subAgent(ChatSession, id) 在父实体的框架级注册表中创建子代理条目，然后向 chat_index 表插入元数据行，最后通过 setState 广播更新后的会话列表。删除会话时，调用 deleteSubAgent(ChatSession, id) 从注册表移除条目，同时在 chat_index 中标记软删除（设置 deleted_at 时间戳），广播更新后的列表，子 DO 在自然休眠后被 Durable Object 的 TTL 机制回收。

会话切换由客户端驱动：当用户在侧边栏选择不同会话时，客户端关闭当前 ChatSession 的 WebSocket 连接，通过 useAgent({ sub: [...] }) 建立到新 ChatSession 的连接。父实体不维护"当前活跃会话"状态，不同浏览器标签页可以同时打开不同会话。

### 共享资源代理机制

子会话不直接持有 Workspace 实例、MCP 连接或 OAuth 凭据。对这些共享资源的访问通过以下受控代理机制实现：

工作区代理——父实体在 onStart 时初始化 Workspace 实例，并向子会话暴露一组 @callable 方法（如 readWorkspaceFile、writeWorkspaceFile、listWorkspaceFiles）。子会话通过 parentAgent(UserAgent) 获取父实体桩，调用这些方法来操作文件。对于需要在 LLM 工具调用中暴露工作区操作的场景，子会话通过 createWorkspaceTools 的替代实现或工具包装层，将文件操作工具的实现转发到父实体 RPC 调用。这种代理方式确保：（a）所有文件操作经过单一写入者（父 DO），避免多子会话并发写入同一文件时的冲突；（b）文件变更事件由父实体统一捕获和广播，不依赖各子会话各自触发；（c）工作区的命名空间隔离和权限控制集中在父实体一层。

MCP 连接代理——父实体负责调用 addMcpServer 建立 MCP 连接和处理 OAuth 回调。子会话需要调用 MCP 工具时，工具调用请求通过 RPC 发送到父实体，父实体将请求转发到对应的 MCP 连接并返回结果。这个设计的关键约束是：子会话（以及浏览器客户端）不能绕过父实体的生命周期直接执行原始 MCP 工具调用。具体实现方式为：（a）父实体暴露一个 @callable 方法 invokeMcpTool(serverId, toolName, args)，子会话通过此方法代理所有 MCP 工具调用；（b）或者父实体在子会话创建时将 MCP 工具的 schema 列表传递给子会话，子会话在本地注册代理工具，每个代理工具的 execute 回调内部通过 RPC 调用父实体的实际执行方法。

OAuth 凭据共享——当用户在某个会话中触发 MCP OAuth 授权流程时，OAuth 回调 URL 路由到父实体。父实体完成令牌交换后将 access_token 和 refresh_token 存储在父实体的 SQLite 表中。所有子会话通过父实体的 getOAuthToken(serverId) 方法按需获取令牌，实现"一次授权，多会话共用"。

### 访问控制与安全边界

系统的安全边界通过多层访问控制实现：（1）用户认证层——Worker 入口在 /chat 路径上验证用户身份（如 GitHub OAuth），通过 getAgentByName(env.UserAgent, user.login) 将请求路由到对应 UserAgent DO，未认证请求返回 401；（2）父实体网守——UserAgent 的 onBeforeSubAgent 钩子拦截所有指向 /sub/{chat-session}/{id} 的请求，通过 hasSubAgent(className, name) 检查该会话 id 是否已在注册表中登记，未登记则返回 404，在子 DO 被唤醒之前即拒绝请求；（3）会话归属验证——chat_index 表中的会话记录与父实体 DO 绑定（父实体 DO id 即为用户标识），一个用户的父实体不可能创建属于另一个用户的子会话，因为每个用户的父实体 DO 命名空间天然隔离；（4）RPC 方法访问控制——父实体的 @callable 方法仅在 DO RPC 上下文中可访问，外部 HTTP 请求无法直接调用；子会话通过 parentAgent() 获取的桩也受框架类型检查约束。

特别地，对于 session-id 猜测攻击：即便攻击者获取了有效的用户认证令牌，尝试访问 /sub/chat-session/{random-id} 时，onBeforeSubAgent 会检查该 id 是否在父实体的注册表中——只有通过父实体 createChat 方法正式创建的子会话才会被注册，随机猜测的 id 必然不在注册表中，请求在到达子 DO 之前被拒绝。

### 实时同步机制

系统通过父实体作为同步中枢，实现共享资源变更的实时推送，避免浏览器轮询：（1）文件变更通知——Workspace 的 onChange 回调在每次文件创建、更新、删除操作后触发，父实体在此回调中调用 broadcast 向所有连接到父实体的侧边栏客户端发送文件变更事件（包含变更路径和操作类型），前端收到后刷新文件树视图；（2）会话列表同步——createChat、deleteChat、renameChat 操作在修改 chat_index 表后调用 setState 更新父实体的广播状态，所有侧边栏客户端自动收到更新后的会话列表，无需手动刷新；（3）MCP 连接状态同步——当 MCP 服务器连接状态变化（连接中、已连接、认证中、断开），父实体广播状态更新，前端更新连接状态指示器。

对于多个浏览器标签页同时打开同一会话的场景：各标签页通过独立的 WebSocket 连接到同一个 ChatSession facet，该 facet 的消息广播（broadcast）将新消息推送到所有连接的标签页，实现多标签页间的消息同步。对于同时打开不同会话的标签页，它们连接到不同的 ChatSession facet，消息广播天然隔离。

### 定时任务调度机制

定时任务统一由父实体（UserAgent）管理，而非分散在各子会话中，原因在于：（1）Durable Object 的 facet 不支持独立 alarm，子会话无法自行注册 schedule()；（2）跨会话任务（如每日摘要、全局索引重建）天然需要父实体协调多个子会话。

父实体在 onStart 中通过 this.schedule(cron, methodName, payload, { idempotent: true }) 注册定时任务，底层基于 Durable Object Alarm 机制实现持久化调度。定时任务触发时，父实体的对应方法被调用，根据任务类型采取不同策略：（1）纯父实体任务——如清理过期会话的软删除记录，直接在父实体内完成；（2）广播式任务——如每日摘要，父实体遍历 chat_index 中的活跃会话列表，通过 subAgent() 获取每个 ChatSession 的 RPC 桩，调用会话的生成摘要方法，汇总结果后存储或推送；（3）单会话驱动任务——父实体将定时触发器转发到特定的 ChatSession，由该会话执行具体的推理流程。

任务调度使用 DO Alarm 的持久化特性：alarm 时间和待执行任务元数据存储在 SQLite（cf_agents_schedules 表）中，即使 DO 进入休眠状态，Alarm 也会在预定时间唤醒 DO。对于重复任务（cron 模式），每次执行完成后自动计算并注册下一次触发时间。

### 关键数据结构与处理流程

父实体 UserAgent 的关键 SQLite 表结构：（1）chat_index——字段包括 id (TEXT PK)、title (TEXT)、created_at (INTEGER)、updated_at (INTEGER)、last_message_preview (TEXT)、deleted_at (INTEGER，软删除标记)；（2）shared_memory——字段包括 label (TEXT PK)、content (TEXT)，存储跨会话共享记忆；（3）mcp_credentials——字段包括 server_id (TEXT PK)、access_token (TEXT)、refresh_token (TEXT)、expires_at (INTEGER)，存储 MCP OAuth 令牌。

子实体 ChatSession 复用 Think agent 的 Session 存储层：assistant_messages（树形消息，含 parent_id）、assistant_compactions（压缩覆盖层）、assistant_fts（FTS5 全文索引）、think_config（会话级配置）。

关键处理流程——创建会话：客户端调用 UserAgent.createChat() RPC → 父实体生成 id，调用 subAgent(ChatSession, id) 在注册表中创建条目 → 向 chat_index 插入元数据 → setState 广播会话列表。切换会话：客户端调用 useAgent({ agent: userAgent, name: userId, sub: [{ agent: chatSession, name: newChatId }] }) → 框架关闭旧 WS，建立到新 ChatSession 的 WS 连接。文件写入：子会话 LLM 调用工具 → 工具 execute 回调通过 parentAgent(UserAgent) 获取父实体桩 → 调用父实体的 writeWorkspaceFile RPC → 父实体 Workspace 写入 → onChange 触发 broadcast 通知所有侧边栏客户端。MCP 工具调用：子会话通过父实体 RPC invokeMcpTool → 父实体查找对应 MCP 连接 → 转发工具调用 → 返回结果给子会话 → 子会话将结果注入 LLM 上下文。

### 与现有系统的复用关系

本方案最大程度复用现有框架能力：（1）子代理路由原语——subAgent()、deleteSubAgent()、hasSubAgent()、listSubAgents()、onBeforeSubAgent、parentAgent()、useAgent({ sub: [...] }) 等已发布的 API 直接作为父子实体通信和寻址的基础；（2）Think Agent——ChatSession 直接复用 Think 的消息存储（Session）、上下文块、压缩、FTS 搜索、可恢复流、工具调用循环、扩展管理、自动续接等能力；（3）Workspace——父实体直接使用 @cloudflare/shell 的 Workspace（SQLite + R2 混合存储）作为共享文件系统；（4）MCP 集成——父实体复用 addMcpServer、OAuth 回调处理等现有 MCP 集成机制；（5）Agent 调度——父实体复用 schedule(cron, method, payload) 和 DO Alarm 机制实现定时任务；（6）Agent 广播——复用 broadcast() 和 setState() 实现实时状态同步。

需要新增或适配的部分：（1）UserAgent 父实体类——实现会话目录管理、共享资源持有和代理方法暴露；（2）ChatSession 子实体中的共享资源代理工具——将文件操作工具和 MCP 工具的实现从本地调用替换为通过 parentAgent 的 RPC 转发；（3）客户端 useChats() Hook——封装侧边栏连接和活跃会话连接的管理；（4）RemoteContextProvider——实现跨 DO 的远程上下文块读写，使子会话可以将共享记忆存储在父实体中。

### 边界条件与异常处理

（1）并发文件写入——父实体 DO 单线程执行保证同一时刻只有一个写入者，但不同子会话的"读取-修改-写入"操作仍可能发生丢失更新。建议对工作区文件操作提供追加式写入（appendFile）作为安全的增量更新原语。

（2）共享记忆的读-改-写竞争——两个子会话同时读取父实体共享记忆、各自修改、写回，可能导致后者覆盖前者。建议优先使用 appendSharedContext 增量追加方法，完整替换操作需在应用层处理冲突。

（3）父实体冷启动延迟——子会话通过 RPC 访问父实体共享资源时，如果父实体处于休眠状态，首次调用会触发冷启动，增加约数百毫秒延迟。Session 的 withCachedPrompt() 机制可缓存冻结的系统提示词，将跨实体 RPC 调用频率降至每回合一次。

（4）子会话删除与活跃连接——用户在标签页 A 打开会话 X，在标签页 B 删除会话 X。父实体标记软删除并广播更新列表，前端 useChats Hook 检测到 X 不在列表中后断开活跃连接。子 DO 保留至自然休眠后被 TTL 回收，期间如有 in-flight 写入仍可完成持久化。

（5）定时任务与子会话生命周期——父实体的定时任务通过 subAgent() 获取子会话桩时，如果子会话 DO 已休眠，RPC 调用会自动唤醒它。对于已标记删除的子会话，父实体在遍历时跳过 deleted_at 不为空的记录。

（6）扩展兼容性——现有 Think 的子代理（如 Researcher）在 ChatSession 内正常运作。ChatSession 内的子代理通过 parentAgent() 获取的是 ChatSession（直接父实体），而非 UserAgent。如需访问用户级共享资源，需显式传递 UserAgent 桩。

### 技术效果

（1）会话隔离与资源共享的统一——通过父子 DO 架构，在保证每个会话消息历史和配置物理隔离的前提下，实现了工作区文件、MCP 连接和 OAuth 授权的用户级共享，避免了每个会话独立维护资源副本带来的数据分裂和维护成本。

（2）并行推理能力——不同会话位于不同 Durable Object facet，可在同一物理机上并行处理各自的 LLM 推理请求，克服了单 DO 多会话方案中所有会话串行化的性能瓶颈。

（3）安全访问控制——通过 onBeforeSubAgent 注册表网守和用户级 DO 命名空间隔离，天然阻止会话 id 猜测攻击和跨用户访问，无需额外的应用层权限校验。

（4）实时同步消除轮询——通过 Workspace onChange 回调 + DO broadcast + WebSocket 推送链，文件变更在毫秒级内通知所有连接的浏览器标签页，无需客户端定时轮询。

（5）框架级复用——方案完全基于已发布的子代理路由、Think Agent、Session 存储、Workspace、MCP 集成和 Agent 调度 API，新增代码量集中于父实体编排逻辑和代理工具适配，不引入新的路由层或存储抽象。

### 风险与待确认问题

（1）MCP 工具代理的性能开销——每个 MCP 工具调用需要额外的父实体 RPC 往返（子→父→MCP 服务器→父→子）。对于高频工具调用场景，累计延迟可能影响用户体验。待确认：是否需要对高频只读工具调用提供本地缓存或直连优化。

（2）父实体单点瓶颈——所有共享资源操作汇聚到父实体 DO，在高并发多会话场景下（如同一用户同时进行 10+ 个会话的大量文件操作），父实体可能成为吞吐量瓶颈。待确认：是否需要工作区分片或读写分离策略。

（3）Workspace 跨会话并发——当多个会话同时操作同一文件时，Workspace 的 onChange 回调会多次触发广播，前端可能收到高频文件变更事件。待确认：是否需要对广播事件做去抖合并。

（4）定时任务的子会话范围——每日摘要等定时任务需要遍历所有活跃子会话，当会话数量超过 50 时，串行 RPC 调用的总延迟可能超过 Alarm 执行时间限制。待确认：是否需要并行遍历和超时保护机制。

（5）OAuth 令牌刷新——共享 OAuth 凭据的 refresh_token 只能使用一次，需要确保父实体在令牌刷新时的竞态条件处理。待确认：是否使用 DO 单线程特性天然规避，还是需要显式的锁机制。
