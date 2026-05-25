## 技术方案

### 技术问题概述

本方案要解决的问题是：在基于 Durable Object（DO）的 agent 框架中，用户与主 agent 对话时，主 agent 在处理一次请求过程中需要按需调用一个或多个专门的子 agent 完成研究、规划、比较、总结等子任务，而用户界面需要实时看到子 agent 的执行过程和结果。现有方案仅支持 chat agent、Think agent 和基本的子 agent 路由，缺乏对子 agent 作为可调用能力的统一抽象、父子执行上下文的隔离与关联、跨 WebSocket 生命周期的流式事件持久化与恢复、以及并发、取消、去重等健壮性保障。

### 系统整体架构

系统在现有 DO agent 框架基础上新增以下核心组件：（1）子 Agent 注册表（SubAgentRegistry）：管理可用子 agent 的类型定义、能力描述和实例工厂；（2）子运行管理器（SubRunManager）：位于主 agent DO 内部，负责创建、跟踪和管理子 agent 的执行生命周期；（3）子 Agent DO（SubAgentDO）：每种类型子 agent 对应一个独立 DO 类，拥有独立的执行上下文、消息历史和状态机；（4）事件总线适配器（EventBusAdapter）：负责将子 agent DO 内部产生的流式事件跨 DO 传递到主 agent 所在会话的 WebSocket 连接，同时持久化到事件日志。

### 子 Agent 身份与父子关联模型

每个子 agent 执行实例拥有全局唯一的子运行标识（sub_run_id），由主 agent DO 在创建子运行时生成。父子关联通过以下字段建立：（1）parent_do_id：发起调用的主 agent DO 实例标识；（2）parent_call_id：若子运行由模型工具调用触发，记录父工具调用标识；若由服务端确定性流程触发，记录为流程步骤标识；若无直接父工具调用（后台运行），该字段为 null；（3）parent_session_id：主 agent 所在会话标识，用于事件路由和恢复时定位；（4）round_id：所属的对话轮次。子 agent DO 在创建时接收上述关联字段，并在所有输出事件中携带，确保事件可被正确路由回主 agent 会话视图。

### 子 Agent 调用机制

子 agent 支持三种调用路径，统一通过 SubRunManager 发起：（1）模型工具调用路径：将子 agent 类型注册为大模型可用的 function tool，模型在推理过程中返回 tool_call 时，SubRunManager 根据 tool_call 中的子 agent 类型和参数创建 SubAgentDO 实例，异步执行并将流式事件写入主会话的事件流；（2）服务端确定性流程调用路径：应用开发者在 agent 流程定义中显式声明子 agent 调用步骤，SubRunManager 在流程执行到该步骤时同步创建子运行，适用于需要确定性编排的场景如审批、分步研究；（3）后台子运行路径：主 agent 可在处理过程中通过代码直接调用 SubRunManager.spawn_background() 创建不绑定特定工具调用的子运行，适用于预加载、持续监控等场景。三种路径共享同一套子运行生命周期管理和事件传播机制。

### 子 Agent 状态机与事件模型

子 agent DO 维护一个有限状态机，状态包括：PENDING（已创建等待执行）、RUNNING（正在执行）、STREAMING（正在输出流式片段）、COMPLETED（成功完成）、FAILED（执行失败）、CANCELLED（被取消）。状态转换规则：PENDING→RUNNING 由 DO 的 alarm 或队列消费触发；RUNNING↔STREAMING 在每次 LLM 流式输出 chunk 时切换；RUNNING/STREAMING→COMPLETED 在子 agent 产出最终结果时触发；RUNNING/STREAMING→FAILED 在遇到不可恢复错误时触发；PENDING/RUNNING/STREAMING→CANCELLED 由取消请求触发。每次状态转换均产生一个 SubAgentEvent，包含 sub_run_id、事件序列号、事件类型、时间戳、负载数据和父关联字段。

### 流式事件实时展示与重放去重

子 agent 产生的每个 SubAgentEvent 通过 DO 之间的存储绑定（Storage Binding）写入主 agent DO 的事件日志，同时通过 WebSocket 推送至前端。前端维护一个基于事件序列号的去重窗口：对于每个 sub_run_id，前端记录已渲染的最大 seq 值，收到新事件时若 seq ≤ 已渲染最大值则丢弃，否则追加渲染。重放场景：用户刷新页面或 WebSocket 重连后，前端请求主 agent DO 的事件日志查询接口，传入当前会话中所有已知 sub_run_id 及其已渲染的最大 seq，服务端返回所有未被覆盖的新事件，前端按 sub_run_id 分组并按 seq 升序重放。对于 RUNNING 状态的子运行，前端在重放完成后建立新的 WebSocket 订阅以接收后续实时事件。

### 恢复机制

恢复机制依赖 Durable Object 的持久化存储和事件日志。主 agent DO 在每次请求处理中持久化当前轮次的子运行清单（active_sub_runs），包含每个子运行的 sub_run_id、类型、状态和创建参数。WebSocket 断开重连时：（1）前端携带会话 ID 和已消费的事件游标发起重连；（2）主 agent DO 从存储中加载 active_sub_runs 清单，对于每个子运行查询其 SubAgentDO 当前状态和全部事件；（3）将自游标之后的所有事件批量推送给前端。子 agent DO 崩溃恢复：得益于 DO 的持久化语义，SubAgentDO 在重启后自动从存储中恢复状态机当前状态、LLM 对话历史和未发送的事件队列，继续执行或标记为 FAILED。对于 RUNNING 状态中断超过阈值的子运行，由主 agent DO 的定期巡检 alarm 自动将其标记为 FAILED 并通知前端。

### 取消与清理保留

取消机制：前端或主 agent 可通过 SubRunManager.cancel(sub_run_id) 发起取消。SubRunManager 向目标 SubAgentDO 发送取消信号，SubAgentDO 在下一个可中断点（LLM 调用前、工具调用前或流式输出块之间）检查取消标志，若已设置则停止执行、将状态转为 CANCELLED、发送取消事件并清空待处理队列。清理保留策略：每个子运行的完整事件日志和结果在创建后的保留窗口（默认 24 小时）内可通过 sub_run_id 查询；保留窗口过后，由主 agent DO 的定期清理 alarm 触发级联删除——先删除 SubAgentDO 的存储数据，再移除主 agent DO 中的子运行记录。若子运行所属的父会话仍在活跃期，保留窗口自动延长至会话生命周期结束。正在运行的子 agent 在清理时先尝试取消，等待宽限期后再强制清理。

### 并发控制与重复请求处理

并发控制：SubRunManager 采用乐观并发模型。多个子运行可以并行创建和执行，每个子运行在独立的 SubAgentDO 中运行，互不阻塞。对于同一子 agent 类型，若业务语义要求串行化，应用开发者可在调用参数中声明 concurrency_key，SubRunManager 在创建前检查是否存在同 key 的 RUNNING/PENDING 子运行，若存在则根据策略（排队等待、返回已有结果、拒绝新建）处理。重复请求处理：基于幂等键（idempotency_key）机制。创建子运行时，调用方可传入幂等键；SubRunManager 在存储中维护幂等键到 sub_run_id 的映射（在保留窗口内有效）。收到重复幂等键请求时，直接返回已有子运行的 sub_run_id 和当前状态，避免重复创建和重复执行。幂等键映射与子运行记录共享同一保留窗口。

### Drill-in 访问控制

drill-in 访问控制确保用户只能查看属于其会话的子 agent 执行详情。核心规则：（1）前端请求子运行详情时，必须携带当前会话 ID；（2）主 agent DO 收到 drill-in 请求后，校验请求的 sub_run_id 对应的 parent_session_id 是否与当前会话 ID 一致，不一致则拒绝；（3）对于跨会话共享的子 agent（如组织级知识库检索 agent），额外校验用户身份对该子 agent 类型的访问权限。访问粒度分为三级：仅查看状态（前端轮询摘要）、查看事件流（获取完整事件序列）、查看内部上下文（获取子 agent 的完整消息历史和中间推理，仅对授权开发者开放）。前端通过子运行卡片组件展示子 agent 的类型、状态、进度摘要，点击可展开 drill-in 面板查看详细事件流。

### 与现有 Agent 体系的兼容性

方案在现有 chat/Think agent 体系基础上扩展，不要求重写框架。关键兼容设计：（1）子 agent 复用现有 AgentDO 基类的消息处理、工具调用、LLM 流式输出等能力，通过继承和配置切换为子 agent 模式；（2）SubRunManager 作为现有 AgentDO 的内部模块嵌入，不影响现有主 agent 的消息处理主路径；（3）子 agent 的事件通过现有 WebSocket 推送通道传输，前端在现有消息渲染管线中增加子运行事件卡片的渲染分支；（4）Think agent 的推理过程展示机制可直接复用于子 agent 的内部推理流式展示；（5）现有子 agent 路由能力通过 SubAgentRegistry 统一管理，路由逻辑从硬编码迁移到注册表驱动。

### 核心数据结构与接口

核心数据结构：（1）SubRunDescriptor：包含 sub_run_id、agent_type、parent_do_id、parent_call_id、parent_session_id、round_id、idempotency_key、concurrency_key、input_params、status、created_at、updated_at；（2）SubAgentEvent：包含 event_id、sub_run_id、seq、event_type（status_change/output_chunk/error/cancelled）、timestamp、payload、parent_do_id、parent_call_id；（3）SubAgentDefinition：包含 agent_type、display_name、description（用于 LLM function tool 描述）、input_schema、timeout_seconds、retention_window_seconds。关键接口：SubRunManager.create(descriptor) → SubRunDescriptor；SubRunManager.cancel(sub_run_id) → void；SubRunManager.get_events(sub_run_id, after_seq) → SubAgentEvent[]；SubRunManager.get_active_runs(session_id) → SubRunDescriptor[]；SubAgentDO.on_alarm() 驱动异步执行。

### 端到端数据流

以模型工具调用路径为例的端到端数据流：（1）用户发送消息到主 agent DO；（2）主 agent 调用 LLM，LLM 返回 tool_call（如调用 research_agent）；（3）SubRunManager 生成 sub_run_id，创建 SubRunDescriptor（状态 PENDING），持久化到主 agent DO 存储；（4）SubRunManager 通过 DO 存储绑定创建 SubAgentDO 实例，传入子运行参数和父关联信息；（5）SubAgentDO 初始化后通过 alarm 触发异步执行，状态转为 RUNNING；（6）SubAgentDO 调用 LLM 进行推理，每收到流式 chunk 即生成 SubAgentEvent（seq=N, type=output_chunk），通过存储绑定写入主 agent DO 并推送到 WebSocket；（7）前端按 sub_run_id 渲染子运行卡片，实时追加文本；（8）子 agent 完成，状态转为 COMPLETED，发送最终事件；（9）主 agent 在确认所有 tool_call 对应的子运行完成后，汇总结果继续与 LLM 对话生成最终回复。

### 技术效果

本方案的技术效果：（1）统一的子 agent 调用抽象使模型自动决策和服务端确定性编排共享同一套生命周期管理，降低应用开发复杂度；（2）基于 Durable Object 的独立执行上下文确保子 agent 之间、子 agent 与主 agent 之间故障隔离，单个子运行失败不影响其他子运行和主会话；（3）基于事件序列号的增量重放与去重机制确保前端在网络中断恢复后不丢失、不重复渲染子 agent 执行过程；（4）幂等键机制防止因网络重试导致的重复子运行创建，节省计算资源和 LLM 调用成本；（5）分层 drill-in 访问控制确保子 agent 的内部数据仅在授权范围内可访问；（6）保留窗口与级联清理策略自动回收过期子运行占用的存储资源；（7）方案通过继承和模块嵌入方式兼容现有 agent 框架，无需应用开发者重写已有逻辑。

### 风险与待确认问题

风险与待确认问题：（1）DO 存储绑定跨 DO 写入事件的延迟和吞吐量上限需在实际负载下验证，若单次主 agent 调用触发数十个子运行，事件写入可能成为瓶颈，可考虑事件批量写入和前端采样渲染作为降级策略；（2）子 agent DO 的 alarm 调度延迟可能影响 PENDING→RUNNING 的响应时间，需评估 Cloudflare Durable Object alarm 的精度和并发限制；（3）幂等键的保留窗口与子运行保留窗口的一致性需仔细设计，避免出现幂等键已过期但子运行仍可查询的安全边界问题；（4）子 agent 内部调用外部 API 或工具时的超时和重试策略需与整体取消机制协调，避免取消信号到达后仍有外部副作用；（5）多租户场景下的子 agent 类型级访问控制和资源配额需进一步细化。
