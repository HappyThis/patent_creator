## 技术方案

### 整体架构概述

本方案在现有 Durable Object 风格的 agent 体系之上，引入用户级父实体（UserAgent）与会话级子实体（SessionAgent）两层结构，实现同一用户账号下的多会话 assistant 系统。UserAgent 作为用户维度的持久化计算单元，统一管理 workspace 文件、MCP 工具连接、OAuth 授权凭据和前端刷新信号通道等共享资源；SessionAgent 作为每个聊天会话的独立执行上下文，持有消息历史、分支、个性化配置、扩展和记忆等会话私有状态。SessionAgent 不直接持有共享资源，而是通过 UserAgent 提供的受控代理接口访问，从而保证会话隔离的同时实现资源共享。

系统包含以下核心模块：UserAgent（父实体）、SessionAgent（子实体）、会话注册表（Session Registry）、资源代理层（Resource Proxy）、实时同步通道（Sync Channel）和定时任务调度器（Scheduler）。各模块之间的关系为：一个 UserAgent 对应一个用户账号；一个 UserAgent 下可创建多个 SessionAgent；每个 SessionAgent 通过会话注册表登记并由 UserAgent 授权访问共享资源；浏览器前端通过会话注册表发现和切换会话，并通过 UserAgent 的同步通道接收文件变更通知。

### 父子 Agent 结构与职责边界

UserAgent 是基于 Durable Object 模型实现的用户级持久化计算单元，每个用户账号对应一个 UserAgent 实例。UserAgent 的生命周期由系统运行时管理：当用户首次登录或系统触发时创建，在用户活跃期间保持在内存中，空闲超时后序列化状态至持久存储并释放内存，后续请求到达时自动从存储恢复。UserAgent 的核心职责包括：（1）持有并管理 MCP 服务器连接池，维护各连接的认证状态和健康状态；（2）持有 OAuth 授权凭据，在凭据过期前主动刷新，并通过加密存储保证凭据安全；（3）管理 workspace 文件系统，维护文件索引、文件内容缓存和文件变更事件源；（4）维护会话注册表，记录该用户下所有 SessionAgent 的元数据（会话 ID、名称、创建时间、最后活跃时间和状态）；（5）运行定时任务调度器，执行跨会话摘要生成、过期会话清理、凭据预刷新等全局周期性任务；（6）管理前端同步通道，向所有连接的浏览器标签页广播文件变更、会话状态变更等事件。

SessionAgent 是每个聊天会话对应的独立 agent 执行上下文，同样基于 Durable Object 模型实现。SessionAgent 的核心职责包括：（1）管理会话私有的消息历史，包括用户消息、agent 回复、工具调用及其结果的结构化记录；（2）管理消息分支链（branching），允许用户从任意历史节点分叉出新分支并维护分支间的父子关系；（3）持有会话级别的个性化配置，如系统提示词、模型参数、温度值、工具启用列表等；（4）管理会话级扩展，如自定义工具、插件、技能模块等；（5）维护会话级记忆存储，包括短期对话摘要和长期向量化记忆嵌入；（6）执行 agent 推理循环：接收用户输入、调用 LLM、解析工具调用请求、通过 UserAgent 代理执行工具、将工具结果反馈给推理循环、生成最终回复。SessionAgent 不直接持有 MCP 连接、OAuth 凭据或文件变更监听器，所有对共享资源的访问必须通过 UserAgent 的资源代理接口完成。

### 会话隔离机制

会话隔离是本方案的核心安全机制，确保不同 SessionAgent 之间的数据和执行上下文互不干扰。隔离分为三个层面：数据隔离、执行隔离和标识隔离。

数据隔离：每个 SessionAgent 的消息历史、分支结构、个性化配置、扩展模块和记忆向量均以该 SessionAgent 的持久化存储为单位独立保存。在存储层，每个 SessionAgent 对应独立的存储分区（如独立的 Durable Object Storage 键空间前缀），不同 SessionAgent 之间不共享存储区域，不存在跨会话数据泄露路径。当 SessionAgent 从空闲状态恢复时，仅加载其自身分区的持久化数据。

执行隔离：每个 SessionAgent 拥有独立的 agent 推理循环实例和 LLM 对话上下文窗口。Agent 推理循环维护自身的消息队列、工具调用栈和流式输出缓冲区。当一个 SessionAgent 正在执行工具调用链时，其他 SessionAgent 的执行不受影响。UserAgent 作为共享资源的唯一管理点，在处理来自不同 SessionAgent 的资源代理请求时，基于请求中的会话标识进行会话级访问权限校验，确保一个 SessionAgent 无法以另一个 SessionAgent 的身份操作共享资源。

标识隔离：每个 SessionAgent 分配一个全局唯一的、不可猜测的会话标识符（session_id），该标识符通过密码学安全的随机数生成器产生。前端在创建会话、切换会话或访问会话资源时，必须提供有效的会话 ID 和用户认证凭据。UserAgent 的会话注册表在每次会话操作（读取消息、执行工具、修改配置等）时校验会话 ID 是否属于当前认证用户的会话注册表条目，防止通过猜测会话 ID 跨用户访问。

### 共享资源代理机制

共享资源代理层是 SessionAgent 访问用户级共享资源的唯一通道。该层在 UserAgent 内部实现为资源代理接口（Resource Proxy Interface），SessionAgent 通过进程内 RPC 或异步消息调用该接口，不得绕过代理层直接执行 MCP 工具调用或直接读写 workspace 文件。

MCP 工具代理：当 SessionAgent 的推理循环需要调用外部工具时，SessionAgent 构造工具调用请求（包含工具名称、参数和会话标识），通过 UserAgent 的工具代理接口发起调用。UserAgent 收到请求后依次执行：校验会话标识是否在注册表中且属于当前用户；从 MCP 连接池中获取对应服务器的健康连接；将工具调用请求转发至 MCP 服务器；等待工具执行结果；将结果格式化后返回给 SessionAgent。在此过程中，MCP 服务器的底层连接凭据、OAuth 令牌和传输层细节对 SessionAgent 完全透明。如果 MCP 连接需要 OAuth 授权且凭据已过期，UserAgent 在执行工具调用前自动完成凭据刷新，SessionAgent 无需感知授权状态。

Workspace 文件代理：SessionAgent 不直接操作文件系统。当需要读取或写入 workspace 文件时，SessionAgent 通过 UserAgent 的文件代理接口发起操作。UserAgent 维护统一的文件索引和内容缓存，多个 SessionAgent 对同一文件的并发写入通过 UserAgent 的文件锁或乐观锁机制协调。文件修改完成后，UserAgent 生成文件变更事件并通过同步通道广播。对于前端发起的文件操作请求，同样通过 UserAgent 代理，确保文件状态一致性。

### 会话注册表与访问控制

会话注册表（Session Registry）是 UserAgent 内部维护的用户级会话元数据索引。注册表以会话 ID 为键，存储每个会话的元数据条目，包含：会话显示名称、会话创建时间和最后活跃时间、会话状态（active/idle/archived）、SessionAgent 的 Durable Object 引用或定位信息，以及可选的会话标签和描述。注册表支持创建、查询、重命名、归档和删除操作，所有操作均在 UserAgent 接收到来自前端的用户认证请求后执行。

访问控制通过双重校验实现：第一层，前端请求必须携带用户认证令牌，API 网关或 UserAgent 入口层先验证令牌有效性并提取用户标识；第二层，对于涉及特定会话的操作，UserAgent 在会话注册表中检索该会话 ID，验证其 owner 字段与当前用户标识一致。如果会话 ID 不存在于注册表中，或存在但不属于当前用户，操作被拒绝并返回未授权错误。新建会话时，UserAgent 自动将该会话 ID 写入注册表并设置 owner 为当前用户，杜绝通过猜测会话 ID 访问未登记子会话的可能性。

注册表的持久化利用 Durable Object 的自动状态序列化能力：UserAgent 的注册表作为其状态的一部分，在 UserAgent 空闲时自动持久化至 Durable Object Storage，崩溃恢复时从存储恢复，无需额外的数据库同步逻辑。注册表的读写操作在 UserAgent 的单线程执行模型下天然串行化，避免了并发注册冲突。

### 实时同步机制

实时同步通道用于在 workspace 文件发生变更时，向同一用户的所有活跃浏览器标签页或会话面板推送变更通知，避免前端通过轮询方式获取文件状态。同步通道由 UserAgent 统一管理，利用 Durable Object 的长期运行能力和 WebSocket 或 Server-Sent Events（SSE）协议实现。

具体机制为：当浏览器标签页打开某个会话时，前端向 UserAgent 的同步端点发起 WebSocket 连接或 SSE 订阅请求，请求中携带用户认证令牌。UserAgent 验证令牌后将该连接注册到该用户的连接表中。当 workspace 文件通过任意 SessionAgent 或前端操作发生变更时，UserAgent 的文件代理层在完成文件写入后，生成文件变更事件（包含文件路径、变更类型如 create/update/delete、变更时间戳和变更来源会话 ID），遍历该用户的连接表，向所有活跃连接广播该事件。前端收到事件后，根据事件中的文件路径增量更新本地文件视图，无需重新加载整个文件列表。

连接管理：UserAgent 为每个连接分配唯一连接标识，并在连接表中记录连接建立时间。当检测到 WebSocket 连接断开时，自动从连接表中移除。同时设置心跳机制，定期发送 ping 帧，对无响应的连接进行清理。对于 SSE 连接，利用 SSE 的自动重连机制，配合 EventSource 的 lastEventId 实现断线重连后的事件补发，确保前端不丢失文件变更事件。

### 定时任务调度机制

定时任务调度器运行在 UserAgent 内部，负责执行跨会话的全局周期性任务。调度器利用 Durable Object 的 alarm（闹钟）机制实现可靠的定时触发：当 UserAgent 被激活或完成上一轮调度后，读取其持久化状态中的任务配置列表，计算下一个即将到期的任务时间点，调用 Durable Object 的 alarm API 设置在指定时间后唤醒。当 alarm 触发时，Durable Object 运行时自动激活 UserAgent，调度器执行到期任务，执行完毕后重新计算下一个 alarm 时间点。

支持的全局任务类型包括：（1）跨会话摘要生成——定期扫描该用户下所有活跃会话的近期消息活动，生成用户级活动摘要；（2）过期会话归档——检测超过设定阈值未活跃的会话，将其状态标记为 archived 并从活跃注册表索引中移除（但保留数据用于后续恢复）；（3）凭据预刷新——在 OAuth 凭据到期前主动刷新，避免 SessionAgent 在工具调用时阻塞等待刷新；（4）workspace 文件索引重建——定期校验文件索引与实际存储的一致性，修复不一致项。定时任务的执行不阻塞 SessionAgent 的正常推理循环，两者在 UserAgent 的异步执行模型中并发运行。

### 现有基础设施的复用

本方案最大限度地复用现有 agent 体系的基础设施，避免重复建设。

（1）Durable Object 基础设施：UserAgent 和 SessionAgent 均基于现有 Durable Object 运行时实现，复用其自动持久化、空闲回收、崩溃恢复和单线程串行执行等能力。UserAgent 和 SessionAgent 只是在业务语义和状态结构上有区分，底层运行时机制完全一致。（2）Agent 推理循环：SessionAgent 复用现有 chat agent 和 Think agent 的推理循环框架，包括 LLM 调用、工具调用解析、流式输出和消息分支管理。改动仅限于工具调用路径：原路径直接调用 MCP 客户端，新路径改为通过 UserAgent 代理调用。（3）MCP 工具连接：UserAgent 的 MCP 连接池复用现有 MCP 客户端实现，包括连接建立、协议协商、工具发现和调用执行。改动在于连接池从单个 agent 实例提升到 UserAgent 层级，增加连接复用和凭据自动刷新逻辑。（4）Workspace：现有 workspace 的文件读写接口从 SessionAgent 内部迁移至 UserAgent 内部，对外暴露文件代理接口。文件存储后端和索引机制不变。（5）Chat Recovery：现有 chat recovery 机制（从持久化存储恢复会话状态）被 SessionAgent 直接复用，用于会话的崩溃恢复和空闲唤醒。UserAgent 的恢复同理复用该机制。

### 关键处理流程

以下描述用户在多会话 assistant 系统中执行一次工具调用的完整处理流程，说明各模块的协作关系。

步骤一：用户认证与会话选择。用户通过浏览器登录，API 网关验证用户令牌后，前端从 UserAgent 获取该用户的会话注册表列表，在侧边栏展示所有会话。用户选择或创建一个会话，前端建立到该 SessionAgent 的消息通道（通过 UserAgent 路由）以及到 UserAgent 的同步通道（WebSocket/SSE）。

步骤二：消息处理与工具调用发起。用户在选中的会话中输入消息，前端将消息发送至 SessionAgent。SessionAgent 的推理循环将用户消息追加到消息历史，调用 LLM 进行推理。LLM 返回工具调用决策（如调用某个 MCP 工具读取外部数据），SessionAgent 构造工具调用请求并通过 UserAgent 的工具代理接口发起调用。

步骤三：共享资源代理执行。UserAgent 收到工具代理请求后，校验会话 ID 和用户身份，从 MCP 连接池获取对应连接。如需 OAuth 授权，检查凭据有效性，必要时自动刷新。将工具调用转发至 MCP 服务器，等待结果返回后，将结果回传给 SessionAgent。SessionAgent 将工具结果追加到消息历史中的工具调用记录，继续 LLM 推理循环。

步骤四：文件变更同步。如果工具调用或 agent 操作导致了 workspace 文件变更（如创建、更新或删除文件），UserAgent 的文件代理层在完成文件操作后生成变更事件，并通过同步通道向该用户的所有活跃浏览器标签页广播。各前端标签页收到事件后增量更新文件视图。

步骤五：会话状态持久化。每次消息处理完成后，SessionAgent 和 UserAgent 的 Durable Object 运行时自动将状态变更持久化。如果 SessionAgent 在后续空闲超时后被回收，下一次该会话被激活时，运行时从持久化存储恢复其完整状态（消息历史、分支、配置、记忆等），用户无感知。

步骤六：定时任务执行。在用户交互间隙，UserAgent 的调度器按预设计划执行全局任务。例如，每 24 小时扫描一次该用户所有活跃会话，生成用户级活动摘要；在 OAuth 凭据到期前 1 小时主动刷新，确保任何 SessionAgent 在需要工具调用时凭据始终有效。

### 必要技术特征归纳

本方案的必要技术特征可归纳为以下要点：（1）用户级 UserAgent 与会话级 SessionAgent 的两层 Durable Object 架构，UserAgent 管理共享资源，SessionAgent 管理会话私有状态；（2）SessionAgent 通过 UserAgent 的资源代理接口访问 MCP 工具和 workspace 文件，浏览器或 SessionAgent 不得绕过 agent 生命周期直接执行原始 MCP 工具调用；（3）会话注册表作为 UserAgent 状态的一部分，以密码学安全的随机会话 ID 为键，在每次会话操作时校验会话-用户归属关系；（4）UserAgent 内部的文件变更事件广播机制，基于 WebSocket/SSE 向所有活跃浏览器标签页推送变更通知；（5）UserAgent 内部的 alarm 驱动定时任务调度器，统一执行跨会话全局任务；（6）MCP 连接池和 OAuth 凭据在 UserAgent 层级统一管理，多个 SessionAgent 共享同一连接和授权结果。

### 风险与待确认问题

以下列出当前方案中需要后续确认或进一步设计的技术风险点。（1）并发工具调用的排队与超时：当多个 SessionAgent 同时通过 UserAgent 代理调用同一 MCP 服务器时，UserAgent 需要对请求进行排队。如果某个工具调用耗时过长，可能阻塞其他会话的工具请求。需设计合理的超时策略和（可选的）优先级队列。（2）UserAgent 单点瓶颈：所有共享资源的访问都经过 UserAgent，在高并发场景下 UserAgent 可能成为瓶颈。Durable Object 的单线程模型存在吞吐上限，需评估是否需要引入读写分离缓存或连接池预热。（3）alarm 精度与延迟：Durable Object 的 alarm 机制存在一定的触发延迟（通常为秒级），对于秒级精度要求不高的定时任务可接受，但对于需要精确时间窗口的任务（如凭据到期前刷新）需设计提前量。（4）workspace 文件一致性：多个 SessionAgent 通过 UserAgent 并发修改同一文件时，乐观锁冲突可能导致某些修改被拒绝。需设计合理的冲突解决策略（如最后写入胜出或合并策略）和用户提示机制。（5）跨区域部署：如果用户和 workspace 文件存储位于不同地理区域，文件代理操作可能引入额外延迟。可以考虑在 UserAgent 内部增加文件内容预读缓存和增量同步策略。（6）前端同步通道连接数：如果用户同时打开大量浏览器标签页，UserAgent 的连接表可能膨胀，需评估 WebSocket 连接数的平台上限及是否需要引入消息队列中间件进行广播卸载。
