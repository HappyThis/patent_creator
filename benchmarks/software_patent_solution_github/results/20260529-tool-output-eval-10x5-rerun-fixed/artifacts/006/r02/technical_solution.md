## 技术方案

本技术方案针对基于 Durable Object 的长时间 agent 对话场景中，客户端生命周期清理（浏览器刷新、组件卸载、ReadableStream reader cancel、网络断开）与服务端 agent turn 取消语义混淆的问题，提出一种双信号架构：将"本地流清理"与"显式取消服务端任务"分离为两条独立控制路径，并引入可配置的生命周期模式（durable 模式与 request-lifetime 模式），使系统在保持流式响应、断线重连、工具调用继续执行和多标签页兼容的前提下，准确传递用户的停止意图。

### 1. 问题分析：客户端清理与取消语义的混淆

在现有基于 WebSocket 的 `useAgentChat` / `AIChatAgent` 架构中，客户端通过 AI SDK 的 `useChat` hook 获取 `stop()` 方法。当调用 `stop()` 或发生 ReadableStream 的 `cancel()`（由组件卸载、浏览器刷新、reader.cancel() 等触发）时，`WebSocketChatTransport` 的 `onAbort` 回调会向服务端发送 `CF_AGENT_CHAT_REQUEST_CANCEL` 消息，服务端收到后通过 `AbortRegistry.cancel(requestId)` 中止对应请求的 `AbortController`，进而终止 `streamText` 的 LLM 推理。

问题在于，`stop()` 的触发来源包含两类语义完全不同的场景：一是"本地生命周期清理"——浏览器刷新、组件卸载、网络断开、reader 被外部取消等，此时客户端仅需释放本地资源，服务端的 agent turn 应继续执行（因为 Durable Object 中可能正在执行工具调用、已生成大量可恢复的流式内容）；二是"用户显式取消"——用户点击停止按钮，意图中止当前 agent 回答，此时取消信号应确实传达到服务端以终止推理。当前实现将两者统一映射为 `CF_AGENT_CHAT_REQUEST_CANCEL`，导致页面刷新或标签页切换等本地清理行为意外终止了服务端正在执行的 agent 对话。

### 2. 双信号架构：本地清理与显式取消分离

本方案的核心思路是在客户端引入两条独立的控制路径，分别处理"本地流清理"和"显式取消服务端任务"，并在 `WebSocketChatTransport` 中为两者设置不同的行为。

本地流清理路径（Local Cleanup Path）：当浏览器刷新、组件卸载、页面切换、网络断开或 reader 被 cancel 时，仅执行本地资源释放——关闭本地 ReadableStream、移除 WebSocket 事件监听器、清理 `activeRequestIds` 集合中的对应条目——但不向服务端发送 `CF_AGENT_CHAT_REQUEST_CANCEL`。此时服务端的 agent turn 在 Durable Object 中继续执行，`ResumableStream` 持续将流式 chunk 写入 SQLite，等待客户端通过现有的 `CF_AGENT_STREAM_RESUME_REQUEST` → `CF_AGENT_STREAM_RESUMING` → `STREAM_RESUME_ACK` 三阶段握手恢复接收。

显式取消路径（Explicit Cancel Path）：当用户主动点击停止按钮或应用代码调用新增的 `cancelTurn()` 方法时，客户端向服务端发送 `CF_AGENT_CHAT_REQUEST_CANCEL` 消息。服务端 `AIChatAgent.onMessage` 接收后调用 `AbortRegistry.cancel(requestId)`，该 AbortController 的 signal 已被传入 `streamText({ abortSignal })`，因此 LLM 推理被中止。同时，`ResumableStream` 将当前流标记为已完成（status='completed'），已缓冲的 chunk 仍可被重连的客户端恢复读取。这与现有的 `resetTurnState()`（清空所有状态）不同——后者用于 `CF_AGENT_CHAT_CLEAR` 的全量清除场景。

### 3. 可配置生命周期模式：Durable 与 Request-Lifetime

为满足不同应用对"客户端断开是否代表取消服务端任务"的不同偏好，系统在 `useAgentChat` 选项中引入 `cancelMode` 配置项，支持两种模式：

Durable 模式（默认，`cancelMode: 'durable'`）：服务端 agent turn 的生命周期与客户端连接解耦。当所有客户端 WebSocket 连接均断开时，Durable Object 中的 agent turn 不受影响，继续执行至完成或超时。`ResumableStream` 持续缓冲 chunk；工具调用（包括 `needsApproval` 审批等待和 `onToolCall` 客户端工具）在服务端继续保持等待状态。任何重连的客户端均可通过现有的 resume 机制恢复接收。此模式适用于长时间推理、多步骤 agent 工作流、以及需要在浏览器刷新/网络切换后继续执行的场景。

Request-Lifetime 模式（可选，`cancelMode: 'request-lifetime'`）：服务端 agent turn 与发起请求的客户端连接绑定。当发起请求的客户端 WebSocket 断开时，`AIChatAgent` 在 `onClose` 回调中检测到该连接承载了活跃的 request，自动调用 `AbortRegistry.cancel(requestId)` 终止推理。此模式降低了服务端资源消耗，适用于短响应、无需跨页面持久化的对话场景。注意：在此模式下，其他观察标签页（通过 `broadcastTransition` 接收跨标签页流式更新的标签页）的断开不会触发取消——仅发起请求的原始连接断开才触发。

### 4. 与现有机制的兼容性设计

本方案的设计需要与现有机制无缝兼容，以下是各关键兼容点的设计：

流式响应与断线重连：Durable 模式下，客户端断开后 `ResumableStream` 继续将 chunk 写入 SQLite。当客户端重连时，`WebSocketChatTransport.reconnectToStream()` 发送 `CF_AGENT_STREAM_RESUME_REQUEST`，服务端通过 `ResumableStream.replayChunks()` 回放已缓冲的 chunk，然后继续推送实时 chunk。整个过程不需要修改现有的三阶段 resume 协议。

工具调用继续执行：在 Durable 模式下，客户端断开时服务端正在执行的工具调用（包括 `needsApproval` 审批等待）不受影响。`ContinuationState` 中的 `pending` 和 `deferred` 状态保持不变。当工具调用完成并触发自动续写（`autoContinueAfterToolResult`）时，生成的流式内容同样通过 `ResumableStream` 缓冲。重连的客户端通过 resume 获取这些内容。在 Request-Lifetime 模式下，如果原始连接断开导致推理被中止，`ContinuationState` 中的待处理状态也会被一并清理。

多标签页观察：现有 `broadcastTransition` 状态机通过 `CF_AGENT_USE_CHAT_RESPONSE` 广播向所有连接的标签页推送流式更新。`activeRequestIds` 机制确保发起请求的标签页不重复处理已在 transport 层面处理的消息。在 Durable 模式下，所有标签页关闭后 agent turn 继续执行；当任一新标签页连接时，`onConnect` 触发 `CF_AGENT_STREAM_RESUMING` 通知客户端有可恢复的流。

迟到服务端消息处理：当 agent turn 在无客户端连接期间完成时，最终消息通过 `persistMessages` 写入 SQLite。新连接的客户端在初始化时通过 `getInitialMessages`（调用 `/get-messages` HTTP 端点）获取最新消息列表，无需依赖 WebSocket 实时推送即可获得最终结果。

### 5. 处理流程

以下按时间线描述 Durable 模式下各关键环节的处理流程：

（1）请求发起：用户在客户端发送消息。`WebSocketChatTransport.sendMessages()` 生成 `requestId`（nanoid 8 位），创建 `AbortController` 和 `ReadableStream`，向服务端发送 `CF_AGENT_USE_CHAT_REQUEST`，并将 `requestId` 加入 `activeRequestIds`。服务端 `AIChatAgent.onMessage` 接收请求后，在 `AbortRegistry` 中为 `requestId` 创建 `AbortController`，将其 signal 传入 `streamText`，通过 `ResumableStream` 开始缓冲 chunk。

（2）客户端本地清理（浏览器刷新/组件卸载/网络断开）：客户端的清理挂钩（useEffect 返回的清理函数、beforeunload 事件处理器）调用本地清理路径。清理路径执行：流 controller 的 error/close（不触发 onAbort）、移除 WebSocket 事件监听器、保留 `activeRequestIds` 中的条目（以便后续服务端 `done:true` 到达时清理 ID）。不发送 `CF_AGENT_CHAT_REQUEST_CANCEL`。

（3）用户显式取消：用户点击停止按钮，应用调用 `cancelTurn()`（或保留现有 `stop()` 方法但改变其内部行为以区分场景）。客户端发送 `CF_AGENT_CHAT_REQUEST_CANCEL`（携带 `requestId`）。服务端 `AbortRegistry.cancel(requestId)` 触发 `AbortController.abort()`，`streamText` 的 `abortSignal` 被触发，LLM 推理中止。`ResumableStream` 将流标记为 `completed`。已缓冲的 chunk 可通过 resume 回放。

（4）断线重连恢复：客户端重连后 `reconnectToStream()` 发送 `CF_AGENT_STREAM_RESUME_REQUEST`。服务端检测到活跃流后发送 `CF_AGENT_STREAM_RESUMING`，客户端回应 `STREAM_RESUME_ACK`，服务端通过 `replayChunks()` 从 SQLite 回放已缓冲 chunk（带 `replay: true` 标志），然后继续推送实时 chunk。

（5）迟到消息处理：当 agent turn 完成时无客户端连接，`onChatResponse` 钩子触发（`status: 'completed'` 或 `'aborted'`），最终消息通过 `persistMessages` 写入 SQLite。新客户端连接时通过 HTTP `/get-messages` 端点获取完整历史。

在 Request-Lifetime 模式下，第（2）步中发起请求的原始连接断开将触发服务端 `onClose` 回调中的自动取消逻辑，行为等同于第（3）步的显式取消。非原始连接的断开不影响服务端执行。

### 6. 关键模块与变更

本方案涉及的关键模块及其变更如下：

WebSocketChatTransport（`packages/ai-chat/src/ws-chat-transport.ts`）：新增 `cancelMode` 配置项和 `cancelTurn()` 方法。修改 `onAbort` 回调，使其在 Durable 模式下不发送 `CF_AGENT_CHAT_REQUEST_CANCEL`，仅执行本地流终止。新增 `_originatingConnectionId` 字段，记录发起请求的 WebSocket 连接标识。

useAgentChat（`packages/ai-chat/src/react.tsx`）：新增 `cancelMode?: 'durable' | 'request-lifetime'` 选项（默认 `'durable'`）。新增 `cancelTurn()` 返回值，供用户显式取消。修改 `stopWithToolContinuationAbort` 的内部逻辑，区分本地清理与显式取消。

AIChatAgent（`packages/ai-chat/src/index.ts`）：在 `onClose` 回调中新增条件逻辑：当 `cancelMode` 为 `'request-lifetime'` 且断开的连接是当前活跃 turn 的原始连接时，调用 `AbortRegistry.cancel()`。在 `onMessage` 中保持现有的 `CF_AGENT_CHAT_REQUEST_CANCEL` 处理逻辑不变——该消息始终代表显式取消意图。

AbortRegistry（`packages/agents/src/chat/abort-registry.ts`）：现有实现已满足需求——`cancel(id)` 方法按 requestId 精确中止，`destroyAll()` 用于 `resetTurnState()` 的全量清除。无需修改。

ResumableStream（`packages/agents/src/chat/resumable-stream.ts`）：现有实现已满足需求——chunk 缓冲、SQLite 持久化、replay 回放、orphaned stream 检测与清理。取消后的流标记为 `completed` 使 replay 仍可读取已缓冲内容。无需修改。

types.ts（`packages/ai-chat/src/types.ts`）：现有 `MessageType.CF_AGENT_CHAT_REQUEST_CANCEL` 消息类型保持不变，新增可选的 `reason` 字段用于区分取消原因（如 `'user'` vs `'connection-lost'`）。

### 7. 技术效果

本方案相比现有技术具有以下技术效果：

（1）语义精确性：将客户端本地生命周期清理与服务端 agent turn 取消分离为两条独立信号路径，解决了浏览器刷新/组件卸载意外终止服务端推理的问题。用户的"停止"意图通过 `cancelTurn()` 精确传递，本地清理行为不再污染服务端状态。

（2）配置灵活性：通过 `cancelMode` 选项，开发者可根据应用场景选择 Durable 模式（长时间 agent 对话持续执行，不受客户端连接变化影响）或 Request-Lifetime 模式（短响应场景下自动释放服务端资源）。模式切换仅需修改客户端配置，服务端逻辑自动适配。

（3）兼容现有机制：方案完全兼容现有的流式响应（`streamText` + SSE over WebSocket）、断线重连（三阶段 resume 握手 + SQLite chunk 回放）、工具调用继续执行（`autoContinueAfterToolResult` + `ContinuationState`）、多标签页广播（`broadcastTransition` + `activeRequestIds` 去重）以及迟到消息处理（`persistMessages` + `/get-messages` HTTP 端点），无需对上述机制进行破坏性修改。

（4）可恢复性增强：在 Durable 模式下，取消操作后 `ResumableStream` 保留已缓冲内容并标记流为已完成，客户端重连后仍可通过 replay 获取已生成的部分内容。这与现有的 orphaned stream 恢复机制一致，但增加了显式取消作为完成原因。

（5）可观察性：`cancelTurn()` 的结果可通过现有的 `onChatResponse` 钩子观察（`status: 'aborted'`），`CF_AGENT_CHAT_REQUEST_CANCEL` 消息可携带 `reason` 字段以区分用户取消与连接断开导致的自动取消，便于监控和调试。

### 8. 风险与待确认问题

以下为需要后续确认的风险点和待解决问题：

（1）AI SDK `useChat` 的 `stop()` 行为依赖：当前 `stop()` 方法来自 AI SDK（`@ai-sdk/react`），其内部通过 `stream.cancel()` 触发 transport 的 cancel 回调。本方案需要在 AI SDK 层面或 transport 层面拦截该行为，以区分 cancel 的来源（用户主动调用 stop vs 组件卸载触发的自动 cancel）。如果 AI SDK 不暴露区分机制，可能需要在 `useAgentChat` 中通过 ref 追踪 `stop()` 的调用来源。

（2）beforeunload 事件可靠性：浏览器 `beforeunload` 事件在移动端浏览器和部分场景下不可靠（如强制终止进程）。对于极端情况，Durable 模式下的 agent turn 继续执行不受影响；但在 Request-Lifetime 模式下，如果 beforeunload 未能触发，服务端需通过 WebSocket 心跳超时作为兜底检测。

（3）`cancelMode` 配置的服务端同步：当前 `cancelMode` 作为客户端侧配置，服务端通过 WebSocket 连接级别获知该偏好。如果同一会话的不同标签页设置了不同的 `cancelMode`，需要定义优先级规则（建议以当前活跃 turn 的发起连接的设置为准）。

（4）与 `resume: false` 选项的关系：当 `resume: false` 时，客户端不会发起 resume 请求，但 Durable 模式下的服务端仍继续执行。需要明确 `resume` 控制的是客户端是否尝试恢复，而 `cancelMode` 控制的是服务端在连接断开后的行为——两者正交。
