## 技术方案

### 技术问题

现有的 codemode 能力将 LLM 生成的编排代码在服务端 Cloudflare Worker 隔离沙箱中执行，工具调用通过 Workers RPC 回传至宿主 Worker。但该模式无法覆盖仅存在于浏览器页面中的动态工具——例如读取当前页面用户选区、访问页面运行时状态、调用前端组件暴露的能力等。如果将这些浏览器私有能力全部搬到服务端，不仅实现成本高，还会破坏前端架构的封装边界。另一方面，若放任 LLM 生成的代码直接在主页面上执行，存在安全风险：生成代码可能意外或恶意访问、修改主页面对象（DOM、全局变量、Cookie 等），污染页面状态。

### 核心技术方案

本方案在浏览器侧引入一种浏览器端代码执行器（BrowserCodeExecutor），实现与 codemode 的 Executor 接口兼容，使 LLM 生成的编排代码在浏览器侧隔离沙箱中执行，并通过结构化消息协议将工具调用代理到主页面。整体架构包含三个核心层次：（1）隔离执行沙箱层，负责加载和执行 LLM 生成的 JavaScript 代码；（2）工具调度协议层，在沙箱与主页面之间建立安全、可审计的工具调用通道；（3）主页面工具代理层，负责接收工具调用请求、执行已注册的真实工具并返回结果。

### 隔离执行沙箱

执行沙箱采用 sandboxed iframe 实现，利用浏览器原生安全边界实现与主页面环境的隔离。iframe 的 src 设置为 srcdoc，由主页面在创建时注入执行器引导代码（executor harness）。该 iframe 具有 null origin，浏览器自动阻止其通过 DOM API 访问父页面。iframe 内部不含任何页面业务逻辑，仅包含一个执行器引导程序，负责接收主页面传入的编排代码和工具代理配置，在 iframe 内部执行代码并将工具调用请求通过结构化消息发送给主页面。

引导程序在沙箱中为每个工具命名空间构建 Proxy 对象。当 LLM 生成的代码调用 browserTools.getSelection({}) 时，Proxy 的 get 陷阱拦截该调用，生成唯一调用标识（callId），将工具名、参数序列化后通过 postMessage 发送给主页面，并返回一个 Promise 等待主页面回传结果。这与现有服务端 codemode 中 ToolDispatcher 通过 Workers RPC 回传工具调用的模式一致，区别在于传输层从 Workers RPC 变为基于 postMessage 的结构化消息协议。

沙箱内部默认禁止网络访问。通过不为 iframe 提供任何网络权限配置，且不在引导程序中暴露 fetch 或 XMLHttpRequest，确保 LLM 生成的代码只能通过工具代理通道与外部交互。沙箱内的 console.log、console.warn、console.error 输出均被重定向捕获，通过日志消息回传至主页面，作为执行结果的一部分返回。

### 工具调度协议

沙箱与主页面之间通过一套结构化消息协议通信，该协议定义以下消息类型：

- sandbox:ready：沙箱初始化完成，引导程序就绪，携带沙箱能力版本号
- sandbox:tool-call：沙箱请求调用某个已注册工具，携带 callId、namespace、toolName、args（JSON 序列化后的参数）
- sandbox:tool-result：主页面返回工具执行结果，携带 callId、result（成功时的返回值）或 error（失败时的错误信息）
- sandbox:log：沙箱内部 console 输出日志，携带 level（log/warn/error）和 message
- sandbox:error：沙箱执行过程中发生的非工具相关错误，携带 error 消息
- sandbox:done：沙箱代码执行完成（无论成功或失败），主页面可据此触发清理

每条消息均携带 sandboxId 用于标识沙箱实例，支持同一页面同时运行多个沙箱实例。消息来源通过浏览器 postMessage 的 origin 校验机制进行验证：主页面只接受来自已知沙箱 iframe 的消息，沙箱只接受来自主页面（通过 allowed origin 白名单配置）的消息。工具调用采用请求-响应模式：每个 sandbox:tool-call 携带唯一 callId，主页面在完成工具执行后以相同 callId 回传 sandbox:tool-result，沙箱内部的 Promise 据此 resolve。

### 动态工具命名空间与来源标识

浏览器侧工具来源多样，可能来自不同前端组件、浏览器 API 封装或第三方 SDK。方案引入工具来源标识机制：每个注册到主页面工具注册表（BrowserToolRegistry）的工具，除名称、描述、输入输出 schema 和 execute 函数外，还必须携带 source 元数据，包含：

- namespace：工具命名空间，在沙箱中作为 Proxy 对象名暴露（如 browserTools、pageState、selection）
- origin：工具来源标识，描述工具由哪个组件或模块提供（如 component:RichTextEditor、api:Geolocation、plugin:Analytics）
- version：工具接口版本号，用于 LLM 生成代码时感知接口兼容性

工具通过命名空间进行分组，不同前端组件可以向不同命名空间注册工具。例如，富文本编辑器组件向 selection 命名空间注册 getSelection、setSelection 等工具，页面状态管理模块向 pageState 命名空间注册 getState、subscribe 等工具。命名空间名称遵循 JavaScript 标识符规则（与现有 codemode 的 provider name 校验规则一致），在沙箱中为每个命名空间生成独立的 Proxy 对象。当存在同名命名空间时，后注册的命名空间中的工具会与先注册的合并；当同一命名空间内存在同名工具时，后注册的工具覆盖先注册的，并发出冲突警告。

### 工具名规范化与冲突处理

工具名称在发送到沙箱之前需经过规范化处理，确保 LLM 生成的代码中合法调用。规范化规则与现有 codemode 的 sanitizeToolName 函数一致：（1）将连字符（-）、点号（.）、空格替换为下划线（_）；（2）移除其余非标识符字符；（3）以数字开头的名称自动加下划线前缀；（4）JavaScript 保留字（如 class、delete、import 等）自动追加下划线后缀。例如，工具名 "get-selection" 规范化为 "get_selection"，"list.items" 规范化为 "list_items"。

命名空间名称也须满足 JavaScript 标识符规则（正则 /^[a-zA-Z_$][a-zA-Z0-9_$]*$/），同时保留名称 __dispatchers 和 __logs 为系统内部使用。冲突处理分为两层：（1）命名空间级别：当两个工具提供者注册同名命名空间时，其内部工具合并，同名工具以后注册为准，并产生控制台警告；（2）全局限级别：工具规范化后的完整调用路径（namespace.toolName）在向 LLM 暴露的类型定义中保持唯一。如果两个不同来源的工具经规范化后产生相同的调用路径，系统在注册时检测并抛出错误，要求开发者调整命名空间或工具名。

### 输入输出 Schema 生成

为使 LLM 能够生成正确调用浏览器工具的代码，系统在每次执行前根据当前已注册工具集生成类型定义和工具描述文本。类型生成流程如下：

1. 从 BrowserToolRegistry 获取当前所有已注册工具（按命名空间分组）；
2. 对每个工具，从其 inputSchema（JSON Schema 格式，与现有 ClientToolSchema.parameters 一致）生成 TypeScript 类型声明；
3. 对每个工具，从其 outputSchema（可选）生成返回值类型声明；
4. 将每个命名空间的工具组装为 TypeScript interface，例如 interface BrowserTools { getSelection(args: {...}): Promise<...>; }；
5. 生成完整的类型块（type block），注入到 codemode 工具描述中，替换 {{types}} 占位符。

类型生成复用现有 codemode 的 tool-types 模块中的 jsonSchemaToTypeString 能力，但输入从 AI SDK 的 Zod schema 变为客户端 JSON Schema（ClientToolSchema.parameters）。对于没有提供 outputSchema 的工具，返回值类型默认为 unknown。工具描述文本同时包含每个工具的自然语言描述（从 BrowserToolRegistry 中获取），帮助 LLM 理解工具用途。

### 生命周期管理

每次浏览器侧代码执行具有明确的五阶段生命周期：

- 准备阶段（prepare）：主页面创建 sandboxed iframe，注入引导程序。引导程序初始化后发送 sandbox:ready 消息。主页面设置超时定时器（默认 30 秒）。如果超时前未收到 ready 消息，进入错误阶段。
- 执行阶段（execute）：主页面将 LLM 生成的编排代码、工具代理配置（命名空间列表、每个命名空间的工具名列表及规范化映射）通过消息发送给沙箱。沙箱内的引导程序创建各命名空间的 Proxy 对象，执行编排代码。
- 结果回传阶段（return）：编排代码执行完成（return 语句执行）后，沙箱发送 sandbox:done 消息，携带 result 和 logs。主页面将结果封装为 ExecuteResult 返回给调用方。
- 错误处理阶段（error）：如果执行过程中发生异常（包括代码语法错误、运行时异常、工具调用失败且未被代码捕获、执行超时），沙箱发送 sandbox:done 消息携带 error 信息，主页面将其封装为 ExecuteResult.error 返回。工具调用失败传播路径：主页面执行工具时捕获异常，通过 sandbox:tool-result 的 error 字段回传沙箱，沙箱中对应的 Proxy 调用 Promise reject，LLM 生成的代码可通过 try/catch 处理。
- 清理阶段（cleanup）：无论执行成功还是失败，在收到 sandbox:done 消息后，主页面移除 iframe 元素，释放沙箱占用的内存。如果执行超时（主页面定时器先于 sandbox:done 触发），主页面强制移除 iframe，并将超时错误作为执行结果返回。

超时机制采用双重保障：（1）主页面侧设置整体执行超时，若超时触发则强制进入清理阶段；（2）沙箱内部针对单个工具调用设置独立超时（可配置，默认 10 秒），单个工具挂起不会阻塞整体生命周期。执行过程中，主页面通过 AbortController 信号支持外部取消：调用方可以提前 abort，主页面收到取消信号后立即进入清理阶段。

### 主页面工具代理边界

主页面工具代理层（BrowserToolProxy）是沙箱与真实浏览器工具之间的边界。它包含以下核心组件：

- BrowserToolRegistry：存储所有已注册浏览器工具的注册表。每个工具记录包含：name（原始工具名）、description、inputSchema（JSON Schema）、outputSchema（可选）、execute（实际执行函数）、source（命名空间、来源标识、版本）。注册表提供 register(namespace, tools)、unregister(namespace)、list(namespace?) 等方法。
- ToolCallHandler：接收沙箱发来的 sandbox:tool-call 消息，从 BrowserToolRegistry 查找对应工具，执行输入校验，调用 execute 函数，将结果或错误通过 sandbox:tool-result 回传。输入校验环节使用 JSON Schema 验证器对沙箱传入的参数进行校验，不合法参数拒绝执行并返回校验错误。
- ToolAccessController：可选的访问控制层，支持对特定命名空间或特定工具设置执行权限。例如，敏感工具（如访问 Cookie 或 localStorage）可配置为需要用户确认后才执行。

关键安全边界：LLM 生成的代码不能直接访问或修改主页面对象。所有与主页面的交互必须通过工具代理通道中的已注册工具，且每个工具的执行函数在主页面的受控上下文中运行。沙箱代码无法访问主页面 window、document、localStorage、Cookie 或任何全局变量。工具 execute 函数的实现完全由应用开发者控制，开发者可以在 execute 内部安全地访问页面状态、DOM、浏览器 API 等，而 LLM 生成的代码只能看到工具的函数签名和描述。

### 安全控制机制

方案通过多层安全控制确保生成代码不会污染或危害主页面环境：

- 沙箱隔离：sandboxed iframe 的 null origin 由浏览器强制隔离，iframe 内代码无法通过 DOM API 访问父页面文档、窗口对象或全局变量。
- 网络隔离：沙箱代码默认无 fetch/XMLHttpRequest/WebSocket 能力，无法发起任何外发网络请求。
- 代理通道唯一入口：沙箱代码仅通过为每个命名空间生成的 Proxy 对象与外部交互，Proxy 内部仅将工具名和参数序列化后通过 postMessage 发送，不暴露任何主页面对象引用。
- 消息来源校验：双向 postMessage 均校验消息来源。主页面只接受已知 iframe 窗口且 origin 匹配的消息；沙箱只接受符合配置的 allowed origin 的主页面消息。
- 工具调用白名单：主页面 ToolCallHandler 仅执行已在 BrowserToolRegistry 中注册的工具。对于未注册的工具名，返回 'tool not found' 错误，不执行任何降级或兜底操作。
- 输入 schema 校验：每个工具调用在到达 execute 函数之前，必须通过其 inputSchema 定义的 JSON Schema 校验，防止注入非法参数。
- 执行超时兜底：整体执行超时和单工具超时双重保障，防止死循环或挂起的 LLM 代码长期占用资源。
- 日志与审计：沙箱内所有 console 输出和所有工具调用（工具名、参数、结果摘要）均被记录，支持问题排查和安全审计。

### 与现有系统集成

BrowserCodeExecutor 实现与现有 codemode 的 Executor 接口完全兼容，可作为 createCodeTool 的 executor 参数直接使用。这保证了现有 codemode / agent chat / client tool 体系的平滑集成：

- 与 codemode 集成：BrowserCodeExecutor 在 createCodeTool 中替代 DynamicWorkerExecutor，使 LLM 在浏览器侧生成编排代码。服务端通过 AI SDK 的 streamText/generateText 照常调用 codemode 工具，只是底层执行器切换为浏览器侧。
- 与 AIChatAgent 集成：服务端 AIChatAgent 定义 codemode 工具时使用 BrowserCodeExecutor，当 LLM 决定调用 codemode 时，服务端将代码执行请求通过 WebSocket 转发到浏览器客户端，客户端创建 BrowserCodeExecutor 实例执行代码并回传结果，配合现有的 autoContinueAfterToolResult 机制实现无缝工具调用链。
- 与 client tool 体系集成：浏览器工具可同时被两种方式使用：一是通过 onToolCall 在单个工具调用级别由 LLM 逐个调用；二是通过 codemode + BrowserCodeExecutor 由 LLM 生成编排代码批量调用。两种模式共享同一 BrowserToolRegistry，工具只需注册一次。
- 与现有 ToolProvider 机制兼容：浏览器工具封装为 ToolProvider（namespace = 浏览器工具命名空间），与现有的 aiTools()、MCP 工具等通过统一的 normalizeProviders 流程合并，在类型生成和执行时分发到对应执行器。

### 处理流程

一次完整的浏览器侧 codemode 执行遵循以下流程：

1. 应用开发者通过 BrowserToolRegistry.register() 注册浏览器侧工具（定义 name、description、inputSchema、execute 等），工具按命名空间分组。
2. 当 LLM 决定使用 codemode 时，服务端 createCodeTool 根据当前可用工具集生成类型定义和工具描述（与现有流程一致），如果配置了浏览器工具 ToolProvider，其类型块包含浏览器命名空间（如 browserTools、selection 等）的函数签名。
3. LLM 生成编排代码（async arrow function），代码中调用 browserTools.xxx(args) 等。
4. 服务端将编排代码和工具提供者配置发送给浏览器客户端。
5. 客户端创建 BrowserCodeExecutor 实例，传入编排代码和已解析的工具提供者列表（ResolvedProvider[]），每个提供者包含命名空间及工具函数引用。
6. BrowserCodeExecutor 创建 sandboxed iframe，注入引导程序和编排代码。引导程序为每个命名空间构建 Proxy，将工具调用通过 postMessage 代理。
7. 沙箱内代码执行，Proxy 拦截工具调用 → 发送 sandbox:tool-call 消息 → 主页面 ToolCallHandler 从 BrowserToolRegistry 查找并执行 → 发送 sandbox:tool-result 回传 → Proxy 的 Promise resolve。
8. 代码执行完成后，沙箱发送 sandbox:done 携带 result/logs/error，主页面封装为 ExecuteResult 返回。
9. 客户端将 ExecuteResult 通过 WebSocket（或 HTTP）回传给服务端，服务端将结果注入 AI SDK 的工具调用返回中，LLM 继续处理后续步骤。

### 技术效果

本方案通过浏览器侧隔离执行、结构化工具调度协议和安全控制机制，实现以下技术效果：

- 将 codemode 的编排能力从服务端扩展到浏览器侧，使 LLM 可以编排调用仅存在于浏览器页面中的动态工具，无需将浏览器私有能力搬迁到服务端。
- 通过 sandboxed iframe 实现执行环境与主页面的硬件级隔离（浏览器原生安全边界），生成代码无法直接访问或修改主页面 DOM、全局变量、存储等对象，所有交互必须通过工具代理通道的已注册工具。
- 通过命名空间与来源标识机制，支持多个前端组件或模块独立注册工具，避免命名冲突，且 LLM 可通过类型系统感知工具的来源和版本。
- 工具调度协议基于结构化消息，以 callId 实现请求-响应关联，支持并发工具调用、错误传播和日志收集，协议独立于具体传输层实现。
- 五阶段生命周期管理（prepare/execute/return/error/cleanup）和双重超时机制，确保每次执行有明确的资源创建和释放边界，防止内存泄漏和资源长期占用。
- 与现有 codemode、AIChatAgent、client tool 体系无缝集成，复用现有 ToolProvider、Executor 接口、类型生成和 autoContinueAfterToolResult 机制，额外依赖最小（仅需浏览器原生 sandboxed iframe 和 postMessage API）。

### 风险与待确认问题

以下为当前方案需要后续确认和关注的风险点：

- sandboxed iframe 兼容性：sandboxed iframe 的 srcdoc + null origin 模式在所有主流浏览器中得到支持，但某些企业浏览器或 WebView 环境可能限制 iframe 创建或 postMessage。建议进行跨浏览器兼容性测试。
- iframe 内代码执行性能：LLM 生成的编排代码在 iframe 沙箱中执行，性能取决于代码复杂度和工具调用次数。iframe 创建和销毁有一定开销，对于高频短小的编排任务，可考虑沙箱复用策略（同一沙箱多次执行不同代码片段），但需要额外考虑状态隔离问题。
- 工具注册的线程安全：BrowserToolRegistry 在单线程浏览器环境中天然线程安全，但如果多个 React 组件同时注册/注销工具，需要注意注册时序。建议工具注册在应用初始化阶段完成，运行时仅允许追加不允许移除关键工具。
- 与 CSP（Content Security Policy）的兼容：如果主页面设置了严格的 CSP 策略，可能限制 iframe srcdoc 内联脚本的执行。需要确保 CSP 配置允许 'unsafe-inline' 或使用 nonce/hash 机制。
- LLM 生成代码质量：生成代码中可能出现未捕获异常、死循环或无意义的工具调用序列。超时机制可防止死循环，但工具调用的合理性仍需依赖 LLM 能力。建议在工具描述中明确说明工具的幂等性和副作用。
- 跨域场景：如果主页面和代理服务器不在同一域，iframe 的 postMessage 通信仍可正常工作（postMessage 支持跨域），但 origin 校验需要正确配置 allowed origin 白名单。
- Web Worker 作为替代方案：方案当前以 sandboxed iframe 为主，但 Web Worker 也是一种可行的沙箱实现。Web Worker 天然无 DOM 访问权限且性能更优，但缺少对某些浏览器 API（如 console 样式输出）的支持。未来可考虑通过 Executor 接口的多种实现支持两种沙箱模式。
