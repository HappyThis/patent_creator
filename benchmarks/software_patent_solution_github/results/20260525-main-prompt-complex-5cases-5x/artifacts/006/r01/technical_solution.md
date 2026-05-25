## 技术方案

本技术方案针对基于 Durable Object 的长时间 agent 对话场景，提出一套客户端与服务端协同的对话生命周期管理机制。核心要解决的问题是：在浏览器刷新、组件卸载、页面切换或网络短暂断开等客户端本地生命周期事件发生时，如何避免误终止已在服务端持续运行的 agent turn，同时又能将用户主动取消意图可靠传递到服务端执行真正的任务取消。

### 一、整体架构与核心机制

系统在架构上划分为三个层次：客户端层（浏览器中的 useAgentChat hook）、传输层（WebSocket 或 HTTP 流式连接）、服务端执行层（Durable Object 中的 agent turn 执行器）。本方案在客户端层引入"取消意图标记"（cancel intent flag），在传输层引入"连接生命周期事件分类器"，在服务端执行层引入"turn 取消仲裁器"（turn cancellation arbiter），三者协同实现本地清理与服务端取消的语义分离。

核心创新在于：将客户端断开事件分类为"被动断开"（浏览器刷新、组件卸载、reader cancel、网络断开）和"主动取消"（用户点击停止按钮、应用调用 stop() 方法）两类，前者仅触发本地资源清理而不通知服务端取消，后者通过独立的取消信令通道（cancel signal channel）将取消意图送达服务端。服务端 Durable Object 在收到取消信令后，通过仲裁器判断当前 turn 是否可取消、是否已被其他标签页恢复、以及工具调用 continuation 是否应被打断。

### 二、客户端生命周期语义分层

本方案明确定义了客户端侧三类生命周期事件及其语义边界，确保每种事件仅触发恰当的本地行为，不扩散为服务端副作用。

第一类：纯本地清理事件。包括浏览器刷新（beforeunload）、React 组件卸载（useEffect cleanup）、页面切换（路由导航离开）、AbortController/reader.cancel() 调用。这些事件的共同特征是：由浏览器或框架运行时触发，不代表用户希望终止服务端 agent 执行。处理方式为：仅执行本地状态清理（关闭 reader、释放 AbortController、清理本地消息缓冲区），不向服务端发送取消信令。useAgentChat hook 在检测到这些事件时，将本地连接状态标记为 disconnected，但不设置取消意图标记。

第二类：网络短暂断开事件。包括 TCP 连接断开、WebSocket 关闭（非客户端主动 close）、HTTP 流中断。这类事件的特征是：连接层不可用但客户端页面仍存活，用户期望恢复后继续接收流。处理方式为：useAgentChat hook 启动指数退避重连（exponential backoff reconnection），重连成功后通过会话标识符向 Durable Object 请求流恢复（stream resumption），从上次已确认的最后一条消息偏移量之后继续接收。重连期间不发送取消信令，Durable Object 中的 agent turn 继续不受影响地执行。

第三类：用户主动取消事件。包括点击 UI 上的停止按钮、调用 useAgentChat 返回的 stop() 方法、或应用层通过程序化接口触发取消。这类事件的特征是：用户明确表达了终止服务端 agent 执行的意图。处理方式为：useAgentChat hook 设置取消意图标记，通过独立的取消信令通道（cancel signal channel）向服务端 Durable Object 发送 CANCEL_TURN 消息，消息体携带当前 turn 的唯一标识符和发起取消的客户端实例标识符。取消信令与正常流式数据通道在逻辑上分离，可通过同一 WebSocket 连接的不同消息类型实现，也可通过独立的 HTTP endpoint 实现。

### 三、双模式设计：Durable 模式与 Request-Lifetime 模式

为满足不同应用场景对客户端断开与服务端任务取消之间关系的不同偏好，本方案设计了两种可配置的运行模式，通过 useAgentChat 的配置参数或全局 provider 配置进行切换。

Durable 模式（默认）：在此模式下，agent turn 的生命周期与客户端连接生命周期彻底解耦。Durable Object 中的 turn 一旦启动即独立运行至自然结束或被显式取消信令终止。客户端的任何被动断开事件均不影响服务端执行。当客户端重连时，通过会话标识符和 turn 标识符向 Durable Object 请求状态同步，Durable Object 返回已生成但未投递的消息流。此模式适用于长耗时 agent 任务（如代码生成、多步推理、数据分析），以及多标签页共享同一对话会话的场景。

Request-Lifetime 模式（可选）：在此模式下，agent turn 的生命周期绑定到发起请求的 HTTP 连接或 WebSocket 连接。当客户端连接因任何原因断开且超过配置的超时阈值（grace period，默认 30 秒）后，Durable Object 自动终止当前 turn。该模式通过将客户端的被动断开视为隐式取消意图来实现。实现方式为：Durable Object 在 turn 执行期间维护一个心跳计时器，每次收到客户端心跳或数据确认时重置；超时未收到任何信号时触发自动取消。此模式适用于对延迟敏感、不希望服务端持续消耗资源的场景。

### 四、服务端 Turn 取消仲裁器

服务端 Durable Object 内部引入 turn 取消仲裁器模块，负责统一处理来自不同客户端的取消信令，解决多标签页场景下的取消冲突。

取消信令的接收与验证：仲裁器收到 CANCEL_TURN 消息后，提取消息中的 turn_id 和 client_instance_id。首先验证 turn_id 是否对应当前正在执行的 turn——若 turn 已自然完成，则忽略该取消信令并返回 TURN_ALREADY_COMPLETED 状态。其次检查 client_instance_id 是否属于当前会话的活跃观察者集合。

多标签页取消投票机制：在 Durable 模式下，同一会话可能被多个标签页同时观察。当单个标签页发起取消时，不能立即终止 turn，因为其他标签页可能仍在等待结果。仲裁器维护每个 turn 的观察者集合（observer set）——即当前通过 WebSocket 连接并订阅该 turn 流的所有客户端实例。取消仲裁策略为：仅当观察者集合为空（所有标签页均已断开且无重连）或所有剩余观察者都发送了取消信令时，才真正终止服务端 turn。该策略通过一个简单的计数比较实现：active_observer_count > 0 且 cancel_vote_count >= active_observer_count 时执行取消。

工具调用 continuation 的取消处理：当 agent turn 正在等待工具调用结果（如函数调用返回）时收到取消信令，仲裁器根据工具调用的幂等性和副作用特征分类处理。对于纯查询类工具（无副作用），允许其继续完成并将结果缓存但不发送给客户端，turn 随后标记为已取消。对于有副作用的工具（如数据库写入、外部 API 调用），若工具调用已发出，则允许其完成以保证系统状态一致性，但在结果返回后不继续下一轮 LLM 推理，而是直接进入取消清理流程。仲裁器通过在工具调用记录中维护一个 cancellation_pending 标志位来实现这一机制。

### 五、关键处理流程

以下描述各关键场景下的完整处理流程，覆盖从客户端发起到服务端执行再到异常处理的端到端路径。

正常对话流程：用户输入消息→useAgentChat 构造请求体（含 session_id、parent_turn_id、消息内容）→通过 HTTP POST 或 WebSocket send 发送至服务端→Durable Object 创建新 turn 记录（状态为 RUNNING），启动 LLM 推理流→流式响应块通过 WebSocket 或 SSE 推送到客户端→客户端 reader 逐块读取并渲染→turn 完成时 Durable Object 将状态更新为 COMPLETED，持久化完整 turn 记录。

客户端被动断开与重连恢复流程：断开事件触发→useAgentChat 检测事件类型为被动断开→清理本地 reader 和 AbortController→本地状态标记为 disconnected，保留 session_id 和最后确认的消息偏移量（last_acked_offset）→启动重连定时器（指数退避，初始间隔 1 秒，最大 30 秒）→重连成功后发送 RESUME 消息，携带 session_id 和 last_acked_offset→Durable Object 收到 RESUME 后，从持久化的 turn 输出流中定位到 last_acked_offset 之后的数据，通过新连接续推→客户端无缝接续渲染。

用户主动取消流程：用户点击停止按钮→useAgentChat 设置取消意图标记为 true→通过取消信令通道发送 CANCEL_TURN 消息（含 turn_id、client_instance_id）→同时执行本地清理（关闭 reader、清理缓冲区）→Durable Object 仲裁器收到 CANCEL_TURN→验证 turn_id 有效性→检查多标签页观察者集合→满足取消条件时，设置 turn 状态为 CANCELLING→通知 LLM 推理循环终止→等待正在执行的工具调用完成（如适用）→将 turn 状态更新为 CANCELLED→持久化取消记录→向所有观察者广播 TURN_CANCELLED 通知。

迟到服务端消息处理流程：当 turn 已被取消（CANCELLED）或已完成（COMPLETED）后，服务端可能仍有延迟到达的消息（如工具调用结果、LLM 推理残片）。Durable Object 在处理每条出站消息前检查 turn 的终态状态：若 turn 处于 CANCELLED 或 COMPLETED，则丢弃该消息并记录日志，不向任何客户端推送。对于工具调用结果这类可能影响状态一致性的消息，若 turn 为 CANCELLED 但工具已在执行，则接收结果但仅用于更新 turn 的内部执行记录（execution log），不触发新的 LLM 推理轮次。

### 六、关键数据模型与接口

本方案涉及以下关键数据结构和接口定义，构成方案的核心可实施基础。

Turn 状态机：每个 agent turn 在 Durable Object 中维护严格的状态机，包含以下状态：PENDING（创建但未开始执行）、RUNNING（正在执行 LLM 推理或工具调用）、CANCELLING（收到取消信令，正在等待工具调用完成或执行清理）、CANCELLED（已取消，终态）、COMPLETED（正常完成，终态）。状态转换规则：PENDING→RUNNING（开始执行）；RUNNING→CANCELLING（收到有效取消信令）；RUNNING→COMPLETED（自然完成）；CANCELLING→CANCELLED（清理完成）。从 CANCELLED 和 COMPLETED 不允许任何后续状态转换。

取消信令消息结构：CANCEL_TURN 消息包含 turn_id（目标 turn 的唯一标识符）、client_instance_id（发起取消的客户端实例标识符，由 useAgentChat 在初始化时生成并持久化到 sessionStorage）、timestamp（取消发起时间戳，用于仲裁器判断信令的先后顺序）、reason（可选，取消原因枚举：USER_STOP、APP_CANCEL、TIMEOUT 等）。

useAgentChat 扩展接口：在现有 hook 基础上新增以下属性和方法。cancelIntent 属性（只读 boolean）：表示当前是否存在未完成的取消意图。stop() 方法：设置取消意图标记并通过取消信令通道发送 CANCEL_TURN，同时执行本地清理。配置项 cancellationMode：取值为 'durable'（默认）或 'request-lifetime'。配置项 reconnectConfig：包含 enabled、maxRetries、baseIntervalMs、maxIntervalMs。配置项 observerTimeoutMs：在 request-lifetime 模式下，服务端等待客户端重连的超时时间。客户端实例标识符 clientInstanceId 在 hook 初始化时生成，基于 crypto.randomUUID() 并写入 sessionStorage，同一标签页在刷新前后保持相同值。

### 七、技术效果

本方案相比现有 agent 对话管理方式，在以下方面产生实质性技术改进。

第一，语义精确的取消控制：通过将客户端断开事件明确分类为被动断开和主动取消，并在架构层面为两者提供不同的处理通道，解决了现有方案中浏览器刷新或组件卸载导致服务端任务被误终止的问题。取消意图通过专用信令通道传递，不与流式数据通道混合，保证了取消指令的可靠投递和语义完整性。

第二，无缝的流恢复能力：在 Durable 模式下，客户端断开后可通过会话标识符和消息偏移量实现精确的流恢复，用户感知不到中断。Durable Object 在 turn 执行期间持续缓冲输出流，重连客户端可以接续接收，避免了重复计算和消息丢失。

第三，多标签页场景的一致性保障：通过观察者集合和取消投票机制，避免了单标签页的取消操作影响其他标签页的正常使用。每个标签页拥有独立的 clientInstanceId，仲裁器基于观察者计数而非单一连接状态做决策，保证了分布式客户端环境下的取消语义正确性。

第四，工具调用 continuation 的安全处理：取消操作不会粗暴中断正在执行的工具调用，而是基于工具副作用特征做分类处理，既保证了系统状态一致性，又避免了对已发出外部请求的资源浪费。cancellation_pending 标志位机制使得工具调用结果返回后能立即停止后续推理，实现了精确的取消边界控制。

第五，开发者可配置的灵活性：通过 durable 和 request-lifetime 双模式设计，开发者可以根据应用场景选择最适合的取消策略，无需修改服务端执行逻辑。配置在客户端侧完成，服务端统一支持两种模式，降低了系统复杂度。

### 八、风险与待确认问题

本方案在实施过程中存在以下需要后续确认和关注的技术风险点。

Durable Object 持久化缓冲区的内存压力：在 Durable 模式下，若 turn 执行时间极长（如数十分钟）且输出流数据量大，Durable Object 需要在内存中缓冲全部输出流以支持后续重连恢复。需要评估是否引入溢出到持久化存储（如 Durable Object Storage）的机制，以及在何种阈值下触发溢出。

取消信令通道的可靠性：若取消信令通道与流式数据通道复用同一 WebSocket 连接，在连接断开后取消信令将无法送达。建议考虑为取消信令提供独立的、轻量级的 HTTP endpoint 作为备用通道（fallback channel），确保在 WebSocket 断开的情况下取消意图仍可达。

clientInstanceId 在浏览器刷新时的持久化策略：当前方案使用 sessionStorage 持久化 clientInstanceId，使得刷新前后同一标签页保持相同标识符。然而 sessionStorage 在部分浏览器行为（如复制标签页）下会复制到新标签页，导致两个标签页具有相同 clientInstanceId。需要评估是否需要结合 BroadcastChannel API 或服务端会话标识符分配机制来检测和解决此类冲突。

工具调用副作用分类的准确性：仲裁器对工具调用副作用的判断依赖开发者提供的工具元数据标注。若标注不准确（如有副作用的工具被误标为无副作用），cancel 操作可能导致状态不一致。建议在方案中增加工具响应状态码检查作为辅助判断依据，并要求关键工具提供补偿/回滚接口。
