## 技术方案

### 整体架构

本方案在主 agent 框架中引入「子 agent（sub-agent）」协作机制，使一个父 agent 能够将复杂任务拆解并委派给多个具有独立执行上下文的子 agent 并行或串行处理。父 agent 与子 agent 均基于 Cloudflare Durable Objects（DO）实现，子 agent 以 DO 的 facet 子实例形态存在，与父 agent 共置在同一机器上，通过 DO RPC 进行类型化通信。

父 agent（Assistant 类，继承自 Think）在 getTools() 中注册面向模型的子 agent 调用工具（如 research、compare、plan），由大模型根据用户意图自主决定何时调用哪个子 agent。同时，父 agent 也支持通过程序化 API（_runHelperTurn）在服务端确定性流程中主动启动子 agent，无需经过模型工具调用路径。

每个子 agent（如 Researcher、Planner，均继承自 HelperAgent 基类，HelperAgent 又继承自 Think）拥有独立的 SQLite 存储、独立的模型实例、系统提示词、工具集、会话状态和推理 fibers。子 agent 通过 this.subAgent(Cls, name) 创建或获取，返回类型化的 SubAgentStub RPC 桩，父 agent 通过该桩调用子 agent 的 runTurnAndStream 方法驱动一次完整的推理回合并以流式方式获取输出。

### 子 agent 创建与生命周期管理

子 agent 的创建通过 Agent 基类提供的 this.subAgent(cls, name) 方法完成。该方法接收目标 Agent 子类和唯一名称，执行两步初始化：（1）通过 ctx.facets.get(name, ...) 创建或获取 DO facet 子实例；（2）发起 set-name fetch 触发 Server 初始化，首次访问时调用 onStart() 建立数据库表结构。子 agent 类必须在 Worker 入口点以原始类名导出，框架在创建前会验证类名是否存在于 ctx.exports 中，未导出时抛出明确错误。

每个子 agent 拥有独立的 SQLite 数据库，与父 agent 的存储在物理上隔离。以 Researcher 为例，其在 onStart() 中创建 cf_agent_helper_runs 表（记录运行状态）以及 Think 框架自带的会话消息表、可恢复流表和 fibers 表。这种隔离确保子 agent 的模型无法绕过安全边界直接访问父 agent 或其他子 agent 的数据。

子 agent 的生命周期管理包括三个核心操作：subAgent（创建/获取）、abortSubAgent（强制中止，子 agent 下次 subAgent 调用时重启）、deleteSubAgent（中止并永久清除存储）。子 agent 支持嵌套——一个子 agent 可以进一步调用 this.subAgent() 创建自己的子 agent，形成树状结构。父 agent 通过 parentAgent(Cls) 可获得对直接父 agent 的类型化 RPC 桩，实现双向通信。事务调度（如 schedule、scheduleEvery、runFiber）在 facet 上透明可用，由顶层父 agent 的物理 alarm 统一管理，按 owner path 路由回调到拥有者子 agent。

### 流式事件通信协议

父 agent 与子 agent 之间的通信采用基于 DO RPC 的流式协议。子 agent 暴露 runTurnAndStream(query, helperId): ReadableStream&lt;Uint8Array&gt; 方法，以 NDJSON（换行分隔 JSON）格式逐帧输出推理结果。每帧结构为 { "sequence": N, "body": "<JSON 编码的 UIMessageChunk>" }，其中 sequence 是从 0 开始的单调递增序号，body 是 Think 框架 _streamResult 产生的 UIMessageChunk 的 JSON 序列化形式（包括 text-start、text-delta、text-end、tool-call 等 AI SDK 标准消息部件）。

父 agent 将子 agent 的原始流帧封装为 helper-event 信封，通过自身 WebSocket 的 broadcast 方法发送给客户端。信封结构为 { type: "helper-event", parentToolCallId: string, sequence: number, replay?: true, event: HelperEvent }。其中 HelperEvent 定义了四种事件类型：

- started：表示子 agent 已启动，携带 helperId、helperType、query 和 display_order；
- chunk：透传子 agent 的 UIMessageChunk，body 字段为 JSON 序列化的消息部件；
- finished：子 agent 正常完成，携带 summary 摘要文本；
- error：子 agent 出错，携带 error 错误信息。

started、finished 和 error 事件由父 agent 合成（非子 agent 直接产生），chunk 事件为子 agent 输出的直接透传。parentToolCallId 将子 agent 事件关联到父 agent 的具体工具调用，支持同一父 agent 回合内多个子 agent 并发运行时的多路分解。sequence 在单个 (parentToolCallId, helperId) 范围内单调递增，客户端以此作为去重键。replay 标记用于区分实时事件与重放事件。

### 状态持久化与运行注册表

父 agent 在自身 SQLite 中维护 cf_agent_helper_runs 运行注册表，记录每次子 agent 调用的持久状态。表结构包含：helper_id（主键，子 agent 的唯一标识）、parent_tool_call_id（关联的父工具调用 ID）、helper_type（子 agent 类型，如 "Researcher" 或 "Planner"）、query（输入查询文本）、status（运行状态：running / completed / error / interrupted）、summary（完成时的摘要文本）、error_message（出错时的错误信息）、started_at / completed_at（时间戳）、display_order（在父工具调用下的展示排序）、stream_id（子 agent 可恢复流的标识符）。

注册表的写入遵循严格的生命周期顺序：（1）父 agent 在启动子 agent 之前先在注册表中插入一行，状态设为 running；（2）子 agent 完成推理后，父 agent 读取其最终输出文本和流错误信息，将对应行更新为 completed 或 error，并填入 summary 或 error_message；（3）父 agent 启动时（onStart），将上一实例遗留的 running 状态行标记为 interrupted，防止崩溃后残留的运行中记录被误认为仍在执行。

子 agent 端也维护 cf_agent_tool_child_runs 映射表（runId → requestId → streamId），将编排层的 runId 与 Think 框架内部的请求 ID 和可恢复流 ID 关联。子 agent 的聊天流数据（UIMessageChunk）通过 Think 自带的 _resumableStream 持久化到子 agent 的 DO SQLite 中，每个 chunk 按 chunk_index 索引存储。这种「父注册表 + 子流存储」的双层持久化保证了：父 agent 崩溃后可以从子 agent 恢复已产生的所有数据，子 agent 完成后的数据不依赖父 agent 的内存状态。

### 重放与去重机制

当客户端通过 WebSocket 连接或重新连接到父 agent 时，父 agent 的 onConnect 方法触发重放流程。该流程遍历 cf_agent_helper_runs 表中所有记录，按 started_at 升序排列，对每条记录依次合成并发送以下事件序列：

1. 从行数据合成 started 事件（sequence = 0），携带 helperType、query、display_order；
2. 通过 subAgent 获取子 agent 实例，调用 getChatChunksForReplay(streamId) 读取子 agent 持久化的聊天 chunk，逐个封装为 chunk 事件（sequence 递增），标记 replay: true；
3. 根据行的 status 合成终端事件：completed 行发送 finished（携带 summary），error 行发送 error（携带 error_message），interrupted 行发送 error（携带中断说明），running 行不发送终端事件（由后续的实时广播完成）。

去重机制基于 (parentToolCallId, helperId, sequence) 三元组。客户端维护已接收的 sequence 集合，对于每个子 agent 运行实例独立判断。replay 标记为 true 的帧在 UI 层可与实时帧区分（如显示为历史内容），但 sequence 去重逻辑对两者统一处理。这种设计确保了同一子 agent 的实时事件和重放事件在客户端不会产生重复渲染，同时允许并行扇出场景下多个子 agent 各自从 0 开始编号。

### 恢复与崩溃一致性

本方案的恢复机制基于子 agent 的持久化聊天流和父 agent 的运行注册表协同工作。当父 agent 因崩溃、被驱逐或重启而丢失内存状态后重新激活时，onStart 首先将注册表中所有 status = 'running' 的行标记为 interrupted，然后 onConnect 通过重放流程恢复所有已完成/已出错/已中断的子 agent 事件序列。

恢复的关键技术点包括：（1）stream_id 存储在注册表行中，使父 agent 能够精确定位到产生当前回合的子 agent 可恢复流，避免 drill-in 后续追加的回合干扰重放内容；（2）getFinalTurnText 方法通过 diff preTurnAssistantIds（回合前的 assistant 消息 ID 快照）识别本回合新产生的 assistant 消息，而非简单地取最后一条消息，确保在子 agent 被 drill-in 追加对话后重放仍能正确提取原始回合的输出；（3）getLastStreamError 作为降级路径——当子 agent 的推理抛异常导致没有产生 assistant 消息时，父 agent 读取可恢复流的错误信息作为 error_message，避免静默失败。对于父 agent 崩溃时仍在运行的子 agent，V1 方案将其标记为 interrupted 而非尝试重新挂载实时观察者，保证状态诚实地反映执行情况。

### 取消传播机制

取消传播链路从父 agent 的工具执行层开始，经 DO RPC 流控层，最终到达子 agent 的推理循环。具体机制如下：

（1）父 agent 的 _runHelperTurn 接收来自 AI SDK 工具执行的 abortSignal。当父 agent 的聊天请求被用户取消或超时时，该 signal 触发；（2）_runHelperTurn 内部调用子 agent RPC 流的 reader.cancel()，通过 workerd 的 JSRPC 流取消机制将取消意图传播到子 agent 端的 ReadableStream；（3）子 agent 的 runTurnAndStream 方法在创建 RPC 流时注册了 cancel 回调，该回调触发内部的 turnAbort AbortController；（4）turnAbort.signal 被传入 saveMessages({ signal })，直接链接到 Think 框架的推理 abort registry 中，使推理循环在下一个检查点同步终止。

该设计的核心创新在于消除了历史上存在的竞态窗口：在引入 saveMessages({ signal }) 之前，取消回调直接调用 _aborts.destroyAll()，但 _aborts 的控制器是在推理过程中延迟创建的——如果取消信号在控制器创建之前到达，取消操作会成为空操作，导致子 agent 继续执行完整推理。新方案将 AbortSignal 从推理启动之初就注入 registry，确保无论取消信号何时到达都能被捕获。此外，cancel 回调中还包含 finally 块清理逻辑，确保 turnAbort 的 signal 在 registry 中被移除，防止内存泄漏。

### 并行扇出与多路分解

本方案支持同一条父工具调用下并行启动多个同类或不同类的子 agent。以 compare 工具为例，其实现在一次调用中使用 Promise.allSettled 并行启动两个 Researcher 子 agent，分别研究不同的子主题。每个子 agent 获得独立的 helperId 和 display_order 值（如 0 和 1），父 agent 的 _broadcastHelperEvent 为每个子 agent 维护独立的序列号计数器。

并行扇出的多路分解依赖 helper-event 信封中的三个字段联合标识：（parentToolCallId, helperId, sequence）。两个子 agent 共享相同的 parentToolCallId（因为来自同一个工具调用），但拥有不同的 helperId，因此各自可以从 sequence = 0 开始编号而不会冲突。客户端按 parentToolCallId 分组、按 display_order 排序渲染。Promise.allSettled 确保任一个子 agent 失败不会阻塞其他子 agent 的执行和结果交付。测试验证了二元（Alpha 模式：不同 parentToolCallId 的两个 Researcher 并发运行）和二元（Beta 模式：同一 parentToolCallId 下的两个 Researcher 并发运行）以及三元并发场景，均实现了正确的多路分解和序列号隔离。

### Drill-in 访问控制

子 agent 支持独立的 WebSocket 连接（drill-in），客户端可以通过 URL 路由 /agents/{parent-class}/{parent-name}/sub/{kebab-class}/{helperId} 直接连接到特定子 agent，获得完整的聊天体验（发送消息、查看历史、使用工具等）。该路由由框架的 routeAgentRequest 在处理时先唤醒父 agent DO，再通过 onBeforeSubAgent 钩子进行访问控制，最后将 WebSocket 帧直接路由到子 agent facet。

访问控制通过父 agent 重写的 onBeforeSubAgent 方法实现，执行两层校验：（1）验证请求的 helperType（子 agent 类名）存在于 helperClassByType 注册表中，即该类确实是已注册的子 agent 类型；（2）查询父 agent 的 cf_agent_helper_runs 表，验证 (helper_id, helper_type) 组合是否存在——即该子 agent 实例确实由父 agent 创建过。任一校验失败返回 404 响应，阻止 WebSocket 升级。

该门控机制提供了跨类隔离：即使存在一个 Researcher 类型的子 agent 实例 helperId = "shared-id"，通过 /sub/planner/shared-id 路径访问也会被拒绝，因为 helper_type 不匹配。这防止了恶意用户通过猜测 helperId 跨越子 agent 类型访问其他租户的数据。此外，在框架驱动的子 agent 运行期间，concurrent 的 _runHelperTurn 调用对同一 runId 表现为幂等（返回已有的运行状态），drill-in 用户发送的聊天消息被延迟或拒绝，确保不会与正在进行的推理回合交叉污染。

### 清理与保留策略

子 agent 的 DO 实例在完成推理后默认保留而不删除，以便客户端刷新页面时通过重放机制恢复历史内容，以及支持 drill-in 连接进行后续对话。清理操作由父 agent 提供的 clearHelperRuns() 方法显式触发，该方法执行两步操作：（1）遍历 cf_agent_helper_runs 注册表中的所有行，对每个子 agent 调用 deleteSubAgent 以中止运行并永久清除其 DO facet 和 SQLite 存储；（2）清空注册表。

当前版本未实现自动 TTL（生存时间）或基于计数的垃圾回收——所有清理均由应用层通过 clearHelperRuns 显式调用触发。设计文档已将基于时间和数量的自动 GC 列为后续迭代方向。清理操作前会先取消仍在运行的子 agent（cancel-then-clean 顺序），以防止遗留无观察者的 LLM 推理工作。deleteSubAgent 设计为幂等操作，同时清理子 agent 的 descendant 子树（包括嵌套子 agent 的定时任务和 fibers），确保整个子 agent 树被完全移除。

### 实时流式转发机制

子 agent 的流式输出通过 HelperAgent 重写的 broadcast 方法实现实时转发。当 Think 框架的 _streamResult 产生 MSG_CHAT_RESPONSE 类型的推理 chunk 时，broadcast 方法在将 chunk 发送到子 agent 自身 WebSocket 客户端的同时，将其 tee（复制）到 RPC ReadableStream 的 controller。该 RPC 流正是 runTurnAndStream 返回给父 agent 的 ReadableStream&lt;Uint8Array&gt;。

父 agent 的 _runHelperTurn 方法通过 while 循环逐帧读取 RPC 流：使用 reader.read() 获取 Uint8Array 帧，通过 TextDecoder 解码为字符串，按换行符分割为 NDJSON 行，每行解析为 { sequence, body } 结构，再调用 _broadcastHelperEvent 封装为 helper-event 信封并通过父 agent 的 WebSocket broadcast 发送给所有连接的客户端。这种「子 agent → RPC 流 → 父 agent 内存 → 父 WebSocket → 客户端」的四级转发路径保证了子 agent 的每个推理 token 都能以最小延迟送达终端 UI。每个 chunk 在 tee 转发的同时也被 Think 的 _resumableStream 持久化存储，确保转发和持久化在同一迭代中完成，不会出现「已转发但未持久化」的 chunk 丢失窗口。

### 三种子 agent 启动方式

本方案提供三种子 agent 启动方式，覆盖不同的编排场景：

（1）模型自动调用（工具驱动）：父 agent 在 getTools() 中将子 agent 包装为 AI SDK 工具入口。以 research 工具为例，其 execute 方法接收模型的 toolCallId、输入的 query 以及 AI SDK 传入的 abortSignal，内部调用 _runHelperTurn 启动 Researcher 子 agent，将 parentToolCallId 设为 toolCallId。模型根据用户意图自主选择调用时机和参数，子 agent 的流式输出作为工具执行的一部分实时展示在父 agent 的聊天界面中。

（2）服务端确定性调用（程序化驱动）：父 agent 的 compare 工具展示了该模式——其 execute 方法不依赖模型推理，而是直接使用 Promise.allSettled 并行调用 _runHelperTurn，为每个分支传入明确的 parentToolCallId 和 display_order。该方式适用于多阶段报表生成、定时任务触发、HTTP 端点触发的后台分析等确定性工作流，无需经过模型工具选择链路。

（3）后台子运行（无父工具调用绑定）：当 _runHelperTurn 调用不传入 parentToolCallId 时，子 agent 的事件不与父 agent 的特定工具调用关联，而是作为独立的后台运行存在。此时 helper-event 信封中 parentToolCallId 为空，客户端从 unboundRuns 列表中渲染。该方式适用于通过 @callable、HTTP handler 或定时任务启动的、与当前聊天回合无关的后台子 agent 任务。
