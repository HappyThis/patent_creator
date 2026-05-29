## 技术方案

本方案在现有服务端 codemode 体系（基于 DynamicWorkerExecutor 与 Workers RPC 的隔离沙箱执行）基础上，扩展一套面向浏览器的隔离代码执行与工具编排机制。核心思路是将 codemode 模式迁移到浏览器侧：LLM 仍然生成一段 JavaScript 异步箭头函数作为编排代码，但该代码在浏览器本地的隔离沙箱中执行，通过消息通道调用主页面注册的浏览器私有工具，工具的实际执行由主页面代理完成，生成代码无法直接访问页面 DOM、全局对象或 JavaScript 上下文。

整体架构分为四个核心部分：（1）浏览器侧隔离执行环境，负责加载和执行 LLM 生成的编排代码；（2）动态工具注册与命名空间体系，支持主页面以声明方式注册浏览器私有工具，并自动生成供 LLM 使用的类型描述和输入输出 schema；（3）安全工具调度协议，在隔离环境与主页面之间建立受控的工具调用通道，包含工具名规范化、非法调用拦截、异常传播和日志收集；（4）生命周期管理，覆盖沙箱就绪、代码执行、超时中断、结果回传和资源清理的全流程。方案在现有 agent chat 的 client tool 协议（CF_AGENT_TOOL_RESULT）和 codemode 的 ToolProvider/ResolvedProvider 抽象基础上扩展，复用已有消息通道和会话管理能力，不引入额外重型依赖。

### 浏览器侧隔离执行环境

隔离执行环境采用 sandboxed iframe 作为代码载体。iframe 的 sandbox 属性设置为 "allow-scripts"，不包含 allow-same-origin 和 allow-top-navigation，确保其中运行的代码无法访问主页面同源存储（Cookie、localStorage、sessionStorage）、无法操作主页面 DOM、无法导航顶层窗口。iframe 通过 src 加载一个预编译的极简 HTML 页面（bootstrap page），该页面仅包含必要的执行器脚本，不引入任何第三方库或主页面资源引用。

执行器脚本在 iframe 内部构建一个受控的 JavaScript 执行环境。它接收来自主页面的编排代码字符串，通过 new Function() 或间接 eval 在隔离全局作用域中执行。执行器劫持 console.log / console.warn / console.error 将日志捕获到内部缓冲区；拦截全局 fetch、XMLHttpRequest、WebSocket 等网络 API，默认禁止任何出站网络请求；对 setTimeout / setInterval 等定时器进行包装，确保超时控制和清理。与现有服务端 DynamicWorkerExecutor 的 console 劫持和 globalOutbound 控制策略保持一致。

执行器通过 postMessage 通道与主页面通信。该通道是隔离环境与外部唯一的通信路径。执行器不暴露任何直接引用主页面对象的接口——所有工具调用都必须通过此消息通道发送给主页面代理执行。iframe 创建后、执行器初始化完成时，向主页面发送 ready 消息，表明沙箱已就绪可接收代码。主页面在收到 ready 消息后才允许向沙箱发送编排代码。

### 动态工具注册与命名空间管理

主页面通过声明式 API 注册浏览器私有工具，每个工具包含：工具名称（name）、自然语言描述（description）、输入参数的 JSON Schema 定义（inputSchema）、输出 Schema（outputSchema，可选）、以及实际执行函数（execute）。这些字段与现有 ClientToolSchema（name、description、parameters）兼容并扩展，也与 codemode 体系中的 ToolDescriptor 接口对齐。注册后的工具存储在主页面侧的工具注册表中，执行函数保留在主页面闭包中，不会泄漏到隔离环境。

为支持多来源工具的共存与冲突处理，方案引入命名空间/来源标识机制。每个工具集合关联一个来源标识（source），可以是固定字符串（如 "page" 表示页面工具、"component" 表示组件暴露的能力）或由工具提供方声明的唯一标识符。工具在注册时以 source + ":" + toolName 构成全局唯一键。当不同来源注册了同名工具时，后注册者不覆盖先注册者，而是通过来源标识明确区分；LLM 在生成代码时通过命名空间前缀调用：sourceName.toolName(args)。

工具名规范化沿用现有 codemode 的 sanitizeToolName 策略：将连接符、点号和空格替换为下划线，剔除非法标识符字符，数字前缀时添加前导下划线，JavaScript 保留字后缀下划线。该规范化同时应用于工具注册表键名和隔离环境中 Proxy 的属性名，确保工具名在 JavaScript 语法层面合法且唯一。工具注册时检测重复键，对同一来源内的重复工具名执行覆盖（后者覆盖前者），并发出控制台警告；跨来源同名工具通过来源前缀自然区分。

### 工具描述生成与 Schema 规范化

为让 LLM 知晓当前可用的浏览器侧工具及其调用方式，方案在注册表之上提供工具描述生成能力。生成过程遍历所有已注册工具，提取每个工具的命名空间、规范化名称、自然语言描述和 JSON Schema 输入输出定义，组装为 TypeScript 风格的接口声明字符串。格式沿用现有 codemode 的 generateTypes 输出风格：每个命名空间对应一个 interface，每个工具对应一个方法签名，参数类型从 JSON Schema 推导为 TypeScript 类型，返回值类型从 outputSchema（如有）推导。

对于输入输出 schema，方案兼容两种来源：Zod schema（来自 AI SDK 的 tool() 定义）和 JSON Schema 对象（来自 wire 格式的 ClientToolSchema）。当工具通过 Zod 定义时，使用现有的 asSchema + generateTypes 路径生成类型；当工具以 JSON Schema 注册时，使用 jsonSchemaToTypeString 进行 JSON Schema 到 TypeScript 类型的转换，支持基础类型、object、array、union、enum、nullable 等常见 JSON Schema 构造。类型描述字符串通过 {{types}} 占位符注入 LLM 的 system prompt 或工具描述中。

工具描述是动态的：当主页面在运行时注册新工具或移除已有工具时，注册表变更触发类型描述重新生成。LLM 在每个对话轮次中可以获取当前最新的可用工具视图。对需要用户审批才能执行的高风险工具（如访问敏感页面状态），沿用现有 needsApproval 标记机制——带此标记的工具在 filterTools 阶段被排除，不暴露给沙箱，但仍可在审批流程通过后以单独通道调用。

### 安全工具调度协议与代理边界

隔离环境与主页面之间的工具调用通过结构化消息协议完成。协议定义以下消息类型：（1）ready：沙箱初始化完成，可接收编排代码；（2）execute：主页面向沙箱发送编排代码字符串及可用工具的命名空间配置；（3）tool_call：沙箱向主页面请求调用指定工具，携带命名空间、工具名和序列化参数；（4）tool_result：主页面返回工具执行结果或错误信息；（5）log：沙箱上报执行日志；（6）done：编排代码执行完成，附带返回值；（7）error：执行异常，附带错误信息和堆栈；（8）timeout：执行超时被中断。所有消息包含单调递增的序列号（seq），用于匹配请求-响应对和检测消息丢失。

沙箱内部采用 Proxy 模式暴露工具，与现有 codemode 的 ToolDispatcher 代理机制一致。主页面向沙箱发送 execute 消息时同时传递命名空间配置——每个命名空间包含名称和该命名空间下所有工具的名称列表（不包含执行函数本身）。沙箱侧为每个命名空间创建一个 Proxy 对象，当编排代码调用 namespace.toolName(args) 时，Proxy 的 get 陷阱将命名空间、方法名和参数序列化为 tool_call 消息通过 postMessage 发送给主页面。

主页面侧消息处理器按以下步骤处理 tool_call：第一步，来源与命名空间校验——根据命名空间名称查找对应工具集合，未注册则返回 "namespace not found" 错误。第二步，工具名反查——将沙箱侧已规范化的工具名在注册表中反查原始定义。第三步，参数校验——使用工具的 inputSchema 对传入参数进行结构校验，失败则返回 schema violation 错误。第四步，执行与超时保护——调用 execute 函数并设置单次工具调用的超时计时器（默认 10 秒）。第五步，结果序列化——将返回值通过结构化克隆算法序列化为 tool_result 消息发回沙箱。

异常传播遵循以下规则：工具执行函数抛出的 Error 对象，其 message 被提取并包装为带 "tool_error" 标记的错误响应，在沙箱中重新抛出为 Error 以便编排代码的 try-catch 捕获；参数校验失败产生 "validation_error" 标记；命名空间/工具名无效产生 "not_found" 标记；执行超时产生 "timeout" 标记。沙箱侧 Proxy 在接收到任意错误响应时抛出异常，使编排代码能够自然地使用 try-catch 处理工具调用异常。

安全控制体现在多个层面。第一，沙箱 iframe 设置 sandbox="allow-scripts" 且不含 allow-same-origin，无法通过 parent、top 或 opener 引用主页面对象。第二，工具执行边界明确——只有通过注册表注册的 execute 函数才能被调用，沙箱无法调用未注册的任意函数，也无法间接访问主页面 DOM 或全局变量。第三，工具调用参数经过 schema 校验后才传入 execute 函数，LLM 生成的任意参数不会直接注入主页面上下文。第四，日志收集在沙箱内部完成，只通过 log 消息将文本日志上报，不传递对象引用。第五，网络隔离——沙箱中 fetch 等网络 API 被禁用，编排代码无法发起外部网络请求，不能将页面数据外泄。

### 生命周期管理

每次浏览器侧 codemode 执行具有明确的五阶段生命周期：准备（prepare）、就绪（ready）、执行（executing）、完成/错误/超时（done/error/timeout）、清理（cleanup）。

准备阶段：主页面创建 sandboxed iframe 并设置 sandbox 属性，加载 bootstrap 执行器页面。同时，主页面快照当前工具注册表状态，为本次执行生成类型描述字符串，构建命名空间配置（仅包含工具名称列表，不含执行函数）。工具注册表的快照保证执行期间的工具视图一致——执行开始后注册的新工具不会出现在当前编排代码的可用工具中，避免中途变更导致的不一致。

就绪阶段：执行器脚本初始化完成后向主页面发送 ready 消息。主页面收到 ready 后，将编排代码字符串、命名空间配置和超时时长通过 execute 消息发送给沙箱。如果 iframe 在指定时间内（默认 5 秒）未发送 ready 消息，主页面判定沙箱初始化失败，触发 error 流程并执行清理。

执行阶段：沙箱通过 Promise.race 同时运行编排代码和超时计时器。编排代码通过 Proxy 调用工具时，通过 tool_call / tool_result 消息与主页面交互。console.log 等输出通过 log 消息流式上报。执行期间，主页面维护与本次执行关联的计时器、消息处理器和资源句柄。单个工具调用的超时和整体编排代码的超时独立计时。

完成/错误/超时阶段：编排代码正常返回时，返回值通过 done 消息携带序列化结果发送给主页面。编排代码抛出未捕获异常时，error 消息携带错误信息和从沙箱捕获的堆栈。执行超时时，主页面侧的计时器触发，向沙箱发送 abort 消息，沙箱中断执行并上报已收集的日志。所有终态消息（done/error/timeout）均包含执行期间的完整日志数组。

清理阶段：主页面在收到终态消息后，移除 iframe 的 message 事件监听器，清理超时计时器，从 DOM 中移除 iframe 元素，释放其占用的内存。对于需要复用的沙箱实例（如同一会话内的多次执行），可配置沙箱池策略——保留 iframe 但重置其内部状态，发送 reset 消息清空日志缓冲区和 Proxy 绑定。清理阶段同时处理异常情况：如果主页面在关闭或导航时仍有未完成的执行，通过 beforeunload 事件尝试向沙箱发送 abort 消息并等待确认。

### 与现有 codemode 及 agent chat 体系的集成

浏览器侧 codemode 方案设计为与现有体系无缝协作，而非替代。具体集成点如下：

与 codemode 体系集成：浏览器侧执行器（BrowserSandboxExecutor）实现与 DynamicWorkerExecutor 相同的 Executor 接口（execute(code, providers)），使得上层 createCodeTool 无需感知执行位置差异。新增的 BrowserToolProvider 类型扩展 ToolProvider 接口，增加 source 字段标识浏览器工具来源。当 createCodeTool 检测到 tools 数组中含有 BrowserToolProvider 时，自动将浏览器工具的类型描述合并到整体的 {{types}} 中，LLM 在一次代码生成中可以同时编排服务端工具和浏览器端工具。

与 agent chat 的 client tool 体系集成：现有 client tool 通过 CF_AGENT_TOOL_RESULT 消息将工具调用从服务端转发到客户端执行。浏览器侧 codemode 在 client tool 的基础上增加了一层编排能力——LLM 不再逐个调用 client tool，而是生成一段代码在浏览器侧编排多个 client tool 的调用序列。这对应了 codemode 对标准 tool calling 的增强逻辑：原本需要多次 round-trip（LLM → server → client → server → LLM → ...）的多步工具编排，压缩为一次 round-trip（LLM → server → client 沙箱 → 多步工具调用 → server → LLM）。

协议层面，在现有 CF_AGENT_TOOL_RESULT 协议基础上扩展两个新消息类型：CF_AGENT_CODE_EXECUTE（服务端请求客户端在沙箱中执行编排代码）和 CF_AGENT_CODE_RESULT（客户端返回执行结果）。现有的 autoContinueAfterToolResult 机制同样适用于代码执行结果——服务端收到 CF_AGENT_CODE_RESULT 后自动调用 onChatMessage() 继续 LLM 对话。客户端 useAgentChat 新增 onCodeExecute 回调，负责创建沙箱、注入浏览器工具、执行编排代码并返回结果，其接口设计与现有 onToolCall 保持一致。

与 MCP 和 agent-tools 体系协作：浏览器侧工具也可以通过 MCP（Model Context Protocol）风格暴露——主页面作为一个轻量 MCP 服务端点，将已注册的浏览器工具以 MCP tools/list 和 tools/call 语义对外提供。agent-tools 中的子 agent 调度能力保持不变：父 agent 通过 agentTool 将浏览器侧 codemode 作为一个整体工具暴露给 LLM，子 agent 负责管理沙箱生命周期和工具注册表。

### 风险与待确认问题

以下为当前设计中需要后续确认和验证的技术风险点：

（1）沙箱 iframe 的跨域限制可能影响某些浏览器工具的执行闭包——如果工具的执行函数依赖主页面闭包中的 DOM 引用或框架内部状态，而这些引用在工具注册时被捕获但在沙箱执行时可能已失效（如 SPA 路由切换导致 DOM 卸载），需要评估闭包生命周期的安全性。建议工具执行函数内部自行处理 DOM 存在性检查。

（2）postMessage 的结构化克隆算法对某些 JavaScript 值类型（Function、Symbol、DOM 节点、包含循环引用的对象）不支持序列化。工具返回值需限制为可序列化类型，复杂场景下建议工具实现自行将结果转为 JSON 兼容格式。需在开发文档中明确序列化边界。

（3）在浏览器主线程繁忙（如正在处理密集渲染或用户交互）时，postMessage 的消息处理可能被延迟，影响工具调用的响应时间。可考虑将工具执行调度器运行在 Web Worker 中，通过 MessageChannel 与沙箱通信，避免主线程阻塞。

（4）沙箱的 sandbox 属性配置需要针对不同浏览器的兼容性进行测试。allow-scripts 在主流浏览器中均支持，但某些企业环境或旧版浏览器可能不支持或行为有差异。此外，某些浏览器扩展可能绕过 iframe sandbox 限制，需评估安全影响。

（5）编排代码中若存在无限循环或死循环（不含异步操作），超时计时器可能无法触发——因为 Promise.race 依赖事件循环，而同步死循环会阻塞事件循环。可在沙箱中注入循环计数限制（如限制最大迭代次数），或在 Web Worker 中运行编排代码利用 Worker 可被 terminate 的特性实现强制终止。
