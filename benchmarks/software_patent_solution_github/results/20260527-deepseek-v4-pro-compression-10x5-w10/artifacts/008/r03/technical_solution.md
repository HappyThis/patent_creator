## 技术方案

本方案提出一种浏览器侧隔离执行与工具代理调度系统，使大语言模型（LLM）能够生成编排代码在浏览器端执行，并通过安全协议调用主页面注册的动态工具。方案将现有服务端 codemode 模式（基于 WorkerLoader 的 DynamicWorkerExecutor 与 ToolDispatcher RPC 调度）适配至浏览器环境，在保持相同编程模型的前提下，解决浏览器私有工具无法全部迁移至服务端、生成代码不应污染主页面环境的双重约束。

### 系统架构

系统由四个核心组件构成：（1）主页面工具注册与代理层，负责维护已注册工具的命名空间、输入输出 schema 及实际执行函数，并作为隔离环境与浏览器能力之间的唯一桥梁；（2）隔离执行容器，在浏览器侧提供一个与主页面 JavaScript 上下文完全分离的执行沙箱，LLM 生成的编排代码在此沙箱内运行；（3）工具调度协议层，定义隔离容器与主页面之间的结构化通信协议，涵盖工具发现、调用请求、结果回传、异常传播、日志推送、超时信号与清理指令；（4）生命周期管理器，统一控制每次执行的准备、就绪、执行、超时、异常、结果收集与资源清理全过程。

### 浏览器侧隔离执行环境

隔离执行容器采用浏览器原生沙箱 iframe（sandbox 属性设置为 allow-scripts，不设置 allow-same-origin、allow-top-navigation、allow-popups），加载由系统生成的引导模块（bootstrap harness）。引导模块在沙箱内构造一个受限的 JavaScript 执行上下文，该上下文不具备 document、window.parent、fetch 等主页面对象引用。引导模块内部实现方案如下：

（1）针对每个工具提供者（ToolProvider），引导模块基于 Proxy 对象动态构造命名空间代理。代理截获对 `namespace.toolName(args)` 的调用，将其序列化为工具调度协议消息发送至主页面，而不是直接执行任何浏览器 API。（2）引导模块重定向 console.log / console.warn / console.error，将日志输出收集到内部缓冲区，在执行结束或异常时随结果一并回传，使 LLM 和开发者可获得沙箱内代码的运行时诊断信息。（3）引导模块将用户代码包装在 `Promise.race` 中与超时定时器竞争，超时后主动终止执行并向主页面发送 timeout 消息。超时值可配置，默认 30 秒，与现有 DynamicWorkerExecutor 的默认超时一致。

### 工具调度协议

隔离容器与主页面之间的通信基于一个结构化消息协议，不直接暴露 postMessage 原始信道给生成的代码。协议消息类型包括：（1）ready：隔离容器完成初始化、代理就绪后发送，携带本次执行标识（executionId）；（2）call：代理截获工具调用请求后发送，消息体包含 namespace、toolName、callId、序列化的调用参数 JSON；（3）result：主页面执行工具后回传，携带 callId 与序列化返回值 JSON；（4）error：工具执行异常时回传，携带 callId、错误消息字符串与可选的错误堆栈；（5）log：引导模块周期性或批量推送日志条目；（6）timeout：超时触发时由隔离容器发送，携带 executionId，触发主页面取消所有进行中的工具调用；（7）cleanup：主页面或隔离容器在任意终止路径上发送，用于触发资源释放。

协议在实现层采用 MessageChannel 的双向通信模型。隔离容器侧持有 MessagePort 的一端，通过该端口发送 call/log/timeout/ready 消息并监听 result/error/cleanup；主页面侧持有另一端，监听并分发消息到对应工具的实际执行函数。主页面维护一个 callId → pending Promise 的映射表，支持并发工具调用。当 timeout 或 cleanup 消息到达时，主页面遍历进行中的调用并拒绝其 Promise，避免悬挂调用。协议消息采用结构化 JSON 格式，字段包括 msgType、executionId、callId、namespace、toolName、payload、errorMessage、errorStack、timestamp 等，拒绝非预期消息类型并记录安全审计日志。

### 工具命名空间与规范化

系统引入工具命名空间与来源标识机制，解决多个工具提供者（页面自身、服务端 MCP 桥接、第三方插件等）共存时的命名冲突与歧义问题。每个工具提供者声明一个 namespace（如 "page"、"remote"、"pluginA"），该 namespace 必须是合法 JavaScript 标识符。工具原始名称（可能包含连字符、点号、空格等）通过规范化函数转换为合法标识符：以连字符/点号/空格替换为下划线，去除其余非法字符，数字开头时前缀下划线，JavaScript 保留字后缀下划线。此规范化规则与 @cloudflare/codemode 包中 sanitizeToolName 函数保持一致，确保 LLM 在编写代码时使用的标识符在隔离容器中有效可调用。

当两个工具提供者声明了相同的 namespace 时，系统在注册阶段检测冲突并拒绝后注册者，同时向开发者发出警告。当同一 namespace 内出现规范化后重名的工具时（例如 "get-data" 和 "get.data" 均规范化为 "get_data"），系统在注册阶段抛出命名冲突错误，并提示冲突的原始名称列表。工具描述与类型生成沿用现有 ToolProvider 模式：根据每个工具的 inputSchema（JSON Schema 格式或 Zod schema）和可选的 outputSchema，生成 LLM 可消费的 TypeScript 类型声明块（declare const namespace: { toolName: (input: TypeInput) => Promise<TypeOutput>; }），嵌入到 code 工具的 description 中，使 LLM 在生成代码时获得精确的类型提示。

### 安全控制机制

安全控制分为三个层次。第一层是沙箱边界隔离：隔离 iframe 不设置 allow-same-origin，使其无法访问主页面 origin 下的任何资源；不设置 allow-top-navigation 和 allow-popups，阻止导航劫持；引导模块不向用户代码暴露任何主页面对象引用，生成的代码在形式上不可能编写 `window.document` 或 `parent.postMessage`。第二层是工具代理边界：生成的代码不能直接调用任何浏览器 API 或主页面函数——所有工具调用必须经过命名空间代理→协议消息→主页面工具注册表→实际执行函数这一完整链路。主页面在收到 call 消息后，先验证 namespace 和 toolName 是否在已注册工具集合中，非法调用返回 error 消息而不执行任何函数。

第三层是输入校验与审计：主页面在执行工具函数前，根据工具注册时的 inputSchema 对参数进行校验（JSON Schema 校验），校验失败不执行实际函数而是返回结构化校验错误。所有工具调用——包括调用时间、namespace、toolName、参数摘要、执行耗时、成功/失败状态——均记录到审计日志缓冲区。异常传播遵循以下规则：工具函数抛出异常时，异常消息和堆栈通过 error 协议消息回传至隔离容器，再由容器作为代码执行异常上抛；主页面自身的协议层异常（如 JSON 解析失败、消息格式错误）记录审计日志后返回通用错误，不向隔离容器泄露主页面内部状态细节。

### 执行生命周期管理

每次代码执行遵循确定的生命周期状态机，包含以下阶段：（1）准备阶段：主页面创建沙箱 iframe 并注入引导模块源码，同时收集当前已注册的工具提供者列表，为每个提供者构造协议代理所需的元数据（namespace、工具名列表、inputSchema）。（2）就绪阶段：引导模块完成初始化后发送 ready 消息，携带 executionId。主页面收到 ready 后记录执行开始时间，将用户代码通过协议发送至隔离容器。（3）执行阶段：隔离容器运行代码，工具调用通过协议往返主页面。主页面并发处理多个 call 消息，每个 call 分配独立的 callId。（4）完成/异常阶段：代码正常返回时，结果与日志一并回传；代码抛出异常时，异常消息和日志回传；超时时，隔离容器终止执行并发送 timeout 消息。

（5）清理阶段：无论执行以何种路径终止，清理阶段确保沙箱 iframe 从 DOM 中移除（包括其 JavaScript 上下文和 MessageChannel 端口），主页面侧终止所有未完成的工具调用 Promise，重置 callId 映射表。清理可由正常完成、异常、超时或外部取消（如用户主动中断）触发。清理后，executionId 被标记为已终止，后续任何携带该 executionId 的消息均被忽略（防止竞争条件）。清理函数设计为幂等——多次调用安全，不会重复释放资源。（6）工具调用超时：除代码级超时外，单个工具调用也设有可配置的超时（默认 10 秒），若工具函数在超时内未返回，主页面自动以超时错误响应对应的 callId，不阻塞其他并发工具调用。

### 与现有体系协作

本方案设计为与现有 @cloudflare/codemode 体系、AIChatAgent 框架及 client tool 机制直接协作。具体连接点如下：（1）与 codemode 的 Executor 接口兼容：浏览器侧隔离执行器实现与 DynamicWorkerExecutor 相同的 Executor 接口（execute(code, providers) → ExecuteResult），因此现有 createCodeTool、aiTools、resolveProvider 等工厂函数无需修改即可同时支持服务端 WorkerLoader 执行和浏览器沙箱执行。（2）与 AIChatAgent / clientTools 模式协作：主页面通过现有的 clientTools 通道将工具的 JSON Schema 发送至服务端 Agent，服务端 Agent 将这些工具包装为 codemode ToolProvider 暴露给 LLM。LLM 生成的代码由浏览器侧执行器运行，工具调用在主页面本地执行，无需服务端中转工具调用。

（3）与 WebMCP / navigator.modelContext 模式共存：页面本地工具（直接注册到 navigator.modelContext 供浏览器内置 AI 使用）和本方案的隔离执行工具代理可以共存。开发者可在同一页面中同时使用 registerWebMcp（将服务端 MCP 工具桥接到浏览器 AI）和 browserCodeExecutor（让 LLM 生成的代码编排浏览器工具），两者的工具注册表独立管理，互不干扰。（4）与动态工具注册的协同：本方案支持运行时动态增删工具提供者。主页面工具注册表在工具集合变化时，向隔离容器发送 tool-changed 通知，容器侧更新对应的 Proxy 代理，确保后续 LLM 代码执行时使用的工具描述始终反映最新可用工具集。（5）依赖最小化：方案仅依赖浏览器原生能力（sandbox iframe、MessageChannel、Proxy、Promise），不引入第三方沙箱库或跨域通信框架。

### 风险与待确认问题

以下是当前方案的风险点与待确认问题：（1）沙箱 iframe 的同源限制：不设置 allow-same-origin 意味着隔离容器运行在跨域上下文中，可能影响某些浏览器 API 的可用性（例如某些需要同源的 Web API）。需确认目标工具的使用场景是否依赖同源。（2）iframe 创建开销：每次执行新建 iframe 并注入引导模块存在冷启动延迟（约 10-50ms），高频执行场景可能需要引入 iframe 池化复用机制。复用需确保前次执行的全局状态被完全重置。（3）MessageChannel 传输限制：结构化克隆算法不支持传输函数、Symbol、Error 对象等，工具返回值的序列化需显式处理循环引用和特殊类型。建议限制工具返回值为 JSON 可序列化类型。（4）跨域消息安全：需实现消息来源校验，拒绝非本页面创建的 MessagePort 消息。方案中 executionId 机制可提供基础的来源鉴别。（5）并发执行隔离：同一页面可能同时存在多个隔离执行实例（如多个 Agent 并行操作），每个实例需独立管理 iframe 和 MessageChannel，并避免工具调用冲突。
