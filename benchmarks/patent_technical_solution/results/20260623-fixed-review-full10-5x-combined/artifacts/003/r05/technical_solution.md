## 技术方案

本方案采用“用户级控制面 + 会话级执行面”的分层结构：同一用户账号对应一个用户级父 Agent，用于统一维护会话目录、共享工作区、外部工具连接、授权凭据、跨会话索引和调度入口；该用户下的每个聊天会话对应一个独立的会话级子 Agent，用于保存该会话自己的消息历史、上下文块、推理过程状态、工具调用状态和 WebSocket 客户端连接。由此，易被互相污染的会话状态被限定在子 Agent 内，需在多个会话之间延续的用户级资源则由父 Agent 持有并代理访问。

### 总体架构：用户级控制面与会话级执行面

用户级父 Agent 的名称或实例标识由认证后的用户身份派生，外部连接入口只允许在完成用户认证后路由到对应的父 Agent，客户端不能自行指定其他用户的父 Agent 标识。父 Agent 内维护会话注册表和会话元数据表；注册表用于判定某个会话子 Agent 是否真实存在，元数据表用于记录标题、最近更新时间、最近消息摘要、删除标记或检索索引等展示和管理信息。创建会话时，父 Agent 先为该会话生成或接收唯一会话标识，再登记对应的子 Agent，并写入元数据；列出会话时，以注册表为存在性的权威来源，元数据仅作为附加信息。

会话子 Agent 作为父 Agent 的下级执行单元被寻址，外部请求到达时先唤醒父 Agent，由父 Agent 根据认证结果、会话注册表、会话状态和访问策略执行路由门控。门控通过后，普通流式 token、会话内广播和消息恢复由对应子 Agent 承载；父 Agent 只参与连接建立、票据续期、撤权通知和共享资源调用。该处理链路在降低父 Agent 对实时流量代理开销的同时，通过短期票据、注册表版本和 delete_epoch 保持直连连接的可撤销性。

在客户端侧，侧边栏或会话列表连接到用户级父 Agent，用于创建、重命名、删除、搜索和展示会话；当前聊天窗口携带父 Agent 签发的连接票据连接到被选中的会话级子 Agent，用于收发该会话消息。切换会话时，客户端断开或替换当前子 Agent 连接，清空本地临时消息缓存，并以目标会话的 last_seen_event_offset 或 last_seen_message_seq 作为游标恢复持久消息流；客户端按服务端事件 offset 去重展示，避免断线重连后重复显示流式片段。

### 数据对象与状态定义

| 对象 | 层级 | 关键字段 | 作用 |
| --- | --- | --- | --- |
| 会话注册表 | 用户级权威状态 | user_id、session_id、generation、child_agent_ref、state、version、delete_epoch、created_at、updated_at、last_connection_id | 判定会话是否存在、归属哪个用户以及能否连接；session_id 与 generation 组合唯一，删除后的 session_id 不复用或必须更换 generation。 |
| 会话元数据表 | 用户级派生信息 | session_id、title、last_preview、last_message_seq、updated_at、search_doc_id、tombstone | 用于列表展示和搜索；缺失或损坏时按注册表和消息日志重建，不作为会话存在性的依据。 |
| 用户级资源表 | 用户级权威资源 | user_id、resource_type、resource_id、namespace、version、permission_scope、credential_ref、storage_ref | 绑定共享工作区、工具连接、授权凭据、共享记忆和调度配置；子 Agent 只持有 resource_id，不持有真实句柄。 |
| 会话事件日志 | 会话级权威过程 | session_id、event_offset、message_seq、run_id、request_id、event_type、status、payload_ref、created_at | 记录消息、流式片段、工具调用和取消事件；客户端重连和崩溃恢复均以该日志游标为准。 |

会话状态包括 CREATING、ACTIVE、READ_ONLY、DELETING、DELETED 和 RECOVERING。创建会话时由不存在进入 CREATING，父 Agent 完成子 Agent 登记和元数据初始化后迁移到 ACTIVE；注册表存在但子 Agent 正在重建时进入 RECOVERING，仅允许读取已提交事件和恢复连接；用户发起删除后进入 DELETING，拒绝新写入并撤销连接票据，清理完成后进入 DELETED；需要保留历史但禁止继续写入时进入 READ_ONLY。禁止从 DELETED 直接回到 ACTIVE，恢复只能使用新的 session_id 或新的 generation。

消息状态包括 RECEIVED、PERSISTED、RUNNING、PARTIAL、COMPLETED、CANCELLED、FAILED 和 TIMEOUT；工具任务状态包括 QUEUED、RUNNING、SUCCEEDED、FAILED、TIMEOUT 和 CANCELLED。用户级资源、会话级状态和派生索引分层保存：共享工作区、凭据、工具连接、共享记忆和调度任务属于用户级资源；消息历史、上下文块、运行中任务和连接集合属于会话级状态；会话列表摘要、消息索引和跨会话检索索引属于可重建的派生索引。

### 请求路由与连接票据

父 Agent 的路由门控输入包括认证主体 $user_id$、目标 $session_id$、请求类型、客户端连接标识 $connection_id$、权限范围 $permission_scope$、客户端已知的事件游标和可选的幂等 $request_id$。父 Agent 先校验认证主体与会话注册表中的 $user_id$ 一致，再校验会话状态为 ACTIVE 或允许只读连接的 READ_ONLY/RECOVERING，随后比较 delete_epoch、generation 和注册表版本，确认目标会话未被删除、未被撤权且请求范围未超出授权。

门控通过时，父 Agent 输出允许结果、子 Agent 内部地址或句柄、短期连接票据、票据有效期、generation、delete_epoch、允许的操作范围和起始事件游标；门控失败时输出拒绝结果和拒绝原因，例如 NOT_FOUND、FORBIDDEN、DELETING、EXPIRED、READ_ONLY 或 RECOVERING。连接票据由父 Agent 根据 $user_id$、$session_id$、$generation$、$connection_id$、权限范围、签发时间和过期时间签发，并写入可撤销票据表或可由注册表版本推导的撤销窗口。

子 Agent 在建立 WebSocket 或 RPC 流时执行二次校验：校验票据签名、有效期、$user_id$、$session_id$、$generation$、权限范围和父 Agent 最新会话状态。长连接运行期间，子 Agent 在连接续期、敏感工具调用、共享资源访问和写入最终消息前重新校验票据或向父 Agent 查询注册表版本；父 Agent 删除会话或撤销权限后，通过提升 delete_epoch、更新 generation 或记录撤销项，使旧票据和旧连接不能继续写入。

### 会话隔离机制

每个会话级子 Agent 具有独立的持久化存储和运行时状态，用于保存该会话的消息树、上下文块、压缩摘要、工具调用记录、未完成的流式响应、客户端连接集合以及会话内配置。一个会话的广播、状态更新和消息恢复只作用于该子 Agent 自身的客户端，不自动传播给父 Agent 或其他兄弟会话。即使同一用户在两个窗口中同时运行两个聊天，会话推理和工具执行也分别落在不同的子 Agent 中，避免因单个用户级实例内部维护会话映射而导致上下文串读或长任务串行阻塞。

会话隔离还体现在提示词和上下文装配阶段。子 Agent 处理用户新消息时，先读取本会话的已提交消息、会话内上下文块和当前 run 状态，再以 $user_id$、$session_id$、$run_id$、共享上下文类型、最大 token 预算和去重键向父 Agent 请求共享记忆或共享工作区摘要。父 Agent 返回包含 source_type、resource_id、version、summary、evidence_message_id 和 expires_at 的上下文片段；子 Agent 按“系统策略、用户级共享记忆、工作区摘要、会话历史、当前用户消息”的顺序装配，并按 resource_id 与 evidence_message_id 去重和截断。共享片段只作为本轮输入和引用来源，不写入其他会话的消息历史。

父 Agent 暂时不可达时，子 Agent 的降级范围被限制为会话内只读能力：允许读取本会话已提交历史、继续展示已缓存流式片段、对不需要共享资源的本地上下文进行推理，并将共享上下文缺失记录为本轮输入条件；共享工作区写入、OAuth 工具调用、外部工具连接、跨会话记忆更新、跨会话索引写入和权限提升请求必须返回 AUTH_UNAVAILABLE 或进入待重试队列，不得绕过父 Agent 使用缓存凭据或本地推断权限。

### 消息处理、重连与降级

子 Agent 接收新消息后，首先依据客户端提供的 $request_id$ 和服务端生成的 message_seq 进行幂等检查；若相同 $request_id$ 已提交，则返回原有 message_seq 和事件游标，不重复触发模型或工具。新消息通过校验后先写入会话事件日志，状态为 RECEIVED，再持久化用户消息并迁移为 PERSISTED；随后创建独立 $run_id$，装配会话历史和共享上下文，将 run 状态置为 RUNNING，并开始模型调用或工具调用。

模型流式片段和工具结果均以递增 event_offset 写入会话事件日志后再广播给本会话客户端，广播失败不回滚已经持久化的事件。模型完成时，子 Agent 写入最终助手消息、工具调用摘要和 COMPLETED 状态；用户取消、会话删除、父 Agent 撤权、工具超时或模型失败时，run 状态分别迁移为 CANCELLED、CANCELLED、FAILED、TIMEOUT 或 PARTIAL，并广播终止事件。已经写入的部分输出以 PARTIAL 或 CANCELLED 标记保留，不得在无新 run_id 的情况下继续追加为 ACTIVE 消息流。

同一会话内多个客户端同时发送消息时，本方案采用会话级单写队列作为确定规则：子 Agent 按 message_seq 依次处理用户轮次，同一时刻仅允许一个写入型 run 处于 RUNNING；后续请求进入 QUEUED，若客户端声明不等待则返回 BUSY。只读恢复、历史查询和已提交事件广播可以并行执行；需要并行工具探索的场景在同一 run_id 内创建多个 tool_call_id 隔离工具状态，但不允许多个写入型 run 同时修改同一会话消息流。

### 用户级共享资源连续性

用户级父 Agent 统一持有需要跨会话延续的资源，包括用户共享工作区、外部工具或 MCP 服务器连接配置、OAuth 等授权凭据、用户级长期记忆、跨会话检索索引和定时调度配置。会话子 Agent 不复制底层句柄，访问请求统一包含 $user_id$、$session_id$、operation、resource_id、request_id、permission_scope、expected_version 和参数；父 Agent 根据用户级资源表把 resource_id 解析为真实工作区命名空间、凭据引用、工具连接或索引分片，完成授权、审计和执行后返回 status、result_ref、diagnostic、resource_version 和 retry_after。

共享工作区被设计为用户级持久文件系统，而非会话级临时目录。父 Agent 在处理工作区请求时先将路径规范化为用户命名空间内的绝对资源标识，拒绝包含 `..` 越界、符号链接跳出命名空间、非法 namespace 或直接指定对象存储键的请求；读取时返回内容引用、mime 类型和当前版本，写入时要求携带 if-match 版本或声明追加模式。替换类写入在版本不一致时返回 CONFLICT、当前版本和差异摘要，不静默覆盖；追加类写入由服务端分配递增序号并按 request_id 去重。

跨会话调度能力设置在用户级父 Agent 中。父 Agent 维护定时任务、后台索引、跨会话搜索和共享记忆整理任务，并在任务触发时先读取会话注册表；目标会话为 ACTIVE 时才分派到该子 Agent，目标会话为 READ_ONLY 时仅执行只读索引，目标会话为 DELETING、DELETED 或不可访问时跳过会话级任务并记录失败原因，必要时转为仅更新用户级索引或共享记忆。会话子 Agent 不单独持有用户级调度配置，后台任务不得重新激活已删除会话。

### 共享资源的统一访问控制

共享资源的访问路径被收敛到父 Agent 或由父 Agent 授权的窄接口。外部 HTTP 或 WebSocket 请求必须先经过认证入口到达用户级父 Agent，再由父 Agent 根据会话注册表执行严格门控；未登记、已删除或不属于当前用户的会话标识在唤醒子 Agent 前即被拒绝。内部按名称取得子 Agent 的能力仅用于父 Agent 已完成认证和授权后的工作器内调用，不作为外部请求绕过父级门控的入口。

对于浏览器工具、动态代码执行环境或其他可能访问网络和文件的外部能力，本方案执行资源代理协议：工具请求包含 tool_id、operation、scoped_resource_id、session_id、request_id、timeout、permission_scope 和参数摘要；父 Agent 或宿主侧执行器校验权限范围、超时上限和资源归属后执行真实操作，返回 status、result_ref、diagnostic、resource_version 和 audit_id。真实 OAuth token、浏览器调试连接、对象存储键、工作区实例和底层文件句柄不得序列化到沙箱，沙箱只获得发送受限命令、读写指定资源或调用已批准工具的窄 RPC。

共享记忆、会话摘要和跨会话索引采用追加、合并和显式覆盖相结合的规则。事实记忆以 fact_key、source_session_id、evidence_message_id、confidence、version 和 updated_at 去重合并；相同事实键的新写入若证据不同则保留来源列表并提升版本，若互相冲突则标记为候选冲突而非覆盖旧事实。会话摘要和索引项以 request_id、message_seq 范围和资源版本去重；完整替换必须携带 if-match 版本，不一致时返回 CONFLICT，子 Agent 需重读当前版本后重新生成写入请求。

### 会话生命周期与删除边界

会话删除被限定为会话级生命周期操作，并按固定事务顺序执行：父 Agent 校验请求用户和会话归属后，在注册表写入 DELETING 状态、递增 delete_epoch 并撤销未过期连接票据；随后拒绝新的连接、消息写入和共享资源请求，对运行中的模型流或工具调用发送取消信号，等待其在可配置的宽限期内提交 COMPLETED、CANCELLED、FAILED 或 TIMEOUT 终态。宽限期届满仍未完成的调用被记录为超时，迟到的工具回调或模型流片段只能写入删除审计或孤立日志，不得追加到 ACTIVE 消息流。

父 Agent 在阻止新写入并处理运行中任务后，删除或标记会话元数据、会话消息索引和会话级状态，广播目录变化，并在满足清理条件时删除子 Agent 或将其状态迁移为 DELETED。删除会话时不删除用户级共享资源；共享工作区、授权凭据、外部工具连接配置、跨会话记忆、跨会话索引和用户级调度任务均绑定到 $user_id$ 或父 Agent 命名空间，只有用户显式撤销授权、清空工作区或删除账号级资源时才清理。session_id 删除后不复用，或必须与新的 generation 共同校验，防止旧连接、旧工具回调或迟到流式片段写入新会话。

创建会话时，父 Agent 先确认用户已认证、父 Agent 已存在或可创建、session_id 未被使用且未处于 tombstone 窗口；随后写入 CREATING 注册表事件、创建子 Agent、初始化会话元数据，并将状态迁移为 ACTIVE。若子 Agent 创建成功但元数据写入失败，父 Agent 以注册表事件为准重试元数据写入或回滚子 Agent；若元数据先写入但子 Agent 初始化失败，元数据被标记为 tombstone 并由后台修复任务重建或清理。列出会话时始终以注册表和事件日志为准，避免展示信息与真实会话生命周期不一致。

### 并发、异常恢复与部署适配

并发写入以资源类型区分处理。替换类资源必须携带 if-match 版本，父 Agent 比较用户级资源表中的当前版本，不一致时返回 CONFLICT、current_version、diff_summary 和可重试标识，调用方需重读当前版本后重新生成写入；追加类资源由父 Agent 分配递增 append_seq，并按 request_id 去重，重复请求返回首次提交结果。该规则使后写请求不能静默覆盖先写结果，并使网络重试不会产生重复事实、重复索引项或重复文件片段。

父 Agent 或子 Agent 重启后，恢复流程以持久事件日志和注册表状态为准。父 Agent 扫描 CREATING、DELETING、RECOVERING 等非终态记录，依据最后事件 offset、租约或心跳时间判断继续、回滚或转入修复任务；子 Agent 扫描 RUNNING 或 QUEUED 的 run 和工具调用，已提交最终事件的按 request_id 去重确认，未提交终态且超过租约的标记为 FAILED 或 TIMEOUT，并向重连客户端广播恢复后的终态。孤儿子 Agent 若无法通过父 Agent 注册表校验自身 session_id、generation 和租约，则停止接受新写入。

注册表、元数据和实际子 Agent 实例不一致时，以注册表事件日志和 delete_epoch 为准修复。存在注册表但元数据缺失的，父 Agent 从消息日志和子 Agent 状态重建标题、摘要和索引；存在元数据但注册表无有效记录的，元数据被标记为 tombstone 并从列表隐藏；存在子 Agent 但注册表已进入 DELETED 的，子 Agent 只能导出诊断信息后自停。该规则阻止孤儿实例或迟到回调恢复已删除会话。

部署形态属于适配层，不改变核心规则。父 Agent 与子 Agent 可以部署为同一用户对象内的父子分面，也可以通过远程 RPC 建立目录对象与会话对象之间的连接；无论采用哪种形态，认证主体派生父 Agent、父 Agent 注册表门控、短期连接票据、用户级资源代理访问、会话状态机和冲突处理规则均保持一致，子 Agent 只处理已经通过父 Agent 门控的会话内消息和任务。

共享工作区、取消控制或宿主工具能力跨边界传递时，基本实现为父 Agent 按次 RPC 代理；在需要跨休眠、跨边界或沙箱环境解析资源时，采用基于可序列化资源标识的回环代理作为适配方式。回环代理不持久化父 Agent 的实时对象引用，每次调用均根据 $user_id$、$session_id$、resource_id 和 permission_scope 重新解析目标资源并执行授权校验。若运行平台尚不支持对子执行环境注入自定义绑定，则该适配层退化为按次 RPC 代理，不影响父 Agent 统一持有共享资源和子 Agent 受控访问的核心结构。
