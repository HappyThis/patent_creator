## 技术方案

本技术方案提出一种基于父子 Agent 架构的多会话协同系统。系统在现有 Durable Object 风格的单会话 Agent 体系上引入用户级父 Agent（UserAgent）和会话级子 Agent（SessionAgent）两层结构。UserAgent 作为用户维度下的唯一持久实体，负责管理共享资源（Workspace 文件、MCP 工具连接、OAuth 授权凭据）、维护会话注册表、调度跨会话定时任务以及向前端广播文件变更信号。SessionAgent 作为会话级隔离的执行上下文，持有独立的对话历史、分支树、个性化配置、扩展插件和短期记忆，并通过受控代理接口访问 UserAgent 托管的共享资源。浏览器前端或子会话在任何情况下均不得绕过 Agent 生命周期直接执行原始 MCP 工具调用。

### 一、用户级父实体与会话级子实体的职责边界

系统将原单一 Agent 拆分为两层：UserAgent（用户级父实体）和 SessionAgent（会话级子实体）。两者均有独立的 Durable Object 存储后端，但职责严格分离。

UserAgent 的职责包括：(1) 维护用户下所有会话的注册表（session registry），记录每个会话的 session_id、名称、创建时间、最近活跃时间和状态（活跃/归档）；(2) 持有 Workspace 文件系统的唯一真实状态，所有文件读写经其仲裁；(3) 管理 MCP 服务器连接池和 OAuth 授权凭据，所有外部工具调用必须经 UserAgent 转发，外部凭据仅存储在 UserAgent 中；(4) 维护一个事件总线（event bus），用于向前端各标签页广播文件变更、会话变更等实时信号；(5) 作为跨会话定时任务的调度器，管理 cron 触发器和任务执行结果。UserAgent 在用户首次登录时创建，用户注销后仍持久保留。

SessionAgent 的职责包括：(1) 持有本会话的完整对话历史（消息事件流、分支树）；(2) 维护会话级个性化配置（系统提示词、模型偏好、温度参数、工具开关等）；(3) 管理会话级扩展和短期记忆（如上下文摘要缓存、RAG 检索索引）；(4) 执行聊天推理循环，包括 Think Agent 推理、工具调用决策和子 Agent 路由。SessionAgent 不直接持有 MCP 连接句柄或 OAuth token，所有对外工具调用通过 UserAgent 的代理接口完成。

### 二、会话隔离机制

每个 SessionAgent 拥有独立的 Durable Object 存储分区，其状态键空间以 session_id 为前缀，确保与 UserAgent 及其他 SessionAgent 物理隔离。

会话隔离的具体措施：(1) 消息历史隔离——每个 SessionAgent 持有自己的事件流存储（沿用现有 JSONL 格式），不同会话的消息事件不可互相读取或交叉引用；(2) 分支树隔离——会话内的消息分支（branch）和版本切换仅作用于本会话，不会影响其他会话；(3) 配置隔离——系统提示词、模型参数、工具启用列表等会话级配置存储在各 SessionAgent 的私有状态中，创建会话时可从模板复制初始化但后续修改互不影响；(4) 扩展与记忆隔离——每个会话的短期记忆缓存、RAG 索引等扩展数据独立管理，不会被其他会话的推理过程污染。

会话创建流程：前端通过 UserAgent 的会话管理接口请求创建新会话。UserAgent 验证用户身份后，在会话注册表中分配新的 session_id，创建对应的 SessionAgent Durable Object 实例，初始化默认配置，并将 session_id 返回前端。前端随后通过 session_id 与该 SessionAgent 建立 WebSocket 连接。会话删除时，UserAgent 先标记会话为删除中状态，等待 SessionAgent 完成当前推理轮次后，清理其 Durable Object 存储并移除注册表条目。

### 三、共享资源代理机制

共享资源包括三类：Workspace 文件系统、MCP 工具连接池和 OAuth 授权凭据。这三类资源均按用户维度统一管理在 UserAgent 中，SessionAgent 通过受控代理接口访问，不得直接持有原始连接句柄或凭据。

Workspace 文件共享：(1) UserAgent 维护一份按用户维度隔离的文件系统（可基于本地磁盘或对象存储），所有 SessionAgent 看到的文件视图完全一致；(2) SessionAgent 需要读取文件时，通过代理接口向 UserAgent 发起 read_file 请求，UserAgent 验证会话合法性后返回文件内容；写入文件同理，SessionAgent 调用 write_file 代理接口，由 UserAgent 执行实际写入并触发文件变更事件；(3) 文件锁机制——当某个 SessionAgent 正在执行涉及文件修改的工具调用时，UserAgent 对该文件路径加乐观锁（基于版本号），并发写入被拒绝并返回冲突错误。

MCP 工具连接共享：(1) 所有 MCP 服务器连接（如 stdio、SSE、HTTP 等传输方式）由 UserAgent 建立并维护连接池；(2) SessionAgent 在推理过程中决定调用某个 MCP 工具时，将工具名称和参数封装为 tool_call 请求发送给 UserAgent，UserAgent 从连接池中选取可用连接执行调用，将结果返回给 SessionAgent；(3) 此代理模式的关键效果是：浏览器和 SessionAgent 均不持有 MCP 服务器的原始连接信息（如 API key、连接字符串），也无法绕过 UserAgent 直接发起 MCP 调用；连接池支持连接复用和心跳保活，避免每个会话重复建连。

OAuth 授权凭据共享：(1) OAuth 授权流程由 UserAgent 统一发起，授权完成后 access_token 和 refresh_token 仅存储在 UserAgent 的加密存储区；(2) SessionAgent 需要访问 OAuth 保护的外部服务时，通过代理接口请求 UserAgent 使用已授权凭据发起 API 调用，SessionAgent 无法获取原始 token 值；(3) Token 过期时由 UserAgent 自动使用 refresh_token 续期，续期结果对所有会话透明生效。

### 四、访问控制机制

系统采用多层访问控制防止未授权访问和会话 ID 猜测攻击。

第一层——用户身份验证：前端与 UserAgent 的所有通信均需携带用户身份令牌（如 JWT）。UserAgent 在处理任何会话管理请求（创建、切换、重命名、删除）或共享资源代理请求前，先验证令牌有效性和用户身份，拒绝未认证请求。

第二层——会话归属校验：每个 SessionAgent 的 Durable Object 存储中记录其归属的 user_id 和父 UserAgent 标识。UserAgent 在处理针对特定 session_id 的操作时，校验该 session_id 确实属于当前已认证用户。即使用户通过猜测获得其他用户的 session_id，归属校验也会拒绝访问。

第三层——会话注册表白名单：UserAgent 维护的会话注册表是所有合法会话的唯一权威来源。前端只能访问注册表中存在且状态为活跃的 session_id。任何试图访问未注册 session_id 的请求均被拒绝。用户不能通过在前端拼接 session_id 来创建或访问未登记的子会话；会话创建必须通过 UserAgent 的 create_session 接口完成。

第四层——工具调用链路完整性：SessionAgent 发起的 MCP 工具调用请求必须包含完整的调用链标识（user_id → session_id → round_id → call_id），UserAgent 在代理执行前验证该调用链的合法性，防止重放攻击和跨会话调用伪造。

### 五、实时文件同步机制

为解决多个浏览器标签页或会话面板之间的文件状态一致性问题，系统在 UserAgent 中引入基于事件总线的实时推送机制，替代传统的轮询方式。

事件总线设计：(1) UserAgent 内部维护一个发布-订阅事件总线，事件类型包括 file_changed（文件内容变更）、file_created（新文件创建）、file_deleted（文件删除）、session_updated（会话元数据变更）和 shared_resource_changed（MCP 连接或 OAuth 状态变更）；(2) 每个前端标签页在与 UserAgent 建立 WebSocket 连接时，自动订阅当前用户的事件通道，连接建立时携带用户令牌进行身份验证；(3) 事件消息携带变更摘要（如文件路径、变更类型、版本号）而非完整文件内容，前端根据摘要决定是否需要拉取最新数据。

写入触发推送的流程：当某个 SessionAgent 通过代理接口修改文件时，UserAgent 完成写入后立即构造 file_changed 事件，携带变更文件路径和新版本号，推送到所有已连接的前端标签页。各标签页收到事件后，对比本地缓存版本号，如有差异则按需重新加载文件内容。对于正在编辑同一文件的其他会话，前端可展示冲突提示。

断线重连与状态同步：WebSocket 连接断开后，前端采用指数退避策略重连。重连成功后，前端发送携带本地已知最大事件序号的状态同步请求，UserAgent 将该序号之后的所有未消费事件批量推送给前端，确保不掉消息。此机制复用了现有会话的事件 seq 编号体系。

### 六、跨会话定时任务调度

跨会话定时任务（如每日摘要生成、定时文件备份、周期性数据抓取）由 UserAgent 统一调度，不分散在各 SessionAgent 中独立管理，避免任务重复执行和状态不一致。

调度器设计：(1) UserAgent 内部包含一个 cron 调度器，支持标准 cron 表达式配置任务触发时间；(2) 每个定时任务以 task_definition 形式存储在 UserAgent 的持久化存储中，包含任务 ID、cron 表达式、任务类型（如 summarize_all_sessions、backup_workspace）、任务参数和最后执行时间戳；(3) 调度器在每个触发时刻计算下一次触发时间，并将任务写入待执行队列。

任务执行机制：(1) UserAgent 从待执行队列中取出任务，根据任务类型决定执行策略——对于需要遍历所有会话的任务（如跨会话摘要），UserAgent 逐一请求各 SessionAgent 生成本会话摘要，聚合后写入 UserAgent 的全局摘要存储；(2) 对于仅涉及共享资源的任务（如文件备份），UserAgent 直接操作 Workspace 文件系统；(3) 任务执行结果（成功/失败、产出摘要）记录在 UserAgent 的任务执行历史中，前端可按需查询。

幂等与防重复：(1) 每个任务执行实例携带唯一的 execution_id，基于任务 ID 加触发时间戳生成；(2) UserAgent 在执行前检查该 execution_id 是否已存在于执行历史中，避免因调度器重启或时钟回拨导致重复执行；(3) 任务执行超时时，UserAgent 标记该执行为超时状态，不自动重试，由用户或系统管理员手动触发重跑。

### 七、技术效果

本方案在现有 Durable Object 单会话架构上做最小侵入性改造，通过引入 UserAgent 父实体实现多会话协同，技术效果如下：

(1) 会话间强隔离与资源高效共享并存——消息历史、配置、记忆按会话隔离，Workspace、MCP、OAuth 按用户共享，避免了为每个会话复制 MCP 连接和 OAuth 凭据的资源浪费，同时保证会话间推理过程互不干扰。

(2) 安全性提升——MCP 工具调用和 OAuth 凭据统一由 UserAgent 代理，前端和 SessionAgent 均无法直接接触原始凭据和连接信息，攻击面缩小；会话归属校验和注册表白名单机制阻止了会话 ID 猜测和未授权访问。

(3) 实时性改进——基于 WebSocket 的事件推送替代轮询，文件变更可在毫秒级同步到所有打开的前端标签页，减少不必要的网络请求和服务器负载。

(4) 全局任务可管理——定时任务集中在 UserAgent 调度，避免了多个会话各自触发相同任务导致的重复执行和数据不一致问题。

(5) 复用现有体系——方案在现有 agent、workspace、MCP、chat recovery 和工具调用框架上扩展，不重写现有推理循环和工具调用协议，仅新增 UserAgent 层级和代理接口。

### 八、风险与待确认事项

以下为当前方案中需要后续确认的技术风险点和待澄清事项：

(1) UserAgent 单点与可用性：当前设计中 UserAgent 作为用户维度的唯一父实体，若其 Durable Object 因故障不可用，该用户下所有会话的共享资源访问、新会话创建和实时推送均会中断。后续需评估是否需要引入 UserAgent 的热备或自动故障转移机制。

(2) MCP 连接池并发限制：单个 MCP 服务器的连接数可能有限制（如 stdio 传输通常为单进程），当多个 SessionAgent 同时通过 UserAgent 请求同一 MCP 工具时，需要在连接池层面设计排队或限流策略，避免连接耗尽。

(3) 会话级工具开关与全局 MCP 连接的关系：当前描述中 UserAgent 管理全局 MCP 连接池，但各 SessionAgent 可配置工具启用列表。需要明确当 SessionAgent 禁用了某个 MCP 工具但 UserAgent 仍持有该连接时，连接是否保持。建议在 UserAgent 中以引用计数方式管理连接生命周期，仅当所有会话均禁用某 MCP 服务器时才关闭对应连接。

(4) 跨会话文件编辑冲突：乐观锁机制可以检测冲突，但冲突发生后的用户体验策略（自动合并、提示用户选择版本、强制覆盖）尚未明确，需要结合实际前端交互设计进一步细化。

(5) 定时任务的会话生命周期依赖：跨会话摘要等定时任务需要遍历所有会话并请求 SessionAgent 生成摘要。若某会话处于长时间空闲状态导致 Durable Object 被休眠或回收，UserAgent 需要具备唤醒机制。建议复用现有 Durable Object 的自动唤醒能力，在 UserAgent 请求 SessionAgent 时由底层存储层自动激活。
