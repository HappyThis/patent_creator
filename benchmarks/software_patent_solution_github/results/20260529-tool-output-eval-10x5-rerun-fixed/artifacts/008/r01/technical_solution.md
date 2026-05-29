## 技术方案

本方案在现有 codemode（服务端 Worker 隔离执行 LLM 生成代码）和 client tool（浏览器侧工具调用回传）体系基础上，提出一套浏览器侧隔离执行 LLM 编排代码、安全调度页面动态工具的技术方案。方案通过浏览器沙箱隔离执行、命名空间化工具管理、结构化消息调度协议和完整生命周期控制，使 LLM 能够在浏览器端以代码形式编排和组合仅存在于浏览器页面中的动态工具，同时保障主页面环境不被生成代码污染。

### 整体架构

系统由三个核心角色组成：主页面（Host Page）、隔离沙箱（Sandbox）和工具注册表（Tool Registry）。主页面是浏览器中运行应用的真实页面，持有对 DOM、页面状态、浏览器 API 和前端组件的访问能力。隔离沙箱是一个与主页面 JS 上下文完全分离的执行环境，接收 LLM 生成的编排代码并在其中执行。工具注册表运行在主页面侧，维护所有已注册的浏览器侧动态工具的元数据（名称、描述、输入输出 schema、来源标识）和实际执行函数。沙箱通过一个受控的结构化消息通道与主页面通信，所有工具调用必须经过该通道由主页面代理执行，沙箱自身无法直接访问任何主页面对象。

### 隔离沙箱架构

隔离沙箱采用浏览器原生沙箱化 iframe（sandbox attribute）结合 Web Worker 的双层隔离模型。外层 iframe 设置 sandbox="allow-scripts" 策略，不包含 allow-same-origin、allow-top-navigation、allow-popups 等权限，确保执行环境与主页面非同源、无法访问主页面 DOM。内层在 iframe 中创建 Web Worker 执行 LLM 生成代码，Worker 线程仅能通过 postMessage 与 iframe 的宿主脚本通信，再由宿主脚本通过结构化克隆通道向主页面转发工具调用请求和接收结果。该双层模型比单独使用 iframe 或 Worker 提供了更强的纵深隔离：iframe 阻断同源访问，Worker 阻断 DOM 访问，两者结合确保生成代码无法绕过消息通道直接操作主页面。

### 工具命名空间与来源标识

每个浏览器侧动态工具具有统一的四元组标识：(namespace, toolName, sourceType, version)。namespace 表示工具的命名空间，例如 "browser" 表示浏览器原生能力、"page" 表示页面级工具、"component" 表示前端组件暴露的能力。toolName 是工具在其命名空间内的唯一名称。sourceType 标识工具的来源类型，包括 "browser-api"（浏览器原生 API，如 clipboard、geolocation）、"page-state"（页面状态读取，如当前选择文本、表单数据）、"component-exposed"（前端组件通过注册接口暴露的能力）、"custom"（开发者自定义工具）。version 为语义化版本号，用于在工具接口变更时进行兼容性管理。

工具注册时，主页面调用 ToolRegistry.register(namespace, definition) 方法。definition 对象包含 name、sourceType、version、description、inputSchema（JSON Schema 7 格式）、outputSchema（可选）和 execute 函数。注册表内部以 "namespace:toolName" 作为全局唯一键，执行去重和冲突检测。当注册相同键的工具时，按 version 优先保留高版本，并记录冲突日志供开发者诊断。

### 工具名规范化与冲突处理

工具名在进入沙箱前需规范化为合法 JavaScript 标识符，以便 LLM 生成代码可直接作为函数名调用。规范化规则：将连字符(-)、点(.)和空格替换为下划线(_)，删除其他非法字符；以数字开头的名称前加下划线前缀；若规范后为 JavaScript 保留字（如 class、return、new），追加下划线后缀。例如，工具 "browser.get-selection" 规范化为 "browser_get_selection"，"page.2d-context" 规范化为 "page_2d_context"。该规范化算法与现有 codemode 的 sanitizeToolName 保持一致。

冲突处理采用三层策略：第一层，同一命名空间内不同来源类型注册同名工具时，按优先级 component-exposed > browser-api > page-state > custom 自动裁决，低优先级工具被遮蔽并产生警告。第二层，不同命名空间下的同名工具名（如 browser.getText 与 page.getText）因命名空间前缀不同，在沙箱中以不同函数名暴露，不存在冲突。第三层，沙箱初始化时向 LLM 提供的工具描述中明确标注每个工具的 namespace 和 sourceType，使 LLM 在生成代码时能够根据来源语义选择正确的工具。

### 输入输出 Schema 与类型生成

每个工具定义必须包含 inputSchema（JSON Schema 7 格式）和可选的 outputSchema。inputSchema 描述工具的输入参数结构，用于在沙箱侧对 LLM 生成的调用参数做本地校验，以及在主页面侧对收到的调用请求做二次校验。outputSchema 描述工具的返回结构，主页面在工具执行完成后对结果进行校验后再返回沙箱。

当 LLM 需要获知当前可用工具时，系统遍历 ToolRegistry 中所有已注册工具，生成类型声明块：对于每个命名空间，生成对应的 TypeScript 风格的接口定义，包括工具函数签名（参数类型从 inputSchema 推导，返回类型从 outputSchema 推导）和 JSDoc 描述。该类型声明块嵌入 code tool 的描述中（类似现有 codemode 的 {{types}} 占位符机制），使 LLM 能够生成类型正确的调用代码。对于使用 JSON Schema 定义的工具（非 Zod），采用 jsonSchemaToTypeString 算法将 JSON Schema 转换为 TypeScript 类型字符串，支持基础类型、对象、数组、枚举、联合类型和可选属性的转换。

### 隔离环境生命周期

每次代码执行具有以下明确的阶段生命周期：

- 准备阶段（prepare）：主页面收集当前 ToolRegistry 中所有工具的元数据和类型声明，构建工具描述上下文。创建隔离沙箱（iframe 宿主 + Web Worker），注册消息通道。设置执行超时定时器（默认 30 秒，可配置）。生成沙箱内 Proxy 对象，每个命名空间对应一个 Proxy，LLM 代码通过 namespace.toolName(args) 调用工具。
- 就绪阶段（ready）：沙箱初始化完成后向主页面发送 ready 消息。主页面将工具描述和类型声明注入沙箱，沙箱侧构建 Proxy 代理对象。ready 消息确认后，主页面将 LLM 生成的代码注入沙箱开始执行。
- 执行阶段（execute）：沙箱内代码通过 Proxy 调用工具时，Proxy 将调用序列化为 {namespace, toolName, callId, args} 消息，通过 postMessage 发送至主页面。主页面收到后查找 ToolRegistry 中对应的 execute 函数，先校验 args 是否符合 inputSchema，通过后执行工具函数，将结果封装为 {callId, result} 或 {callId, error} 返回沙箱。沙箱内 Proxy 解析结果，若为 error 则在沙箱内抛出异常供 LLM 代码捕获处理。
- 超时阶段（timeout）：若执行时间超过预设阈值，主页面通过 AbortController 信号中止执行，向沙箱发送 abort 消息，沙箱内 Promise.race 中的超时 Promise 先于代码执行 Promise 完成，触发超时错误。超时后立即进入清理阶段。
- 错误阶段（error）：沙箱内代码执行异常（包括工具调用错误、语法错误、运行时异常）被 try/catch 捕获，结构化为 {error: message, stack, toolCallId?} 格式返回主页面。主页面将错误信息记录到日志收集器。
- 清理阶段（cleanup）：执行结束（正常返回、超时或异常）后，主页面执行清理流程：终止 Web Worker（worker.terminate()），移除 iframe，清理消息事件监听器，释放与本次执行关联的 AbortController 和定时器，将执行日志写入日志缓冲区。

### 工具调度协议与消息通道

沙箱与主页面之间的工具调用采用结构化的请求-响应消息协议，基于 postMessage 实现。所有消息遵循统一的消息信封格式：{type: "tool:call"|"tool:result"|"tool:error"|"system:ready"|"system:abort"|"system:log", payload: object, meta: {callId: string, timestamp: number}}。

工具调用流程：沙箱内 Proxy 的 get 陷阱拦截 namespace.toolName 访问，返回一个异步函数。当 LLM 代码调用该函数时，生成唯一的 callId，构造 {type: "tool:call", payload: {namespace, toolName, args}, meta: {callId, timestamp}} 消息发送至主页面。主页面收到后在 ToolRegistry 中查找对应的 execute 函数，先执行 inputSchema 校验，校验通过后执行 execute(args)，将结果构造为 {type: "tool:result", payload: {callId, result}, meta} 或 {type: "tool:error", payload: {callId, error: {message, code}}, meta} 返回沙箱。沙箱内 Proxy 维护一个 callId -> Promise resolver 的映射表，收到结果消息后根据 callId resolve 对应的 Promise。

非法工具调用处理：当主页面收到工具调用请求但 ToolRegistry 中不存在对应的 (namespace, toolName) 条目时，返回 type: "tool:error" 消息，error.code 设置为 "TOOL_NOT_FOUND"，error.message 包含请求的工具名和可用工具列表。当 args 不符合 inputSchema 时，返回 code 为 "INVALID_ARGUMENTS" 的错误，附带 schema 校验的详细失败信息（如缺失字段、类型错误等）。这些错误以异常形式在沙箱内抛出，LLM 代码可 try/catch 捕获并决定重试或替换策略。

### 主页面工具代理边界

主页面作为工具的实际执行者，承担代理边界职责。该边界由以下机制保障：

- 工具白名单执行：只有通过 ToolRegistry.register() 显式注册的工具才能被沙箱调用。LLM 无法在生成代码中调用任意主页面函数或访问任意全局变量。
- Schema 强制校验：主页面在每次工具执行前对 args 进行 inputSchema 校验，执行后对 result 进行 outputSchema 校验（如已定义）。不通过校验的调用返回错误而不执行实际工具函数。
- 执行超时控制：每个工具执行的单独超时（默认 10 秒）和整体代码执行的全局超时（默认 30 秒）两级控制。任何一层超时均触发清理流程。
- 异常隔离：工具执行函数内部抛出的异常被主页面侧 try/catch 捕获，转换为结构化错误消息返回沙箱，原始异常对象不会穿越消息通道。这防止了主页面内部状态通过异常对象泄漏到沙箱。
- 并发限制：主页面维护当前会话的活跃工具调用计数，超过上限（默认可配置，如 10 个并发调用）时新的工具调用请求被排队或拒绝（返回 TOO_MANY_REQUESTS 错误）。

### 安全控制机制

本方案通过多层安全控制确保 LLM 生成代码无法直接访问或污染主页面环境：

- 同源隔离：iframe 不设置 allow-same-origin，沙箱页面与主页面不同源，无法通过 window.parent 或 window.top 访问主页面对象。
- DOM 隔离：代码在 Web Worker 内执行，Worker 全局作用域无 document、window 等 DOM API，完全无法访问页面 DOM 树。
- 网络隔离：沙箱 iframe 的内容通过 blob URL 或 data URL 加载，不依赖外部网络资源。Worker 内不注入 fetch 或 XMLHttpRequest 能力，生成代码无法发起网络请求。
- 存储隔离：Worker 内无 localStorage、sessionStorage、indexedDB 访问权限，无法读写主页面存储数据。
- 代码规范化：LLM 生成的代码在执行前经过 normalizeCode 处理：去除 import/export 语句，包裹为 async 箭头函数，禁止使用 eval() 和 Function() 构造函数（通过 CSP 策略和代码静态检测双重保障）。
- 消息通道限流：主页面对所有来自沙箱的消息进行速率限制，防止恶意代码通过高频消息攻击主页面事件循环。
- 日志安全过滤：沙箱内 console.log/warn/error 输出被重定向收集，不会直接输出到浏览器控制台。收集的日志在返回主页面时经过敏感信息过滤（如自动遮蔽可能包含令牌、密码等敏感字段的值）。

### 异常传播与日志收集

异常传播采用结构化错误模型，确保跨消息通道的错误信息既包含足够诊断信息又不泄漏主页面敏感内部状态。沙箱内代码抛出的异常被 catch 后构造为 {name: string, message: string, stack?: string, toolCallId?: string} 结构。工具调用失败产生的错误附加 toolCallId 字段，使开发者能够关联到具体的失败调用。主页面侧工具执行异常按错误码分类：TOOL_NOT_FOUND（工具未注册）、INVALID_ARGUMENTS（参数校验失败）、TOOL_EXECUTION_ERROR（工具执行异常）、TOOL_TIMEOUT（工具执行超时）、TOO_MANY_REQUESTS（并发超限）、UNKNOWN（未知错误）。沙箱内代码可根据错误码决定重试、降级或放弃。

日志收集采用分级缓冲机制。沙箱内的 console.log/warn/error 输出被重定向捕获，每条日志记录包含级别、消息内容、时间戳。执行期间日志缓存在沙箱内存中，执行结束后（无论正常、异常或超时）日志作为 ExecuteResult.logs 数组返回主页面。主页面将日志合并到会话级日志缓冲区，并可选择性上报至服务端（如现有的 observability 通道）。日志条目结构为 {level: "log"|"warn"|"error", message: string, timestamp: number, source: "sandbox"|"host"}。

### 与现有体系协作

本方案与现有体系的关系如下：

- 与 codemode 的关系：现有 codemode 基于 Cloudflare Workers 的 DynamicWorkerExecutor 在服务端执行 LLM 生成代码，通过 Workers RPC 和 ToolDispatcher 调度工具。本方案可视为 codemode 的浏览器侧对等实现：同样采用 Proxy + 命名空间的工具暴露模式，同样基于 isolate-execute-result-cleanup 生命周期，同样支持 ToolProvider 的多命名空间体系。差异在于执行环境从 Worker 变为浏览器沙箱，工具调度从 Workers RPC 变为 postMessage 协议。两者共用 sanitizeToolName、normalizeCode、generateTypes 等基础能力。
- 与 client tool 的关系：现有 client tool 机制中，LLM 每次只能调用单个客户端工具，工具调用通过 WebSocket 发送到浏览器，由 onToolCall 回调逐个处理。本方案允许 LLM 生成代码一次性编排多个浏览器工具的组合调用，在浏览器本地完成逻辑判断、循环、条件分支和错误处理，大幅减少服务端与浏览器之间的往返次数。执行结束后结果一次性返回服务端。
- 与 agent chat / think 的协作：agent chat 的 onChatMessage 流程中，当 LLM 选择调用 browser codemode 工具时，服务端将代码块和工具描述转发至浏览器侧 WebSocket。浏览器侧 SandboxManager 接收后执行隔离代码，执行期间的工具调用消息通过主页面 ToolRegistry 代理执行，执行结果通过 WebSocket 返回服务端，触发 auto-continuation 流程。think 的 client tool schema 传递机制（ClientToolSchema 的 name/description/parameters 格式）可复用于向 LLM 描述可用的浏览器侧工具。

### 技术效果

本方案的技术效果包括：（1）通过浏览器侧隔离沙箱执行 LLM 生成的编排代码，避免了将浏览器私有工具搬到服务端的架构复杂度和安全风险；（2）双层隔离（sandbox iframe + Web Worker）确保生成代码无法直接访问或污染主页面对象，安全边界清晰可验证；（3）命名空间化的工具管理和来源标识使得多来源的动态工具能够共存且避免冲突，开发者可以明确知晓每个工具的来源和用途；（4）基于 JSON Schema 的输入输出校验在沙箱侧和主页面侧双重执行，既防止了非法参数注入，也保证了返回结果的结构一致性；（5）完整的生命周期管理（prepare-ready-execute-timeout-error-cleanup）确保每次执行都是独立的、可预测的，资源可被可靠回收；（6）与现有 codemode 体系共享工具描述生成、名称规范化、代码规范化等基础能力，最大程度复用现有基础设施，减少额外依赖。

### 风险与待确认点

以下为当前方案中需要后续确认或存在技术风险的点：

- 跨域限制：sandbox iframe 不设置 allow-same-origin 意味着 iframe 内的 blob URL 与主页面不同源。在某些浏览器中，blob URL 的 iframe 创建 Worker 可能受到额外限制，需要验证各主流浏览器的兼容性表现。
- Web Worker 内的 postMessage 中转：当前方案中 iframe 宿主脚本充当 Worker 与主页面之间的消息中转。需要确认 iframe 页面被 sandbox 属性限制时，其内部脚本能否正常调用 postMessage 与主页面通信（通常可以，但需验证）。
- CSP 策略兼容：沙箱内的 CSP 需要足够严格以禁止 eval/Function，同时又要足够宽松以允许动态代码执行。blob URL 加载的页面可以设置独立的 CSP 头，但在某些 CSP 配置下可能受限。
- 工具注册的时序：LLM 代码执行期间，主页面侧可能有新的工具被动态注册或注销。需要明确执行期间工具集是否冻结（推荐冻结，保证执行期间工具集不变），还是允许动态变化。
- 与现有 DynamicWorkerExecutor 的代码复用：codemode 的 executor 接口设计为通用抽象，但当前 DynamicWorkerExecutor 强依赖 Workers 平台的 WorkerLoader。需要新增 BrowserSandboxExecutor 实现 Executor 接口，保持与现有 ToolProvider/ResolvedProvider 体系的兼容性。
- 性能考量：每次执行都需要创建新的 iframe 和 Worker，存在一定的初始化开销。对于高频小任务场景，可考虑引入沙箱复用池，在清理阶段不销毁而是重置沙箱状态以供下次复用。
