## 技术方案

本方案提出一种浏览器侧隔离执行环境（BrowserSandboxExecutor），使 LLM 生成的编排代码可在浏览器端安全执行，并能通过标准化调度协议调用主页面已注册的动态客户端工具。方案在保持与现有 codemode 体系（@cloudflare/codemode）中 Executor 接口和 ToolProvider 命名空间机制一致的前提下，将代码执行位置从 Cloudflare Worker 侧扩展到浏览器端，复用已有的工具描述生成、代码规范化、工具名消毒等基础设施。

### 整体架构

系统由主页面端的浏览器工具注册中心（BrowserToolRegistry）和浏览器沙箱执行器（BrowserSandboxExecutor），以及沙箱 iframe 中的沙箱 Harness（SandboxHarness）三部分组成，三者通过基于 postMessage 的工具调度协议通道（ToolDispatchChannel）进行通信。整体架构如下：

- 主页面包含 BrowserToolRegistry，管理页面内所有动态客户端工具，每个工具带有来源标识（sourceId）、命名空间（namespace）以及输入输出 schema；
- BrowserSandboxExecutor 实现与 DynamicWorkerExecutor 相同的 Executor 接口，负责创建和管理隔离 iframe 的生命周期，并将工具调用请求通过协议通道转发到主页面执行；
- 沙箱 iframe 内运行的 SandboxHarness 接收 LLM 生成代码和工具命名空间配置，为每个命名空间构建 Proxy 全局变量，使沙箱内代码可通过 namespace.toolName() 语法调用工具；
- ToolDispatchChannel 基于 postMessage 实现，请求携带 correlationId 进行调用匹配，响应携带结果或错误。

### 浏览器工具注册中心（BrowserToolRegistry）

BrowserToolRegistry 是主页面端管理动态客户端工具的注册中心。每个注册的工具描述符包含以下核心字段：

- sourceId：工具来源标识，用于区分不同来源的工具。取值如 "page"（页面内置工具）、"component:datepicker"（组件暴露工具）、"extension:xxx"（浏览器扩展工具）。sourceId 不参与沙箱内的调用命名，但用于工具审计和冲突溯源；
- namespace：对应 codemode 体系中 ToolProvider.name，定义工具在沙箱内的调用前缀。默认命名空间为 "page"；
- name：工具原始名称，注册时经过 sanitizeToolName 规范化为合法 JavaScript 标识符，规则为：将连字符、点号和空格替换为下划线，去除其余非法字符；数字前缀加下划线；JavaScript 保留字追加下划线后缀；
- inputSchema：工具的输入 JSON Schema，描述参数结构和类型约束；
- outputSchema：可选，描述工具返回值的 JSON Schema；
- execute：在主页面上下文实际执行工具逻辑的函数，接收经 schema 校验后的参数，返回执行结果。

工具注册时进行冲突检测：同一 namespace 下同一规范化名称重复注册时，后注册工具覆盖前者并产生警告日志；不同 namespace 间允许同名工具，互不干扰。工具描述符可批量转换为 ToolProvider[] 格式，直接供 codemode 的 createCodeTool 使用。Registry 还提供 generateLLMTypes() 方法，基于已注册工具的 inputSchema/outputSchema，复用 codemode 中 generateTypesFromJsonSchema，为 LLM 生成可在沙箱代码中使用的 TypeScript 类型声明。

### 浏览器沙箱执行器（BrowserSandboxExecutor）

BrowserSandboxExecutor 实现 codemode 的 Executor 接口，与 DynamicWorkerExecutor 保持同一接口契约：execute(code, providersOrFns) → ExecuteResult { result, error?, logs? }。两者的差异在于底层执行载体：DynamicWorkerExecutor 在 Cloudflare Worker 隔离环境中通过 Workers RPC 调度工具，而 BrowserSandboxExecutor 在浏览器 iframe 沙箱中通过 postMessage 协议调度工具。

BrowserSandboxExecutor 的核心职责包括：

- iframe 生命周期管理：创建带有 sandbox="allow-scripts" 属性的隔离 iframe，注入 SandboxHarness 代码，并在执行完成后销毁 iframe 释放资源；
- 代码规范化与注入：复用 normalizeCode 对 LLM 生成的代码进行规范化处理（去除 markdown 代码围栏、自动补全 async 箭头函数包装等），然后与工具 Proxy 初始化片段合并，通过 postMessage 注入沙箱；
- 工具调度中转：接收沙箱内 Proxy 发出的工具调用请求，通过 ToolDispatchChannel 转发到主页面 BrowserToolRegistry 的 resolveCall 方法，并将执行结果回传沙箱；
- 超时控制：在沙箱 Harness 内部使用 Promise.race 对用户代码包装超时 Promise，超时后拒绝并收集已产生的日志；
- 日志收集：沙箱内 console.log/warn/error 被代理拦截，所有输出收集到 __logs 数组中，作为 ExecuteResult.logs 返回；
- 资源清理：执行结束或异常时，移除 message 事件监听器，拒绝所有未完成的工具调用 Promise，销毁 iframe 元素，释放关联的 Blob URL。

每个 BrowserSandboxExecutor 实例对应一次独立的执行会话。对于多轮 LLM 调用场景，可配合连接池复用 iframe 以降低创建开销（单次冷创建约 50-200ms），但每次执行前后需重置沙箱状态。

### 工具调度协议（ToolDispatchChannel）

工具调度协议定义了沙箱 iframe 与主页面之间的请求/响应消息格式。协议基于 postMessage 实现，使用 correlationId 进行请求匹配。

请求消息格式：

- type: "tool_call" —— 标识工具调用请求；
- correlationId: string —— 唯一请求标识，由沙箱侧生成（crypto.randomUUID()），用于匹配响应；
- namespace: string —— 工具命名空间，对应 ToolProvider.name；
- name: string —— 工具规范化名称（经 sanitizeToolName 处理）；
- args: string —— JSON 序列化的工具参数。

响应消息格式：

- type: "tool_result" —— 标识工具调用响应；
- correlationId: string —— 对应请求的标识；
- result?: unknown —— 工具执行成功时的返回值，经结构化克隆可序列化的 JSON 值；
- error?: string —— 工具执行失败时的错误消息字符串。

协议层面还定义了以下控制消息：

- sandbox_init: 主页面 → 沙箱，携带规范化后的代码和命名空间 Proxy 配置；
- sandbox_ready: 沙箱 → 主页面，表示沙箱 Harness 初始化完成，可以开始执行；
- sandbox_error: 沙箱 → 主页面，携带沙箱初始化或运行时错误信息。

主页面侧的 ToolDispatchChannel 在处理工具调用时执行以下步骤：(1) 验证消息来源 origin（沙箱 origin 为 null 时校验消息事件来源为对应 iframe 的 contentWindow）；(2) 根据 namespace+name 查找已注册工具，未找到返回 "Tool not found" 错误；(3) 使用工具的 inputSchema 对参数进行 JSON Schema 校验，校验失败返回详细错误；(4) 调用工具的 execute 函数，捕获同步/异步异常；(5) 将结果序列化为 JSON 并通过 postMessage 回传。

### 安全隔离机制

安全控制采用四层纵深防御体系，确保 LLM 生成的代码无法直接访问或污染主页面对象。

第一层——iframe sandbox 属性隔离：沙箱 iframe 使用 sandbox="allow-scripts" 属性创建。显式不设置 allow-same-origin，使沙箱页面 origin 为 null，与主页面不同源，浏览器强制执行同源策略，阻止沙箱代码访问主页面 DOM、Cookie、localStorage、sessionStorage、IndexedDB 等存储。同时不设置 allow-top-navigation、allow-popups、allow-forms、allow-pointer-lock 等权限，阻断导航逃逸和交互劫持。

第二层——全局对象 Proxy 白名单拦截：沙箱 Harness 内部通过 new Proxy(globalThis, handler) 劫持全局对象访问。handler 的 has 和 get 陷阱实现白名单策略：仅暴露工具命名空间函数（如 page、selection）、标准内置对象（Object、Array、Promise、Math、JSON、Date、String、Number、Boolean、RegExp、Map、Set、WeakMap、WeakSet、Symbol、BigInt、console 代理、Error、TypeError 等）、以及沙箱执行必要的基础能力。以下能力被显式禁止访问：fetch、XMLHttpRequest、WebSocket、EventSource、localStorage、sessionStorage、indexedDB、Worker、SharedWorker、ServiceWorker、eval、Function 构造器、setTimeout/setInterval 的直接引用（由沙箱内部管理定时器并在 cleanup 时强制清理）。未知属性访问返回 undefined 并记录告警。

第三层——postMessage 工具调度通道：工具调用统一通过 postMessage 请求/响应协议进行。沙箱代码无法绕过协议直接调用主页面函数。参数和返回值均经过 JSON 序列化/反序列化，函数、闭包、DOM 节点、Symbol、循环引用等无法序列化的对象在序列化阶段被自然截断或抛出错误。单次执行设置最大工具调用次数限制（默认可配置，如 100 次），防止无限循环调用耗尽主页面资源。

第四层——主页面工具代理边界：BrowserToolRegistry 维护已注册工具的白名单，仅白名单内的工具可被沙箱调用。每个工具的 execute 函数在主页面上下文执行，但沙箱传入的参数已经 schema 校验和反序列化为纯数据。工具返回值经结构化克隆后回传，不暴露主页面内部对象引用。每个工具调用的执行在 try/catch 包裹中进行，异常信息经脱敏处理（移除文件路径、内部状态等敏感信息）后返回沙箱。

### 执行生命周期

每次代码执行均遵循明确的生命周期阶段，确保可观测、可恢复、可清理。

准备阶段（PREPARE）：创建 sandbox iframe 元素并设置 sandbox="allow-scripts" 属性。加载 SandboxHarness HTML 页面（内联或通过 Blob URL），注入 Harness 脚本。主页面发送 sandbox_init 消息，携带规范化后的 LLM 代码片段、工具命名空间 Proxy 初始化配置、以及超时时间。沙箱 Harness 完成初始化后回传 sandbox_ready 信号。若初始化失败（如代码语法错误、iframe 加载超时），返回 sandbox_error 并进入清理阶段。

执行阶段（EXECUTE）：沙箱 Harness 接收到 sandbox_init 后：(1) 为每个工具命名空间构建 Proxy 全局变量，Proxy 的 get 陷阱拦截方法调用，将 namespace.toolName(args) 转换为 postMessage 工具调用请求；(2) 代理 console.log/warn/error，将输出推入 __logs 数组；(3) 将用户代码包装为 IIFE，与超时 Promise（setTimeout + reject）通过 Promise.race 竞速；(4) 执行结果（含 result 或 error 以及 __logs）通过 postMessage 回传给主页面。主页面在执行期间处理工具调用请求并回传结果。

错误处理阶段：区分三类错误并分别处理。沙箱初始化错误（iframe 加载失败、Harness 脚本解析失败）由 BrowserSandboxExecutor 捕获，返回 ExecuteResult.error 且不再尝试执行代码。代码运行时错误（用户代码抛出异常、超时）由沙箱 Harness 的 try/catch 和 Promise.race 捕获，以 error 字段返回并附带已收集的日志。工具调用错误（工具未找到、参数校验失败、工具执行异常）由主页面 ToolDispatchChannel 捕获，以 tool_result 的 error 字段返回给沙箱，沙箱内 Proxy 将其转换为 throw 供用户代码感知。

清理阶段（CLEANUP）：执行结束后（无论成功、失败、超时），依次执行：移除主页面侧 message 事件监听器；遍历所有未完成的工具调用 Promise 并以 "Execution terminated" 拒绝；使用 AbortController 终止沙箱内所有由沙箱管理的定时器；从 DOM 中移除 iframe 元素；若使用了 Blob URL 则调用 URL.revokeObjectURL 释放。清理阶段本身不抛出异常，确保不影响后续执行。

### 与现有 codemode 体系的接口对齐

本方案与现有 codemode 体系在以下层面保持接口对齐，确保 LLM 和上层应用无需感知执行环境差异。

Executor 接口统一：BrowserSandboxExecutor 实现与 DynamicWorkerExecutor 完全一致的 Executor 接口——execute(code: string, providersOrFns: ResolvedProvider[] | Record<string, fn>): Promise<ExecuteResult>。上层 createCodeTool 函数接受任意 Executor 实现，传入 BrowserSandboxExecutor 即可生成可在浏览器侧使用的 AI SDK tool。LLM 看到的工具描述和类型声明完全一致，不因执行位置改变。

ToolProvider 命名空间机制复用：浏览器端工具通过 BrowserToolRegistry 注册时使用 namespace 字段（如 "page"、"selection"、"component"），与 codemode 的 ToolProvider.name 对应。SandboxHarness 内为每个 namespace 生成的 Proxy 结构（Proxy.get → 工具调用）与 DynamicWorkerExecutor 中 proxyInits 的逻辑相同，仅底层调度方式从 Workers RPC 切换为 postMessage。

工具描述与类型生成复用：BrowserToolRegistry.generateLLMTypes() 直接调用 codemode 的 generateTypesFromJsonSchema（或 generateTypes），从 ToolDescriptors 的 inputSchema/outputSchema 生成 TypeScript 类型声明。normalizeCode 的代码规范化（去围栏、补箭头函数等）和 sanitizeToolName 的工具名消毒在浏览器侧直接复用，无需重新实现。

与 agent chat / client tool 体系协作：BrowserSandboxExecutor 可嵌入到 AIChatAgent 的工具链中。当 LLM 选择调用 code 工具时，agent 根据可用工具列表判断工具位于服务端还是浏览器端：若所需工具全部在服务端，使用 DynamicWorkerExecutor；若涉及浏览器私有工具，使用 BrowserSandboxExecutor。两者的切换对 LLM 透明，由 agent 框架根据工具来源标识（sourceId）自动选择执行器。

### 技术效果

本方案的技术效果包括以下几个方面。

- 执行环境从服务端扩展到浏览器端：通过实现与 DynamicWorkerExecutor 相同接口的 BrowserSandboxExecutor，LLM 生成的编排代码可在浏览器侧安全执行，无需将浏览器私有工具搬到服务端，降低了系统复杂度和网络延迟。
- 工具描述自动生成：基于已注册工具的 JSON Schema，自动生成 LLM 可理解的 TypeScript 类型声明和调用签名，LLM 可据此生成正确的编排代码，无需人工编写工具说明。
- 多层次安全隔离：通过 iframe sandbox 属性、全局对象 Proxy 白名单、postMessage 协议通道、主页面工具代理边界四层防御，确保生成代码无法直接访问或修改主页面对象，工具调用必须经注册、校验和调度。
- 与现有体系无缝兼容：BrowserSandboxExecutor 实现标准 Executor 接口，可被 createCodeTool 直接使用；工具注册使用 ToolProvider 命名空间机制；代码规范化和工具名消毒直接复用 codemode 已有模块。
- 生命周期可控：每次执行具备明确的准备、执行、错误处理和清理阶段，支持超时控制、日志收集和资源释放，避免 iframe 泄漏和未完成 Promise 悬挂。
- 工具名规范化与冲突处理：通过 sanitizeToolName 确保工具名在沙箱内为合法 JavaScript 标识符，通过 namespace 隔离不同来源的工具，同 namespace 冲突时明确覆盖并告警。
- 额外依赖最小化：方案仅依赖浏览器原生 API（iframe、postMessage、Proxy、Promise），无需引入第三方沙箱库或 Web Worker polyfill，与现有 agent chat / client tool 体系可直接协作。

### 风险与待确认问题

以下为当前方案需要后续确认的风险点和技术决策待定项：

- iframe vs Web Worker 选型：iframe sandbox 提供更彻底的隔离（浏览器强制同源策略、存储隔离），但创建开销约 50-200ms 且有跨域通信序列化开销；Web Worker 创建更快但需要额外措施阻断 DOM 访问。当前方案选择 iframe，需在实际场景中验证性能是否满足要求。
- 多实例并发策略：每次 LLM 调用是创建全新 iframe 还是使用连接池复用。创建新 iframe 隔离性最好但开销较大；池化复用可降低延迟但需额外实现沙箱状态重置机制。建议初始版本每次新建，后续根据性能数据引入池化。
- 返回值序列化边界：postMessage 的结构化克隆算法不支持 Error、Map、Set、Symbol、循环引用、Blob、File 等对象类型。工具返回此类对象时需定义降级策略（如 Map 转普通对象、Error 提取 message 和 stack），需与工具开发者约定返回值类型约束。
- sourceId 标准化：工具来源标识（sourceId）的取值规范需与 codemode 上游协商确定，确保服务端工具和浏览器端工具的来源标识体系一致，便于 agent 框架自动路由执行器选择。
- 沙箱内定时器清理：沙箱代码可能调用 setTimeout/setInterval，销毁 iframe 时浏览器会自动清理，但在 Proxy 白名单中拦截定时器可提供更细粒度的超时控制和调用次数统计。需确定拦截粒度。
- 与 navigator.modelContext 的关系：浏览器原生工具注册 API（navigator.modelContext.registerTool）仍在早期预览阶段。本方案的 BrowserToolRegistry 可与 modelContext 并存，Registry 管理供 LLM 编排代码使用的工具，modelContext 管理供浏览器内置 AI 使用的工具。未来可考虑 Registry 到 modelContext 的双向桥接。
- 错误信息脱敏策略：沙箱内抛出的异常可能包含主页面文件路径、组件内部状态等敏感信息，需定义脱敏规则（如移除 URL 路径前缀、替换组件实例 ID 等），避免信息泄露。
