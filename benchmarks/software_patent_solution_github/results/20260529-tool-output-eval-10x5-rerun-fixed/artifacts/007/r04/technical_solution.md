## 技术方案

本技术方案提出一种基于持久对象（Durable Object）子代理（Sub-Agent）体系的多代理协作方法。在该方法中，用户始终只与一个主代理（父代理）建立连接，但父代理在处理请求时可将特定任务委派给一个或多个子代理独立执行。子代理保持独立的计算上下文和持久存储，父代理通过框架托管的运行注册表跟踪每个子代理的执行状态，并将子代理的流式输出事件以标准化的 agent-tool-event 协议实时转发到与父代理保持的同一会话连接中。该方案建立在已有的子代理路由（sub-agent routing）和 chat/Think 代理体系之上，通过新增编排层（orchestration layer）实现代理工具的调度、观测、恢复、取消和清理。

### 技术问题与场景

在已有的基于持久对象的代理框架中，用户通过 WebSocket 连接到一个顶层代理（如 Think 或 AIChatAgent），代理执行 LLM 推理并流式返回结果，支持断线重连后从持久存储恢复流。子代理（sub-agent/facet）原语已提供通过 parent.subAgent(Cls, name) 创建同主机子持久对象、子对象自带独立 SQLite 存储和外部可路由地址（/sub/{className}/{name}）的能力，但缺少将子代理作为可调用工具进行编排的连接层。父代理可以手动创建子代理并调用其方法，但应用开发者需要自行实现运行记录创建、输出流转发、生命周期事件合成、多子代理信号分离、断线重放、取消传播和访问控制等机制。

### 系统架构概述

本方案的系统架构在已有的持久对象父子体系之上引入一个框架托管的编排层，包含三个核心组件：（1）父代理端的代理工具运行注册表（agent-tool run registry），位于父代理的 SQLite 存储中，记录每个子代理运行的元数据和执行状态；（2）子代理端的子运行映射表（child run mapping），位于子代理的 SQLite 存储中，记录对外运行标识 runId 到内部聊天请求标识 requestId 和持久流标识 streamId 的映射；（3）代理工具事件协议（agent-tool-event protocol），定义父代理向其客户端广播子代理事件的标准化线格式。子代理本身可以是任何已支持程序化 turn 的聊天代理（如 Think 子类），不需要继承特殊的 AgentTool 基类——子代理之所以成为'代理工具'，是因为父代理通过编排 API 将其作为工具调度，而非因为子代理类继承了某个特殊父类。

### 代理工具运行注册表与父子关联

父代理维护一个框架托管的代理工具运行注册表（cf_agent_tool_runs），该表位于父代理的 SQLite 存储中，包含如下关键字段：run_id（主键，全局唯一运行标识）、parent_tool_call_id（可选，关联的父工具调用标识，用于 LLM 调度的场景）、agent_type（子代理类型名）、input_preview（输入摘要，默认不持久化完整原始输入）、status（状态：starting/running/completed/error/aborted/interrupted）、summary（完成时的文本摘要）、error_message（失败时的错误信息）、display_order（同一父工具调用下多个子代理的显示排序）、started_at/completed_at（时间戳）、stream_id（子代理持久流标识，用于重放时精确定位本运行的聊天块）。

子代理端维护一个子运行映射表（cf_agent_tool_child_runs），将编排标识 runId 映射为聊天系统内部使用的 requestId（聊天回合标识）和 streamId（持久可恢复流标识）。这种映射解耦设计使得未来的多回合代理工具运行（一个 runId 对应多个聊天回合）可以在不改变外部 API 的情况下演进。

父子关联通过以下机制建立：父代理在调用 this.subAgent(Cls, runId) 创建子代理之前，先在注册表中插入一行 status='running'；子代理初次启动时，将 runId→requestId→streamId 映射持久化到自身的子运行映射表；父代理在运行完成后更新注册表行为 terminal 状态。runId 是跨越父注册表行、子代理名称、子映射行、重放、钻入、取消和清理 API 的稳定连接键。

### 子代理运行身份与执行隔离

每个代理工具运行拥有独立的执行上下文。子代理由 parent.subAgent(Cls, runId) 创建，获得自己的 SQLite 存储、内存状态和 WebSocket 客户端集合。子代理的聊天流（chat stream）持久化在其自己的持久可恢复流（_resumableStream）上——不存在父代理托管第二份流数据的情况，避免了同一持久对象上多流冲突。

子代理支持三种调用模式：（1）LLM 工具调用模式：父代理 LLM 通过 getTools() 中声明的 agentTool(Cls, options) 工具自主决定何时调度子代理，工具执行时传递 toolCallId 和 abortSignal；（2）服务端确定性调用模式：通过 this.runAgentTool(Cls, { input, parentToolCallId?, signal? }) 在 @callable 函数、HTTP 处理器或后台作业中主动调度子代理，不依赖 LLM 判断；（3）无父工具调用模式：调用 runAgentTool 时不传递 parentToolCallId，子代理事件通过 unboundRuns 通道独立呈现，不与任何聊天工具部件关联。三种模式共享相同的 runId 稳定标识和事件协议。

runAgentTool 以 runId 为幂等键：如果调用方传入已存在行的 runId，terminal 运行直接返回已有结果而不重复执行；non-terminal 运行也不启动重复工作。这使得从重试路径、alarm 和重连恢复中安全调用 runAgentTool 成为可能。

### 流式事件协议与实时展示

父代理向所有连接的客户端广播代理工具事件，事件线格式为 agent-tool-event 消息。每个消息包含：type（固定为 'agent-tool-event'）、parentToolCallId（可选，将事件归属到父聊天消息的具体工具调用部件）、sequence（父代理为每个子代理运行独立维护的单调递增序号）、replay（可选标记，表示该事件来自重放而非实时广播）、以及 event 负载。

event 负载有六种类型：（1）started：包含 runId、agentType、inputPreview、order（显示排序）和可选的 display 元数据（名称、图标）；（2）chunk：包含 runId 和 body——body 是 JSON 编码的 UIMessageChunk，即子代理的聊天流块，与主代理使用的相同的 AI SDK 块词汇一致，客户端可通过 applyChunkToParts 构建子代理消息部件数组；（3）finished：包含 runId 和 summary（子代理的最终文本摘要）；（4）error：包含 runId 和 error（子代理执行失败信息）；（5）aborted：包含 runId 和可选的 reason（取消原因）；（6）interrupted：包含 runId 和 error（父代理恢复时发现子代理仍在运行但无法重接实时流，标记为中断）。

chunk 的 body 为不透明 JSON 字符串，框架不发明第二套文本、推理、工具调用的词汇表。客户端用与主代理聊天响应相同的 applyChunkToParts 原语重建子代理的 UIMessage.parts，无需重新实现消息组装逻辑。terminal 事件（finished/error/aborted/interrupted）互斥，UI 可据此渲染不同的完成状态。

### 重放去重与恢复机制

本方案区分执行（execution）与观测（observation）：启动代理工具创建由 runId 标识的持久工作；向父代理转发事件是对该工作的观测。观测流的断开不自动取消执行——浏览器断开、父代理重启或重放连接失败应仅分离观测，而非终止子代理运行。

重连恢复机制如下：父代理在 onConnect 时（在 Think 的聊天协议恢复完成后）遍历 cf_agent_tool_runs 表中的所有行。对每一行，合成 started 事件（从行数据读取 helperType、query、displayOrder 等元数据），然后通过 RPC 调用子代理的 getChatChunksForReplay(streamId) 从子代理的持久可恢复流中获取存储的聊天块，逐一转发为 chunk 事件（标记 replay: true），最后根据行的 status 合成相应的 terminal 事件（finished/error/interrupted）。使用 streamId 精确定位本运行对应的流，防止钻入用户在子代理上的后续聊天回合覆盖原始工具调用的回放内容。

去重机制：客户端以 (parentToolCallId, runId, sequence) 为去重键。同一父工具调用下的多个子代理可能各自从 sequence 0 开始，该三元组能正确区分。当 reconnect 发生时，onConnect 重放路径发送标记 replay: true 的事件，而 _runHelperTurn 的实时广播路径也可能会发送同一子代理的事件——客户端通过去重键过滤重复帧，保证 UI 状态一致性。

父代理崩溃恢复的特殊处理：父代理在 onStart 中将所有 status='running' 的行批量更新为 status='interrupted'，因为原始的观测转发循环已丢失。但子代理自身的持久聊天数据和 chunk 仍完整保留，重连时 onConnect 仍可重放已存储的 chunk，并合成 interrupted terminal 事件告知 UI 该运行未完整结束。未来支持 tailAgentToolRun（实时尾随重接）后，可从容错恢复为重新附加观测。

### 取消传播与并发控制

取消传播链路从父代理的 AbortSignal 开始，经过多跳传递到子代理的 LLM 推理循环：（1）父代理的聊天回合或工具执行被取消（用户点击 Stop、关闭标签页、sibling abort）；（2）runAgentTool 检测到 signal.aborted，取消子代理 RPC reader（reader.cancel(reason)）；（3）workerd 的 RPC 桥接层取消子代理端的 ReadableStream，触发 stream 的 cancel 回调；（4）cancel 回调 abort 该回合的 AbortController（per-turn AbortController）；（5）该 AbortController 的 signal 在 saveMessages({ signal }) 中被 Think 的推理循环观测到，终止 LLM 调用。该链路无竞争窗口——即使在 saveMessages 内部分配 linkExternal 之前 cancel 已触发，signal 在 link 时被观测为已 abort 状态，推理工作直接跳过。已存储到持久可恢复流的 chunk 仍用于重连重放。

取消与 terminal 状态的保护规则：terminal 状态（completed/error/aborted/interrupted）是权威的。一旦子代理运行达到 terminal 状态，迟到的 cancel 不得覆写 completed 或 error 为 aborted；迟到的父代理恢复也不得覆写 aborted 为 interrupted。父代理的 cancelAgentToolRun(runId) 和子代理的 cancelAgentToolRun 都是幂等的——对已 terminal 运行不产生效果。

并发控制包含两层：第一层，每个子代理实例内部通过同步标志 _runInProgress 防止同一实例上的并发 runTurnAndStream 调用（父代理每次工具调用创建新的子代理实例，该检查用于防御编程）；第二层，父代理级别的 maxConcurrentAgentTools 可配置选项在超过并发上限时快速失败，发出明确的 error 事件而不排队等待。

### 清理保留与访问控制

默认保留策略：代理工具运行在完成后继续保留——包括父代理端注册表行和子代理持久对象。这是必要的，因为运行完成正是事后检查最有价值的时刻：页面刷新重放、钻入查看子代理的完整对话、失败运行调试、审计追溯都依赖保留的子代理对象和注册表行。框架提供显式的 clearAgentToolRuns(...) API，支持按时间范围（olderThan）和状态筛选（status: ['completed', 'error', ...]）进行清理。清理操作同时删除父注册表行和调用 deleteSubAgent 删除对应的子代理持久对象——删除注册表行而保留子代理对象将产生无法通过重放或钻入访问的孤儿数据。

钻入访问控制：子代理通过已有的子代理路由原语对外可访问（URL 形状：/agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}）。客户端通过 useAgent({ agent: parent, name: userId, sub: [{ agent: agentType, name: runId }] }) 钻入到子代理，可以直接使用 useAgentChat 与子代理对话。父代理通过 onBeforeSubAgent 中间件实现严格注册表门控：仅当父代理的 cf_agent_tool_runs 表中存在匹配 (agentType, runId) 的行时，才将请求转发到子代理；否则返回 404。内部 subAgent(...) 调用绕过该钩子（类似 getAgentByName 绕过 onBeforeConnect），因此父代理自身的子代理创建不受影响。runId 本身不是能力凭证——钻入 URL 始终通过父代理的身份和租户上下文达成，框架不鼓励将 runId 作为 bearer token 分发。

### 风险与待确认问题

以下为当前基于原型验证的技术方案中的已知风险点和待确认问题：（1）实时尾随重接（live-tail reattach）：V1 不支持父代理在丢失观测后重新附加到正在运行的子代理的实时流，目前仅支持通过存储的 chunk 重放并标记 interrupted。runId/sequence 设计已为未来的 tailAgentToolRun 预留扩展空间。（2）多回合代理工具运行：V1 将一个 runId 映射为一个聊天回合和一个持久流，子运行映射表的结构（runId→requestId→streamId）已为未来的一对多映射（runId→多个 turn 的 requestId）预留空间。（3）AIChatAgent 兼容性：原型基于 Think 实现，AIChatAgent 的适配器是本方案的后续里程碑，两者的子适配器合约（AgentToolChildAdapter）已定义结构接口。（4）自动 TTL/GC：V1 仅提供显式 clearAgentToolRuns API，基于时间或数量的自动清理策略属于应用层策略决策，推迟到后续版本，但子代理调度（sub-agent scheduling）机制已消除实现后台 GC 的技术障碍。（5）父代理聊天回合恢复：如果 agentTool 调用是父代理 LLM 回合的一部分，父代理崩溃后恢复子代理的 transcript 不足以保证父代理回合能从工具结果继续——这需要父代理聊天恢复机制在重建 LLM 上下文时能注入已恢复的工具结果，该能力独立于本方案的核心编排机制。
