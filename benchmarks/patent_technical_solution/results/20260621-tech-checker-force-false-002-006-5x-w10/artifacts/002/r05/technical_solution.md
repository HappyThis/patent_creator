## 技术方案

本方案面向外部系统通过 webhook、HTTP/RPC 或其他程序化方式提交一次对话任务的场景，在持久化 agent 实例内设置统一任务接收层、任务记录层、异步执行层、状态查询与控制层以及恢复处理层。外部请求到达后先完成认证、幂等判定和任务持久化，再快速返回已受理结果；后续由 agent 内部队列、对话轮次队列、可恢复流和持久化运行记录协同完成实际模型交互、状态推进、取消清理和中断恢复，从而避免外部连接生命周期直接决定服务端执行生命周期。

### 外部任务接收与持久化受理

外部任务接收层可以同时覆盖 webhook、普通 HTTP 请求、WebSocket RPC、同一 Worker 或 agent 之间的 Durable Object RPC 以及调度任务等入口。接收层从请求 URL、请求头、请求体或 RPC 参数中解析租户、会话、用户或业务实体标识，并据此将请求路由到对应的有状态 agent 实例，使同一实体的对话历史、任务状态和流式输出缓存在同一个持久化边界内维护。对于 webhook 入口，接收层先校验请求方法、签名、时间戳和权限范围；对于 RPC 或 callable 方法入口，接收层校验参数是否 JSON 可序列化且符合任务创建规则。

每个外部提交被规范化为一条对话任务记录。该记录至少包含任务标识或幂等键、外部请求标识、目标会话标识、待写入的用户消息或系统事件、当前状态、创建时间、更新时间、取消标记、清理标记以及与内部对话轮次关联的 requestId。任务记录先写入 agent 的持久存储，再返回 accepted 或等价的受理响应；受理响应携带任务标识、查询地址或 RPC 查询参数，而不等待模型推理完成。这样，即使外部调用方超时、断线或 webhook 平台重试，服务端仍可以依据持久任务记录继续执行或返回已有任务状态。

### 幂等提交与重复任务去重

为处理 webhook 重试、RPC 调用重放和客户端重复点击等重复提交，任务接收层对外部任务标识或由请求内容计算出的幂等键设置唯一约束。若同一键首次出现，则创建任务记录并进入待执行状态；若同一键再次出现，则不重新生成对话轮次，而是读取已有任务记录并返回其当前状态、内部 requestId、已产生的消息或流式输出位置。该去重发生在业务任务记录层，而不是依赖内部异步队列自动识别外部 taskId，从而避免队列项采用内部随机标识时产生重复执行。

当任务需要写入对话消息时，消息持久化以服务端存储中的权威 transcript 为准。程序化提交可以采用在轮次实际开始时基于最新消息列表派生新增消息的方式，避免后台 webhook 或 schedule 在排队等待期间使用陈旧基线；对于已存在的 assistant 消息、工具调用结果或短回复内容，可结合 exact message id、内容键以及 toolCallId 等合并规则进行 reconciliation，防止客户端与服务端消息标识不一致导致重复数据库行或重复工具结果。

### 异步执行与对话轮次串行化

受理完成后，任务执行与外部请求连接解耦。任务记录可以被投递到 agent 内部队列、由调度器延迟触发，或者直接调用程序化对话接口进入对话轮次。内部队列的队列项持久保存在 agent 存储中，按照创建时间顺序取出，并可携带重试选项；执行成功或重试耗尽后，队列项被出队。对于需要跨休眠和重启保持执行意图的长任务，可以通过可恢复运行记录登记运行名称和 checkpoint，并在关键阶段写入 snapshot。

同一会话内的模型推理和流式回复通过对话轮次队列串行化，防止两个程序化提交同时改写同一 transcript 或同时占用同一 stream。每个轮次关联一个 requestId，并在进入队列时记录当前 generation；如果会话被清空或重置导致 generation 变化，旧 generation 下尚未执行的轮次在到达队首时被识别为 stale 并跳过。对于高并发提交，接收层可以按业务需要选择 queue、latest、merge、drop 或 debounce 等接纳策略：queue 保留全部任务顺序，latest 或 debounce 适合只保留最新意图，merge 适合把短时间内的多个外部事件合并为一次对话输入。

执行对话轮次时，系统先持久化待提交消息，再调用对话处理函数生成模型响应；流式响应 chunk 被写入可恢复流存储，完成后形成最终 assistant 消息并更新任务终态。程序化 saveMessages 或 continueLastTurn 可以接受外部 AbortSignal，内部将该信号桥接到当前 requestId 的 abort controller；轮次结束后释放 controller，并通过 onChatResponse 或等价完成回调把 completed、error 或 aborted 结果写回任务记录。

### 任务状态查询与状态推导

状态查询接口通过 HTTP、RPC 或 callable 方法接收 taskId 或幂等键，并从持久任务记录与内部执行痕迹共同推导任务状态。任务记录中的状态字段用于表达 accepted、queued、running、streaming、completed、failed、cancelled、interrupted、cleaned 等业务可见阶段；队列项是否存在、对话轮次队列的 active requestId、可恢复流 metadata 的 streaming/completed/error 状态、已持久化消息、运行 snapshot 以及完成回调结果用于校正该状态。

| 阶段 | 进入条件 | 主要处理 | 可见结果 |
| --- | --- | --- | --- |
| accepted | 外部请求通过认证且幂等键首次创建 | 写入任务记录并返回受理响应 | 返回 taskId 或查询参数 |
| queued | 任务已投递内部队列或等待对话轮次 | 保留队列项、重试选项和 requestId 关联 | 查询显示等待执行 |
| running/streaming | 轮次开始并产生模型处理或流式输出 | 持久化消息、stream metadata 和按序 chunk | 可查询进度或恢复读取 |
| completed | 模型响应完成且最终消息已写入 | 写回终态、结果摘要和完成时间 | 重复查询返回同一结果 |
| failed | 执行抛出非取消错误或重试耗尽 | 记录错误类别、是否可重试和失败原因 | 不再自动重复执行，除非外部重新提交新任务 |
| cancelled/aborted | 外部显式取消或运行中 abort signal 生效 | 停止未开始任务或中止当前轮次并记录原因 | 查询返回确定取消结果 |
| interrupted | 休眠、重启或驱逐后存在未完成痕迹但尚未确定终态 | 根据 snapshot、stream chunks 和消息记录恢复、续写或标记终止 | 查询显示恢复中或中断结果 |
| cleaned | 终态任务超过保留条件或被显式清理 | 删除临时队列、流和运行索引，保留终态摘要及幂等键 | 重复提交仍命中已处理记录 |

当查询命中已完成任务时，接口返回终态、最终 assistant 消息摘要、错误类别或取消原因；当查询命中 streaming 任务时，接口返回 streamId、当前已持久化 chunk 位置或可重连恢复所需的信息；当查询命中 queued 或 running 任务时，接口返回排队或执行中的状态而不重复触发执行。若任务记录存在但内部队列项、流记录和运行记录均已清理，则以任务记录的终态和清理标记作为查询结果，保证外部系统能够得到稳定、可重复的应答。

### 取消、停止与资源清理

取消接口根据 taskId 找到对应 requestId、队列项和任务记录。若任务尚未开始执行，则设置取消标记并删除或跳过相应队列项；若任务已经进入对话轮次，则通过 requestId 定位内部 abort controller，或通过程序化提交时预先传入的 AbortSignal 中止模型推理循环。取消完成后，任务记录写入 cancelled 或 aborted 状态，并保留必要的 partial chunks、已持久化消息和取消原因，以便外部系统查询到确定结果。

客户端断开、浏览器本地流清理或普通 fetch abort 不自动等同于服务端取消。默认情况下，服务端 durable turn 可以继续运行并缓存后续 chunks，客户端重连后再恢复读取；只有外部系统显式调用 cancel/stop，或在接收层配置为客户端 abort 同步取消服务端轮次时，才向服务端发送取消意图。这样可以区分“调用方暂时失联”和“业务上不再需要该任务”，避免因网络波动造成误取消。

清理操作与取消操作分离。终态任务可以按保留策略删除过期流式 chunk、stream metadata、内部队列索引和临时运行记录，同时保留任务记录的终态摘要和幂等键，以便后续重复提交仍能命中已处理结果。对于会话清空或强制重置场景，系统推进对话轮次 generation、销毁所有 abort controller、清空 pending continuation 和 submit concurrency 状态，使旧轮次自然失效，并避免重置前的排队任务继续改写清空后的会话。

### 休眠、重启与执行中断恢复

agent 休眠或运行时重启后，恢复处理层首先从持久存储加载 state、任务记录、消息 transcript、内部队列、调度记录、可恢复流 metadata/chunks 以及未完成运行记录。对于 accepted 但未入队的任务，重新投递执行；对于 queued 任务，沿用持久队列顺序继续处理；对于 running 或 streaming 任务，依据 requestId、streamId、partial chunks 和运行 snapshot 判断其是可继续、已完成但未写终态，还是需要标记为 interrupted。

长对话轮次可以运行在可恢复 fiber 中。fiber 启动时写入运行记录，执行过程中通过 checkpoint 写入 snapshot；若 Durable Object 因空闲、部署更新或运行时重启在中途被驱逐，下一次激活时恢复处理层读取 orphaned run，构造恢复上下文并调用 onFiberRecovered。对于 AI 对话轮次，内部 chat fiber 可以映射到 onChatRecovery：恢复上下文包含 requestId、streamId、partialText、partialParts、最近消息、上次 body、客户端工具信息和 checkpoint 数据；恢复策略可以选择持久化部分回复、停止继续，或在状态稳定后调用 continueLastTurn 续写最后一条 assistant 消息。

可恢复流负责在断线和重启之间维持输出一致性。每个 stream 保存 requestId、状态和按序 chunk；客户端重连时按 chunk_index 顺序回放已持久化数据，再接续 live stream。若恢复时发现流已失去 live producer，则可将 orphaned chunks 固化为 partial assistant message 并完成该 stream，随后由恢复策略决定是否基于完整 transcript 继续生成。由于 partial 输出、最终消息和任务终态都以持久记录为准，外部系统在重复查询或重新连接时不会看到互相矛盾的结果。

### 入口适配与扩展方式

上述机制不限定外部系统的接入形式。webhook 适合由第三方事件平台触发，HTTP API 适合服务端轮询或回调式集成，WebSocket RPC 或 callable 方法适合浏览器、移动端和内部服务发起可交互任务，schedule 或 queue 适合由 agent 自身定时补偿、重试或发起后台任务。各入口只负责把外部事件规范化为统一任务记录，后续均使用同一套幂等、串行化、状态查询、取消清理和恢复机制。
