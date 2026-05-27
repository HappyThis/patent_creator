## 技术方案

### 技术问题概述

在基于 WebSocket 长连接的 Agent 对话系统中，浏览器端存在多种本地生命周期事件（页面刷新、组件卸载、ReadableStream reader cancel、网络短暂断开），而服务端 Agent 对话可能已在 Durable Object 中持续执行。现有方案将客户端断开与服务端任务取消混为一谈，导致两个问题：一是客户端意外断开时服务端任务被误终止，失去恢复机会；二是缺少明确的停止通道，用户无法可靠地将取消意图传达到服务端。本方案在客户端传输层、服务端 Agent 运行时和 WebSocket 协议层分别引入机制，实现客户端本地清理与服务端 Agent turn 取消的语义分离，并提供可配置的执行模式以适应不同应用偏好。

### 整体架构

系统由三层协同组成。客户端层：useAgentChat React Hook 封装 WebSocketChatTransport 传输层，管理本地请求生命周期和流式响应消费。传输层：WebSocketChatTransport 作为 AI SDK 的 ChatTransport 实现，负责请求/响应流的创建、中断信号传播、以及断线重连的 resume 握手。服务端层：AIChatAgent 运行于 Cloudflare Durable Object 中，内置 AbortRegistry（按请求 ID 管理取消控制器）、ResumableStream（基于 SQLite 的流块持久化与回放）、TurnQueue（生成代数驱动的串行 turn 队列）和 ContinuationState（工具结果触发的自动继续状态管理）。三层通过 cf_agent_chat_* 协议消息族通信，协议消息类型包括 USE_CHAT_REQUEST/RESPONSE、CHAT_REQUEST_CANCEL、STREAM_RESUME_REQUEST/RESUMING/ACK/NONE 等。

### 客户端生命周期与取消语义分离机制

客户端本地生命周期事件与用户主动取消在传输层被明确区分，由不同的代码路径触发。

1. 本地生命周期清理路径（不通知服务端取消）：浏览器刷新/关闭标签页 → WebSocket 断开，React 组件卸载；组件卸载 → useEffect cleanup 函数执行，移除 WebSocket 事件监听器；ReadableStream 的 reader.cancel()（非 AbortSignal 触发）→ 仅释放本地 reader，不调用 onAbort；网络短暂断开 → WebSocket close 事件触发，但服务端 Agent 继续在 DO 中运行。上述事件均不发送 CF_AGENT_CHAT_REQUEST_CANCEL，服务端 Agent turn 不受影响。

2. 用户主动取消路径（通知服务端取消）：用户点击停止按钮 → useAgentChat 调用 stop()（来自 AI SDK useChat），该函数 abort 传入 sendMessages 的 AbortSignal → onAbort 回调触发：通过 WebSocket 发送 CF_AGENT_CHAT_REQUEST_CANCEL（含 requestId）到服务端，然后终止本地 ReadableStream。此外，stopWithToolContinuationAbort 在 stop() 之后额外调用 customTransport.abortActiveToolContinuation()，确保工具继续流也被中止。关键设计：cancel 路径在终止本地 stream 时保留 requestId 在 activeRequestIds 中（keepId=true），使随后到达的同 requestId 服务端消息被跳过，避免流关闭后写入报错。

### 服务端取消执行与部分持久化

服务端收到 CF_AGENT_CHAT_REQUEST_CANCEL 后，AbortRegistry.cancel(requestId) 中断对应请求的 AbortController。该 AbortController 的 signal 已注入推理循环（LLM 调用），abort 后推理停止。已流式输出的部分块已通过 ResumableStream.storeChunk() 写入 SQLite，不会被丢弃。Turn 结束状态报告为 "aborted"，部分结果持久化保留，客户端重新连接后可恢复查看。

### 流式响应的持久化与断线重连

ResumableStream 在 SQLite 中维护 cf_ai_chat_stream_metadata（流元数据：id、request_id、status、created_at）和 cf_ai_chat_stream_chunks（流块：id、stream_id、body、chunk_index）两张表。流开始时调用 start(requestId) 写入 streaming 状态记录；每个 LLM 输出块通过 storeChunk() 缓冲后批量刷入数据库（缓冲阈值 10 块，上限 100 块）；流完成时调用 complete() 标记 completed。DO 因休眠被逐出后重新唤醒时，restore() 从 SQLite 恢复活跃流状态：读取 status='streaming' 记录，恢复 activeStreamId、activeRequestId 和 chunk_index。此时 _isLive=false（孤儿流），LLM reader 已丢失，不会再有新块到达。

断线重连流程：客户端 reconnectToStream() 发送 CF_AGENT_STREAM_RESUME_REQUEST。服务端收到后，若有活跃流且该流属于当前连接的 turn，发送 CF_AGENT_STREAM_RESUMING（含 stream id）；若流属于其他连接（多标签页场景）或无活跃流，发送 CF_AGENT_STREAM_RESUME_NONE。客户端传输层通过 _resumeResolver / _resumeNoneResolver 同步回调机制消除竞态：onAgentMessage 收到 RESUME_NONE 时调用 handleStreamResumeNone() 直接 resolve(null)；收到 RESUMING 时调用 handleStreamResuming() 创建 resume 流并发送 RESUME_ACK。服务端收到 ACK 后调用 replayChunks() 将 SQLite 中存储的块按 chunk_index 升序回放（标记 replay:true），然后客户端无缝接入 live 块。对于孤儿流（DO 重启后恢复），回放完全部块后发送 done:true 并标记 completed。

### 工具调用的继续执行与中断

工具调用继续执行通过 ContinuationState 管理。当服务端收到客户端工具结果（CF_AGENT_TOOL_RESULT）或审批响应（CF_AGENT_TOOL_APPROVAL）后，若启用了 autoContinueAfterToolResult，服务端自动将工具继续 turn 排入 TurnQueue。传输层在 addToolOutput/addToolApprovalResponse 调用后通过 _expectToolContinuation 标记，使下一次 reconnectToStream() 直接创建工具继续流（_createToolContinuationStream），而非 page-load resume 流。工具继续流等待服务端通过 STREAM_RESUMING 广播继续 turn 的 requestId，ACK 后进入正常流式消费。用户点击停止时，stopWithToolContinuationAbort 在调用 stop() 后额外执行 abortActiveToolContinuation()，中断正在等待的工具继续流。

### 多标签页协同与广播机制

useAgentChat 的 onAgentMessage 处理器监听所有 WebSocket 广播消息。当其他标签页发起的 Agent turn 产生流式输出时，服务端向所有连接广播 USE_CHAT_RESPONSE 消息。当前标签页通过 streamStateRef 状态机跟踪跨标签页流状态（idle/observing/replaying），判断消息属于本地请求（localRequestIdsRef 中）、pending replay 流还是跨标签页观察流。CF_AGENT_CHAT_MESSAGES 消息用于跨标签页同步完整消息列表。CF_AGENT_CHAT_CLEAR 消息广播清空历史操作到所有标签页。服务端 ContinuationState.awaitingConnections 跟踪等待继续流的连接集合，确保 RESUMING 只通知到正确的连接。

### 可配置的执行模式

系统提供两层可配置性。第一层：客户端 resume 选项（默认 true）。resume=true（默认持久模式）：客户端重连后自动发送 STREAM_RESUME_REQUEST，恢复消费服务端正在执行的 Agent turn。浏览器刷新/组件卸载不触发 CHAT_REQUEST_CANCEL。服务端 Agent turn 在 DO 中持续运行，不受客户端断开影响，可被后续重连恢复。resume=false（请求生命周期模式）：客户端不发送 resume 请求，浏览器断开后服务端 turn 仍继续执行（DO 中），但客户端不再主动恢复。此模式下使用者可自行决定是否在客户端 disconnect 时发送取消消息。第二层：服务端 chatRecovery 属性（默认 false）。设置为 true 时，Agent turn 被包裹在 runFiber 持久化纤程中执行，DO 休眠/重启后通过 onChatRecovery 钩子恢复执行。

### 技术效果

与现有方式相比，本方案具有以下技术效果。第一，语义精确分离：浏览器刷新、组件卸载、reader cancel、网络断开等本地清理事件不触发服务端取消，避免误终止长时间 Agent 对话；用户点击停止按钮则通过专用协议消息将取消意图可靠传达到服务端，两端语义一致。第二，无缝断线重连：基于 SQLite 持久化的流块存储和竞态消除的 resume 握手协议，客户端重连后可从已输出的第一个块开始恢复，无需重新执行推理，节省 token 成本。第三，DO 休眠恢复兼容：ResumableStream 在 DO 重启后自动恢复活跃流状态，孤儿流通过 replay→done 完成，保证数据不丢失。第四，多标签页安全：跨标签页消息广播和 streamStateRef 状态机使各标签页独立跟踪流归属，避免重复 ACK 和消息混淆。第五，灵活可配置：通过 resume 选项和 chatRecovery 属性，应用开发者可根据自身场景选择持久模式或请求生命周期模式，无需修改核心协议。

### 持久化纤程执行与恢复

当 AIChatAgent.chatRecovery 设为 true 时，每个 Agent turn 通过 runFiber 包装执行。runFiber 在 DO 的 SQLite 中维护持久化纤程表，记录纤程状态（running/suspended/completed）。如果 DO 在执行过程中因休眠被逐出，纤程状态保留在 SQLite 中。DO 重新唤醒时，onChatRecovery 钩子被调用，接收恢复上下文（streamId、requestId、partialText、partialParts、recoveryData（来自 this.stash() 的检查点数据）、messages、lastBody、lastClientTools、createdAt）。钩子返回配置决定恢复行为：persist（是否持久化已流出的部分响应，默认 true）和 continue（是否调度 continueLastTurn 继续执行，默认 true）。createdAt 时间戳可用于判断 turn 是否已过期，避免恢复过时任务。恢复继续通过 _chatRecoveryContinue 方法安全调度，等待 DO 状态稳定后执行。

### 关键协议消息语义

cf_agent_use_chat_request（客户端→服务端）：发起 Agent 对话请求，携带消息列表、触发类型和自定义 body。cf_agent_use_chat_response（服务端→客户端）：流式响应块，含 body（JSON 块内容）、done（是否结束）、replay（是否为回放块）、replayComplete（回放是否完成）。cf_agent_chat_request_cancel（客户端→服务端）：取消指定 requestId 的正在执行的 turn，由用户主动停止触发。cf_agent_stream_resume_request（客户端→服务端）：客户端重连后请求恢复活跃流。cf_agent_stream_resuming（服务端→客户端）：通知客户端存在可恢复的活跃流，携带 stream id。cf_agent_stream_resume_ack（客户端→服务端）：客户端确认接收恢复流。cf_agent_stream_resume_none（服务端→客户端）：通知客户端无活跃流可恢复。cf_agent_chat_clear（双向广播）：清空聊天历史，触发 TurnQueue 代数递增使排队 turn 失效。

### 风险与待确认问题

1. 迟到服务端消息处理：当用户在 turn 执行期间点击停止后迅速重连，服务端可能尚未处理完 CHAT_REQUEST_CANCEL。当前方案通过 keepId=true 保留 requestId 在 activeRequestIds 中，使取消后的 in-flight 消息被跳过。但若服务端在取消后仍发送最终 done:true 块，需确认该块不会被客户端误认为新 turn 完成。2. resume=false 模式下的多标签页一致性：当 resume 关闭时，一个标签页的断开不会取消服务端 turn，但其他标签页仍在观察该 turn。如果观察标签页也想取消，需额外机制。建议方案：提供跨标签页广播的停止意图通道。3. 孤儿流的时间窗口：ResumableStream 清理孤儿 streaming 记录的默认阈值为 24 小时。对于长时间无重连且无恢复钩子的场景，这些记录会占用 SQLite 空间。可考虑根据具体应用的恢复预期调整 CLEANUP_AGE_THRESHOLD_MS。4. chatRecovery 模式下 DO 花费：持久化纤程需要额外的 SQLite 写入，在高频短 turn 场景可能增加 DO 计费。建议按需启用 chatRecovery。
