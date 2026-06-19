## 技术方案

### 总体构思：取消意图与本地流清理解耦

本方案在长时间 agent 对话的客户端传输层与服务端 turn 执行层之间设置取消控制层，将“本地观察端停止接收流”与“服务端终止本次 agent turn”拆分为两个不同动作。客户端的 reader 关闭、组件卸载、页面切换、浏览器刷新、WebSocket 短暂断开等事件，默认仅触发本地 detach 操作：释放当前页面持有的事件监听器、ReadableStream 控制器和 UI streaming 状态，但不向服务端发送终止 turn 的取消消息。只有用户明确点击停止按钮、应用业务逻辑主动调用取消接口，或策略管理器判定该事件属于显式取消时，才通过独立取消通道向服务端发送携带 request id 的取消指令。

取消控制层维护每个 agent turn 的 request id、stream id、当前观察端集合、恢复状态和取消原因。服务端 turn 在收到请求后进入可恢复执行状态，流式输出被写入可重放的 chunk 存储并同时广播给已连接观察端；观察端断开只改变连接集合，不改变 turn 的执行状态。由此，网络连接的生命周期、前端组件的生命周期和 agent turn 的生命周期彼此解耦，避免把前端清理动作误解释为用户希望停止模型生成或工具执行。

### 客户端取消原因分类与策略判定

客户端对导致本地流结束的原因进行分类，并为不同原因选择不同动作。第一类为显式取消，包括用户点击停止、应用调用 cancelTurn 或 stopWithCancel、父级任务明确撤销等，处理结果是发送服务端取消消息并关闭本地流。第二类为本地清理，包括 reader.cancel、React 组件卸载、路由切换、刷新前清理、AI SDK 因实例重建而取消旧 reader 等，处理结果是 detach-only，即仅关闭当前 reader 和监听器并保留 request id。第三类为连接异常，包括 WebSocket close、短暂网络抖动、标签页休眠恢复等，处理结果是进入可恢复等待窗口，在窗口内不触发服务端取消。

在一种实现中，传输层为 abortSignal、ReadableStream.cancel、WebSocket close 和 stop 按钮分别生成内部取消原因码。abortSignal 不再天然等同于服务端取消，而是先交由策略管理器判定：若该 abortSignal 来自显式 stop 或应用主动取消，则发送取消指令；若其来源是组件重建或本地 reader 释放，则仅本地报错或关闭 reader。ReadableStream.cancel 默认被视为消费者停止读取，不发送服务端取消；WebSocket close 默认被视为观察端离线，仅清理本连接上的 pending resolver 和事件监听器。

策略管理器可输出三种结果：一是 detach，表示仅本地清理并等待恢复；二是 cancel，表示向服务端发送取消消息并终止该 request id 对应的 turn；三是 defer-cancel，表示先 detach 并启动宽限计时，若在宽限期内没有新的观察端恢复或业务侧仍保持取消意图，再升级为 cancel。该分类使短暂断网和刷新不会立即损失服务端进度，同时为需要严格节约算力的部署保留按策略取消的空间。

### 服务端 turn 生命周期与显式取消通道

服务端以 request id 作为 turn 级取消和恢复的统一索引。每个进入执行的 turn 创建独立的 AbortController，并把该 abortSignal 传递给模型调用、工具调用编排和用户自定义消息处理逻辑。服务端收到显式取消消息后，只取消对应 request id 的 AbortController，并产生 message:cancel 等内部事件；未收到该消息时，即使所有客户端连接暂时断开，turn 仍按原有执行链路继续推进，持续写入流式 chunk 和必要的消息状态。

显式取消通道与恢复通道相互独立。取消消息采用 client-to-server 的控制帧，至少携带 request id，并可附带取消原因、发起端标识和时间戳；恢复消息采用 stream resume request 与 resume ack 的握手，携带或返回当前 active request id。服务端只有在取消通道收到合法 request id 时才触发 abortRegistry.cancel；恢复请求、连接关闭、观察端 ACK 超时或 pending resume 清理均不直接触发 abort，从而防止恢复协商失败被误判为用户取消。

为避免取消后仍向前端混入旧输出，服务端在 turn 状态中记录取消态和完成态。若取消已经生效，服务端可以停止继续读取模型响应，终止后续工具 continuation，并向相关观察端广播 done 或 error 结束帧；若取消消息到达时 turn 已完成，则该消息被视为幂等清理，不回滚已经持久化的消息。该机制保证显式取消能够传达到服务端，同时不破坏已完成或正在恢复的流式响应一致性。

### 恢复、工具 continuation 与多观察端协同

恢复协同基于持久化流式 chunk 与握手机制完成。服务端在 turn 执行期间为 active stream 记录 stream id、request id、chunk index 和流状态，并将输出 chunk 批量写入本地持久化存储。客户端重新连接后先注册消息处理器，再发送恢复请求；服务端若存在 active stream，则返回 stream resuming 并把该连接标记为 pending resume，使其在 ACK 前不接收 live chunk；客户端发送 resume ack 后，服务端按 chunk index 重放已存储 chunk，并在 replay complete 后继续推送 live chunk。

工具 continuation 场景沿用相同的 turn 生命周期控制。客户端提交工具结果或审批结果后，可先创建一个延迟的 continuation stream，使 UI 进入提交状态并等待服务端宣布新的可恢复流；在服务端启动 continuation stream 之前，客户端本地取消仅清理等待中的 resolver，不取消尚未开始的服务端执行；在 continuation 已绑定 request id 后，只有显式 stop 或策略判定为 cancel 的事件才发送取消消息。这样可以避免工具调用已经在服务端继续执行时，因页面切换或 reader 释放导致工具 continuation 被非预期中断。

多标签页或多观察端同时存在时，服务端把连接视为观察者而不是 turn 所有者。live chunk 向已就绪连接广播，处于 pending resume 的连接暂时排除在广播之外，待其完成 replay 后再加入 live 流；一个标签页关闭只移除该连接的 pending 状态和 continuation 等待状态，不影响其他标签页继续观察，也不触发服务端 abort。若某一标签页显式停止，则取消消息按 request id 作用于服务端 turn，其他标签页通过结束帧或状态广播同步感知该 turn 已被取消。

### 可配置策略与边界条件

取消策略可以按应用场景配置。默认策略为 explicit-only，即只有用户明确停止或应用主动取消才终止服务端 turn；detach-only 可用于希望最大化恢复成功率的长任务；cancel-on-disconnect 可用于对资源消耗敏感且不需要断线恢复的场景；grace-period cancel 可设置数秒至数分钟的宽限期，在所有观察端断开后仍允许短时间恢复，超时后再由服务端或客户端控制层发起取消。策略还可以按请求类型区分，例如普通短回复采用较短宽限期，长工具链或后台 agent turn 采用显式取消优先。

本方案还设置若干边界处理：恢复请求若发现没有 active stream，可返回 resume none 并由客户端回到非流式观察状态；恢复到已完成 request id 时，可重放已完成 chunk 并发送 done；服务端重启后若仅能恢复到持久化 chunk 而无法恢复 live reader，可将该流标记为孤儿流，重放已有 chunk 后完成并持久化部分消息；历史清空或会话销毁时，才清除对应 stream metadata、chunk 和 pending resume 状态。上述边界均不把连接断开直接等同于取消，只有显式取消或配置策略升级后的取消才会终止服务端执行。
