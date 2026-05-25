## 技术方案

本方案在现有 Cloudflare Agents SDK 的 Agent 基类（基于 Durable Object）、聊天协议（cf_agent_chat_* 系列消息）、子 Agent 路由（facets + /sub/{class}/{name} URL 体系）和可恢复流（ResumableStream）的基础上，提出一套子 Agent 运行（SubAgentRun）系统，使主 Agent 在一次请求处理中可将一个或多个专门子 Agent 作为可调用能力使用，并将子 Agent 的执行进度、输出片段和终态结果实时推送到主 Agent 所在会话视图，同时支持断线重连后的状态恢复。

### 一、技术问题概述

现有框架中，主 Agent 调用子 Agent 通过 subAgent() RPC 方式完成，调用方直接等待子 Agent 方法返回结果。这种模式存在三个关键不足：第一，子 Agent 的执行过程对用户不可见，用户只能看到主 Agent 最终汇总文本，无法感知子 Agent 的研究、规划、比较等中间过程；第二，WebSocket 断线或页面刷新后，子 Agent 执行状态完全丢失，用户无法恢复查看已发生的执行过程；第三，缺少统一的子 Agent 运行生命周期管理——没有标准化的运行标识、状态跟踪、取消机制和结果保留策略。本方案通过引入 SubAgentRun 概念和配套的事件流、持久化、恢复机制解决上述问题。

### 二、系统组件与数据模型

系统在现有 Agent 基类中新增 SubAgentRunManager 组件，负责子 Agent 运行的完整生命周期。核心数据模型包括 SubAgentRun（运行记录）和 SubAgentEvent（运行事件），均持久化在父 Agent 的 SQLite 存储中，利用 Durable Object 的持久存储保证故障恢复。

SubAgentRun 表（cf_agents_sub_runs）每条记录包含：run_id（全局唯一标识）、parent_request_id（关联的主 Agent 请求）、sub_agent_class 和 sub_agent_name（目标子 Agent 标识）、invocation_mode（调用模式：tool / explicit / background）、status（queued / running / completed / failed / cancelled）、input_payload（JSON 序列化的输入参数）、result_payload（JSON 序列化的最终结果）、error_message、created_at、started_at、completed_at、parent_tool_call_id（当作为工具调用时的父工具调用 ID，可为空）、owner_path（父 Agent 的 selfPath，用于隔离和清理）。

SubAgentEvent 表（cf_agents_sub_events）记录每次运行的原子事件，每条记录包含：event_id、run_id（外键关联）、seq（单调递增序号，用于去重重放）、event_type（thinking / tool_call / tool_result / text_chunk / progress / status_change / error）、payload（JSON 事件体）、created_at。seq 字段是客户端重连时判断已接收事件和需重放事件的锚点。

### 三、三种调用模式

本方案支持三种子 Agent 调用模式，统一通过 SubAgentRunManager 管理。

（1）模型驱动工具调用：主 Agent 将每个可用的子 Agent 类型自动生成为 AI 工具定义（Tool Definition）。工具的 name 为子 Agent 类名，description 来自子 Agent 声明的能力描述，inputSchema 来自子 Agent 的输入参数类型。当模型调用该工具时，框架拦截 tool-call 事件，创建 SubAgentRun（invocation_mode=tool），通过 RPC 调用对应的子 Agent 方法。

（2）服务端确定性流程调用：应用开发者可在 Agent 代码中调用 runSubAgent(subAgentClass, name, input) API，创建 SubAgentRun（invocation_mode=explicit）并立即获得一个包含运行状态和事件订阅能力的 SubAgentRunHandle。该 handle 提供 await handle.result 获取最终结果，以及 handle.onEvent() 订阅事件流。

（3）后台子运行：通过 runSubAgentBackground(subAgentClass, name, input) 启动，创建 SubAgentRun（invocation_mode=background），不阻塞调用者。运行状态和事件仍然持久化并可查询。该模式适用于无直接父工具调用的场景，如守护任务、定时触发的研究任务、预热任务等。后台运行的父子关联通过 owner_path 记录，确保清理时可追溯。

### 四、流式事件实时展示与去重

子 Agent 执行过程中产生的所有事件（thinking 文本块、tool_call/tool_result 对、progress 更新、status_change 等）通过 SubAgentEventRelay 组件实时桥接到父 Agent 的 WebSocket 连接。

事件桥接机制：子 Agent 在其自身执行过程中调用 SubAgentRunHandle.emit(eventType, payload)，该调用将事件写入父 Agent 的 cf_agents_sub_events 表并分配一个单调递增的 seq 序号。同时，父 Agent 通过已有的 WebSocket 广播通道（broadcast 方法），以新增的协议消息类型 cf_agent_sub_run_event 推送事件到所有连接到父 Agent 的客户端。

WebSocket 协议新增消息类型：cf_agent_sub_run_start（通知客户端有子 Agent 运行开始，携带 run_id、sub_agent 标识和 invocation_mode）、cf_agent_sub_run_event（携带 run_id、seq、event_type 和 payload 的增量事件）、cf_agent_sub_run_status（running/completed/failed/cancelled 状态变更）、cf_agent_sub_run_list（响应客户端列表请求，返回所有已知运行及其状态摘要）。

去重机制：每个连接到父 Agent 的客户端维护一个 last_seen_seq 映射（按 run_id 索引），记录该客户端已确认接收到的最大事件序号。服务器推送事件时携带 seq，客户端根据 seq 去重。当客户端检测到 seq 跳跃（如断线重连期间产生的事件），触发重放请求。

### 五、断线重连与状态恢复

恢复机制的核心思路是将子 Agent 运行状态和事件全部持久化在 SQLite 中，客户端重连时通过已有的流恢复协议扩展实现状态重建。

客户端重连时执行以下步骤：（a）客户端发送 cf_agent_sub_run_resume 消息，携带每个已知 run_id 及其 last_seen_seq；（b）服务器查询 cf_agents_sub_runs 表，返回所有 SubAgentRun 的状态摘要列表（cf_agent_sub_run_list），客户端据此重建 UI 中的运行卡片；（c）对于状态为 running 且客户端 last_seen_seq < 服务器当前最大 seq 的运行，服务器从 cf_agents_sub_events 表中按 seq 升序读取增量事件，以 replay=true 标记逐条发送；（d）对于在客户端断开期间状态已变为 completed/failed/cancelled 的运行，重放所有缺失事件后发送终态状态变更消息。

Durable Object 休眠唤醒恢复：当父 Agent 因空闲被休眠后重新唤醒，其 SubAgentRunManager 在 onStart() 期间扫描 cf_agents_sub_runs 表，将所有 status=running 且 started_at 早于当前时间一定阈值（如 30 秒）的运行标记为 failed（reason: agent_evicted），并生成对应的 status_change 事件。这防止了因父 DO 休眠而导致悬挂运行状态无法收敛的问题。对于子 Agent 自身也可能休眠的场景，子 Agent 内部通过已有的 runFiber 机制或在其 onStart() 中检查自身是否仍有待完成工作来处理。

### 六、并发、幂等、取消与清理

系统对子 Agent 运行的并发、重复、取消和清理进行了完整设计。

并发执行：多个子 Agent 运行可同时进行。每个 runSubAgent 调用在独立的 Durable Object facet 中执行，互不阻塞。框架通过 Promise.all 或逐个 await 的方式收集多个子 Agent 的结果。事件推送层面，不同 run_id 的事件通过各自的 seq 通道独立编号，客户端按 run_id 分别管理。

幂等性——重复请求处理：runSubAgent 支持 idempotency_key 参数。当提供幂等键时，SubAgentRunManager 首先查询 cf_agents_sub_runs 中是否存在同一 (owner_path_key, idempotency_key) 且状态为 completed/failed/cancelled 的有效记录。若存在且未过期（在可配置的保留窗口内），直接返回已有运行的句柄，通过 revalidateRun 方法从 SQLite 重建事件流，不重复执行。保留窗口通过 idempotency_ttl_seconds 配置，默认为 3600 秒。已过期记录的 idempotency_key 可被新运行覆盖。

取消正在运行的子 Agent：调用 cancelSubAgentRun(runId, reason) 方法。框架执行以下步骤：（a）将目标 SubAgentRun 的 status 更新为 cancelling；（b）通过现有 ctx.facets.abort(facetKey, reason) 机制中止子 Agent 的 Durable Object 执行，该机制会触发子 Agent 内部的 AbortSignal，使其正在进行的 LLM 调用或工具执行被中断；（c）子 Agent 的 onAbort 钩子在捕获到 abort 后，可在最终清理前调用 emit(status_change, cancelled) 写入终态事件；（d）父 Agent 在收到子 Agent 中止反馈后，将 status 更新为 cancelled，推送 cf_agent_sub_run_status 消息。

清理保留策略：SubAgentRun 记录及其关联的 SubAgentEvent 不会被立即删除。系统提供 cleanupSubRuns(options) API，支持按 created_before（创建时间阈值）、status（仅清理终态运行）、owner_path 前缀等条件批量清理。默认保留周期为 7 天。清理操作同步删除 cf_agents_sub_runs 和 cf_agents_sub_events 中的对应行，同时释放子 Agent facet 的存储空间（ctx.facets.delete）。定时清理可通过 Agent 的 scheduleEvery 在 onStart 中注册，利用现有调度基础设施自动执行。

### 七、子 Agent 身份、父子关联与 drill-in 访问控制

子 Agent 的外部可寻址性和 drill-in 访问通过扩展现有的 /sub/{class}/{name} 路由体系实现，配合细粒度访问控制。

子 Agent 身份：每个子 Agent（facet）拥有独立的 Durable Object 标识，通过 ctx.id.name 唯一定位。父子关联通过 parentPath 属性记录完整的祖先链（root-first），例如 [{className: "MainAgent", name: "user-123"}, {className: "ResearchSubAgent", name: "research-1"}]。SubAgentRun 表的 owner_path 字段存储父 Agent 的 selfPath，用于按前缀查找、清理和访问控制。

drill-in 访问控制：当客户端需要直接查看某个子 Agent 的详细执行过程时（如独立的思考链、工具调用历史），可以通过 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name} URL 发起 WebSocket 连接，直接与子 Agent 通信。访问控制通过 onBeforeSubAgent 钩子实现：父 Agent 在此钩子中验证请求来源（如检查 WebSocket 升级请求中的 session cookie、JWT token 或父 Agent 连接中已认证的用户身份），确认该客户端有权访问目标子 Agent 后才放行。对于无直接父工具调用的后台运行子 Agent，由于其仍记录在父 Agent 的 sub_agent 注册表中，drill-in URL 同样有效。

### 八、与现有框架的兼容性

本方案完全建立在现有框架基础上，不要求应用开发者重写 Agent 代码。具体兼容性体现在：（1）runSubAgent 和 runSubAgentBackground 是 Agent 基类的新公开方法，现有 subAgent() 方法保持不变；（2）子 Agent 内部无需感知自己是作为 SubAgentRun 被调用——它们仍然实现自己的 chat() 或自定义 callable 方法，只是框架层在调用前后额外管理运行记录和事件桥接；（3）WebSocket 协议新增消息类型为独立命名空间，不影响现有 cf_agent_chat_* 消息的处理逻辑；（4）SubAgentRun 的持久化表与现有 cf_agents_* 表平级共存，不影响现有 Schema 版本和迁移。

### 九、技术效果

通过 SubAgentRun 系统，本方案取得以下技术效果。

（a）执行过程可见性：用户在同一会话视图中可实时看到每个子 Agent 的思考过程、工具调用和进度更新，而非仅看到最终汇总文本。（b）断线恢复能力：页面刷新或网络短暂断开后，所有子 Agent 的执行历史和当前状态可完整恢复，包括已完成的运行结果和进行中的增量事件。（c）多样调用模式：模型自动选择、服务端确定性流程、后台任务三种模式覆盖不同使用场景，同一套基础设施支持全部模式。（d）资源安全：通过 idempotency_key 防止重复执行，通过 abort 机制支持取消，通过可配置的 TTL 和批量清理 API 控制存储增长。（e）架构兼容：利用已有 Durable Object facets、sub-routing、stream resumption 和 alarm 调度机制，最小化新增复杂度。

### 十、风险与待确认问题

以下风险点需要在实施过程中进一步确认和细化。

（1）事件写入延迟：子 Agent 在调用 emit() 时需要通过 RPC 写回父 Agent 的 SQLite，跨 DO 边界的写入可能引入延迟。建议在子 Agent 内部缓冲事件、批量写回，并利用 ctx.waitUntil 保证写入不被请求结束中断。（2）子 Agent 休眠导致的悬挂运行：当子 Agent 作为独立 DO 休眠时，其内部的执行状态丢失。需确认子 Agent 的 runFiber 恢复机制是否能在此场景下正确重新驱动执行，或需要在父 Agent 侧增加心跳超时检测。（3）大规模事件存储：长时间运行的子 Agent 可能产生大量事件。建议在 SubAgentEvent 表上实现事件截断策略——当同一 run_id 的事件数超过阈值（如 10000）时，将早期事件压缩为摘要记录。（4）并发子 Agent 对父 DO 内存压力：多个子 Agent 同时活跃时，父 Agent 需维持到每个子 Agent 的 RPC 连接。需评估 Cloudflare Workers 的并发子请求限制是否构成瓶颈。（5）与 Think Agent 的深度集成：Think 的 Session 和 context block 体系是否需要在 SubAgentRun 层面做特殊适配，需要与 Think 团队进一步确认。
