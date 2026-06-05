## 技术方案

本技术方案提出一种客户端本地清理与服务端 agent turn 取消之间的解耦控制方法。通过在客户端引入取消意图分类器和可配置的信号映射策略，将浏览器刷新、组件卸载、网络波动等隐式断连场景与用户点击停止按钮等显式取消场景区分开来，使前者仅执行本地资源释放而不中断服务端 turn 执行。服务端通过 AbortRegistry 取消原因标记扩展、TurnQueue 生命周期管理、ResumableStream 流式缓冲重放以及协议扩展，在保持现有流式恢复、多标签页观察和工具调用自动续接能力的同时，实现取消意图的精确传达与可恢复 turn 的保护。

### 整体架构

本方案在客户端与服务端之间引入一层取消意图分类与信号映射机制，将客户端本地资源清理（如连接断开、reader 取消、组件卸载）与服务端 agent turn 的取消决策解耦。系统由三个核心部分组成：（1）客户端侧的取消意图分类器，负责识别触发源并决定是否向服务端发送取消指令；（2）可配置的信号映射策略，将不同触发条件映射为本地清理或远程取消行为；（3）服务端侧的 Turn 生命周期管理器，在 AbortRegistry、TurnQueue、ResumableStream 和 ContinuationState 的配合下，区分客户端断连与用户主动取消，保障可恢复 turn 不被误中断。

### 取消意图分类机制

取消意图分类器运行于客户端侧，拦截所有可能触发取消行为的信号源，并根据信号来源将其归类为“隐式断连”或“显式取消”。隐式断连包括：浏览器页面刷新或关闭触发的 WebSocket 断开、前端组件卸载导致的 AbortController abort、流式读取器的 reader.cancel() 调用、以及短暂的网络波动引起的连接中断。这些场景不代表用户希望终止服务端正在执行的 agent turn，因此分类器将其标记为“仅本地清理”，即释放客户端侧的 reader、AbortController 和 WebSocket 连接资源，但不向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息。显式取消包括：用户点击界面上的“停止生成”按钮、应用层调用专用的 cancel() API、或外部系统通过 AbortRegistry.linkExternal() 桥接的特定 AbortSignal。这些场景明确表达了用户或调用方的取消意图，分类器将其标记为“远程取消”，触发向服务端发送取消指令。

### 客户端可配置信号映射策略

信号映射策略以配置表的形式定义不同触发条件与处理行为之间的对应关系。每条策略规则包含三个要素：触发源类型、分类标签和响应动作。触发源类型涵盖 readerCancel、abortSignal、websocketClose、pageUnload、explicitStop 等枚举值；分类标签标识该触发源被归类为隐式断连还是显式取消；响应动作决定执行本地清理还是同时向服务端发送取消指令。系统提供默认策略配置：readerCancel 和 abortSignal（组件卸载场景）标记为隐式断连、仅本地清理；explicitStop 标记为显式取消、发送远程取消指令；websocketClose 在未携带用户主动关闭标识时视为隐式断连；pageUnload 在页面可见性变化且无显式取消记录时视为隐式断连。该策略表支持部署方按业务需求覆盖，例如将特定场景的 websocketClose 也映射为远程取消。

### 服务端解耦与Turn生命周期管理

服务端在 AbortRegistry 中为每个请求的 AbortController 附加取消原因标记，区分“未取消”“用户主动取消”和“客户端断连”三种状态。当收到 CF_AGENT_CHAT_REQUEST_CANCEL 消息时，AbortRegistry.cancel() 将对应 controller 标记为用户主动取消，终止当前 turn 的执行。当客户端仅执行本地清理而未发送取消消息时，服务端不感知断连事件——ResumableStream 继续将 agent 生成的流式 chunk 写入 SQLite 缓冲区，TurnQueue 中的当前 turn 不受影响地执行至完成或工具调用等待状态。服务端通过心跳或流式写超时检测到客户端长时失联后，可选择将 turn 转入挂起状态而非取消，保留恢复能力。

### 与流式恢复、多标签页观察及工具续接的兼容设计

本方案的解耦设计与现有流式恢复、多标签页观察和工具续接机制协同工作。当客户端因隐式断连执行本地清理后重新连接时，ResumableStream.replayChunks() 通过 STREAM_RESUMING / STREAM_RESUME_ACK 握手协议将缓冲的 chunk 重放给客户端，确保用户不会丢失断连期间已生成的内容。多标签页场景下，BroadcastStreamState 状态机管理各标签页的连接状态：当标签页 A 因页面刷新触发隐式断连时，标签页 B 可继续观察同一 turn 的流式输出；仅当所有观察标签页均断开且超过阈值时间后，服务端才考虑将 turn 挂起。工具调用触发的自动续接（ContinuationState）不受客户端连接状态影响——工具执行完成后，TurnQueue 照常将续接任务入队，生成的内容由 ResumableStream 缓冲，待客户端恢复连接后重放。

### AbortRegistry取消原因标记扩展

AbortRegistry 在原有 getSignal、cancel、remove、linkExternal、destroyAll 方法基础上增加取消原因标记能力。每个 AbortController 关联一个 CancelReason 枚举值：NONE（未取消）、USER_INTENT（用户主动取消）、CLIENT_DISCONNECT（客户端断连）、TIMEOUT（超时挂起）。cancel() 方法新增可选的 reason 参数，调用方在发送 CF_AGENT_CHAT_REQUEST_CANCEL 时传入 USER_INTENT，服务端内部超时检测触发 cancel 时传入 TIMEOUT。linkExternal() 桥接外部 AbortSignal 时，支持传入 intent 标签以区分该外部信号属于隐式断连还是显式取消。TurnQueue 在执行每个 turn 前检查 AbortRegistry 中关联 controller 的取消原因：若为 USER_INTENT，立即中止 turn 并清理资源；若为 CLIENT_DISCONNECT 或 TIMEOUT，保留 turn 的执行结果和 ResumableStream 缓冲区，标记 turn 为可恢复状态。

### 协议扩展

在现有协议常量 CHAT_REQUEST_CANCEL、STREAM_RESUMING、STREAM_RESUME_ACK 等基础上，新增以下协议元素以支持解耦控制：（1）CANCEL_INTENT 字段，附加在 CHAT_REQUEST_CANCEL 消息中，取值为 USER 或 SYSTEM，标识取消来源是用户主动操作还是系统自动触发；（2）TURN_SUSPENDED 服务端到客户端通知，当 turn 因客户端长时断连被挂起（而非取消）时发送，携带 turnId 和恢复令牌；（3）TURN_RESUMABLE 状态查询消息，客户端重连后可主动查询是否存在可恢复的挂起 turn。这些协议扩展与现有 STREAM_RESUMING 握手流程正交：流恢复负责 chunk 重放，turn 状态管理负责告知客户端当前 turn 是运行中、已挂起还是已取消，两者通过独立的协议消息类型分别处理。

### 客户端传输层改造

在客户端 WebSocketChatTransport.sendMessages() 中，原有的 onAbort 回调被改造为经过取消意图分类器的间接调用。sendMessages 方法接收一个可选的 CancelPolicy 配置对象，其中包含信号映射策略表。当 reader.cancel()、AbortSignal abort 事件或 WebSocket close 事件触发时，分类器首先检查策略表，确定当前触发源对应的分类标签。若为隐式断连，仅执行 reader.releaseLock()、AbortController 清理和本地状态重置，不调用 sendCancelMessage()；若为显式取消，则构造携带 CANCEL_INTENT: USER 的 CHAT_REQUEST_CANCEL 消息并通过 WebSocket 发送。此外，在页面 beforeunload 事件和 visibilitychange 事件（页面隐藏）中，分类器结合用户最近的交互记录判断：若在阈值时间内（默认 500ms）检测到用户点击停止按钮，则标记为显式取消；否则仅记录隐式断连标记供重连时查询。
