## 技术方案

本方案在现有服务端 codemode（基于 DynamicWorkerExecutor + Worker Loader 的隔离代码执行）基础上，扩展一类浏览器侧执行场景：LLM 生成的编排代码在浏览器端沙箱中运行，通过受控代理通道调用主页面注册的动态客户端工具，同时保持与主页面执行环境的严格隔离。

### 技术问题

当前 codemode 机制（参考 @cloudflare/codemode 包及 DynamicWorkerExecutor）在服务端 Worker isolate 中执行 LLM 生成的 JavaScript/TypeScript 代码，工具调用通过 Workers RPC 回传宿主执行。然而，许多有价值的工具仅存在于浏览器页面运行环境中，例如：读取当前页面用户选中文本、访问页面 DOM 状态、调用前端组件暴露的编程接口、组合多个浏览器端动态客户端工具等。将这些浏览器私有能力迁移到服务端成本高且不必要，但允许 LLM 生成的代码直接在主页面中执行则存在安全风险——生成代码可能读取、修改或污染主页面全局对象、DOM 和 JavaScript 运行时状态。

### 核心技术方案

方案整体架构分为四层：（1）服务端代码生成与分发层，复用现有 codemode 的 createCodeTool、generateTypes、sanitizeToolName 等机制，在服务端完成工具集类型声明生成和 LLM 代码生成，并将待执行代码下发至浏览器；（2）浏览器端隔离沙箱执行层，以沙箱化 iframe 承载 LLM 生成的编排代码，通过受控消息通道与主页面交互；（3）主页面工具代理层，接收沙箱发出的工具调用请求，在主页面执行上下文中实际调用已注册的客户端工具，并将结果序列化回传；（4）生命周期管理层，统一管理沙箱的准备、执行、结果返回、异常传播、超时和清理全流程。

### 浏览器隔离沙箱执行环境（BrowserSandboxExecutor）

BrowserSandboxExecutor 是 Executor 接口（来自 @cloudflare/codemode，定义为 execute(code: string, fns: Record<string, (...args: unknown[]) => Promise<unknown>>): Promise<ExecuteResult>）的浏览器端实现。与 DynamicWorkerExecutor 对应，BrowserSandboxExecutor 将代码执行从服务端 Worker isolate 迁移到浏览器端沙箱化 iframe 中。

沙箱创建时，采用严格的 iframe sandbox 属性组合：sandbox="allow-scripts"，不设置 allow-same-origin（阻断同源访问）、不设置 allow-top-navigation、不设置 allow-forms。iframe 的 src 指向一个与主页面同源但无 DOM 访问权限的空白 HTML 文档，该文档通过不可序列化的闭包引用注入执行桥接脚本，而非通过 URL 参数或 innerHTML 传递——这避免了将可执行代码暴露在可被检查的 DOM 中。同时，通过 Content-Security-Policy HTTP 响应头限制 iframe 内只允许执行内联脚本（script-src 'unsafe-inline'），禁止任何网络请求（connect-src 'none'），禁止加载外部资源。

执行桥接脚本在 iframe 内部构建一个 Proxy 对象（类比服务端 ToolDispatcher），拦截形如 codemode.toolName(args) 的调用。每次拦截将调用转换为结构化消息 { callId, namespace, toolName, args }，通过 postMessage 发送至主页面。主页面工具代理执行后，将结果包装为 { callId, result } 或 { callId, error } 回传，iframe 内 Proxy 解析对应 Promise。这一机制确保 LLM 生成的代码"以为"在直接调用本地函数，实际所有副作用均由主页面受控执行。

iframe 内部通过劫持 console.log / console.warn / console.error 方法，将所有日志输出序列化并通过独立日志通道（区别于工具调用通道）发送至主页面。日志以 { type: 'log', level, args } 格式传输，主页面收集后作为 ExecuteResult.logs 返回。

### 主页面工具代理层（ToolProxy）

主页面工具代理层是沙箱与真实工具实现之间的安全边界。其核心职责包括：接收并校验来自沙箱的工具调用请求，在主页面执行上下文中调用已注册工具，将结果序列化后回传沙箱。

代理层维护一个按命名空间组织的工具注册表：Map<namespace, Map<toolName, RegisteredTool>>。每个 RegisteredTool 包含：工具描述（description）、输入 schema（Zod schema 或 JSON Schema）、输出 schema、执行函数（execute: (args) => Promise<unknown>）、来源标识（sourceOrigin）、权限声明（permissions）。注册表设计支持多个命名空间共存，每个命名空间对应不同的来源（如当前页面、特定前端组件、浏览器扩展等）。

代理层在收到沙箱发来的工具调用消息 { callId, namespace, toolName, args } 后，执行以下校验流程：（1）验证 namespace 是否存在于注册表；（2）验证 toolName 是否在该命名空间下已注册；（3）使用注册工具的输入 schema 对 args 进行校验（Zod parse / JSON Schema validate）；（4）检查工具权限声明是否允许从沙箱调用。校验通过后，代理层在主页面执行上下文中调用工具的 execute 函数，捕获返回值或异常。返回值需经过结构化克隆（structuredClone）序列化后回传沙箱，以确保不泄露不可序列化的主页面对象引用。

代理层对非法工具调用执行统一错误处理：命名空间不存在返回 NAMESPACE_NOT_FOUND 错误；工具名不存在返回 TOOL_NOT_FOUND 错误；参数校验失败返回 INVALID_ARGUMENTS 错误并附带校验详情；工具执行异常返回 EXECUTION_ERROR 并附带异常消息和堆栈（仅开发模式）。所有错误均以结构化格式 { callId, error: { code, message, details? } } 回传。

### 动态工具命名空间、来源标识与冲突处理

为解决浏览器端工具的命名空间冲突和来源识别问题，方案引入"命名空间:工具名"二级命名体系，与现有 codemode 的 sanitizeToolName 机制协同工作。

每个工具在注册时被分配一个命名空间标识符（namespace），命名空间对应工具的来源：例如 "page" 表示当前页面提供的全局工具（如 getSelection、getPageTitle），"component:MyWidget" 表示特定前端组件暴露的工具，"extension:my-extension" 表示浏览器扩展注入的工具。命名空间标识符本身遵循与工具名相同的 sanitize 规则（字母数字下划线、不得以数字开头）。

工具全限定名格式为 namespace__toolName（双下划线分隔），避免与单下划线 sanitize 结果冲突。例如：页面工具 getSelection 的全限定名为 page__getSelection；组件 MyWidget 的刷新工具为 component_MyWidget__refresh。在生成 TypeScript 类型声明供 LLM 使用时，全限定名被转换为合法的命名空间对象路径：codemode.page.getSelection()、codemode.component_MyWidget.refresh()。工具名 sanitize 规则（参考 sanitizeToolName）：短横线/点号转为下划线、数字开头添加前导下划线、保留字追加下划线。

冲突处理策略：（1）同一命名空间内重复注册同名工具，后注册者覆盖先注册者并产生警告日志；（2）不同命名空间注册相同 baseToolName 不视为冲突——全限定名不同；（3）全限定名冲突（两个不同来源注册了相同的 namespace 和 toolName）时，拒绝后注册者并返回注册冲突错误。此外，方案提供一个可选的全局工具别名表，允许将特定全限定名映射为简短的别名（如将 page__getSelection 别名设为 getSelection），用于当 LLM 上下文只有一个活跃命名空间时简化代码编写。

### 工具描述与输入输出 Schema 生成

方案复用 codemode 中 generateTypes 的类型生成机制，为浏览器端工具生成 LLM 可消费的 TypeScript 类型声明和工具描述。与现有 generateTypes 直接从 AI SDK tool 定义生成类型不同，浏览器端需额外处理工具的命名空间归属。

工具描述生成器（BrowserToolDescriptor）遍历主页面工具代理层的注册表，对每个命名空间下的每个工具执行：（1）提取工具的输入 schema（Zod schema 或 JSON Schema），生成对应的 TypeScript 输入类型定义；（2）提取工具的输出 schema（若有），生成返回类型定义；（3）生成带命名空间的 TypeScript 声明，格式为 declare const codemode: { namespace: { toolName: (input: InputType) => Promise<ReturnType>; }; }。最终将所有命名空间和工具的类型声明合并，作为系统提示或工具描述的一部分发送给 LLM。

对于仅有 JSON Schema（非 Zod）定义的客户端工具——即通过 ClientToolSchema 从浏览器注册的工具——方案使用 json-schema-to-zod 或等效转换器，先将 JSON Schema 转为 Zod schema，再进入上述 generateTypes 流程。转换过程中保持对 JSON Schema 关键约束的支持：type、properties、required、enum、oneOf/anyOf、$ref（仅限同文档内引用）。不支持的约束标注为 "unknown" 类型并记录警告。工具描述中的 description 字段直接取自 RegisteredTool.description，不作变换。

### 执行生命周期管理

每次浏览器端代码执行具有明确定义的生命周期阶段，由 BrowserSandboxExecutor 统一管理，确保资源的确定性分配与释放。

生命周期包含以下阶段：（1）准备阶段（PREPARING）：创建沙箱化 iframe，注入执行桥接脚本，初始化 Proxy 拦截层和日志劫持。此阶段设置超时计时器（默认 5 秒），超时则转入 ERROR 状态。沙箱就绪后通过 postMessage 发送 { type: 'sandbox:ready' } 确认。（2）就绪阶段（READY）：主页面收到 ready 确认后，通过消息通道将 LLM 生成的代码文本传入沙箱。代码在 iframe 内通过 new Function(code) 或间接 eval 执行（视 CSP 策略选择），而非通过 innerHTML 注入 script 标签。（3）执行阶段（EXECUTING）：代码开始执行，工具调用经 Proxy→postMessage→主页面代理→工具执行→结果回传链路完成。此阶段设置总体执行超时（默认 30 秒，可配置）。每次工具调用的 RPC 往返也设置独立超时（默认 10 秒）。（4）返回结果阶段（RESULT）：代码执行完成后，收集所有 console 日志输出，与返回值（或 undefined）组装为 ExecuteResult { result, logs }，通过回调返回。（5）异常阶段（ERROR）：代码执行抛出未捕获异常、沙箱创建超时、执行总超时、或工具代理层返回未恢复错误时进入此阶段。异常信息序列化为 ExecuteResult { error } 返回。超时场景额外标注 error.type = 'TIMEOUT'。（6）清理阶段（CLEANUP）：无论哪个终止路径，均执行清理：移除 iframe 元素，取消所有未完成的工具调用 Promise，清空日志缓冲区，释放 Proxy 引用。

生命周期状态机通过有限状态枚举管理，状态转换路径为：PREPARING→READY→EXECUTING→RESULT→CLEANUP，或从任意非终止状态→ERROR→CLEANUP。状态转换是不可逆的；一旦进入 CLEANUP，任何后续消息被忽略并记录警告。状态机实现在主页面侧，iframe 侧通过心跳消息（每 2 秒一次）帮助检测沙箱意外销毁。

### 安全控制机制

方案的安全控制围绕一个核心原则设计：LLM 生成的代码不能直接访问或修改主页面全局对象、DOM 或 JavaScript 运行时。

第一层——iframe 沙箱属性隔离：通过 sandbox="allow-scripts" 且不含 allow-same-origin，iframe 内代码运行在完全独立的源（null origin）中。iframe 内的 window、document、parent、top 等全局对象与主页面无关联，无法通过 parent.document 等路径访问主页面 DOM。此外不设置 allow-forms 阻止表单提交、不设置 allow-popups 阻止弹窗、不设置 allow-modals 阻止对话框劫持。

第二层——CSP 策略强化：通过 Content-Security-Policy HTTP 响应头对 iframe 内的 HTML 文档施加额外限制：default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; style-src 'unsafe-inline'; frame-src 'none'。这确保 iframe 内代码无法发起任何网络请求（XHR、fetch、WebSocket、EventSource 均被浏览器阻断），无法加载外部图片或其他资源。

第三层——消息通道结构化约束：沙箱与主页面之间的所有通信仅通过 postMessage 进行，且消息必须符合预定义的结构化协议。主页面侧的消息监听器执行以下校验：（1）验证 event.source 是否来自当前活跃的沙箱 iframe 的 contentWindow；（2）验证 event.origin 是否为 null（沙箱化 iframe 的 origin）；（3）验证消息结构是否包含必填字段（如 callId、type）；（4）验证消息类型是否为已知类型白名单中的一种。不符合任一条件的消息被静默丢弃并记录安全日志。

第四层——工具执行的主页面边界：工具的真实执行始终发生在主页面侧。沙箱内代码只能通过 Proxy→postMessage 发起工具调用请求，实际执行由主页面工具代理层完成。主页面代理层对每次调用执行参数校验和权限检查，工具返回的数据通过 structuredClone 序列化后回传——不可序列化的对象（如 DOM 节点、函数、闭包）在序列化阶段被自然丢弃，不可能泄漏到沙箱中。

第五层——代码注入安全：LLM 生成的代码文本通过 new Function(code) 在 iframe 内执行，而非通过 innerHTML 或 document.write 注入。new Function 创建的函数据有其自身的词法作用域，无法访问 iframe 内除全局对象之外的局部变量。代码执行完成后，该函数对象被置为 undefined 并允许垃圾回收。同时，代码在执行前经过 AST 静态分析（使用 acorn 或等效解析器），检测并拒绝包含以下模式的代码：访问 parent / top / opener 全局变量、使用 import() 动态导入、使用 Worker() 构造函数创建子 Worker、使用 eval() 或类似动态执行函数。

### 工具调度协议

沙箱与主页面之间的工具调度遵循结构化的消息协议。消息分为三类：控制消息、调用消息和日志消息。

控制消息由主页面发往沙箱或沙箱发往主页面，用于生命周期协调。沙箱→主页面：{ type: 'sandbox:ready' }（沙箱就绪）、{ type: 'sandbox:error', error: { message, stack? } }（沙箱初始化失败）。主页面→沙箱：{ type: 'host:execute', code: string, executionId: string }（下发待执行代码）、{ type: 'host:abort', executionId: string }（中止执行）。

调用消息是工具调用的核心协议。沙箱→主页面：{ type: 'tool:call', callId: string, namespace: string, toolName: string, args: unknown }。主页面→沙箱：{ type: 'tool:result', callId: string, result: unknown }（成功）、{ type: 'tool:error', callId: string, error: { code: string, message: string, details?: unknown } }（失败）。每条调用消息携带唯一的 callId（UUID v4），沙箱侧 Proxy 为每个 callId 创建一个 Promise，resolve/reject 函数存入 Map<callId, {resolve, reject, timer}>。收到对应结果或错误消息时取出并调用。超时未收到响应的调用（默认 10 秒），对应 Promise 被 reject 并附带 TIMEOUT 错误码，同时从 Map 中清理。

日志消息：沙箱→主页面：{ type: 'log', executionId: string, level: 'log'|'warn'|'error', args: SerializableValue[] }。主页面侧按 executionId 分组收集日志，在 ExecuteResult.logs 中按时间顺序排列返回。

### 端到端处理流程

完整执行流程整合了服务端与浏览器端各模块，以下为一次典型调用的端到端流程。

第一步——工具注册与描述生成：浏览器端应用在页面加载时或动态地通过 ToolProxy.register(namespace, toolName, descriptor) 注册客户端工具。注册信息包含输入/输出 schema、执行函数和来源标识。服务端的 createCodeTool 在构建 codemode 工具时，除服务端工具集外还从当前会话的 clientTools 中提取浏览器工具描述，统一经 generateTypes 生成包含所有工具（服务端+浏览器端）的类型声明，发送给 LLM 作为工具描述。

第二步——代码生成与分发：LLM 在对话中决定使用 codemode 工具时，生成包含编排逻辑的 JavaScript/TypeScript 代码。服务端 createCodeTool 的 handler 对代码进行沙箱化包装：代码文本首先经 AST 扫描检测禁止的浏览器 API 引用模式（parent、top、import()、Worker()、eval()），检测到违规则拒绝执行并返回错误给 LLM。通过检测后，代码与当前会话的浏览器工具注册信息一同下发至客户端。

第三步——沙箱创建与代码注入：浏览器端 BrowserSandboxExecutor 创建沙箱化 iframe，等待 sandbox:ready 确认。就绪后，将代码文本通过 host:execute 消息发送至沙箱。沙箱内的执行桥接脚本通过 new Function(code) 创建可执行函数并调用。代码执行过程中对 codemode.namespace.toolName(args) 的调用被 Proxy 拦截，转换为 tool:call 消息发送至主页面。

第四步——工具调用代理执行：主页面 ToolProxy 接收 tool:call 消息后，按 namespace→toolName 查找注册表，校验输入参数，在主页面执行上下文中调用工具的 execute 函数。返回值经 structuredClone 序列化后以 tool:result 消息回传沙箱。沙箱内 Proxy 解析对应 Promise，代码继续执行。

第五步——结果收集与清理：代码执行完成后（或抛出异常、触发超时后），主页面收集所有日志消息，组装 ExecuteResult。iframe 被移除，所有待处理的 Promise 被清理，日志缓冲清空。ExecuteResult 返回给服务端 codemode handler，最终作为工具调用结果返回给 LLM。

### 与现有体系协作

本方案设计为与现有 codemode、agent chat、client tool 体系松耦合协作，而非替代。

与 codemode 的集成：BrowserSandboxExecutor 实现与 DynamicWorkerExecutor 相同的 Executor 接口。createCodeTool 通过执行器选择策略（executorStrategy）决定代码在服务端还是浏览器端执行：（1）当 LLM 生成的代码仅调用服务端工具时，使用 DynamicWorkerExecutor；（2）当代码调用任一浏览器端工具（即带有 namespace 前缀的工具）时，使用 BrowserSandboxExecutor；（3）混合场景中，代码在浏览器端执行，对服务端工具的调用通过主页面→服务端 RPC 回环（类似 DatabaseLoopback 的反向模式）完成。

与 agent chat / Think 的集成：复用 Think agent 的 clientTools 注册、持久化和工具结果回传机制。浏览器端工具通过 ClientToolSchema[] 格式注册到 Think agent，Think 将其持久化到 SQLite（think_request_context 表），并在 onChatMessage 中合并到工具集。与现有 client tools 的区别在于：现有 client tools 由 LLM 通过标准 AI SDK tool calling 逐个调用，本方案扩展为 LLM 可通过 codemode 在浏览器端以代码编排方式批量、带逻辑地组合调用这些工具。两者共存：简单单步工具调用仍走标准 tool calling，多步编排走 codemode。

额外依赖最小化：浏览器端方案的核心依赖仅包括：一个与主页面同源的空白 HTML 文档（作为沙箱宿主）、postMessage API（浏览器原生）、structuredClone API（浏览器原生）、acorn 解析器（用于代码 AST 静态扫描，体积小且纯 JS）。不引入额外的 iframe 通信库、沙箱运行时或 worker 线程管理库。

### 技术效果

（1）安全隔离：通过五层安全控制（iframe sandbox 属性、CSP 策略、消息协议校验、主页面工具代理边界、AST 静态扫描），确保 LLM 生成的代码无法访问、修改或污染主页面全局对象和 DOM。与仅在服务端执行代码相比，本方案将攻击面从服务端 isolate 转移到了浏览器沙箱，而浏览器沙箱的安全属性（null origin、无网络访问、无同源访问）对生成代码的约束甚至强于服务端 Worker isolate。

（2）工具编排能力扩展：使 LLM 能够以编程方式编排浏览器端工具，支持条件判断、循环、数据转换、多工具组合等复杂逻辑，而非只能逐个调用工具。这使得一次 LLM 调用即可完成"从页面选择内容→分析文本→调用格式化工具→写入剪贴板"等多步工作流，减少 LLM 与服务端的往返次数。

（3）零迁移成本：浏览器端工具无需任何改造即可被 codemode 编排——开发者只需通过 ToolProxy.register 注册工具（与现有 clientTools 注册方式兼容），方案的 generateTypes 机制自动生成类型声明，LLM 即可在生成代码中调用。

（4）与现有体系兼容：BrowserSandboxExecutor 实现与 DynamicWorkerExecutor 相同的 Executor 接口，createCodeTool 无感知底层执行器差异。方案复用现有的工具 sanitize 规则、类型生成管线、clientTools 注册与持久化机制，不引入新的概念模型。

### 风险与待确认问题

（1）CSP 与 new Function 兼容性：某些浏览器在启用严格 CSP 时，即使设置了 script-src 'unsafe-inline'，仍可能阻止 new Function() 调用——因为 new Function 在 CSP 规范中被归类为 eval 类执行。解决方案备选包括：使用 Blob URL + import()（但需 allow-same-origin）、使用 Web Worker 替代 iframe（Worker 的 CSP 继承自创建它的文档）、或在构建时预编译为间接 eval 兼容形式。此问题需在目标浏览器矩阵上实测验证。

（2）structuredClone 序列化边界：structuredClone 无法序列化函数、Symbol、DOM 节点、WeakRef 等对象类型。工具返回数据中若包含这些类型，序列化将抛错。需在工具注册文档中明确约束返回数据类型，并在代理层捕获序列化异常，返回 SERIALIZATION_ERROR 而非静默丢弃数据。

（3）沙箱 iframe 的跨域限制与同源文档托管：空白 HTML 文档需与主页面同源以便 postMessage 通信，但 iframe 的 sandbox 属性去除了 allow-same-origin，导致 iframe 内 origin 为 null。postMessage 的 origin 校验在 targetOrigin 为 null 时行为在部分浏览器中存在差异。需在消息监听中对 event.origin === null 做显式兼容。

（4）并发工具调用处理：LLM 生成的代码可能包含 Promise.all([codemode.a.tool1(), codemode.b.tool2()]) 等并发调用。当前协议设计中，多个 tool:call 消息携带不同 callId 可以并发发出，主页面代理层需支持并发工具执行。需要考虑并发调用时对主页面状态的竞争访问问题——由工具实现者自行保证线程安全（JavaScript 单线程模型下天然串行，但 await 点之间可能交错）。

（5）服务端工具在浏览器端的回环调用：混合场景中，代码在浏览器端执行但需调用服务端工具。回环路径为：沙箱→postMessage→主页面→WebSocket→服务端 Agent→工具执行→结果返回。此路径的延迟远高于同主机 RPC，需要为回环调用设置独立的更长超时时间，并在工具描述中标注哪些工具为 remote 类型以便 LLM 生成代码时考虑延迟。

（6）沙箱复用与隔离度权衡：每次执行创建新 iframe 保证最强隔离，但 iframe 创建和初始化有固定开销（数百毫秒）。连续多次小规模代码执行场景下，可考虑沙箱复用——即一次创建、多次执行。但复用需处理状态残留（上次执行的全局变量、定时器等）。方案当前选择每次新建以简化安全和生命周期模型；沙箱复用作为后续优化方向。

### 必要技术特征

基于上述方案，必要技术特征可归纳为以下要点：

- 一种浏览器端代码隔离执行方法：创建沙箱化 iframe（sandbox="allow-scripts"、不含 allow-same-origin），通过 CSP 策略阻断网络与资源访问，在 iframe 内通过 Proxy 对象拦截代码对工具的调用请求，将调用请求序列化为结构化消息通过 postMessage 发送至主页面。
- 一种主页面工具代理机制：维护按命名空间组织的工具注册表，接收沙箱消息后按 namespace→toolName 查找工具，执行输入 schema 校验和权限检查后，在主页面执行上下文中调用工具函数，将返回值经 structuredClone 序列化后回传。
- 一种"命名空间:工具名"二级命名体系：每个浏览器端工具被分配来源命名空间标识符，全限定名格式为 namespace__toolName，通过双下划线分隔避免与 sanitize 规则冲突；生成 LLM 类型声明时映射为 codemode.namespace.toolName() 的嵌套对象调用路径。
- 一种工具描述生成方法：从工具注册表中的 Zod/JSON Schema 定义生成 TypeScript 类型声明，按命名空间组织为嵌套对象类型，合并后作为 LLM 的工具描述输入。
- 一种执行生命周期状态机：定义 PREPARING→READY→EXECUTING→RESULT→CLEANUP 及任意状态→ERROR→CLEANUP 的状态转换路径；每个阶段定义独立超时；进入 CLEANUP 后不可逆地释放所有资源。
- 一种安全控制层次：包含 iframe sandbox 属性层、CSP 策略层、消息协议校验层（验证 origin、source、消息结构和类型白名单）、主页面代理边界层（工具执行仅在主页面侧、数据经序列化边界回传）、AST 静态扫描层（检测并拒绝包含 parent/top/import()/Worker()/eval() 等禁止模式的代码）。
- 一种与现有 codemode 体系的协作方法：BrowserSandboxExecutor 实现与 DynamicWorkerExecutor 相同的 Executor 接口，createCodeTool 通过执行器选择策略自动决定服务端或浏览器端执行；服务端工具在浏览器端场景通过主页面→服务端 RPC 回环调用。
