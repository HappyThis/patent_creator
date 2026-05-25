## 技术方案

本方案在现有基于 Durable Object（DO）的 agent 框架之上，增加一层子 agent 编排机制，使主 agent（Parent Agent）在一次请求处理中能够按需创建、驱动和观察一个或多个专门的子 agent（Sub-Agent），并将子 agent 的执行过程以流式事件的形式实时回传到主 agent 所在的同一会话视图中。方案同时覆盖三类调用模式：模型将子 agent 作为工具自动调用（agent-as-tool）、服务端确定性流程通过命令式 API 主动调用子 agent（runAgentTool）、以及没有直接父工具调用的后台子运行。

### 一、系统整体架构

系统由三类 Durable Object 角色构成：（1）父 agent（Parent Agent），即用户直接连接的主 chat agent，负责处理用户请求、管理模型推理循环、向所有已连接客户端广播消息；（2）子 agent（Sub-Agent / Helper Agent），即被父 agent 按需创建的独立 DO 实例（facet），拥有自己的 SQLite 存储、模型、系统提示词、工具集、会话状态和推理循环；（3）子 agent 运行注册表（Agent Tool Run Registry），位于父 agent 的 SQLite 中，持久记录每一次子 agent 运行的元数据，用于重连重放、生命周期管理和访问控制。父 agent 与子 agent 之间通过 DO RPC（Durable Object Remote Procedure Call）通信，子 agent 的执行输出通过父 agent 的 WebSocket 连接以流式事件帧的形式广播给客户端。

### 二、子 agent 运行身份与父子关联

每一次子 agent 运行由唯一的 runId 标识。runId 由父 agent 在发起子 agent 调用时生成（使用 nanoid 等唯一 ID 生成器），贯穿整个子 agent 生命周期。父子关联通过父 agent 侧的子 agent 运行注册表（cf_agent_helper_runs 表）维护，该表记录 runId（helper_id）、父工具调用 ID（parent_tool_call_id）、子 agent 类型（helper_type）、输入预览（query）、运行状态（status：running/completed/error/interrupted）、最终摘要（summary）、错误信息（error_message）、展示顺序（display_order）和持久流 ID（stream_id）等字段。子 agent 本身通过现有的 subAgent(Cls, name) 原语创建，名称即 runId，从而天然继承 DO 的路由可达性。

### 三、子 agent 调用模式

子 agent 的创建与调度分为三种模式，统一通过框架提供的两个 API 实现：（1）agentTool(Cls, options)——工具工厂，将子 agent 类包装为 AI SDK 标准工具定义，使父 agent 的 LLM 可以像调用普通工具一样自动决定何时调用子 agent，包括参数 schema、工具描述和 execute 函数；（2）runAgentTool(Cls, { input, ...options })——命令式 API，允许服务端确定性流程（如工作流引擎、定时任务、后台作业）在不依赖 LLM 决策的情况下直接启动子 agent 运行。两种模式内部共享同一套运行引擎，区别仅在于触发方式。没有直接父工具调用的后台子运行通过 runAgentTool 启动，其 parentToolCallId 字段设置为 null 或独立的调度标识，客户端仍可以通过类型筛选和 runId 获取其执行过程。

### 四、核心处理流程

子 agent 执行过程中的核心处理流程如下：（1）父 agent 在工具 execute 函数或 runAgentTool 调用中，通过 subAgent(Cls, runId) 获取子 agent 的 RPC 代理（stub）；（2）父 agent 向注册表插入一行状态为 running 的记录；（3）父 agent 向所有连接客户端广播一条合成的 started 生命周期事件，携带 helperId、helperType、query 和 displayOrder，客户端据此立即渲染子 agent 面板；（4）父 agent 调用子 agent 的 runTurnAndStream(input, runId) 方法，该方法返回一个基于 DO RPC 的 ReadableStream<Uint8Array>；（5）子 agent 在 runTurnAndStream 内部调用自身的 saveMessages(...) 驱动一次完整的推理循环，并通过覆盖 broadcast 方法将推理过程中产生的每一条 MSG_CHAT_RESPONSE 类型的 chunk 转发到 RPC 流中；（6）父 agent 读取 RPC 流中的 NDJSON 帧（每帧为 {sequence, body}），将每个 chunk 封装为 helper-event 类型的 chunk 事件帧（携带 parentToolCallId、helperId、sequence 和 body），通过 WebSocket 广播给客户端；（7）子 agent 推理循环结束后，父 agent 通过 RPC 读取子 agent 的最终助手消息文本作为工具返回值，广播 finished 事件并更新注册表状态为 completed。若过程中发生异常，则广播 error 事件并将注册表状态更新为 error。

### 五、流式事件协议与实时展示

子 agent 的执行进度和结果通过统一的 helper-event 事件协议传输。事件帧格式为 { type: "helper-event", parentToolCallId, event, sequence, replay? }，其中 event 字段支持四种类型：

- started：由父 agent 在子 agent 启动前合成，携带 helperId、helperType、query、order，sequence 恒为 0。
- chunk：子 agent 推理过程中产生的 UIMessageChunk 的 JSON 序列化字符串，由父 agent 从 RPC 流中读取后原样转发。sequence 为递增序号。客户端使用 applyChunkToParts 函数将 chunk 累积重建为子 agent 的消息部件数组。
- finished：由父 agent 在子 agent 正常完成后合成，携带 helperId 和 summary。
- error：由父 agent 在子 agent 执行异常时合成，携带 helperId 和 error 错误信息。

客户端通过监听同一 WebSocket 连接上的消息事件，按 type === "helper-event" 筛选，再按 parentToolCallId 分组，将各子 agent 的事件挂载到对应的父 agent 工具调用部件下渲染。多个子 agent 可属于同一 parentToolCallId（如并行比较场景），客户端通过 helperId 区分，通过 order 字段确定展示顺序。

### 六、重连恢复与重放去重

重连恢复机制分为两个层面：（1）子 agent 侧：子 agent 本质是 Think DO 实例，其推理过程中产生的每条 chunk 都通过 _resumableStream.storeChunk 持久化到自身的 SQLite 中，不依赖父 agent 的在线状态。子 agent 启用 chatRecovery = true，即使在父 agent 崩溃或休眠后重建，其已存储的 chunk 和消息历史依然可读取。（2）父 agent 侧：父 agent 的 onConnect 钩子在每次客户端 WebSocket 连接建立时（包括页面刷新重连），遍历 cf_agent_helper_runs 注册表中的所有记录，对每条记录：根据 helper_type 解析正确的子 agent 类别，通过 subAgent 获取子 agent 的 RPC 代理；合成 started 事件；调用子 agent 的 getChatChunksForReplay(streamId) 方法读取该次运行的持久化 chunk 列表，逐条转为 chunk 事件转发；根据注册表中的 status 字段合成 finished 或 error 终端事件。所有重放事件标记 replay: true，与实时事件的区分由客户端根据 (parentToolCallId, helperId, sequence) 三元组进行去重处理。

去重机制：客户端维护一个已见事件三元组的集合（使用 useRef 持久化）。当同一三元组的事件同时通过重放路径（replay: true）和实时广播路径（replay: undefined）到达时——这发生在父 agent 的读循环尚未追上已持久化 chunk 时用户刷新页面——只有第一条被处理，后续重复的帧被丢弃。三元组必须包含 helperId 而不能仅包含 parentToolCallId，因为在并行子 agent（如 compare 工具的扇出调用）中，不同子 agent 都会产生 sequence: 0 的 started 事件。

### 七、取消与中断处理

取消机制采用基于 AbortSignal 的端到端传播链路：（1）父 agent 的工具 execute 函数接收来自 AI SDK 的 abortSignal；（2）父 agent 的 _runHelperTurn 方法监听该信号，当信号触发时调用子 agent RPC 流的 reader.cancel()；（3）workerd 的 DO RPC 桥接层将该取消操作传播到子 agent 侧，触发子 agent ReadableStream 的 cancel 回调；（4）子 agent 在 cancel 回调中调用本次运行的独立 AbortController.abort()；（5）该 AbortController 的信号通过 saveMessages({ signal }) 传入 Think 的推理循环，使推理立即终止。采用每次运行独立创建 AbortController 的策略，确保并发运行各自的取消互不干扰。注册表在父 agent 启动时（onStart）将所有 running 状态的记录批量更新为 interrupted 状态，避免因父 agent 崩溃导致的僵死运行记录。

### 八、清理保留与调度

清理与保留策略：子 agent DO 实例在运行完成后默认保留，不执行 deleteSubAgent。这是重连重放功能的基础——已完成子 agent 的持久化 chunk 和消息历史必须在子 DO 存在时才能被父 agent 的 onConnect 路径读取。清理由应用层通过显式的 clearHelperRuns() 方法触发：该方法遍历注册表中的所有记录，对每条记录调用 deleteSubAgent 删除对应的子 DO 实例，然后清空注册表。生产环境中可扩展为基于时间（TTL）或数量上限的自动 GC 策略。调度层面的子 agent 调度通过现有的 Agent 调度 API 实现，子 DO 不拥有独立的物理 alarm 槽位，但父 agent 存储以 owner 路径标记的子调度行，当 alarm 触发时将回调路由到子 DO 内部执行。

### 九、Drill-in 访问控制

Drill-in（深入查看）访问控制通过父 agent 的 onBeforeSubAgent 钩子实现。框架的 sub-agent 路由原语允许客户端通过嵌套 URL（/agents/{agent}/sub/{className}/{name}）直接连接到子 agent。为防止任意客户端猜测子 agent 名称并创建未授权连接，onBeforeSubAgent 钩子执行严格的注册表门控检查：（1）验证请求的 className 是否属于已注册的子 agent 类型集合（helperClassByType）；（2）查询 cf_agent_helper_runs 注册表，验证 (helperType, helperId) 组合是否存在。只有当该子 agent 确实由合法的 _runHelperTurn 调用创建（即注册表中存在对应记录）时才放行，否则返回 404。内部 subAgent 调用绕过此钩子，因此不会阻塞父 agent 自身的子 agent 创建。合法客户端通过注册表验证后，可直接使用 useAgentChat 对子 agent 发起完整的聊天会话——因为子 agent 本身是 Think 实例，所有 chat 协议原生可用。

### 十、并发控制与幂等性

并发控制：（1）同一子 agent 实例通过 _runInProgress 同步标志防止并发 runTurnAndStream 调用，必须在方法入口处同步检查而非依赖异步 forwarder 检查，因为 ReadableStream 的 start 回调是懒执行的；（2）父 agent 侧通过 Promise.allSettled 支持同一 parentToolCallId 下的多子 agent 并行执行，每个子 agent 独立运行互不影响，单个失败不影响其他子 agent 继续执行；（3）幂等性方面，runAgentTool 以 runId 为键，若子 agent 侧已存在该 runId 对应的运行记录则直接返回已有状态而非重复启动。重复请求通过注册表的状态检查避免重复启动。注册表记录的插入在子 agent 创建之前完成，保证 started 事件始终在子 agent 的 chunk 事件之前到达客户端。注册表中的状态一旦进入终态（completed/error/aborted/interrupted），后续的延迟取消或重复调和不得覆盖该终态。

### 十一、与现有框架的兼容性

方案完全兼容现有 chat/Think agent 体系：（1）父 agent 和子 agent 均基于现有 Think 或 AIChatAgent 类扩展，无需引入新的 agent 基类；（2）子 agent 的持久化复用 Think 已有的 _resumableStream 机制，chunk 存储在子 agent 自身 SQLite 中，不与父 agent 的流产生表名或帧类型冲突；（3）子 agent 通过现有的 subAgent 原语创建，继承 DO 的路由可达性、SQLite 隔离和生命周期管理；（4）WebSocket 连接仍由父 agent 统一管理，子 agent 事件作为附加帧类型（helper-event）在现有 WebSocket 上传输，不改变客户端连接拓扑；（5）现有 Think 的 chatRecovery、saveMessages、broadcast 等机制在子 agent 中继续有效，无需重写。方案与现有 sub-agent 路由系统的关系：子 agent 既是 turn-scoped helper（在父 agent 工具调用内运行），也是独立的 addressable DO（通过 subAgent 可达、可 drill-in），这一双重身份通过注册表桥接统一。

### 十二、技术效果

（1）将子 agent 作为可调用能力统一抽象，同时支持 LLM 工具调用和命令式 API 调用，降低了应用开发者的集成成本。（2）子 agent 保持独立的 DO 执行上下文，状态隔离于父 agent，使得长任务和并行任务不会阻塞主会话，也使得已完成任务的执行过程可被后续查询和审计。（3）基于统一 helper-event 协议的流式展示机制，使用户界面能在同一会话视图中实时观察各子 agent 的执行进度、输出片段和生命周期状态转换。（4）Durable Object 级别的持久化保证用户刷新或断线重连后，已发生的子 agent 执行过程可完整恢复，chunk 级别的去重机制确保不出现重复展示。（5）端到端的 AbortSignal 取消传播链路确保取消操作可同步终止子 agent 的推理循环，无竞态窗口；onStart 阶段的 running→interrupted 批量调和防止父 agent 崩溃后的僵死状态。

### 十三、风险与待确认问题

（1）子 agent 嵌套调用：方案理论上支持子 agent 再调用其自身的子 agent，因为子 agent 本身也是完整的 agent，可通过相同的 runAgentTool/agentTool 机制递归编排。但嵌套子 agent 的执行事件默认不会自动向上桥接到最顶层父 agent 的客户端，需要额外的追踪机制。（2）AIChatAgent 兼容性：当前方案在 Think 上验证完成，AIChatAgent 作为父 agent 和子 agent 的适配尚未实施，主要集中在流式输出格式和恢复机制的差异对齐上。（3）子 agent 输入持久化：当前注册表默认仅存储输入的简短预览而非完整输入，完整输入的可选持久化策略（opt-in）尚未实现，应用开发者如需要完整输入重放需自行扩展。（4）DO RPC 流传输限制：当前 workerd 的 DO RPC 层仅支持 ReadableStream<Uint8Array>，不支持直接传输对象 chunk，这要求子 agent 将 chunk 序列化为 NDJSON 字节流。该限制是运行时特性而非方案设计缺陷，未来运行时升级后可直接优化。
