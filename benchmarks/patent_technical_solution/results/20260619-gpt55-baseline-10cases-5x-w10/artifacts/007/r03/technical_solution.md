## 技术方案

### 总体构思与控制边界

本方案在浏览器端流式会话、服务端 agent turn 以及可恢复流之间增加“取消意图判定层”，将客户端连接或读取资源的本地清理，与服务端正在执行的 agent turn 取消解耦。浏览器刷新、组件卸载、路由切换、ReadableStream reader 的 cancel、WebSocket 短暂关闭等事件，首先被归类为观察端生命周期事件，仅用于关闭当前页面持有的本地流控制器、事件监听器和请求标识；只有用户点击停止、应用逻辑主动取消、清空会话或外部 AbortSignal 明确触发时，才生成可传输的取消意图并发送至服务端。

服务端以每个 chat request 的请求标识作为 agent turn 的控制键，为该请求建立独立的取消控制器，并将对应 AbortSignal 传入 onChatMessage、流式模型调用、工具调用延续和程序化 saveMessages 等执行路径。流式输出侧继续使用可恢复流管理器记录 stream id、request id、分块序号和流状态，将输出分块持久化或缓存后广播给连接端。因此，连接是否存在不再直接决定 turn 是否继续执行；turn 的执行状态由请求标识和服务端控制器管理，流的可观察状态由 stream id 和恢复协议管理。

### 客户端断开事件的本地化处理

客户端传输层为每次 sendMessages 生成 request id，并维护本标签页发起或正在接管的活动请求集合。接收到服务端分块时，只有与当前 request id 匹配且仍由本地传输层负责的分块才写入 ReadableStream；当本地读取链路结束或页面连接关闭时，默认仅执行本地 finish 操作，包括关闭或报错本地 ReadableStream、移除 message/close 监听器、释放本地 AbortController、清理本地活动请求集合中的观察关系。该本地 finish 不自动发送 chat-request-cancel 消息，从而避免把刷新、卸载或网络抖动误判为用户希望终止服务端任务。

为区分不同原因的 reader cancel，本地流增加取消原因或策略参数。若取消原因属于“observer-detach”类，例如组件卸载、页面隐藏后释放资源、路由切换、Socket close 或恢复握手超时，则只断开本地观察端；若取消原因属于“intentional-stop”类，则进入显式取消路径。对于无法提供原因的运行环境，可以设置默认策略，例如 retain-on-disconnect、cancel-on-disconnect 或 grace-period-retain；其中 grace-period-retain 在短暂断开后的宽限期内保留服务端 turn，超过宽限期且没有任何观察端恢复时再按应用配置处理。

### 显式取消意图的服务端传递

当用户点击停止按钮、应用调用 stop、清空会话、父级任务取消或工具延续被主动中止时，客户端构造包含 request id 和取消类型的控制帧并通过当前 WebSocket 发送至服务端。服务端收到该控制帧后，使用 request id 在取消注册表中查找对应 AbortController 并执行 abort，同时记录取消事件。由于取消注册表与连接对象分离，即使发送取消指令的标签页不同于最初发起请求的标签页，只要其具备同一会话上下文和 request id，也可以表达针对同一 agent turn 的取消意图。

服务端执行体在可中断位置消费 AbortSignal：流式模型调用传入该信号以停止 token 产生，服务端工具或子 agent 调用通过外部信号链接到同一请求控制器，客户端工具结果触发的 continuation 也沿用新的 continuation request id 建立控制器并与父级信号关联。被取消后，服务端停止继续读取上游流或继续调度后续工具，向相关观察端发送终止分块或 done 消息，并在 finally 路径删除对应控制器，避免取消控制器在长会话中泄漏。

### 恢复、多观察端与可配置策略

恢复机制通过 stream resume request、stream resuming、resume ack 和 resume none 等协议帧完成。新的或重新挂载的浏览器实例先向服务端询问是否存在活动流；服务端若存在可恢复流，则返回正在流式输出的 request id 或 stream id，客户端将该 request id 加入本地接管集合并发送确认，随后接收历史分块重放和实时分块；服务端若不存在活动流，则返回无可恢复流，客户端结束本次恢复尝试。该流程使短暂断线、刷新后的页面或另一个标签页能够重新观察仍在运行的 agent turn，而不是因原 reader cancel 而迫使服务端提前中断。

多标签页场景下，服务端可将流式分块广播给多个观察端，但取消语义仍按策略集中判定。可配置项至少包括：断开是否默认保留服务端 turn、保留宽限时间、无观察端时是否继续执行、显式停止是否允许跨标签页生效、工具 continuation 是否随父 turn 取消。通过上述配置，同一框架既可支持“刷新后继续生成”的长任务体验，也可支持对资源敏感应用在断开后尽快释放服务端任务；同时，显式取消仍能沿 request id 精确传达到服务端，兼容流式响应恢复、工具调用继续执行、子 agent 调用和多观察端接管。
