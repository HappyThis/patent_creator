## 技术方案

### 技术方案总述

本方案在现有 codemode 体系（服务端 Worker 隔离执行）的基础上，扩展支持浏览器侧隔离执行 LLM 生成的编排代码，使其能够安全调用仅存在于浏览器页面中的动态工具（如读取页面选择内容、访问页面状态、调用前端组件暴露的能力等），而无需将浏览器私有能力搬到服务端，也不允许生成代码直接访问或污染主页面环境。

方案核心思路：在浏览器主页面中创建一个沙箱化的隔离执行环境（基于 iframe 的 sandbox 机制），将 LLM 生成的编排代码注入该环境执行；主页面维护一个工具注册表，包含工具名、命名空间、来源标识、输入输出 schema 和实际执行函数；隔离环境通过结构化消息协议向主页面代理发起工具调用请求，主页面工具调度器校验、执行并回传结果。整个机制在复用现有 ToolProvider/Executor 接口和工具描述生成管线的前提下，新增一个浏览器侧 Executor 实现。

### 浏览器侧隔离执行环境

浏览器侧隔离执行环境采用 iframe 配合 sandbox 属性构建。iframe 的 sandbox 属性仅启用 allow-scripts，不启用 allow-same-origin、allow-top-navigation、allow-forms、allow-popups 等权限。由于未启用 allow-same-origin，iframe 内的代码运行在与主页面完全不同的源上下文中，无法通过 DOM API 访问主页面的 window、document 或任何 JavaScript 对象。

隔离环境内部注入一段 executor 引导代码（bootstrap），该引导代码负责：（1）建立与主页面的双向 MessageChannel 通信链路；（2）接收主页面发送的工具描述符集合（包含命名空间、工具名、输入输出类型签名）和执行代码；（3）为每个工具命名空间创建 Proxy 拦截对象，拦截对 codemode.xxx()、page.xxx() 等命名空间的调用；（4）执行 LLM 生成的异步箭头函数并通过 Promise.race 与超时定时器竞争；（5）捕获执行结果、异常和 console 日志，通过消息通道回传给主页面。

引导代码在消息协议层面区分以下消息类型：ready（隔离环境就绪）、execute（主页面下发代码与工具描述符）、tool_call（代理请求调用工具）、tool_result（主页面返回工具执行结果）、tool_error（主页面返回工具调用异常）、exec_result（执行完成，含返回值与日志）、exec_error（执行异常，含错误信息和日志）。每种消息均包含唯一的执行标识 executionId，以支持同一隔离环境实例中多次执行的生命周期隔离。

### 动态工具命名空间与来源标识

浏览器侧的动态工具按其来源和应用场景划分为不同的命名空间。每个命名空间由一个 ToolProvider 描述，包含：name（命名空间标识符，如 "page"、"component"、"mcp:orders"）、tools（该命名空间下的工具集合，每个工具有 name、description、inputSchema、outputSchema 和 execute 函数）、source（来源标识，标明工具注册来源，如 "user-page"、"react-component:SearchBar"、"mcp-bridge:remote-server"）。

命名空间在工具注册时由主页面工具注册表统一管理。注册表以 Map<namespaceId, ToolProvider> 结构维护所有已注册的工具提供者。当 LLM 发起 codemode 调用时，主页面将当前可用的所有命名空间及工具的描述信息（类型签名和 JSDoc 注释）序列化后传递给隔离环境，供 Proxy 拦截层使用。生成的类型声明中，每个命名空间对应一个独立的 declare const 块（如 declare const page: { getSelection: ... }），使 LLM 生成的代码可以自然地按命名空间调用工具。

来源标识（source）不传递给 LLM 或隔离环境，仅用于主页面侧的审计日志和调试。当工具调用失败或出现异常时，日志中携带 source 信息以辅助定位问题。不同来源的工具可以在同一命名空间下共存（如多个组件的工具合并在 "component" 命名空间下），此时工具名的唯一性由注册表在插入时校验。

### 工具名规范化与冲突处理

工具名规范化沿用现有 codemode 包中的 sanitizeToolName 算法：将工具名中的连字符（-）、点号（.）、空格替换为下划线；剥离其他非标识符字符；以数字开头的名称前加下划线前缀；JavaScript 保留字后追加下划线。例如 "get-weather" 规范化为 "get_weather"，"3d-render" 规范化为 "_3d_render"，"delete" 规范化为 "delete_"。规范化发生在两个层面：（1）隔离环境内 Proxy 拦截层的属性访问键；（2）主页面工具调度器在注册表中的查找键。

冲突处理采用分层策略。第一层：同一命名空间内，工具注册表以原始工具名为键，后注册的工具覆盖先注册的同名工具，并发出警告日志。第二层：不同命名空间之间允许同名工具共存，因为 LLM 生成的代码通过命名空间前缀（如 page.getSelection 与 component.getSelection）区分调用目标，不会产生歧义。第三层：当工具名规范化后产生碰撞（两个不同的原始工具名规范化到同一标识符，如 "my-tool" 和 "my.tool" 均规范化为 "my_tool"），注册表在插入时检测此碰撞并抛出注册异常，要求工具提供方修改名称。

传递给 LLM 的类型声明中使用规范化后的工具名，并在 JSDoc 注释中保留原始工具名作为参考，帮助 LLM 理解工具语义。

### 工具描述与输入输出结构生成

工具描述与输入输出结构生成沿用现有 codemode 的 generateTypes 管线，支持从 Zod schema、Standard Schema 协议以及 AI SDK 的 jsonSchema 包装器三种输入格式中提取 JSON Schema，并转换为 TypeScript 类型声明。每个工具生成如下结构：（1）Input 类型别名（如 type GetSelectionInput = { ... }），描述工具的输入参数结构；（2）Output 类型别名（如 type GetSelectionOutput = { text: string; html: string }），描述工具的输出结构；（3）在对应命名空间的 declare const 块中声明工具函数签名（如 getSelection: (input: GetSelectionInput) => Promise<GetSelectionOutput>）。

对于浏览器侧特有的动态工具，工具提供方在注册时直接提交 JSON Schema 描述或 Zod schema，无需服务器侧预处理。注册表在接收注册请求时校验 schema 的有效性（必须是合法的 JSON Schema draft-07 或更高版本），校验通过后存储并可用于后续的类型生成。当工具集合发生变化（新增、移除或更新工具）时，重新生成类型声明并可通过消息协议推送至隔离环境，使 LLM 获得最新的工具描述。

输入输出 schema 还在主页面工具调度器侧执行运行时校验：调度器在调用实际工具执行函数之前，将隔离环境传来的参数与注册时的 inputSchema 进行校验，校验失败则直接返回错误而不执行工具函数；同理，工具函数返回的结果与 outputSchema 进行校验，不匹配时发出警告日志但仍返回结果，以保证 LLM 能获得用于纠错的反馈。

### 隔离执行协议与代理边界

隔离环境与主页面之间的通信采用基于 MessageChannel 的结构化消息协议，而非原始 postMessage 字符串。MessageChannel 提供点对点的双向通信通道，port1 留在主页面，port2 通过 iframe 的 contentWindow 传递到隔离环境。两个端口之间传递的是结构化克隆（structured clone）后的消息对象，天然防止函数、DOM 节点和不可序列化对象的泄露。

工具调用的代理边界如下：LLM 生成的代码在隔离环境中调用如 codemode.search({ query: "test" }) 时，Proxy 拦截层将调用转换为 tool_call 消息，消息体包含：executionId（执行实例标识）、namespace（命名空间，如 "codemode"）、toolName（规范化后的工具名）、callId（每次调用的唯一标识）、args（参数对象）。Proxy 通过 port2.postMessage() 发送该消息并等待响应。主页面侧的 port1.onmessage 处理器接收 tool_call 消息后，按以下流程处理：

（1）根据 namespace 查找对应的 ToolProvider；（2）在 ToolProvider 的注册表中以 toolName 反查原始工具名和 execute 函数，若未找到则返回 tool_error 消息（含 "Tool not found" 错误）；（3）使用 inputSchema 对 args 进行运行时校验，校验失败返回 tool_error 消息（含校验错误详情）；（4）调用 execute(args)，若 execute 抛出异常则捕获并返回 tool_error 消息（含异常信息）；（5）正常返回时构造 tool_result 消息，包含 callId 和 result 值。Proxy 拦截层收到 tool_result 后 resolve Promise，收到 tool_error 后 reject Promise。

关键安全约束：生成代码只能通过 Proxy 拦截层间接调用主页面工具，永远无法获取 execute 函数的引用、无法直接访问主页面 DOM 或全局对象、无法通过 import() 或 eval() 绕过 Proxy。这是因为 iframe 的 sandbox 属性隔离了所有浏览器 API 的直接访问路径，Proxy 层是唯一的工具访问通道。即使用户代码中写 document.querySelector(...)，在 sandbox 隔离下该调用将抛出 ReferenceError。

### 生命周期管理

每次浏览器侧 codemode 执行具有明确的生命周期，分为以下阶段：

（1）准备阶段（prepare）：主页面创建 iframe 元素，设置 sandbox="allow-scripts" 属性和 Content-Security-Policy 头部（通过 srcdoc 或 Blob URL 注入 meta 标签）。创建 MessageChannel，将 port2 通过 iframe.contentWindow.postMessage 传递到隔离环境。注入 executor 引导代码。此阶段设置超时（如 5 秒），若超时未收到 ready 消息则进入错误清理流程。

（2）就绪阶段（ready）：引导代码完成初始化后，通过 port2 发送 ready 消息。主页面收到 ready 后，发送 execute 消息，携带 executionId、工具描述符集合（含各命名空间的工具名、类型签名和 JSDoc）、经 AST 规范化后的执行代码。同时启动执行超时定时器（默认 30 秒，可配置）。

（3）执行阶段（execute）：隔离环境构造各命名空间的 Proxy 对象，将执行代码包装为异步箭头函数并通过 Promise.race 与超时 Promise 竞争。执行过程中，工具调用通过 tool_call / tool_result 消息对同步完成（Proxy 拦截层在调用处 await 等待主页面响应）。console.log / console.warn / console.error 输出被引导代码拦截并收集到日志数组。

（4）结果/错误/超时阶段（result/error/timeout）：执行成功时，引导代码发送 exec_result 消息，包含 result（函数返回值）和 logs（日志数组）。执行异常时发送 exec_error 消息，包含 error（错误信息）和 logs。超时触发时，Promise.race 中的超时 Promise resolve，引导代码捕获超时异常并发送 exec_error。三种情况均结束后进入清理阶段。

（5）清理阶段（cleanup）：主页面关闭 MessageChannel 的两个端口（port1.close()、port2.close()），从 DOM 中移除 iframe 元素，释放关联的 Blob URL（如有），清空该执行实例在注册表中缓存的临时状态。清理操作保证在正常返回、异常和超时三种路径下均执行。

### 安全控制机制

本方案的安全控制机制围绕"生成代码不能直接访问或污染主页面对象"这一核心约束，构建多层防护：

第一层——iframe sandbox 隔离：iframe 的 sandbox 属性仅启用 allow-scripts，不启用 allow-same-origin。这意味着 iframe 内的代码运行在一个无源的上下文中，所有对主页面 DOM、window、document 的访问尝试均被浏览器运行时拦截。同时不启用 allow-top-navigation（防止劫持主页面导航）、allow-forms（防止提交表单）、allow-popups（防止弹窗）、allow-modals（防止模态对话框）。

第二层——CSP 策略：隔离环境通过 iframe 的 srcdoc 或 Blob URL 注入 Content-Security-Policy 的 meta 标签，策略为：default-src 'none'; script-src 'unsafe-inline'（仅允许内联脚本，禁止加载外部脚本）；禁止 connect-src、img-src、style-src 等所有外部资源加载。这防止了生成代码通过创建 script 标签加载外部代码绕过隔离。

第三层——结构化克隆通信：MessageChannel 的 postMessage 使用结构化克隆算法传递数据，该算法不能序列化函数、DOM 节点、Error 对象、Symbol 等，从协议层面杜绝了主页面对象引用泄露到隔离环境的可能。即使 Proxy 拦截层尝试通过消息传递函数引用，结构化克隆也会抛出 DataCloneError。

第四层——工具调度器校验：主页面的工具调度器在每次工具调用时执行三重校验：工具名是否在注册表中存在、参数是否符合 inputSchema、调用者是否具有执行权限（基于命名空间的调用白名单）。校验失败不执行工具函数，返回标准错误结构。

第五层——执行超时：每个执行实例有独立的超时定时器（默认 30 秒），超时后 Promise.race 使执行终止，防止 LLM 生成的代码陷入死循环或长时间阻塞。超时后仍进入清理阶段，确保资源释放。

第六层——非法工具调用拦截：当隔离环境中的代码尝试调用未注册的工具名时，Proxy 拦截层仍会发送 tool_call 消息，但主页面调度器因查不到工具而返回 tool_error，Proxy 层将其转为 JavaScript 异常抛出。这保证了未注册工具调用不会静默失败，LLM 会收到明确的错误反馈。

### 与现有 codemode / agent chat / client tool 体系的协作

本方案通过与现有 codemode 体系共享 Executor 接口实现无缝协作。在服务端场景中，createCodeTool 使用 DynamicWorkerExecutor；在浏览器场景中，createCodeTool 使用新增的 BrowserSandboxExecutor。两个 Executor 实现遵循相同的 Executor 接口（execute(code, providers) → ExecuteResult），因此 createCodeTool 的工具描述生成、代码规范化、类型声明、错误包装等逻辑完全复用，无需修改。

与现有 agent chat 和 client tool 体系的协作方式：当 AIChatAgent 在服务端运行时，LLM 可通过 codemode 工具生成编排代码；若代码中调用的工具包含 client tool（在服务端无 execute 函数），则在代码执行到该工具时通过现有的 onToolCall / CF_AGENT_TOOL_RESULT 流程回退到浏览器侧处理。当使用 BrowserSandboxExecutor 时，所有工具（包括 server tool 和 client tool）均可直接在浏览器侧执行，无需服务端中转，减少了网络往返。

具体接入点：在 createCodeTool 的工具描述阶段，工具提供方可通过一个标志位（如 executionHint: "browser" | "server" | "auto"）声明工具的推荐执行位置。当所有工具均标记为 "browser" 时，系统自动选择 BrowserSandboxExecutor。当工具混合时（部分有服务端 execute、部分仅浏览器可用），系统根据 executionHint 智能选择或在工具描述中区分可用性。

### 关键模块与处理流程

整个浏览器侧 codemode 系统由以下关键模块组成：

（1）BrowserSandboxExecutor：实现 Executor 接口的浏览器侧执行器，负责创建隔离 iframe、建立 MessageChannel、注入引导代码、发送执行指令、等待结果并清理资源。对外暴露 execute(code, providers) 方法，返回 ExecuteResult。

（2）ToolRegistry（工具注册表）：主页面侧的集中式工具管理器，维护 Map<namespaceId, ToolProvider>。提供 register(namespaceId, provider)、unregister(namespaceId)、listTools()（返回所有可用工具的扁平化描述）、resolve(namespaceId, toolName)（查找并返回工具的 execute 函数和 schema）等方法。支持动态增删工具并在变化时通知活跃的执行实例。

（3）SandboxBootstrap（引导代码）：注入到隔离 iframe 中的自执行脚本。包含：MessageChannel 端口初始化、消息协议解析与分发、各命名空间 Proxy 构造、console 拦截、Promise.race 超时逻辑、结果序列化与回传。该脚本为纯 JavaScript，不依赖任何外部库。

（4）ToolCallDispatcher（工具调度器）：主页面侧 port1.onmessage 的处理核心。接收 tool_call 消息后执行查找→校验→执行→回传的完整链路。同时负责日志收集，将每次工具调用的 namespace、toolName、args、result/error、耗时记录到执行实例的日志缓冲区。

（5）SchemaValidator（Schema 校验器）：对工具输入参数和输出结果进行运行时 JSON Schema 校验的模块。支持 draft-07 及以上版本。校验失败时生成结构化错误信息，包含字段路径和失败原因。

处理流程为：用户发起请求 → LLM 决定使用 codemode → createCodeTool 生成含工具类型声明的工具描述 → LLM 生成 JavaScript 编排代码 → normalizeCode 通过 acorn AST 规范化 → BrowserSandboxExecutor.execute() 被调用 → 创建隔离 iframe → 建立 MessageChannel → 注入引导代码 → 等待 ready → 发送 execute 消息 → 隔离环境执行代码，工具调用通过 Proxy → 消息协议 → 主页面 ToolCallDispatcher → ToolRegistry.resolve() → SchemaValidator 校验 → 执行函数 → 结果回传 → 执行完成/异常/超时 → 清理资源 → 返回 ExecuteResult 给 LLM。

### 技术效果

本方案在浏览器侧实现 LLM 生成代码的安全编排执行，具有以下技术效果：

（1）浏览器私有工具就地编排：LLM 生成的代码可在浏览器侧直接调用页面工具（DOM 操作、页面状态读取、前端组件能力、Web API 等），无需将工具能力暴露到服务端，减少了网络传输和序列化开销，也避免了敏感页面数据离开浏览器环境。

（2）多层安全隔离：通过 iframe sandbox、CSP 策略、结构化克隆通信、工具调度器校验、超时控制五层安全机制，确保 LLM 生成的代码无法访问或污染主页面对象，无法绕过工具注册表执行未授权操作。

（3）与现有体系无缝复用：BrowserSandboxExecutor 遵循与 DynamicWorkerExecutor 相同的 Executor 接口，使得 createCodeTool、类型生成、代码规范化等基础设施完全复用，降低实现复杂度和维护成本。

（4）工具动态注册与类型生成：支持浏览器页面在运行时动态注册和注销工具，工具描述和类型声明随工具集合变化自动更新，LLM 始终获得当前可用的最新工具视图。

（5）完整的生命周期与错误传播：每次执行具有明确的准备、就绪、执行、结果/错误/超时、清理五个阶段，异常通过结构化消息协议完整传播至主页面和 LLM，console 日志被捕获并返回，便于调试和问题定位。

### 风险与待确认问题

（1）iframe sandbox 兼容性：sandbox 属性在所有现代浏览器中均有良好支持，但部分旧版本浏览器对 allow-scripts 与 CSP 的交互行为存在差异（例如某些浏览器在 sandbox 下忽略 meta CSP 标签）。建议以 Chromium 系和 Firefox 最新两个主版本为最低支持目标，并在初始化时检测 sandbox 支持情况，不支持时回退到服务端 codemode 或直接拒绝浏览器侧执行。

（2）结构化克隆的性能边界：MessageChannel 的结构化克隆对大体积数据（如超过 1MB 的 ArrayBuffer）存在性能瓶颈。对于返回大体积数据的工具（如页面截图），建议工具函数将结果写入 Blob URL 并返回 URL 引用，而非直接通过消息通道传递二进制数据。

（3）并发执行实例管理：同一页面可能同时存在多个 BrowserSandboxExecutor 实例（如多个 codemode 调用并发执行）。每个实例有独立的 iframe 和 MessageChannel，资源消耗随实例数线性增长。建议设置最大并发实例数（如 3 个），超出的请求排队或拒绝。

（4）跨域 iframe 限制：若主页面与工具服务不在同源，sandbox 属性下的 iframe 可能无法通过 postMessage 正确建立 MessageChannel（取决于浏览器的跨域隔离策略）。在跨域场景下，需额外配置 Cross-Origin-Embedder-Policy 和 Cross-Origin-Opener-Policy 头部以启用 crossOriginIsolated 模式。

（5）工具注册的线程安全：浏览器 JavaScript 是单线程的，因此工具注册表的并发修改不会出现传统多线程竞态问题。但异步注册（如通过 MCP bridge 远程拉取工具列表）与同步执行之间可能存在时序问题：执行已开始但某个工具尚未注册完毕。建议在执行前等待所有待注册的异步 provider 完成，或在执行时对未就绪的工具返回明确的 "not ready" 错误而非静默忽略。

（6）与现有 needsApproval 机制的交互：当前 codemode 不支持 needsApproval 工具（此类工具被过滤掉）。在浏览器侧，某些工具可能需要用户确认（如访问剪贴板、获取地理位置）。建议在工具注册时为这类工具设置 requiresUserGesture 标志，调度器在执行前通过主页面 UI 请求用户确认，确认超时或拒绝时返回相应错误。

（7）日志收集的数据量上限：console 日志拦截可能因 LLM 生成代码中大量 console.log 导致日志数组膨胀。建议设置日志总长度上限（如 1000 条或 100KB），超出后截断并在末尾附加截断提示。
