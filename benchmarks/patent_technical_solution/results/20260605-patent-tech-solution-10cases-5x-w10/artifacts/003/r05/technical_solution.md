## 技术方案

本技术方案提出一种基于双层持久对象（Durable Object，DO）架构的多会话代理助手系统，其核心构思在于：将用户级共享资源与会话级隔离状态分别托管于不同层级的 DO 实例中，并通过受控的跨层 RPC 桥接和子代理路由机制，在保证各会话上下文严格隔离的前提下，实现用户级工作区文件、外部工具连接、授权凭据和跨会话调度能力的连续性。

### 整体架构

系统将代理助手的状态空间划分为两个层级。上层为会话目录代理（Chats DO），每个用户或工作区对应一个 Chat DO 实例，负责维护该用户下全部聊天会话的索引、用户级共享上下文块、跨会话全文搜索，以及工作区文件、外部工具连接、授权凭据等用户级资源。下层为会话代理（Think DO），每个聊天会话对应一个独立的 DO 实例，承载该会话专属的消息树（树形消息存储）、上下文块、压缩覆盖层和推理执行状态。Chat DO 与 Think DO 之间通过 DO RPC 进行受控通信，客户端通过子代理路由直接与 Think DO 建立 WebSocket 连接，Chat DO 仅在连接建立时承担认证与授权中介角色。

### 会话隔离层

每个聊天会话被建模为一个独立的 DO 实例，拥有独立的 SQLite 存储空间和单线程执行上下文。会话内部的消息存储采用树形结构：每条消息通过 parent_id 字段指向上一条消息，构成从根到叶的完整对话路径。当用户在同一消息节点触发重新生成时，新生成的回复作为兄弟节点附加到同一父节点下，形成分支。系统默认沿最新叶节点返回历史记录，同时保留全部历史分支，实现非破坏性的重新生成。

消息存储表 assistant_messages 以消息 id 为主键、session_id 为命名空间隔离字段。对于仅承载单一会话的 Think DO，session_id 为固定值或空值。消息写入采用 INSERT OR IGNORE 策略实现幂等追加，客户端重发相同消息 id 不会产生重复记录。会话专属的上下文块（Context Block）通过可插拔的上下文提供器（ContextProvider）机制注入系统提示词，包括只读的身份描述块、可写的记忆块、可按需加载的技能文档块以及可全文检索的知识库块。这些上下文块的数据存储在同一 DO 的 SQLite 中，天然与会话绑定。

会话级别还实现了非破坏性压缩（Compaction）：当会话消息估计 token 数超过预设阈值时，系统对中间段消息生成 LLM 摘要，存储为压缩覆盖层（compaction overlay）。原始消息保留在 SQLite 中不删除，读取历史时动态应用覆盖层——用摘要消息替换被压缩的消息区间。压缩使用 Hermes 风格算法：保护对话头部的 N 条消息，从尾部按 token 预算向前保留，在工具调用/结果对边界处对齐切口，确保不切割完整的工具交互组。

### 用户级共享资源层

Chat DO 作为用户级的持久对象实例，承担该用户下所有会话的共享资源托管。Chat DO 内部维护以下关键数据结构：

- 会话索引表（chats_index）：记录每个会话的 id、标题（title）、创建时间（created_at）、更新时间（updated_at）、最后消息预览（last_message_preview）及软删除标记（deleted_at）。该表兼作文本搜索的 FTS5 虚拟表，支持对会话标题的前端目录级搜索。
- 共享上下文存储：以标签（label）为键的键值对存储，提供 getSharedContext、setSharedContext、appendSharedContext 三类操作。appendSharedContext 为追加式写入，是推荐的共享记忆写入方式——由于 Chat DO 为单线程执行，追加操作天然避免了读出-修改-写入竞态导致的丢失更新。setSharedContext 用于完整替换场景。
- 共享搜索索引：支持 indexShared 写入键值对并建立 FTS5 索引，支持 searchShared 按标签和查询文本进行全文检索。会话子 DO 通过 RemoteSearchProvider 以 RPC 方式使用该能力。
- 工作区文件与外部工具连接：Chat DO 可挂载 Workspace 实例（虚拟文件系统，支持 SQLite 内联小文件 + R2 溢出大文件的混合存储），并提供外部工具连接池和授权凭据的统一管理。会话子 DO 不直接持有文件系统和外部连接的访问句柄，而是通过 Chat DO 的 RPC 接口间接操作。

Chat DO 还支持跨会话消息搜索（searchMessages）：通过向所有活跃子 DO 并行发起 RPC 调用各自的 session.searchMessages(query)，收集结果后按相关度合并排序。该操作以全局 limit 参数控制总返回量，在活跃会话数在数十个量级时性能可控。

### 跨层资源访问控制

会话子 DO 对用户级共享资源的访问全部通过受控的跨层 RPC 桥接实现，核心机制如下：

一、远程上下文提供器（RemoteContextProvider / RemoteSearchProvider）。这是一组通用的、与会话和 Chat 均无耦合的 RPC 代理类。RemoteContextProvider 实现了可写上下文提供器接口，其 get() 和 set() 方法分别转换为对远端 Chat DO 上 getSharedContext(label) 和 setSharedContext(label, content) 的 RPC 调用。RemoteSearchProvider 则进一步封装 searchShared 和 indexShared 方法。会话子 DO 在配置 Session 时，通过 this.parentAgent(ChatsClass) 获取父 DO 的 RPC 桩，并注入到 RemoteContextProvider 中。

二、父代理引用与单跳访问。子 DO 通过框架提供的 this.parentAgent(ParentClass) 方法获取直接父 DO 的类型化 RPC 桩。该方法基于 DO 实例化时传入的 parentPath（祖先链）进行校验：提取 parentPath 的最后一个条目，验证其 className 与传入的 ParentClass 匹配，然后通过标准的 DO 命名空间绑定解析桩对象。这个校验在调用时即进行，若类名不匹配则立即失败，避免向错误的 DO 发送 RPC 调用。

三、故障软化（Fail-soft）策略。当父 DO 因冷启动、临时不可达或已被删除而无法响应 RPC 时，RemoteContextProvider.get() 返回 null 而非抛出异常。会话子 DO 在当前推理轮次中以空的共享上下文继续运行，下一个轮次重试。这确保了单个会话的可用性不依赖于父 DO 的持续可达性，也避免了父 DO 被删除时所有子会话连锁崩溃。

四、系统提示词冻结与缓存。会话子 DO 在首次推理时调用 freezeSystemPrompt()，将所有上下文块（包括通过 RPC 获取的远程上下文）渲染为系统提示词并持久化到本地 SQLite。后续推理轮次直接返回缓存值，不触发额外的父 DO RPC 调用。当父 DO 上的共享上下文被其他会话更新后，当前会话在当前轮次中使用的是过期快照——这是有意接受的折中：它保护了 LLM 前缀缓存的有效性，避免了每次推理都引入跨 DO RPC 延迟。会话可通过 refreshSystemPrompt() 在轮次边界主动拉取最新内容。

### 会话生命周期与资源安全

会话的创建、删除和重命名等生命周期操作均通过 Chat DO 的 @callable 方法完成，确保目录一致性。

创建会话（createChat）：在 Chat DO 的 chats_index 表中插入新行，生成唯一会话 id（默认使用 crypto.randomUUID()），设置默认标题和创建时间戳。随后通过 this.subAgent(childClass, id) 惰性创建子 DO——首次有客户端连接或 RPC 调用到达时，DO 平台才实际分配实例。因此仅创建目录条目不会消耗 DO 实例资源。

删除会话（deleteChat）：采用软删除策略——将目标行的 deleted_at 设置为当前时间戳，广播更新后的 chats 状态给所有连接的客户端。软删除而非硬删除的原因在于：用户可能在标签页 A 中打开了该会话，正在执行推理写入；硬删除会中断进行中的写入操作。软删除后，持有该会话的客户端通过状态广播感知到该会话已从列表中消失，主动关闭 WebSocket 连接；子 DO 随后进入休眠并由 DO 平台按 TTL 回收。Chat DO 保留软删除行一段时间（软删除窗口），若在窗口期内以相同 id 重新创建会话，则清除旧行并使用递增的代际计数器（generation counter）写入新行，防止旧连接意外重连到新会话。

重命名会话（renameChat）：更新 chats_index 中目标行的 title 字段，广播更新后的状态。子会话 DO 不需要感知标题变更——标题仅在目录列表中使用。

子代理路由认证（onBeforeSubAgent）：Chat DO 在将客户端的 WebSocket 升级请求或 HTTP 请求转发给子 DO 之前，触发 onBeforeSubAgent 中间件钩子。该钩子可执行三类操作：返回 void（放行原始请求）、返回修改后的 Request（注入身份头等）、返回 Response（短路拒绝，不唤醒子 DO）。典型用法包括：检查子 DO 是否在注册表中存在（strict registry gate）、注入用户身份信息到请求头、基于速率限制短路拒绝。这使得 Chat DO 可以在不唤醒子 DO 的前提下拦截未授权访问，同时将子 DO 自身的 onBeforeConnect 留给会话级业务逻辑。

### 多会话并行执行

本方案通过子代理路由（Sub-agent Routing）实现客户端与各会话子 DO 的直接 WebSocket 连接，确保多个会话可以真正并行执行。

一、URL 路由结构。客户端连接子 DO 使用嵌套 URL 路径：/agents/{父类名}/{父实例名}/sub/{子类名}/{子实例名}[/...]。路由支持递归嵌套（如 /agents/tenant/acme/sub/inbox/alice/sub/chat/abc），每层 /sub/ 分隔一个父子跳。路由解析由 routeAgentRequest 完成：先解析顶层父 DO 绑定，再沿 /sub/ 段逐级通过父 DO 的 ctx.facets.get() 获取子 DO 的 Fetcher，最后将请求（WebSocket 升级或 HTTP）转发到目标子 DO。

二、WebSocket 升级后的直连路径。在 WebSocket 升级握手阶段，请求经过路由链上的每个父 DO（触发各自的 onBeforeSubAgent 钩子），父 DO 将请求转发给子 DO，子 DO 返回 101 切换协议响应。该响应沿调用链传播回客户端后，WebSocket 帧直接在客户端与目标子 DO 之间路由，父 DO 从热路径中退出。这意味着会话推理的流式输出、工具调用交互等高频通信不经过父 DO，不引入额外延迟和序列化瓶颈。

三、并行性保证。由于每个会话子 DO 是独立的 DO 实例，Cloudflare Workers 平台可以将其分配到不同的物理运行时上并行执行。用户同时在多个标签页中与不同会话交互时，各自的推理过程完全并行，互不阻塞。这与将所有会话托管在单一 DO 内的方案（如 SessionManager 方案）形成根本差异——后者因 DO 的单线程特性，所有会话的推理操作必须排队执行。

四、客户端多会话连接管理。前端通过 useChats() React Hook 管理多会话状态：该 Hook 内部使用 useAgent({ agent: directory, name: userId, sub: [{ agent: chatAgent, name: activeChatId }] }) 建立与活跃会话的连接，同时通过 Chat DO 的状态广播获取会话列表。切换活跃会话时，前一个 WebSocket 连接以正常关闭码（1000）断开，新会话的 WebSocket 连接通过相同的子代理路由建立。得益于 Think DO 的可恢复流（ResumableStream）机制——流式输出的每个 chunk 在发送客户端前先写入 SQLite 缓冲区——用户在会话间切换后返回时，系统可重放缓冲的 chunk 恢复流式输出状态。
