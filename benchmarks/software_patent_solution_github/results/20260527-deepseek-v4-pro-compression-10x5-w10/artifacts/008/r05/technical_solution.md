## 技术方案

### 技术问题与方案概述

本方案要解决的技术问题是：在大语言模型（LLM）驱动的智能体系统中，大量工具天然存在于浏览器页面环境内（如读取页面选择内容、访问页面 UI 状态、调用前端组件暴露的能力、调用浏览器专有 API 等），但现有 LLM 代码编排能力（codemode）的执行环境位于服务端或 Worker 侧，无法直接调用这些浏览器侧工具。若将浏览器私有能力全部搬到服务端，将带来安全性、实时性和架构复杂度的多重问题。

本方案提出一种浏览器侧隔离代码执行机制：在浏览器页面中创建一个与主页面隔离的执行沙箱，LLM 生成的编排代码在该沙箱内运行，通过结构化工具调度协议向主页面代理请求工具调用，主页面负责实际执行已注册工具并返回结果。该方案在保持与现有服务端 codemode、Agent Chat 和 Client Tool 体系兼容的前提下，以最小额外依赖实现浏览器侧工具的安全编排。

### 总体架构

系统由三个核心域组成：（1）主页面工具注册域，负责注册、管理浏览器侧工具并实际执行工具调用；（2）隔离执行沙箱域，在浏览器中创建与主页面隔离的 JavaScript 执行环境，接收并运行 LLM 生成的编排代码；（3）工具调度协议层，在沙箱与主页面之间建立结构化的请求-响应通道，实现工具调用的安全代理。

整体数据流如下：LLM 根据当前可用工具的类型描述生成编排代码 → 编排代码被注入隔离沙箱 → 沙箱内代码执行过程中，对工具代理对象的方法调用被 Proxy 拦截 → Proxy 将调用序列化为结构化工具调度消息 → 消息通过安全通道发送到主页面 → 主页面工具调度器查找已注册工具、校验参数 schema → 主页面执行工具并捕获结果 → 结果通过同一通道回传给沙箱 → 沙箱内代码继续执行或返回最终结果。代码执行完毕后，沙箱及其所有资源被销毁。

### 浏览器侧隔离执行环境

隔离执行沙箱是浏览器侧编排代码的运行载体。其核心设计要求是：沙箱内代码可以调用主页面注册的工具，但无法直接访问或修改主页面 DOM、全局对象、JavaScript 作用域或网络资源。

沙箱基于以下机制构建：（1）使用 sandboxed iframe 作为执行容器，iframe 的 sandbox 属性仅开放 allow-scripts，不开放 allow-same-origin、allow-top-navigation、allow-forms 等；（2）iframe 以 data: URI 或 blob: URI 加载，拥有独立的 null origin，与主页面不存在同源关系；（3）沙箱内部注入一个工具代理对象，该对象通过 Proxy 拦截所有属性访问和方法调用，将其转换为结构化消息通过 postMessage 发送到主页面，而非直接执行任何 DOM 或 JS 操作；（4）沙箱内部的全局对象（window、document、fetch 等）或者被删除，或者被替换为受限存根——例如 fetch 被替换为抛出异常的函数，确保编排代码无法发起网络请求；（5）沙箱内代码通过主页面注入一个封装后的 eval 或 Function 构造器执行，执行上下文被限制在沙箱的全局作用域内。

工具代理对象是沙箱与主页面之间的唯一桥梁。代理对象的构造过程为：（a）主页面从已注册工具列表中提取每个工具的名称、输入 schema 和输出 schema；（b）将工具名称通过名称规范化算法转换为合法的 JavaScript 标识符（如将 "page.get-selection" 转换为 "page_get_selection"）；（c）生成一个嵌套代理对象，使得 codemode.page_get_selection(args) 这样的调用被 Proxy 的 get 陷阱捕获，返回一个异步函数；（d）该异步函数将工具名（原始名）和参数序列化为 ToolCallRequest 消息，通过 postMessage 发送到主页面，并返回一个 Promise，该 Promise 在主页面回传 ToolCallResult 消息时 resolve。

与简单的 '用 iframe 执行代码并 postMessage' 方案相比，本方案的关键差异在于：（i）工具调用不是由沙箱代码直接构造 postMessage，而是由注入的工具代理对象透明拦截和序列化，编排代码只需调用 codemode.toolName(args) 这样的自然 API；（ii）工具代理对象的成员集由主页面动态生成并与工具注册表严格对应，沙箱内不存在任何可枚举的主页面引用；（iii）沙箱的全局环境被主动净化（全局对象删除/替换），而非依赖 sandbox 属性的默认限制。这些差异使得编排代码无法绕过代理直接访问主页面，也无法通过全局对象逃逸沙箱。

### 工具注册与命名空间体系

浏览器侧工具需要被显式注册到主页面工具注册表中，才能被编排代码通过工具代理对象调用。每个注册工具包含以下元数据：（1）工具名——工具的唯一标识符，采用带命名空间前缀的点分隔命名法，如 "page.getSelection"、"app.theme.toggle"；（2）命名空间/来源标识——标识工具的来源类别，至少包含 "page"（页面内置工具）、"app"（应用注册工具）、"mcp"（MCP 服务器桥接工具）、"remote"（远程服务桥接工具）四种标准命名空间；（3）输入 schema——描述工具参数的 JSON Schema 或等效类型定义；（4）输出 schema——描述工具返回值的 JSON Schema 或等效类型定义；（5）执行函数——由主页面提供的实际工具实现，在主页面上下文执行；（6）权限声明——可选的执行权限标记，如是否需要用户确认（needsApproval）、是否涉及副作用（sideEffect）等。

命名空间的引入解决了多来源工具的标识和冲突问题。每个命名空间对应一个工具来源，命名空间前缀在工具注册时由系统根据来源自动添加或由开发者显式指定。当多个来源注册了逻辑上相似的工具时（例如 MCP 服务器提供了 "getSelection" 工具，页面本身也有 "page.getSelection"），不同前缀自然区分了二者，不会产生冲突。

工具注册表支持以下操作：（a）registerTool(toolDescriptor)——注册单个工具，若工具名与已注册工具重名，则根据冲突策略处理（默认拒绝并抛出错误，也可配置为覆盖或添加来源后缀）；（b）unregisterTool(toolName)——注销单个工具；（c）listTools(filter?)——列出所有或按命名空间过滤后的工具及其描述；（d）generateToolTypes()——根据已注册工具生成 LLM 可用的 TypeScript 类型定义和工具描述文本。工具注册表在页面生命周期内保持存活，支持动态增删工具以响应页面状态变化。

### 工具调度协议

沙箱与主页面之间的工具调度采用结构化的双向消息协议。协议定义以下消息类型：

请求方向（沙箱 → 主页面）：（1）ToolCallRequest——包含 callId（唯一调用标识）、toolName（原始工具名，非规范化名）、arguments（JSON 序列化的参数对象）；（2）ExecutionReady——沙箱初始化完成、工具代理对象就绪的信号；（3）ExecutionComplete——编排代码执行完毕的信号，携带最终返回值或未捕获异常；（4）ConsoleOutput——沙箱内 console.log/warn/error 等输出的批量转发消息。

响应方向（主页面 → 沙箱）：（1）ToolCallResult——与 ToolCallRequest 配对，包含 callId、result（工具执行结果）或 error（工具执行异常）；（2）ToolCallError——当工具名未注册、参数校验失败或执行权限不满足时返回的结构化错误，包含 callId、errorCode、errorMessage；（3）SandboxReady——主页面确认沙箱就绪的信号，可在其中携带工具代理对象的初始化批次数据。

消息传输基于 postMessage 通道，并使用 MessageChannel 或直接 window.postMessage 进行。每个 ToolCallRequest 内部创建一个一次性 MessageChannel，将其 port2 随请求发送，主页面通过 port2 回传结果，实现请求-响应的一对一匹配而无需维护全局回调映射表。消息体采用结构化克隆算法（structured clone）兼容的格式传输（JSON 可序列化对象、ArrayBuffer 等），避免序列化/反序列化带来的类型信息丢失。

主页面侧的调度器收到 ToolCallRequest 后执行以下流程：（1）从注册表中按 toolName 查找工具定义，若不存在则返回 ToolCallError（errorCode: UNKNOWN_TOOL）；（2）若工具声明了 needsApproval，且当前未获得批准，则返回 ToolCallError（errorCode: APPROVAL_REQUIRED）；（3）使用工具的 inputSchema 校验 arguments，校验失败则返回 ToolCallError（errorCode: INVALID_ARGUMENTS）并附带校验详情；（4）在当前主页面上下文中调用工具的执行函数，捕获返回值或异常；（5）将结果封装为 ToolCallResult 通过 port2 回传。

### 工具名规范化与冲突处理

工具在注册表中的原始名称采用带命名空间前缀的点分隔格式（如 "mcp.my-server.list-items"、"page.getSelection"），但编排代码中的工具调用必须符合 JavaScript 标识符规范。因此系统在生成沙箱内工具代理对象时，对工具名执行规范化转换。

名称规范化算法：（1）将点号（.）替换为下划线（_）；（2）将连字符（-）替换为下划线（_）；（3）若首字符为数字，在前面添加下划线前缀（如 "3d_render" → "_3d_render"）；（4）若规范化后的名称与 JavaScript 保留字冲突（如 "delete"），追加下划线后缀（"delete_"）；（5）将命名空间前缀与工具本地名用双下划线连接，形成唯一标识：namespace__localName。例如 "mcp.my-server.list-items" 转换为 "mcp_my_server__list_items"。这种转换是可逆的——系统维护一个规范化名到原始名的双向映射表，在发送 ToolCallRequest 时还原为原始名。

冲突处理策略：（1）注册时检测——当新注册工具的规范化名称与已有工具冲突时，默认抛出 ToolNameConflictError；支持配置冲突策略为 'reject'（拒绝）、'override'（静默覆盖）或 'rename'（自动添加数字后缀）；（2）跨命名空间隔离——不同命名空间下的相同本地名（如 "page.getText" 和 "app.getText"）因前缀不同而天然不冲突；（3）MCP 工具桥接时的额外处理——MCP 工具名可能包含多个点号和特殊字符，桥接适配器在导入 MCP 工具时预先完成规范化并记录完整的原始名映射，确保调度时精确还原。

### 类型描述与输入输出结构生成

为使 LLM 能正确生成编排代码，系统需要将已注册工具集合转换为 LLM 可理解的类型描述。本方案采用与现有服务端 codemode 的 generateTypes() 一致的模式，但在浏览器侧本地执行生成，无需服务端往返。

类型生成流程：（1）从工具注册表获取所有已注册工具的名称、输入 schema 和输出 schema；（2）对每个工具的输入 schema（JSON Schema 或 Zod schema），生成对应的 TypeScript 接口定义。例如输入 schema { location: z.string() } 生成 type GetWeatherInput = { location: string }；（3）对每个工具生成函数签名声明，采用 declare const codemode 的声明合并模式，将各工具作为 codemode 命名空间下的异步方法：declare const codemode: { ...; getWeather(input: GetWeatherInput): Promise<GetWeatherOutput>; ... }；（4）将生成的类型定义与系统提示词合并，形成最终的 LLM 工具使用说明，包含可用工具列表、每个工具的输入输出类型和文字描述。

输入输出结构在运行时也用于参数校验和结果类型断言。工具的 inputSchema 不仅用于 LLM 类型生成，也用于主页面调度器在实际执行前的参数校验——这确保即使 LLM 生成了不符合 schema 的参数，也会在工具执行前被拦截并返回结构化错误，而不会传入工具执行函数。同理，outputSchema 用于对工具返回值的类型断言，当返回值不符合声明结构时记录警告，帮助开发者发现工具实现与声明不一致的问题。

### 执行生命周期管理

每次编排代码执行具有明确的生命周期，包含七个阶段。

（1）准备阶段（Prepare）：主页面创建新的 sandboxed iframe 或 Web Worker 实例；注入工具代理对象工厂代码和全局环境净化代码；将当前工具注册表的元数据（工具名列表、规范化映射表）序列化后传入沙箱；在沙箱内初始化基于 Proxy 的工具代理对象；注册 console 输出转发钩子。准备阶段结束时沙箱发送 ExecutionReady 消息。

（2）就绪确认阶段（Ready）：主页面收到 ExecutionReady 后向沙箱发送 SandboxReady，携带可选初始化数据；沙箱将工具代理对象挂载到全局作用域（如 window.codemode）；系统记录执行开始时间戳并启动超时计时器。

（3）执行阶段（Execute）：LLM 生成的编排代码（经 AST 解析和规范化的异步函数）被注入沙箱并执行；编排代码调用 codemode.toolName(args) 时，工具代理对象拦截调用、序列化为 ToolCallRequest 并通过 postMessage 发送到主页面；主页面调度器处理请求并回传 ToolCallResult；工具代理对象将结果 resolve 到编排代码的 Promise；编排代码可包含条件判断、循环和异常处理等任意逻辑；所有 console 输出被捕获并异步批量转发。

（4）结果返回阶段（Result）：编排代码正常执行完毕后，返回值被序列化并通过 ExecutionComplete（携带 result 字段）发送到主页面；主页面将结果与执行期间收集的 console 输出合并为 ExecuteResult 对象，返回给调用方。（5）异常处理阶段（Error）：若编排代码内部抛出未捕获异常，异常被捕获并序列化为 ExecutionComplete（携带 error 字段，包含 message、stack 和 toolCallContext——异常发生时正在进行的工具调用信息）；若工具调用失败，该错误作为 Promise rejection 抛给编排代码，编排代码可用 try/catch 处理；若未处理则最终进入 error 字段。

（6）超时处理阶段（Timeout）：超时计时器由主页面维护（不与沙箱共享时钟，避免被绕过）；默认超时 30 秒，可配置；超时触发后主页面向沙箱发送 AbortExecution 消息，沙箱内代码通过 AbortController 中断；超时前的部分 console 输出仍保留在结果中；最终返回 ExecuteResult 并标记 timedOut: true。（7）清理阶段（Cleanup）：执行结束后，主页面移除 sandboxed iframe 或终止 Web Worker，清理所有 MessageChannel 端口和超时计时器，清理工具代理对象引用链；可选地将日志和结果写入执行历史存储。清理阶段保证每次执行独立，无状态泄漏。

### 安全控制机制

安全控制是本方案的核心设计目标之一，通过多层防御机制确保编排代码不能直接访问或污染主页面对象。

（1）执行环境隔离：sandboxed iframe 使用 data: URI 加载，拥有 null origin；sandbox 属性不开放 allow-same-origin、allow-top-navigation、allow-forms、allow-popups；Web Worker 方案中 Worker 自身无 DOM 访问能力。两种方案下编排代码均无法通过常规 DOM API 访问主页面。（2）全局对象净化：沙箱初始化时删除或替换全局危险对象——window、document、parent、top、self 被替换为受限存根对象；fetch、XMLHttpRequest、WebSocket 被替换为抛出异常的函数；eval 和 Function 构造器在注入编排代码后被冻结或删除，防止动态代码执行绕过静态分析。（3）工具调用代理边界：沙箱内不存在任何对主页面对象、函数或 DOM 节点的直接引用。所有工具调用必须通过工具代理对象→序列化消息→主页面调度器这一链路的单向通道完成。主页面调度器是唯一有权执行工具函数的主体，它在主页面上下文中执行，但编排代码无法访问或干扰调度器的执行上下文。

（4）网络隔离：沙箱内 fetch、XMLHttpRequest、WebSocket、EventSource 等网络 API 被替换为抛出异常的函数；若业务需要受控的网络访问，主页面可在工具注册表中注册专用的网络请求工具（如 "http.fetch"），该工具在主页面侧实现并受主页面安全策略（如 CSP、CORS）约束，编排代码只能通过该工具发起经主页面审计的网络请求。（5）工具调用校验：主页面调度器在每次工具调用时执行三重校验——工具名是否已注册、参数是否匹配 inputSchema、是否需要且已获得用户批准。任何校验失败均返回结构化错误而不执行工具函数。（6）消息来源校验：主页面在接收 postMessage 时校验消息来源（event.origin 为 null 或匹配沙箱 origin），忽略来自非沙箱源的消息，防止其他 iframe 或扩展注入伪造的工具调用请求。（7）每次执行创建全新沙箱实例，执行完毕立即销毁；沙箱实例之间不共享任何状态、全局变量或工具代理对象，防止跨执行信息泄漏。

### 异常传播与日志收集

异常传播机制确保编排代码中的错误、工具执行中的错误和系统级错误都能被正确捕获和分类传递。（1）编排代码异常：沙箱内用 try/catch 包裹整个编排代码执行；捕获到的异常提取 message、stack 和 name 属性；若异常发生在工具调用 Promise 链中，额外记录 toolCallContext（当前正在调用的工具名和参数）。（2）工具执行异常：当主页面工具执行函数抛出异常时，异常被捕获并封装为 ToolCallResult 的 error 字段返回；沙箱内工具代理对象将该 error 作为 Promise rejection 抛出，编排代码可以通过 try/catch 自行处理；若编排代码未处理，该 rejection 成为未捕获异常被顶层 try/catch 捕获。工具执行异常与编排代码异常的区分通过 error 对象中的 source 字段实现（'tool' vs 'code'）。（3）系统级异常：包括沙箱创建失败、消息通道断开、超时等场景；这些异常由主页面侧捕获并直接构造为 ExecuteResult 返回，不经过沙箱内传播路径。

日志收集机制：沙箱内的 console.log、console.warn、console.error 等输出在沙箱初始化时被重写——每个日志方法将调用参数序列化后通过 ConsoleOutput 消息异步批量发送到主页面。日志收集采用缓冲批量发送策略（每个消息携带最多 50 条日志或 100ms 缓冲窗口），避免高频日志导致的 postMessage 风暴。主页面侧将日志按级别分类存储到执行结果中，同时可选地将日志写入持久化存储用于调试和审计。

### 与现有体系的协作

本方案设计为与项目中现有的 codemode（服务端 Worker 隔离执行）、Agent Chat（AIChatAgent 对话流程）和 Client Tool（浏览器侧工具调用与自动续接）三大体系协同工作，而非替代它们。

（1）与 codemode 的协作：本方案在概念上复用 codemode 的 Executor 接口——在浏览器侧实现 BrowserSandboxExecutor，其 execute(code, fns) 方法与 DynamicWorkerExecutor（服务端 Worker 执行器）保持相同签名。上层的 createCodeTool 可通过依赖注入选择执行器：服务端场景使用 DynamicWorkerExecutor，浏览器侧场景使用 BrowserSandboxExecutor。工具类型生成（generateTypes）和名称规范化（sanitizeToolName）逻辑在两种执行器间共享。（2）与 Agent Chat 的协作：BrowserSandboxExecutor 的执行结果可直接作为 AIChatAgent 工具调用的返回结果，融入现有的流式对话流程。当 Agent Chat 的 autoContinueAfterToolResult 开启时，浏览器侧代码执行完毕后自动触发 LLM 继续响应。编排代码中调用的浏览器侧工具可与服务端工具在同一对话上下文中共存——LLM 根据工具描述自行决定使用哪个工具。

（3）与 Client Tool 的协作：现有的 Client Tool 机制通过 onToolCall 回调让浏览器执行单个工具并返回结果。本方案在此基础上增加了编排能力——LLM 可以通过一段代码组合多个浏览器侧工具的调用，包含条件逻辑和循环，在一次沙箱执行中完成多步操作。BrowserSandboxExecutor 本身也可以作为 Client Tool 注册到 Agent Chat 中（工具名为 "codemode.browser"），由 LLM 决定何时使用代码编排而非逐个调用。（4）与 WebMCP 的协作：MCP 服务器桥接到浏览器的工具（通过 registerWebMcp）与本地注册的浏览器工具共享同一注册表，编排代码可以无缝调用两类工具。命名空间前缀（mcp.* vs page.* vs app.*）保证来源清晰可辨。（5）与执行器接口兼容：BrowserSandboxExecutor 实现与 DynamicWorkerExecutor 相同的 Executor 接口（execute(code, fns) → ExecuteResult），但内部实现完全不同——一个基于 Cloudflare Workers V8 隔离，一个基于浏览器 sandboxed iframe/Worker。上层调用者（createCodeTool、Agent Chat）不感知执行器差异。

### 技术效果

本方案的技术效果体现在以下几个方面。（1）安全性：通过多层隔离（sandboxed iframe + 全局对象净化 + 工具代理边界 + 网络阻断 + 来源校验），编排代码无法访问或污染主页面对象，无法发起未授权网络请求，工具调用受注册表和 schema 双重约束。（2）可组合性：LLM 可将多个浏览器侧工具调用编排为一段包含条件判断和循环逻辑的代码，在一次沙箱执行中完成需要多次客户端-服务端往返才能完成的复合操作，减少延迟和交互轮次。（3）可扩展性：工具注册表支持动态增删，开发者可随时注册新的浏览器侧工具，无需修改沙箱或协议层。命名空间体系支持任意数量的工具来源共存。（4）兼容性：BrowserSandboxExecutor 复用现有 Executor 接口，与 codemode、Agent Chat、Client Tool、WebMCP 等现有体系无缝协作。（5）可观测性：结构化异常传播、分级日志收集、执行历史记录和超时标记提供了完整的调试和审计能力。（6）低依赖：方案仅依赖浏览器原生 API（iframe、postMessage、Proxy、AbortController），不引入第三方沙箱库或重型框架。

### 风险与待确认问题

以下为当前方案需要后续确认和验证的风险点。（1）sandboxed iframe 兼容性：不同浏览器对 sandbox 属性的实现细节可能不同（特别是 console 输出拦截、错误堆栈传递），需要在主流浏览器上进行兼容性测试。作为备选方案，Web Worker + 受限消息通道可提供更一致的跨浏览器隔离语义，但 Web Worker 无法使用部分浏览器 API（如 localStorage），可能影响特定工具场景。（2）大对象序列化性能：工具参数和返回值可能包含大量数据（如页面全文内容），需评估 structured clone 和 JSON 序列化在大对象场景下的性能开销，考虑引入传输上限或流式传输。（3）MCP 工具桥接的语义保真度：WebMCP 桥接的 MCP 工具当前将返回值展平为字符串，未来若支持结构化返回值，需要同步更新本方案的 outputSchema 验证逻辑。（4）并发执行：当前设计为每次执行创建独立沙箱，多个并发编排任务各自拥有独立沙箱实例。需验证浏览器对同时存在的多个 sandboxed iframe 的资源限制和性能表现。（5）嵌套工具调用：编排代码中的工具调用是否允许在其执行函数中再次触发编排（递归 codemode）需要明确边界，建议初始版本禁止嵌套避免死循环。（6）安全审计：全局对象净化列表需要持续审查，新兴 Web API 可能引入新的沙箱逃逸面。
