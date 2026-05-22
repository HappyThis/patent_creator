## 技术方案

本技术方案描述一种基于持久对象（Durable Object）的 agent 子运行编排系统，使得主 agent 在一次请求处理过程中能够按需调用一个或多个专门的子 agent 执行研究、规划、比较、总结等任务，并将子 agent 的执行过程以流式事件形式实时回传到主 agent 的同一会话视图中，同时支持断线重连恢复、并发控制、访问控制和生命周期管理。

### 1. 系统总体架构

系统基于持久对象（Durable Object, DO）运行时构建。每个 agent 实例是一个 DO，拥有独立的 SQLite 存储、内存状态和 WebSocket 连接管理能力。系统包含以下核心组件：

- 主 Agent（Parent Agent）：用户直接与之对话的顶层 DO，负责接收用户消息、进行 LLM 推理、决定调用哪些子 agent 工具，并向其 WebSocket 客户端转发自身及子 agent 的流式事件。
- 子 Agent（Agent Tool / Child Agent）：嵌入在主 agent DO 内部的持久对象（通过 DO Facet 机制），拥有独立的 SQLite 存储和推理循环。子 agent 可作为主 agent 的工具被调用，执行独立的研究、规划、比较等任务。
- 父端运行注册表（Parent Run Registry）：存储在主 agent 的 SQLite 中的 cf_agent_tool_runs 表，记录每次子 agent 调用的运行标识、关联的父工具调用标识、状态、摘要和元数据。
- 子端运行映射表（Child Run Mapping）：存储在子 agent 自身 SQLite 中的 cf_agent_tool_child_runs 表，记录运行标识到内部请求标识和流标识的映射。
- 可恢复流（ResumableStream）：子 agent 内部的持久化流缓冲机制，将每个流式输出块（chunk）按序号写入 SQLite，支持断线后从指定位置继续回放。
- 事件协议层：定义 agent-tool-event 消息格式，包括 started、chunk、finished、error、aborted、interrupted 等事件类型，通过父 agent 的 WebSocket 广播到所有连接的客户端。

### 2. 子 Agent 工具的两种调用模式

子 agent 工具的运行支持两种调用模式，均通过统一的 runAgentTool 原语实现：

（1）模型驱动调用（agentTool）。主 agent 将子 agent 封装为 AI SDK 工具，注册在 getTools() 中。当 LLM 决定调用该工具时，工具执行体调用 runAgentTool，将 LLM 传入的 toolCallId 作为 parentToolCallId，将 LLM 提供的 AbortSignal 作为取消信号传入。子 agent 的输出经过结构化提取后返回给 LLM 作为工具结果，使得 LLM 可以基于子 agent 的研究结果继续推理。

（2）服务端确定性调用（runAgentTool）。主 agent 在 @callable 方法、HTTP 处理器或后台定时任务中直接以编程方式调用 runAgentTool，不依赖 LLM 的工具选择决策。适用于多阶段工作流、定时报告生成、后台批量处理等场景。此时 parentToolCallId 为可选参数，未提供时子 agent 事件归属于无父工具调用的独立运行列表（unboundRuns），在前端可独立渲染。

两种模式的底层共享同一执行器：创建子 agent DO（通过 subAgent(Cls, runId)），在父端注册表中插入 starting 状态行，向所有连接的客户端广播 started 事件，驱动子 agent 执行推理轮次，将子 agent 的流式输出块封装为 chunk 事件广播，最终根据执行结果广播 finished、error 或 aborted 事件并更新注册表状态。

### 3. 运行身份与父子关联

子 agent 运行的身份体系与父子关联通过三层数据结构确立：

（a）runId 作为全局唯一运行标识。每次调用 runAgentTool 时，系统生成一个 runId（或由调用方显式提供），该标识跨父端注册表、子端映射表、事件流、重放、取消和清理 API 统一使用。runId 同时作为子 agent DO 的名称（即 subAgent(Cls, runId) 的 name 参数），使得运行标识与 DO 实例一一对应。

（b）父端运行注册表 cf_agent_tool_runs。该表存储在主 agent 的 SQLite 中，核心字段包括：run_id（主键）、parent_tool_call_id（关联的父工具调用标识，可为空）、agent_type（子 agent 类名，如 "Researcher"）、status（starting/running/completed/error/aborted/interrupted）、summary、error_message、display_order、started_at、completed_at。该表的职责是回答"某次运行属于哪个父工具调用、当前状态如何、回放时应合成什么生命周期事件"。

（c）子端运行映射表 cf_agent_tool_child_runs。该表存储在子 agent 自身的 SQLite 中，记录 runId 到内部 requestId（推理请求标识）和 streamId（可恢复流标识）的映射。当父 agent 崩溃后重启时，可通过 runId 从子 agent 查询这些内部标识以恢复回放能力，而无需让应用层暴露 request/stream 的内部语义。startAgentToolRun 操作按 runId 幂等：若子 agent 已存在该 runId 的记录，则返回现有状态而不创建重复的推理轮次。

### 4. 流式事件协议与实时展示

子 agent 的执行过程通过结构化事件协议从子 agent 传递到主 agent，再由主 agent 广播到所有 WebSocket 客户端。事件类型包括以下六种：

- started：子 agent 运行开始。携带 runId、agentType、inputPreview、displayOrder 和可选的 display 元数据（名称、图标）。在父端注册表插入行之后、子 agent 推理轮次启动之前广播，确保 UI 在收到任何 chunk 之前即可渲染面板。
- chunk：流式输出块。body 字段为 JSON 编码的 UIMessageChunk，与主 agent 自己的流式输出使用相同的词汇表。客户端可使用相同的 applyChunkToParts 原语重建子 agent 的消息部件（文本、推理过程、工具调用等），无需为子 agent 单独实现渲染逻辑。
- finished：子 agent 正常完成。携带 summary（子 agent 最终输出文本摘要）或结构化 output。
- error：子 agent 执行失败。携带 error 描述信息。
- aborted：子 agent 被显式取消。携带 reason 描述取消原因。
- interrupted：父 agent 在子 agent 运行期间崩溃或丢失观察者连接，重连后无法恢复实时观察，将运行标记为 interrupted。这是父端独有的状态——子 agent 不会自行报告 interrupted。

每条事件消息的 wire 格式为 { type: "agent-tool-event", parentToolCallId, sequence, replay?, event }。sequence 是父 agent 为每个子 agent 运行独立维护的单调递增序号（从 0 开始），包括生命周期事件和 chunk 事件共享同一序号空间。客户端使用 (parentToolCallId, runId, sequence) 三元组进行去重——因为不同子 agent 运行（甚至同一工具调用下的并行运行）都可能从 sequence 0 开始，仅凭 sequence 无法唯一标识事件。

### 5. 重连恢复与事件重放去重

系统区分"运行"与"观察"两个独立关注点。子 agent 运行是持久化工作——其推理轮次、流式输出块和最终结果持久存储在子 agent 自身的 SQLite 中。父 agent 的实时事件转发是该运行的一个观察者；断开观察者不应自动取消运行。

重连恢复流程如下：（1）父 agent 的 onConnect 被触发（客户端 WebSocket 重新连接）；（2）父 agent 遍历 cf_agent_tool_runs 中所有运行记录；（3）对每条记录，通过 subAgent 获取子 agent DO 引用，调用 getAgentToolChunks(runId) 获取已持久化的流式块；（4）按运行记录的 status 和 display_order 合成 started 事件，按 chunk_index 顺序发送 chunk 事件（标记 replay: true），最后合成 finished/error/interrupted 终端事件；（5）若运行状态为 starting 或 running，且子 agent 仍然活跃但有新的实时 chunk 继续产生，V1 阶段在回放完已存储块后将运行标记为 interrupted（附说明"实时重连接暂不支持"），未来阶段可通过 tailAgentToolRun 实现重连接实时尾随。

去重机制：客户端维护已接收的 (parentToolCallId, runId, sequence) 集合。重连时，回放事件中的 sequence 与原始实时广播时使用的 sequence 一致（父 agent 在 onConnect 回放时按相同的序号规则重新编号），因此客户端能正确跳过已经接收过的事件。chunk 事件中的 body（UIMessageChunk）是幂等的——重复应用同一 chunk 到同一消息部件不会产生重复内容。

父 agent 崩溃恢复的特殊处理：若父 agent 在子 agent 运行期间崩溃（DO 被驱逐），父 agent 重新激活时 onStart 执行协调逻辑——扫描 cf_agent_tool_runs 中所有非终端状态（starting/running）的行。对每条行：（a）若对应子 agent 不存在或子端无匹配运行记录，说明子 agent 从未启动或已被删除，标记为 interrupted；（b）若子 agent 报告 completed/error/aborted，回放已存储块并更新父端状态为对应终端状态；（c）若子 agent 报告 running（推理仍在进行），V1 回放已存储块后标记为 interrupted，未来支持实时重连接。

### 6. 取消、并发控制与幂等性

取消机制。取消操作从父 agent 向子 agent 通过 AbortSignal 链传播：（1）父 agent 的聊天轮次或工具调用被取消（用户点击停止按钮、关闭标签页等）；（2）父 agent 的 runAgentTool 通过 AbortSignal 检测到取消；（3）父 agent 调用子 agent 的 cancelAgentToolRun(runId) RPC 方法；（4）子 agent 内部的 per-turn AbortController 被触发；（5）子 agent 将 AbortSignal 传入 saveMessages({ signal })，终止推理循环；（6）父 agent 断开该运行的实时观察者流；（7）子 agent 向父 agent 报告 aborted 结果，父端注册表更新为 aborted 状态并广播 aborted 事件。

重要区分：断开观察者（如浏览器断开 WebSocket）本身不是取消请求。只有父 agent 当前操作的显式 AbortSignal 才应触发运行取消。已存储的 chunk 保持持久化——即使运行被取消，已经产生的流式输出块仍可通过重放获取。

并发控制。系统在父 agent 层面提供 maxConcurrentAgentTools 选项，限制同时处于 running 状态的子 agent 工具运行数量。当调用 runAgentTool 时若当前运行数已达上限，立即返回 error 事件而不创建新的子 agent DO。对于模型驱动的 agentTool 调用，LLM 会收到包含错误信息的结果以便决定是否等待后重试。子 agent 实例级别通过 _runInProgress 标志防止同一 DO 实例上的并发框架驱动轮次。

幂等性。startAgentToolRun 按 runId 幂等：若调用方传入已存在的 runId，终端运行返回已有结果而不重新执行；非终端运行返回当前状态而不启动重复工作。这使 runAgentTool 可安全用于重试路径、定时器回调和重连恢复，而不会意外重复 LLM 推理工作。

### 7. Drill-in 访问控制

子 agent 通过现有的子 agent 路由原语对外可寻址，URL 格式为 /agents/{parent-class}/{parent-name}/sub/{child-class}/{child-name}。这允许客户端直接连接到特定子 agent 的 WebSocket 进行 drill-in 查看（即深入查看某个子 agent 的完整对话历史）。

访问控制通过 onBeforeSubAgent 中间件钩子实现。该钩子在父 agent DO 上运行，在框架将请求转发到子 agent 之前触发。对于 agent 工具场景，推荐的访问控制策略包括：（1）严格注册表门控——检查 cf_agent_tool_runs 中是否存在匹配 (agentType, runId) 的记录，不存在则返回 404；（2）身份注入——在转发请求前将父 agent 的身份信息（如 x-inbox-id）注入请求头，供子 agent 的 onBeforeConnect/onBeforeRequest 进一步验证。默认的门控策略是宽松的（允许任意名称创建新 facet），但生产部署应覆盖为严格门控以防止通过猜测 runId 创建任意子 agent DO。

drill-in 的并发安全性：当框架驱动的 agent 工具轮次正在子 agent 上运行时，对该子 agent 的并发 runAgentTool 调用（相同 runId）因幂等性直接返回已有检查状态而非启动第二个轮次。drill-in 用户若在框架驱动轮次期间发送聊天消息，应被延迟或明确拒绝，而非静默交错到正在进行的轮次中。

### 8. 清理与保留策略

清理策略。系统默认保留子 agent 运行记录和子 agent DO 而不自动删除，因为完成后正是复盘、drill-in 和审计最有价值的时机。提供显式清理 API：clearAgentToolRuns() 清除所有运行记录及对应的子 agent DO；clearAgentToolRuns({ olderThan }) 按时间清理；clearAgentToolRuns({ status }) 按状态筛选清理。清理操作同时删除父端注册表行和对应的子 agent facet——仅删除注册表行而保留子 agent 会导致孤立副本（无法通过回放或 drill-in 访问）。

保留策略注意事项。当清理操作在子 agent 处于 starting/running 状态时执行，系统先取消该运行（cancelAgentToolRun），等待终止确认后再删除注册表行和 facet。跳过取消步骤将留下无观察者的孤立 LLM 推理工作。应用层可基于生命周期钩子（onAgentToolStart、onAgentToolFinish）实施自定义的 TTL、计数限制或基于聊天历史生命周期的联动清理策略。

### 9. 完整执行流程

一次完整的子 agent 工具调用走以下关键路径：

1. 父 agent 调用 runAgentTool(Cls, { input, parentToolCallId?, signal? })。
2. 框架生成 runId（若调用方未提供），通过 subAgent(Cls, runId) 获取或创建子 agent DO 引用。
3. 在父端 cf_agent_tool_runs 表中插入 status=starting 的行，包含 runId、parentToolCallId、agent_type、display_order 等字段。
4. 向所有连接的 WebSocket 客户端广播 agent-tool-event（kind: started），携带 runId、agentType 和 inputPreview。
5. 通过 DO RPC 调用子 agent 的 startAgentToolRun(runId, input, signal)，子 agent 在其 SQLite 中记录 cf_agent_tool_child_runs 映射行。
6. 子 agent 开始推理轮次，产生的每个 UIMessageChunk 通过可恢复流（ResumableStream）持久化到 SQLite（按 chunk_index 编号），同时通过 DO RPC 的 ReadableStream 回传给父 agent。
7. 父 agent 将每个 chunk 封装为 agent-tool-event（kind: chunk，sequence 递增），广播到所有 WebSocket 客户端。
8. 子 agent 推理完成，父 agent 通过 DO RPC 获取尾轮次的最终输出（getFinalTurnText），更新注册表状态为 completed，广播 finished 事件。若过程出错，按对应终端状态（error/aborted）更新。
9. LLM 驱动的调用将结果返回给父 LLM（completed 时返回 output/summary，非 completed 时返回结构化错误信息），使父 LLM 可基于子 agent 结果继续推理。

### 10. 技术效果

本方案的技术效果包括：

- 实时可见性：用户可在同一会话视图中实时观察每个子 agent 的执行进度、中间输出片段和最终结果，而非只能看到主 agent 汇总后的文本，显著提升复杂多步骤任务的可解释性和用户信任。
- 断线恢复：通过将子 agent 的流式输出持久化到独立 SQLite 并结合父端运行注册表和结构化事件协议，用户刷新页面或网络短暂断开后能完整恢复已发生的子 agent 执行过程，包括所有中间状态。
- 执行与观察解耦：子 agent 的运行独立于父 agent 的观察者连接。父 agent 崩溃或浏览器断开不会自动取消子 agent 运行，已持久化的结果仍可通过重连回放获取。
- 独立执行上下文：每个子 agent 拥有独立的 SQLite 存储和推理循环，支持长任务、并行任务（多个子 agent 在同一父工具调用下并行执行）和后续查看（drill-in）。子 agent 之间状态隔离，一个子 agent 的失败不影响其他并行运行的子 agent。
- 调用灵活性：既支持 LLM 自主决定调用子 agent（模型驱动），也支持服务端确定性工作流主动编排（程序驱动），还支持无父工具调用的后台子运行（如定时任务），三种场景共享同一底层机制。

### 11. 与现有 Agent 体系的兼容性

本方案完全兼容现有的 chat agent（AIChatAgent）和 Think agent 体系。子 agent 本身就是普通的 chat-capable agent 子类——它之所以成为"agent 工具"是因为父 agent 通过 runAgentTool 调度了它，而非因为它继承自某个特殊的基类。子 agent 需要满足的契约是能够运行可编程的聊天轮次（programmatic chat turn）、以 UIMessageChunk 形式产生流式输出、将输出块持久化到可恢复流、接受外部 AbortSignal 取消信号。Think agent 天然满足这些条件；AIChatAgent 通过适配器层（AgentToolChildAdapter）同样可满足。

现有应用迁移为零成本：（1）routeAgentRequest 在 URL 不包含 /sub/ 时的行为不变；（2）onBeforeSubAgent 默认宽松（转发原始请求）；（3）useAgent 不传入 sub 参数时行为不变；（4）subAgent/deleteSubAgent 在维护注册表副作用的同时保持现有返回类型和失败模式不变。应用开发者无需重写现有 agent 框架即可逐步采用子 agent 工具编排能力。

### 12. 风险与待确认问题

以下是当前方案中需要后续确认和完善的技术风险点：

- 实时尾随重连接（Live-tail Reattach）：V1 阶段不支持在父 agent 崩溃后重新连接到正在运行的子 agent 的实时输出流。当前的回退方案是回放已存储块后将运行标记为 interrupted。后续需实现 tailAgentToolRun 以支持从指定 sequence 之后重连接实时流——协议中的 runId、sequence 和观察/运行分离设计已为此预留兼容空间。
- 跨父 LLM 轮次恢复：当 agentTool 调用是父 LLM 推理轮次的一部分时，恢复子 agent 运行不足以恢复父 LLM 轮次——除非父 LLM 的聊天恢复机制也能从中断的工具结果继续。在当前实现中，父 LLM 驱动的 agent 工具在父 agent 崩溃后可能只能恢复子 agent 的记录（transcript），而父端工具调用仍标记为 interrupted。程序化 runAgentTool 的恢复路径更清晰——应用代码可在事后检查运行结果而无需重建一个进行中的 LLM 轮次。
- 结构化输出提取：V1 阶段不为 agent 工具自动从自然语言输出中提取结构化 JSON。如果 outputSchema 被设置而子 agent 仅产生文本摘要，结果合成会将运行标记为 error。需要结构化输出的应用需要在子 agent 自身的提示词/工具契约中明确要求结构化输出。
- 成本与可观测性：子 agent 可能显著增加 LLM 调用成本（父 LLM 并行扇出 5 个 research 调用意味着 5 个子 agent Think 轮次独立调用模型）。V1 提供生命周期钩子（onAgentToolStart/onAgentToolFinish）供应用层集成日志、计量和审计，但不提供标准化的计费或 OpenTelemetry 追踪格式。
- 跨机器子 agent：Facet 机制要求子 agent 与父 agent 位于同一机器上。未来可能需要支持远程子 agent（通过标准 DO stub 跨机器调用），但 API 和失败模式将有显著差异。
