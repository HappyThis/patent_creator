## 技术方案

本方案提出一种基于 Durable Object agent 框架的子 agent 协作系统，通过将子 agent 建模为父 agent 的 colocated DO facet、外部可寻址 HTTP/WebSocket 路由、以及后台 workflow 调度三种路径，统一覆盖模型驱动工具调用、服务端确定性流程调用和后台自主子运行三类场景。系统在父 agent 与子 agent 之间建立可追溯的父子身份链（parentPath/selfPath），通过 StreamCallback 桥接机制将子 agent 的流式推理事件实时推送至父 agent 的 WebSocket 会话；同时利用 ResumableStream 的 chunk 持久化与索引重放、AbortRegistry 的级联取消、以及幂等键 + TurnQueue 的并发控制，解决子 agent 场景下的流中断恢复、事件去重、取消传播和重复请求处理问题。访问控制采用 onBeforeSubAgent 三层鉴权体系（跨域 → 父级 → 子级），drill-in 访问通过递归嵌套的 URL 模式实现。

### 整体架构

系统由七个核心组件构成，协同实现子 agent 的全生命周期管理与流式协作。

- 主 agent DO：基于 Agent 基类扩展，拥有独立 SQLite 和 WebSocket chat 会话管理，是用户交互入口，负责协调子 agent 调用并聚合流式结果回传客户端。
- 子 agent facet：通过 ctx.facets.get(SubAgentClass, name) 创建的 colocated DO 实例，与父 agent 共享 worker 但拥有独立 SQLite。对外暴露类型化 RPC 代理 SubAgentStub，仅保留业务方法（chat、run 等），排除 Agent/Server 内部方法。
- 子 agent 注册表：基于父 agent SQLite 中的 cf_agents_sub_agents 表，记录子 agent 的 class 名、实例名、创建时间和状态。提供 hasSubAgent、listSubAgents、getSubAgentByName 内省接口，支持跨 DO 查找与路由。
- 流式桥接层：子 agent 通过 StreamCallback 接口将流式事件（onEvent、onDone、onError）实时推送到父 agent，父 agent 桥接至自身的 ResumableStream，写入 cf_ai_chat_stream_chunks 表并通过 WebSocket 推送客户端。桥接层维护事件序列号 chunk_index，确保中断恢复时按索引重放去重。
- 恢复层：基于 ResumableStream 的 SQLite 持久化。每个子 agent 的流式事件作为独立 chunk 序列存储在父 agent 流表中，携带子 agent 来源标识。主 agent 恢复时重放所有活跃子 agent 事件，已持久化但未确认送达的事件通过 STREAM_RESUMING/STREAM_RESUME_ACK 协议完成重放去重。
- 外部路由层：URL 模式 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name} 支持递归嵌套，允许客户端 drill-in 访问任意层级子 agent。routeSubAgentRequest 处理 HTTP 路由，parseSubAgentPath 解析路径提取父子信息。
- Workflow 调度器：AgentWorkflow 基类支持与 Agent 双向通信，可触发后台子 agent 运行，不依赖父 agent 的 tool call 驱动，适合长时间批处理或定时任务场景。

### 子 agent 调用机制

系统提供三种子 agent 调用模式，覆盖从模型自主决策到服务端确定性调度、再到后台自主运行的完整场景。

模式一：模型驱动工具调用。主 agent 将子 agent 注册为可调用工具（tool），模型在推理过程中自主决定何时调用哪个子 agent。调用流程：模型输出 tool_call 指令 → 主 agent 的 beforeToolCall 钩子拦截 → 通过 SubAgentStub 发起 RPC 调用子 agent 的 chat 或 run 方法 → 子 agent 通过 StreamCallback 实时回传流式事件 → 主 agent 将事件包装为 TOOL_RESULT 消息回传给模型继续推理。子 agent 的流式输出在工具调用期间即可逐步展示给用户，而非等待工具调用完全结束。主 agent 的 AbortRegistry 监听用户取消请求，通过 AbortSignal 传递到子 agent，实现级联中断。

模式二：服务端确定性流程调用。在 beforeTurn、beforeStep 等 lifecycle hook 中，服务端根据预设规则确定性调用子 agent，不依赖模型输出的 tool_call。典型场景包括：每条用户消息自动路由到审核子 agent 进行内容安全检查；在每轮推理前调用上下文检索子 agent 注入相关文档。该模式的核心机制：hook 中通过 getSubAgentByName 查找已注册子 agent → 构造子 agent 调用参数 → 通过 SubAgentStub 发起调用 → 子 agent 的流式事件通过桥接层合并到主 agent 的流中 → 子 agent 的返回结果可直接注入当前 turn 的上下文或作为系统消息追加。确定性调用与模型驱动调用共享同一套流式桥接和恢复基础设施。

模式三：后台子运行。子 agent 不绑定特定的 tool call 或 hook，而是由 AgentWorkflow 调度器或定时任务触发，在后台自主执行。后台子 agent 拥有独立的执行生命周期：调度器通过 ctx.facets.get 创建或获取子 agent → 调用子 agent 的 run 方法启动长时间任务 → 子 agent 将执行进度写入自身的 SQLite → 通过 StreamCallback 将阶段性事件推送到主 agent 的事件总线 → 主 agent 可在任意时刻通过 listSubAgents 查询子 agent 状态、通过 getSubAgentByName 获取其最新输出、或通过 abortSubAgent 终止执行。后台子 agent 完成后，通过 onDone 回调通知主 agent，主 agent 可选择向用户推送通知或将结果持久化为上下文。后台子运行的关键特性：不阻塞主 agent 的用户交互；支持主 agent 在后续 turn 中引用子 agent 的中间或最终结果；子 agent 的流式事件独立持久化，主 agent 重启后可恢复并继续接收事件。

### 流式事件桥接与实时展示

子 agent 的推理过程对用户透明是本方案的核心创新。系统通过两层桥接实现子 agent 流式事件的实时传递与可靠交付。

第一层：子 agent 到父 agent 的事件桥接。子 agent（Think 或其子类）在执行 chat 或 run 方法时，通过 StreamCallback 接口产生流式事件。StreamCallback 定义三个回调：onEvent(json) 传递增量推理内容（token 级别或段落级别），onDone() 标记推理完成，onError(error) 传递异常。这些回调在子 agent 的 DO 实例内触发，通过 DO 间的直接 RPC 调用（SubAgentStub）传递到父 agent。父 agent 在调用子 agent 时注入一个桥接回调，该回调将子 agent 的每个事件包装为统一的事件结构，包含：事件来源标识（sub_agent_id、sub_agent_class、sub_agent_name）、事件序列号（由子 agent 侧自增分配）、事件类型（token/status/error/done）和事件载荷。

第二层：父 agent 到客户端的 WebSocket 推送。父 agent 的 ResumableStream 将桥接后的子 agent 事件与自身推理事件合并写入 cf_ai_chat_stream_chunks 表，每条 chunk 记录 chunk_index、事件内容 JSON、来源标记和写入时间戳。ResumableStream 按 chunk_index 顺序通过 WebSocket 推送给客户端。客户端收到事件后，根据来源标记区分父 agent 的直接输出和子 agent 的委托输出，可在 UI 中以不同样式（如嵌套卡片、缩进、标签）展示。客户端发送 ACK 确认已消费的 chunk_index，ResumableStream 据此推进确认水位线。

关键机制：事件序列号与来源隔离。每个子 agent 维护独立的事件序列号空间（从 0 开始自增），父 agent 的 ResumableStream 为各子 agent 分配独立的 chunk_index 段（如子 agent A 占用 [1000-1999]，子 agent B 占用 [2000-2999]），通过 chunk_index 即可定位事件来源。当主 agent 断线重连时，客户端发送最后确认的 chunk_index，ResumableStream 从该位置重放所有未确认事件（含父 agent 和所有子 agent 的事件），客户端根据来源标记执行去重：对于每个子 agent，只应用序列号大于上次已应用最大序列号的事件，实现精确的去重恢复。

### 父子关联与身份体系

系统为每个 agent 实例维护一条可追溯的父子身份链，支持递归嵌套、外部寻址和 drill-in 访问控制。

身份链数据结构：每个 agent 实例在创建时记录两条路径——selfPath（自身完整路径，如 /agents/RootAgent/main/sub/Analyst/research）和 parentPath（父 agent 的完整路径，如 /agents/RootAgent/main）。路径由 parseSubAgentPath 从 URL 解析，或由 ctx.facets 创建时自动推导。parentPath 为顶层 agent 时为空。递归嵌套的子 agent 携带完整祖先链，通过 selfPath 可追溯到根 agent。

注册表持久化：父 agent 在通过 ctx.facets.get 创建子 agent facet 时，同步在 cf_agents_sub_agents 表中写入记录，字段包括：child_class（子 agent 类名）、child_name（子 agent 实例名）、created_at、status（idle/running/error/done）、parent_path、self_path。该表支持 hasSubAgent(class, name) 布尔查询、listSubAgents(filter) 列表查询和 getSubAgentByName(name) 精确查找。注册表是跨 DO 路由的基础：当外部请求通过 routeSubAgentRequest 到达父 agent 时，父 agent 先查注册表确认子 agent 存在，再通过 DO 内部引用转发请求。

外部 drill-in 访问路径：客户端可通过递归嵌套 URL 直接访问任意层级的子 agent。例如，/agents/RootAgent/main/sub/Analyst/research/sub/Writer/draft 表示访问 RootAgent/main 下的 Analyst/research 下的 Writer/draft 子 agent。routeSubAgentRequest 递归解析路径，每一层父 agent 都通过 onBeforeSubAgent 钩子执行鉴权，鉴权通过后才将请求转发到下一层。该机制允许管理面工具（如调试面板）直接查询子 agent 的状态、日志和中间输出，而无需通过父 agent 的 chat 会话间接获取。

子 agent 身份在流式事件中的体现：每个桥接事件携带 selfPath 和 parentPath，客户端可根据身份链构建树状展示结构。当用户点击某个子 agent 的输出时，客户端通过 selfPath 发起 drill-in WebSocket 连接，直接订阅该子 agent 的实时事件流。

### 重复请求处理与并发控制

子 agent 协作场景引入额外的并发和重复请求风险：同一用户可能快速连续发送多条消息触发多次子 agent 调用，或网络重传导致相同请求到达多次。系统通过幂等键、TurnQueue 和条件领取三层机制应对。

幂等键（Idempotency Key）：每个用户请求入口生成全局唯一的幂等键（基于 session_id + turn_index + request_hash），贯穿主 agent 和所有子 agent 调用。子 agent 在接收调用时检查幂等键：若已存在相同幂等键的已完成调用，直接返回缓存的结果对象（包含流式事件的完整序列）；若已存在相同幂等键的进行中调用，将新请求加入等待队列，复用进行中调用的结果流。幂等键的保留窗口为会话生命周期或可配置的 TTL。

TurnQueue 并发控制：主 agent 的 TurnQueue 管理用户提交的 turn 队列。当子 agent 调用正在进行时，新的用户消息进入 TurnQueue 排队。TurnQueue 的策略：同一 session 的 turn 严格串行执行，避免子 agent 状态竞争；不同 session 的 turn 可并行。子 agent 调用期间，父 agent 将当前 turn 标记为 waiting_on_sub_agent 状态，TurnQueue 对此状态执行特殊处理：不阻塞其他 session，但阻止同一 session 的新 turn 开始，直到子 agent 返回 onDone 或超时。

条件领取（Conditional Claim）：子 agent 的后台运行模式中，多个触发源（如不同的 workflow 实例或定时器）可能同时尝试启动同一子 agent。系统通过 SQLite 事务 + 状态 CAS 实现条件领取：UPDATE cf_agents_sub_agents SET status='running', claimed_by=$worker_id WHERE name=$name AND status='idle'。只有成功更新一行（affected_rows=1）的触发源获得执行权；其他触发源收到 claimed 响应后，可选择等待结果或放弃。该机制防止后台子 agent 被重复启动，同时避免分布式锁的复杂性。

### 恢复、取消与清理

子 agent 的长时间运行特性使恢复、取消和清理机制成为系统可靠性的关键。系统基于 ResumableStream、AbortRegistry 和生命周期管理三套机制协同工作。

恢复与重放去重。当主 agent 的 DO 实例因网络中断或重启而恢复时，ResumableStream 从 cf_ai_chat_stream_metadata 表中读取最后确认的 chunk_index 水位线，从 cf_ai_chat_stream_chunks 表中按 chunk_index 升序重放所有未确认事件。对于子 agent 事件的重放去重，系统采用双层判定：（1）来源层——客户端按子 agent 的 selfPath 分组维护 last_applied_seq，对每个子 agent 独立去重；（2）内容层——每个子 agent 事件的 chunk 记录包含 content_hash（事件内容的 SHA-256），客户端在应用事件前比对 content_hash 与已应用事件，相同则跳过。客户端通过 STREAM_RESUMING 消息发起恢复，携带每个子 agent 的 last_applied_seq；服务端从各子 agent 的最小未确认 seq 开始重放；客户端发送 STREAM_RESUME_ACK 确认恢复完成。恢复期间，仍在运行的子 agent 继续产生新事件，这些新事件的 seq 大于客户端声明的 last_applied_seq，自然被包含在重放窗口中。

级联取消。AbortRegistry 维护一个 abort 树：每个子 agent 调用注册一个 AbortController，形成父子链接。用户发起取消（通过 CHAT_REQUEST_CANCEL 消息）→ 主 agent 的 AbortRegistry 触发根 AbortController → 信号沿树传播到所有活跃子 agent → 子 agent 的 AbortSignal 触发推理中断 → 子 agent 通过 onError 回调通知父 agent → 父 agent 将取消状态写入流表并推送客户端。级联取消的关键保证：子 agent 的 SQLite 写入操作在取消时执行回滚或标记为 cancelled 终态，不留下中间脏状态；子 agent 的流表 chunk 序列以 cancelled 事件作为终止标记。

清理保留策略。deleteSubAgent 删除子 agent facet 时执行分级清理：（1）立即清理——删除子 agent 的 DO 实例和 SQLite 存储；（2）保留项——父 agent 流表中已持久化的该子 agent 事件 chunk 不删除，保留历史对话的完整性；cf_agents_sub_agents 注册表记录标记为 deleted 而非物理删除，保留审计追溯。abortSubAgent 仅终止运行中的子 agent 而不删除实例和存储，子 agent 可被后续调用重新激活。超时清理机制：子 agent 可配置 max_idle_time，超过空闲时间未收到新调用时自动执行 abortSubAgent，释放计算资源。

### 访问控制

子 agent 的嵌套层级和外部可寻址特性要求精细的访问控制。系统采用 onBeforeSubAgent 三层鉴权体系，从外向内逐层过滤。

第一层——跨域鉴权（Cross-Cutting）：onBeforeConnect 钩子在 WebSocket 连接建立时执行，验证客户端身份（JWT token、API key 等），将认证后的 principal 对象绑定到连接上下文。该层对所有 agent（父和子）统一生效，拦截未认证请求。

第二层——父级鉴权（Parent-Specific）：onBeforeSubAgent 钩子在父 agent 收到子 agent 访问请求时执行。钩子接收三个参数：parentContext（父 agent 的上下文，含当前用户 principal、session 信息）、childClass（目标子 agent 的类名）、childName（目标子 agent 的实例名）。钩子返回 boolean 或抛出异常来放行或拒绝。典型策略：检查当前用户是否有权访问该子 agent 类型（基于 RBAC role），检查子 agent 实例是否属于当前 session（防止跨 session 越权），检查调用频率是否超过配额。父级鉴权应用于三种调用模式：模型驱动的 tool call、服务端 hook 调用、以及外部 drill-in 请求。

第三层——子级鉴权（Child-Specific）：子 agent 自身通过 Agent 基类的 beforeTurn hook 执行独立鉴权。即使父级放行，子 agent 也可根据自身的业务逻辑拒绝执行。例如，敏感操作子 agent 检查请求中的 approval_token 是否有效。子级鉴权与父级鉴权形成纵深防御：父级控制谁能访问子 agent，子级控制能执行哪些操作。

drill-in 访问的鉴权链：当客户端通过递归 URL 直接访问深层子 agent 时，请求依次经过每一层父 agent 的 onBeforeSubAgent 钩子，形成鉴权链。任一层的钩子拒绝则整条链中断，返回 403。鉴权结果（通过/拒绝/原因）记录在各层父 agent 的审计日志中，支持事后追溯。

### 与现有 chat/Think 体系的兼容关系

本方案在现有 chat 协议和 Think agent 体系之上增量构建，不重写框架，所有扩展通过继承和钩子注入实现。

与 Chat 协议的兼容：子 agent 的流式事件推送复用现有 CHAT_MESSAGES 消息类型，通过消息体中的 source 字段（值为 {selfPath}）区分父 agent 和子 agent 的输出。客户端无需新增消息类型即可展示子 agent 内容。TOOL_RESULT 消息在子 agent 作为工具调用时自然承载子 agent 的完整输出。STREAM_RESUMING/STREAM_RESUME_ACK 协议扩展了 resume 握手：在现有握手消息中增加 sub_agent_states 字段，携带每个活跃子 agent 的 last_applied_seq，实现子 agent 事件的精确恢复。CHAT_REQUEST_CANCEL 消息的语义扩展为级联取消：主 agent 收到后触发 AbortRegistry 树形传播。

与 Think agent 的兼容：Think 基类的 StreamCallback 机制被直接复用为子 agent 到父 agent 的事件桥接通道。Think 的 beforeTurn/beforeStep/beforeToolCall 钩子继续在子 agent 内部生效，子 agent 可独立执行工具调用（形成嵌套子 agent 调用）。Think 的 session-backed 存储与子 agent 的独立 SQLite 并行不悖：父 agent 的 session 管理用户对话上下文，子 agent 的 session 管理子任务上下文。AbortSignal 机制从 Think 的单 agent 取消扩展为跨 agent 级联取消。

与 AgentWorkflow 的兼容：AgentWorkflow 基类的双向通信能力为后台子运行提供调度基础。Workflow 通过 getSubAgentByName 获取子 agent 引用后，直接调用子 agent 的业务方法；子 agent 的执行进度通过 StreamCallback 回传给 Workflow，Workflow 再转发给主 agent。该路径不经过 chat 会话的 tool_call 流程，与模型驱动调用完全解耦。

不重写边界：以下现有模块不做任何修改——ResumableStream 的核心缓冲/持久化/重放逻辑、TurnQueue 的排队策略、SQLite 的 cf_ai_chat_stream_chunks 表结构、WebSocketChatTransport 的连接管理、CHAT_MESSAGE_TYPES 常量定义。所有子 agent 相关的扩展均在上述模块的外部或钩子层实现，通过注入回调、扩展消息字段、增加注册表查询等方式与现有体系对接。

### 技术效果

本方案的技术效果体现在以下六个方面。

1. 统一的多模式子 agent 协作：同一套基础设施（SubAgentStub、StreamCallback、注册表、恢复层）同时支持模型驱动工具调用、服务端确定性 hook 调用和后台 workflow 调度三种模式，避免为不同场景开发独立通路，降低系统复杂度。
2. 实时透明的子 agent 推理展示：通过 StreamCallback 桥接和 chunk_index 序列化，子 agent 的推理过程以 token 级别实时推送到客户端，用户无需等待子 agent 完全执行完毕即可看到中间结果。来源标记机制使多子 agent 并行输出时客户端可区分并结构化展示。
3. 可靠的流中断恢复与去重：基于 ResumableStream 的 SQLite 持久化和子 agent 独立序列号空间，断线重连后精确恢复每个子 agent 的未确认事件。content_hash + last_applied_seq 双层去重保证不重复应用事件，即使子 agent 在恢复期间仍在产生新事件也不会丢失或重复。
4. 级联取消的一致性保证：AbortRegistry 树形结构确保用户的一次取消操作自动传播到所有活跃子 agent，子 agent 的 SQLite 写入通过事务回滚或终态标记保持一致性，不产生孤儿进程或脏状态。
5. 细粒度的嵌套访问控制：onBeforeSubAgent 三层鉴权 + drill-in 鉴权链实现逐层验证，既支持父 agent 统一管控子 agent 的访问权限，又允许子 agent 保留独立的操作级鉴权，审计日志记录完整鉴权链路。
6. 与现有体系的无侵入扩展：所有子 agent 能力均通过继承、钩子注入和消息字段扩展实现，不修改 ResumableStream、TurnQueue、WebSocketChatTransport 等核心模块，现有单 agent 场景的功能和性能不受影响。

### 风险与待确认问题

以下为当前方案中需进一步确认的技术风险和待定设计决策。

- 子 agent 嵌套深度限制：当前 URL 模式支持无限递归嵌套，但深层嵌套会导致鉴权链过长、事件桥接延迟累积。建议设定最大嵌套深度（如 3 层），需确认业务场景是否满足。
- 子 agent 事件序列号溢出：子 agent 使用自增整数序列号，长时间运行的后台子 agent 可能溢出。需确认是否采用 64 位整数（Snowflake 风格）或循环序列号 + 代际标记。
- 跨 session 子 agent 共享：当前方案中子 agent 与父 agent 的 session 绑定，尚未明确同一子 agent 实例是否可被多个父 agent（或同一父 agent 的多个 session）共享。共享场景需要额外的并发控制和结果隔离机制。
- 子 agent 的流表空间膨胀：子 agent 的 chunk 持久化在父 agent 的 cf_ai_chat_stream_chunks 表中，多子 agent + 长时间运行场景下表空间增长显著。需确认清理策略：按 TTL 自动清理、按 session 结束清理、或迁移到独立表。
- 条件领取的幂等键保留窗口：后台子运行的幂等键保留窗口若设为会话生命周期，则跨会话的重复触发无法去重。需确认是否需要全局幂等键注册表或基于时间的保留窗口。
- 子 agent 超时与父 agent 超时的协调：子 agent 的 max_idle_time 与父 agent 的 turn timeout 可能不一致。当父 agent 超时但子 agent 仍在运行时，需明确行为：父 agent 是否强制 abort 子 agent，还是允许子 agent 继续后台运行并异步通知。
