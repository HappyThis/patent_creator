## 技术方案

本方案在现有服务端 codemode 能力的基础上，提出一种浏览器侧隔离代码执行系统。该系统使 LLM 能够在浏览器页面中生成并执行编排代码，安全地调用浏览器页面提供的动态工具，同时保持与页面主环境的隔离，避免生成代码直接访问或污染主页面对象。方案包括浏览器侧隔离执行环境、工具调度协议、安全控制机制、生命周期管理和与现有 codemode / agent chat / client tool 体系的协作方式。

### 整体架构

系统由四个核心部分构成：（1）主页面工具注册与代理层，负责收集浏览器侧可用工具、生成工具描述并代理实际工具执行；（2）隔离执行沙箱，承载 LLM 生成的编排代码，与主页面通过结构化消息通信；（3）工具调度协议，定义沙箱与主页面之间的请求-响应格式，支持工具发现、调用、结果返回和异常传播；（4）生命周期管理器，控制沙箱从创建、就绪、执行到销毁的完整过程。

### 浏览器侧隔离执行环境

隔离执行沙箱基于浏览器原生安全机制构建，不使用单一 iframe + postMessage 的简单方案。沙箱由三层隔离组成，每层承担不同职责。

第一层：iframe 沙箱容器。使用 sandbox 属性设置为 "allow-scripts"，显式禁止 allow-same-origin、allow-top-navigation、allow-popups、allow-forms。该 iframe 的 src 通过 Blob URL 动态生成，包含执行引导代码（bootstrap），不与任何服务端来源同源，从而阻断对主页面 DOM、Cookie、localStorage 和 sessionStorage 的直接访问。Blob URL 在沙箱销毁时通过 URL.revokeObjectURL() 释放。

第二层：执行上下文隔离。引导代码在 iframe 内部创建独立的 JavaScript 执行上下文。对于 LLM 生成的编排代码，采用函数作用域封装执行：将代码包装在异步函数体内，通过间接 eval 或 Function 构造函数在当前 iframe 的全局作用域中执行，但不暴露主页面任何对象引用。编排代码可见的全局对象仅限于沙箱引导代码显式注入的工具代理对象和标准 ECMAScript 内置对象。

第三层：工具代理层。编排代码不直接调用任何浏览器 API 或页面对象，而是通过注入的工具代理对象（如 codemode.getSelection()、page.scrollToSection() 等）发起调用。每个工具代理对象拦截属性访问和方法调用，将其序列化为工具调度协议消息，通过 postMessage 发送到主页面。主页面中的工具执行代理负责解析消息、执行对应的已注册工具、并将结果序列化回传。编排代码无法区分工具是页面本地工具还是远程桥接工具。

### 工具调度协议

工具调度协议是沙箱与主页面之间唯一允许的消息格式，定义在结构化消息通道上。所有消息通过 iframe 的 postMessage 传递，使用结构化克隆算法进行序列化。

消息类型包括：TOOL_CALL（沙箱→主页面，发起工具调用请求）、TOOL_RESULT（主页面→沙箱，返回工具执行结果）、TOOL_ERROR（主页面→沙箱，返回工具执行异常）、SANDBOX_READY（沙箱→主页面，沙箱就绪信号）、SANDBOX_ERROR（沙箱→主页面，沙箱执行异常）、以及 LIFE_CYCLE 系列（超时通知、清理确认等）。

每条 TOOL_CALL 消息包含：callId（全局唯一调用标识，使用 crypto.randomUUID()）、namespace（工具命名空间，对应 ToolProvider.name）、toolName（经过规范化的工具名，与 codemode 现有 sanitizeToolName 逻辑一致）、arguments（JSON 序列化的调用参数，经结构化克隆后传递）。主页面收到后，根据 namespace + toolName 查找已注册工具，校验参数符合工具的 inputSchema 定义后执行。TOOL_RESULT 包含 callId、result（执行结果，可序列化为 JSON 的值）和 executionTime（执行耗时毫秒数）。TOOL_ERROR 包含 callId、errorMessage、errorStack（可选）和 errorCode（标准化错误码，如 TOOL_NOT_FOUND / INVALID_ARGS / EXECUTION_FAILED / TIMEOUT）。

### 安全控制机制

安全控制基于纵深防御原则，在沙箱层、协议层和代理层分别施加约束。

沙箱层安全：（1）iframe sandbox 属性仅启用 allow-scripts，禁止 allow-same-origin 确保 Blob URL 不与主页面同源，禁止 allow-top-navigation 和 allow-popups 防止导航劫持；（2）Blob URL 一次性使用，销毁时撤销；（3）编排代码的全局作用域不包含 document、window.parent、window.top、window.opener 等页面对象引用；（4）沙箱内不注入任何网络请求能力（无 fetch、无 XMLHttpRequest、无 WebSocket），编排代码只能通过工具代理对象与外界交互。

协议层安全：（1）消息来源验证：主页面收到 postMessage 时校验 event.source === iframe.contentWindow 且 event.origin === "null"（Blob URL 的 origin 为 "null"）；（2）消息格式校验：每一条 TOOL_CALL 必须包含必填字段，callId 为合法 UUID 格式，namespace 和 toolName 仅允许白名单字符集（字母、数字、下划线、连字符、点号），arguments 必须是可解析的 JSON；（3）消息通道单向能力限制：沙箱只能发出 TOOL_CALL，主页面只能发出 TOOL_RESULT / TOOL_ERROR，不允许跨类型消息注入。

代理层安全：（1）主页面工具注册表是唯一可调用入口：沙箱只能调用已在主页面注册表中注册的工具，未注册工具名返回 TOOL_NOT_FOUND 错误；（2）参数校验：每个工具定义包含 inputSchema（JSON Schema 格式），主页面在执行前使用 schema 校验器验证参数，不符合 schema 的参数返回 INVALID_ARGS 错误并拒绝执行；（3）执行隔离：工具的实际执行在主页面上下文中进行，编排代码不感知工具的内部实现细节，无法通过返回值窃取主页面对象引用——所有返回值经过结构化克隆序列化；（4）防御性超时：每个工具调用有独立超时限制（默认 30 秒），超时后中断工具执行并返回 TIMEOUT 错误。

### 生命周期管理

每次代码执行具有明确的生命周期状态机，包含以下阶段：

PREPARING：创建 iframe 元素、生成 Blob URL（包含引导代码和 LLM 生成代码）、设置 sandbox 属性、注入工具代理定义。此阶段不向 LLM 返回就绪信号。READY：引导代码完成初始化，工具代理对象已挂载到沙箱全局作用域，通过 SANDBOX_READY 消息通知主页面。主页面收到后开始计时。EXECUTING：编排代码在沙箱中运行，通过工具代理对象发起 TOOL_CALL。主页面逐条处理工具调用，将 TOOL_RESULT 或 TOOL_ERROR 回传沙箱。代码执行完成后，沙箱将最终返回值（或 undefined）通过 SANDBOX_COMPLETE 消息发送给主页面。ERROR：代码执行过程中发生未捕获异常，沙箱捕获异常并通过 SANDBOX_ERROR 消息发送给主页面，包含 error.message 和 error.stack。TIMEOUT：从 READY 开始计时，超过配置的超时时间（默认 30 秒）后，主页面主动销毁沙箱并返回超时错误。若某次工具调用也超时，先返回 TOOL_ERROR(TIMEOUT) 给编排代码，由编排代码决定如何处理。CLEANUP：无论何种原因结束，主页面执行清理流程：移除 iframe 元素、释放 Blob URL（URL.revokeObjectURL）、清空工具代理引用、记录执行日志。清理后主页面释放所有相关资源。

执行日志收集：编排代码中的 console.log / console.warn / console.error 调用被沙箱引导代码拦截，输出到内部日志缓冲区。在 SANDBOX_COMPLETE 或 SANDBOX_ERROR 消息中附带完整日志数组，供调试和审计使用。日志不写入主页面控制台，仅通过消息通道传递。

### 动态工具的命名空间与工具名规范化

浏览器侧工具来源多样：页面注册的本地工具（如读取选择内容、操作 DOM）、通过 WebMCP 桥接的远程 MCP 工具、以及 codemode 内置工具。每个工具来源分配独立的命名空间标识，在注册时由 ToolProvider 的 name 字段指定。命名空间使用点号分隔的层次化命名规则，例如："codemode"（内置工具）、"page"（页面本地工具）、"remote" 或 "mcp.serverName"（远程桥接工具）。工具发现时，系统收集所有已注册 ToolProvider 的工具列表，为每个工具生成规范化的全局名称（namespace.toolName）。

工具名规范化：遵循与 codemode 现有 sanitizeToolName 相同的规则——将连字符、点号和空格替换为下划线，去除其他非法标识符字符，数字开头的前缀下划线，JavaScript 保留字后缀下划线。工具名冲突处理：若同一 namespace 内出现同名工具（如两个 Provider 注册了相同的 toolName），后注册者覆盖前者，并输出警告日志。不同 namespace 之间的工具名允许重复，因为调用时需携带 namespace 前缀。命名空间名本身也经过 sanitizeToolName 处理，确保作为 JavaScript 标识符合法。

### 工具描述与输入输出 Schema 生成

浏览器侧工具的执行函数（execute）存在于主页面中，但工具的元数据需要传递给 LLM 用于生成正确的编排代码。系统为每个已注册工具生成以下描述信息：（1）工具名（规范化后的 namespace.toolName）；（2）工具描述文本（来自工具定义的 description 字段）；（3）输入参数类型定义（从工具的 inputSchema / parameters 生成 TypeScript 类型声明）；（4）输出类型定义（从工具的 outputSchema 生成，若无则标记为 unknown）。

Schema 生成利用 codemode 现有的 generateTypes 和 jsonSchemaToTypeString 能力：支持 Zod schema、Standard Schema 协议对象和 AI SDK jsonSchema 包装器三种输入格式。生成结果合并为一段 TypeScript 类型声明块，注入到 LLM 的 codemode 工具描述中。LLM 根据这些类型信息生成调用正确工具名的代码，参数格式与 schema 定义一致。在浏览器场景中，工具的 inputSchema 可能来自页面注册时提供的 JSON Schema 对象，也可能来自远程 MCP 服务器返回的工具描述。系统在注册阶段统一归一化为内部 ToolDescriptor 格式。

### 主页面工具执行代理边界

主页面中的工具执行代理（HostToolProxy）是隔离沙箱与页面工具之间的唯一桥梁。其边界设计如下：

工具注册边界：页面通过 registerTool({ name, description, inputSchema, execute }) 接口注册工具。注册时系统校验 name 唯一性、inputSchema 合法性（必须是有效的 JSON Schema 或 Zod schema），并存储 execute 函数引用。注册表以 Map<namespace, Map<toolName, ToolEntry>> 结构维护，ToolEntry 包含 schema、execute 函数、来源标识（local / remote）和注册时间戳。已注册工具可动态增减（通过 registerTool / unregisterTool），变更后触发工具列表刷新通知。

执行边界：HostToolProxy 收到 TOOL_CALL 后执行以下步骤：（1）根据 namespace 和 toolName 查找 ToolEntry，未找到返回 TOOL_NOT_FOUND；（2）使用 schema 校验器校验 arguments，不符合 schema 返回 INVALID_ARGS 并附带校验错误详情；（3）调用 execute(arguments)，捕获同步和异步异常；（4）将返回值通过结构化克隆序列化后作为 TOOL_RESULT 返回；（5）若 execute 抛出异常，构造 TOOL_ERROR 返回。HostToolProxy 不暴露 execute 函数的内部实现给沙箱，沙箱仅获得序列化后的结果值。

安全性：HostToolProxy 在执行工具时不向沙箱暴露任何主页面对象引用（如 document、window、React state 等），也不允许工具返回值中包含函数、DOM 节点、Symbol 等不可序列化类型。结构化克隆会自动过滤不可克隆类型，若工具尝试返回不可克隆值，代理层捕获异常并返回序列化错误。

### 与现有体系协作

浏览器侧执行方案实现与 codemode 相同的 Executor 接口，因此可以无缝集成到现有 createCodeTool 流程中。具体协作方式如下：

与 codemode 协作：新增 BrowserSandboxExecutor 类实现 Executor 接口，其 execute(code, providers) 方法创建浏览器沙箱、注入工具代理、执行代码并返回 ExecuteResult。createCodeTool 接受 BrowserSandboxExecutor 作为 executor 参数，与现有的 DynamicWorkerExecutor 使用方式一致。ToolProvider 的 namespace 机制在浏览器侧同样适用——页面本地工具使用 "page" 命名空间，远程 MCP 工具使用 "mcp" 或自定义命名空间，内置工具使用 "codemode" 命名空间。

与 agent chat 协作：在 AIChatAgent 的 onChatMessage 中创建 BrowserSandboxExecutor 实例，将其传递给 createCodeTool。当 LLM 决定使用 codemode 工具时，生成的代码在浏览器沙箱中执行，工具调用结果返回给 agent 继续流式生成。由于 agent 在服务端而沙箱在浏览器侧，需要 agent 通过 WebSocket 将代码发送到浏览器、浏览器执行后通过 WebSocket 返回结果，或采用混合模式（服务端工具仍在服务端执行，浏览器工具在浏览器侧执行）。

与 client tool 体系协作：浏览器侧 codemode 中的工具可以复用 client tool 的注册和发现机制。页面通过 ClientToolSchema 格式声明工具（name、description、parameters），系统自动生成对应的输入输出 schema 和 TypeScript 类型声明。同时，已注册的 client tool 也可作为浏览器沙箱中的可调用工具——沙箱中的编排代码调用 toolName(args) 时，调用被路由到对应的 client tool execute 函数。

### 技术效果

本方案实现以下技术效果：

（1）安全隔离：通过三层隔离架构（iframe sandbox + 执行上下文封装 + 工具代理），LLM 生成的编排代码无法直接访问主页面 DOM、Cookie、存储或全局对象，所有页面交互必须通过注册的工具代理进行。（2）细粒度工具控制：主页面精确控制暴露给 LLM 的工具集，每个工具可独立注册和注销，参数经过 schema 校验，执行在主页面侧进行。（3）统一的编程模型：浏览器侧执行器实现与 codemode 兼容的 Executor 接口，开发者使用相同的 createCodeTool API，无需区分代码将在服务端还是浏览器侧执行。（4）多来源工具编排：LLM 可以在同一段编排代码中组合调用页面本地工具、远程 MCP 桥接工具和内置工具，工具来源对 LLM 透明。（5）完善的异常处理：超时、工具未找到、参数非法、执行异常等错误通过标准化错误码传播，编排代码可捕获和处理这些错误。（6）最小额外依赖：方案基于浏览器原生 API（iframe、postMessage、Blob URL），无第三方运行时依赖。

### 风险与待确认问题

以下问题需要在进一步实施中确认：（1）跨域 iframe 场景：若主页面与 agent 服务端不同源，sandbox iframe 的 Blob URL（origin 为 "null"）与主页面通信是否受浏览器跨域策略影响，需在目标浏览器版本中验证；（2）性能：每次执行创建新的 iframe + Blob URL 存在固定开销，对于高频小任务场景可能需要引入沙箱复用池机制；（3）Service Worker 干扰：主页面注册的 Service Worker 可能拦截 Blob URL 请求或影响 iframe 加载，需要在实际应用中测试兼容性；（4）iOS / Safari 兼容性：Safari 对 iframe sandbox 属性的支持程度与 Blob URL 行为的差异需要专项测试；（5）与 CSP 的交互：主页面 Content-Security-Policy 中 script-src 和 frame-src 指令可能阻止 Blob URL iframe 的创建和脚本执行；（6）工具执行顺序保证：当前方案中沙箱内编排代码的多个 TOOL_CALL 是按 JavaScript 异步调用顺序发出的，主页面是否保证按序执行取决于工具自身的同步/异步特性；（7）不可序列化返回值的降级策略：结构化克隆无法处理函数、Symbol 等类型，需要明确降级为 null 还是抛出异常。
