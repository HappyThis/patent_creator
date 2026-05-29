## 技术方案

本方案针对 @cloudflare/ai-chat 项目中 useAgentChat 在长时间 agent 对话和浏览器连接变化场景下的控制体验问题，提出一种区分「客户端本地生命周期清理」与「服务端 turn 取消」的改进设计。核心思想是将当前 stop() 方法无条件向服务端发送取消指令的行为，解耦为两种独立操作：本地清理（仅终止客户端流消费和释放本地资源）和服务端取消（通过 WebSocket 发送 CF_AGENT_CHAT_REQUEST_CANCEL 真正中断服务端推理），并为 useAgentChat 引入 durable 模式（默认）与 request-lifetime 模式两种策略，使用户在不同场景下获得精确的控制能力。

### 技术问题概述

当前 useAgentChat 的 stop() 方法在调用链路中始终向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，触发服务端 AbortRegistry.cancel(id)，进而 abort 掉对应的 AbortController，导致服务端推理被中断。这一行为在用户主动点击「停止」按钮时是合理且必要的，但在以下场景中产生了不希望的技术后果：

- 浏览器标签页刷新或关闭：组件卸载导致 React Effect 清理逻辑触发 stop()，服务端正在执行的长推理被意外中止，用户刷新后看到的对话是截断的。
- React 组件卸载（路由切换、条件渲染）：同样的清理逻辑触发 stop()，打断了本应继续在服务端执行并持久化结果的 turn。
- ReadableStream reader.cancel()：AI SDK 底层流取消会触发 onAbort 回调，向服务端发送 cancel，这本质上是客户端消费终止而非用户意图取消。
- WebSocket 连接断开：虽然当前实现中 disconnect 本身不发送 cancel（因为连接已断），但如果断线后在重连前客户端触发了清理，仍存在误取消的风险。
- 多标签页场景：一个标签页的组件卸载不应影响其他标签页正在接收的流式响应，但当前 stop() 的广播式 cancel 会影响所有标签页的同一 turn。

### 核心技术方案：连接生命周期与 Turn 生命周期的解耦

本方案的核心创新在于将当前 stop() 的单一行为拆分为两个正交操作：本地清理（localCleanup）和服务端取消（serverCancel），并引入模式参数控制二者在自动触发场景下的组合关系。

定义两个基础操作：localCleanup 仅在客户端执行，调用 reader.cancel() 终止本地流消费，将 transport 内部状态复位，但不向服务端发送任何取消消息，也不从 activeRequestIds 中移除 requestId（保留 keepId 语义，使后续到达的 in-flight chunk 仍能被正确跳过）。serverCancel 在客户端执行 localCleanup 的基础上，向服务端发送 CF_AGENT_CHAT_REQUEST_CANCEL 消息，触发 AbortRegistry.cancel(id) 中止服务端推理，并等待服务端广播 done:true 后从 activeRequestIds 中清理。

基于上述两个操作，useAgentChat 新增 mode 选项，支持两种模式。durable 模式（默认）：浏览器刷新、组件卸载、reader cancel、WebSocket 断开等自动触发的生命周期事件仅执行 localCleanup，不取消服务端 turn；仅用户显式调用 stop() 时才执行 serverCancel。这对应于「对话持久化优先」的语义——服务端推理结果应在可能的情况下完整执行并持久化到 SQLite，客户端随时可以通过重连（resume 协议）恢复接收。request-lifetime 模式：保持当前行为，所有触发 stop() 的路径（包括组件卸载和刷新清理）均执行 serverCancel，确保请求生命周期与组件生命周期严格绑定，适用于需要精确控制 token 消耗或避免服务端计算浪费的场景。

### 关键模块

本方案涉及的改造集中在客户端侧，服务端 AbortRegistry 和现有的 resume/resumable stream 机制保持不变。以下分三个模块描述改造要点。

一、useAgentChat 抽象层（react.tsx）。在 UseAgentChatOptions 中新增 mode 选项，类型为 'durable' | 'request-lifetime'，默认值为 'durable'。hook 内部维护一个区分触发来源的停止逻辑：用户显式调用 stop()（通过 UI 按钮）标记为 intentionalStop，始终执行 serverCancel；而组件卸载（useEffect cleanup）、路由切换等自动生命周期事件，在 durable 模式下仅执行 localCleanup。具体实现上，利用已有的 stopWithToolContinuationAbort 封装，在其中增加 mode 判断分支：durable 模式下，非 intentional 路径跳过 WebSocketChatTransport 的 abort 回调调用转而仅取消本地 reader。

二、WebSocketChatTransport 传输层（ws-chat-transport.ts）。在现有 onAbort 回调（sendMessages 方法内部）中增加 localOnly 参数。当 localOnly 为 true 时，跳过发送 CF_AGENT_CHAT_REQUEST_CANCEL 的步骤，但仍执行 finish() 以终止 ReadableStream 并将 streamController.error(abortError)；keepId 保持为 true，使 activeRequestIds 中的 requestId 暂不清除，等待服务端自然完成后的 done:true 广播来最终清理。当 localOnly 为 false（或未设置，保持向后兼容）时，执行完整的 onAbort 流程（发送 cancel + finish with keepId）。新增方法 abortLocalOnly(requestId: string)，由 useAgentChat 在 durable 模式下的自动清理路径中调用。

三、服务端 AbortRegistry 与 AIChatAgent（index.ts / agents/src/chat/abort-registry.ts）。服务端无需任何改造。AbortRegistry 仅响应 CF_AGENT_CHAT_REQUEST_CANCEL 消息触发 cancel(id)；若客户端在 durable 模式下不发送该消息，服务端 turn 正常执行至完成。现有的 abortSignal 链路（abortSignal → reader.cancel() → 广播 done:true）和 resume/resumable stream 机制（SQLite chunk 缓冲 + STREAM_RESUMING 握手）保持不变。服务端在 turn 完成后照常广播 done:true，客户端收到后从 activeRequestIds 中清理对应的 requestId。

### 处理流程

以下描述三种典型场景下的完整处理流程。

场景一：durable 模式下浏览器刷新。用户在 durable 模式下发送了一条长推理请求，服务端正在流式生成。用户刷新浏览器标签页。React 组件卸载触发 useEffect cleanup → useAgentChat 检测到 mode='durable' 且触发来源为自动生命周期事件（非用户显式 stop）→ 调用 WebSocketChatTransport.abortLocalOnly(requestId)：仅终止本地 ReadableStream，不向服务端发送 cancel。服务端继续推理至完成，将 chunks 缓冲到 SQLite（ResumableStream），最终广播 done:true。用户刷新后页面重新加载，useAgentChat 通过 resume 协议（STREAM_RESUME_REQUEST → STREAM_RESUMING → STREAM_RESUME_ACK）恢复接收已完成的流式结果，对话完整保留。

场景二：用户显式点击「停止」按钮。无论何种模式，用户点击停止按钮触发 intentionalStop → useAgentChat 调用 stop() → WebSocketChatTransport.onAbort(localOnly=false) → 发送 CF_AGENT_CHAT_REQUEST_CANCEL（含 requestId）→ 服务端 AbortRegistry.cancel(id) → abortSignal 触发 → reader.cancel() → 广播 done:true。客户端在收到 done:true 后从 activeRequestIds 中清理 requestId。这与当前行为完全一致。

场景三：request-lifetime 模式下组件卸载。用户选择了 request-lifetime 模式发送请求，中途通过路由切换离开当前对话视图。组件卸载触发清理 → useAgentChat 检测到 mode='request-lifetime' → 无论触发来源，均执行 serverCancel 完整流程：发送 CF_AGENT_CHAT_REQUEST_CANCEL 到服务端，中止推理。这保持了当前 stop() 的行为语义，确保请求生命周期严格跟随组件生命周期。

### 与现有机制兼容

本方案设计为对现有机制的最小侵入增强，与以下已有能力保持兼容。

流式响应与断线重连：durable 模式下本地清理不取消服务端，意味着 ResumableStream 中的 chunk 缓冲（SQLite）继续累积。当客户端通过 resume 协议重连时，reconnectToStream 返回的 ReadableStream 包含完整的已缓冲 chunks（replay chunks）+ 后续实时 chunks，与当前 resume 机制完全兼容。重连时的 3 步握手（STREAM_RESUME_REQUEST → STREAM_RESUMING → STREAM_RESUME_ACK）和 5 秒超时兜底逻辑保持不变。_resumeResolver/_resumeNoneResolver 的同步握手机制仍然正确工作，因为服务端在没有收到 cancel 的情况下，turn 仍在活跃状态，STREAM_RESUMING 会被正常发送。

工具调用继续执行：当服务端 turn 涉及工具调用时，工具结果返回后的 auto-continuation（autoContinueAfterToolResult 默认 true）涉及 expectToolContinuation() 和 deferred ReadableStream 创建。在 durable 模式下，若客户端在工具调用阶段断开，服务端工具调用继续执行并返回结果，auto-continuation 照常触发。客户端重连后，通过 resume 协议接收已完成的工具结果和 continuation 流。abortActiveToolContinuation 仅影响客户端侧的活跃工具 continuation 流，不向服务端传播，因此即使在 durable 模式本地清理期间被调用，也不会中断服务端的工具执行。

多标签页场景：当多个标签页连接到同一 agent 实例时，服务端 turn 的结果通过 broadcast 发送到所有连接。activeRequestIds 机制在每个标签页的 transport 中独立维护，确保 chunks 去重。在 durable 模式下，一个标签页的组件卸载仅执行本地清理（其 activeRequestIds 中保留 requestId 以跳过后续 broadcast chunks），不影响服务端 turn 的执行，也不影响其他标签页继续接收流式响应。broadcastTransition 在处理跨标签消息时通过 replay/replayComplete 标志和 localRequestIdsRef 去重，与当前行为保持一致。

迟到服务端消息：在 durable 模式下本地清理后，服务端 turn 继续执行并最终广播 chunks 和 done:true。由于 keepId=true 保留了 requestId 在 activeRequestIds 中，onAgentMessage 处理器会跳过这些已清理标签页上的广播 chunks（与当前行为一致），避免向已卸载组件推送消息。当 done:true 到达时，onAgentMessage 从 activeRequestIds 中正常清理 requestId。如果用户在 turn 完成前重连并建立新连接，resume 协议会接管，新连接收到完整的 replay chunks。

### 技术效果

本方案在保持向后兼容的前提下，实现了以下技术效果。

- 精确的控制语义：将「停止接收」与「停止生成」两个独立意图解耦。durable 模式下刷新页面不再丢失正在执行的长推理结果；request-lifetime 模式下保持当前严格的生命周期绑定。
- 服务端计算不浪费：durable 模式下服务端 turn 总能完整执行并持久化到 SQLite，避免了因客户端生命周期波动导致的重复推理和 token 浪费。
- 与 resume 机制的天然协同：本地清理不中断服务端，使得 chunk 缓冲继续累积，重连后 resume 能回放完整流式内容，消除了当前「刷新后对话截断」的体验缺陷。
- 多标签页安全：一个标签页的组件卸载不会波及其他标签页的流式接收，每个标签页独立管理其 activeRequestIds，服务端 broadcast 语义不受影响。
- 最小侵入：服务端 AIChatAgent、AbortRegistry、ResumableStream 无需任何修改；仅客户端 useAgentChat 和 WebSocketChatTransport 增加模式参数和本地清理分支，现有调用方无需改动即可获得默认的 durable 行为。
- 工具调用延续性：长时间工具调用（如代码执行、外部 API 查询）在客户端断开期间继续在服务端完成，重连后结果完整可用，避免了工具执行被半途废弃的问题。
