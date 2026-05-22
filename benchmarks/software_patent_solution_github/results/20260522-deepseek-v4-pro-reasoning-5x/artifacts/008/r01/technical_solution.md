## 技术方案

本技术方案提出一种浏览器侧隔离代码执行系统，使大语言模型（LLM）能够生成编排代码在浏览器中执行，调用浏览器页面提供的动态工具，同时保持与主页面的安全隔离。方案在已有的服务端 Code Mode 执行体系（基于 Worker 隔离执行与 ToolDispatcher RPC 调度）基础上，向浏览器端延伸，引入浏览器侧沙箱执行环境、工具注册与命名空间管理、安全调度协议和完整生命周期控制。

### 整体架构

系统由主页面侧的「工具注册中心」与「工具代理执行器」、iframe 中的「隔离沙箱执行环境」、以及连接二者的「工具调度协议」三部分组成。LLM 生成的编排代码在 iframe 沙箱中执行，通过命名空间代理对象调用工具；每次工具调用经由 MessageChannel 传递至主页面侧的代理执行器，由代理执行器校验工具合法性后在主页面上下文执行已注册工具，并将结果按相同通道回传。

主页面开发者预先将浏览器侧工具注册到工具注册中心，每个工具携带命名空间标识、工具名称、输入输出 schema 和执行函数。工具注册中心同时负责生成可供 LLM 使用的 TypeScript 类型声明，使 LLM 能理解可用工具的类型签名。沙箱执行环境加载 LLM 生成的代码后，通过注入的命名空间代理将工具调用转换为结构化 RPC 请求，经 MessageChannel 发送至主页面。

### 浏览器侧工具注册与命名空间管理

工具注册中心维护一个动态工具注册表。每个已注册工具包含以下元数据：命名空间标识（namespace），用于区分工具来源（如 "page" 表示页面级工具、"extension" 表示浏览器扩展工具、自定义前缀表示第三方注入的工具）；工具原始名称；工具描述文本；输入 JSON Schema；输出 JSON Schema；以及工具执行函数引用。所有工具按命名空间分组管理，同一命名空间内工具名必须唯一。

工具名规范化机制处理以下问题：（1）原始工具名中可能包含连字符、点号、空格等非 JavaScript 标识符字符，系统将其替换为下划线，并去除其余非法字符；（2）若规范化后名称为 JavaScript 保留字，追加下划线后缀；（3）若规范化后名称以数字开头，添加下划线前缀。规范化后的名称用作沙箱中代理对象的属性名。

命名空间冲突处理遵循以下规则：（1）不同命名空间的工具互相隔离，即使规范化后名称相同也不会冲突——LLM 代码通过不同命名空间代理对象调用，如 page.getSelection() 与 extension.getSelection() 分别访问页面工具和扩展工具；（2）同一命名空间内，后注册的同名工具覆盖先注册的工具，并输出警告日志；（3）保留命名空间列表（如 "__system"、"__dispatchers"、"__logs"）禁止开发者使用，注册时直接拒绝。每个工具额外记录来源标识（source），用于调试和审计日志中追踪工具注册来源。

### 隔离沙箱执行环境

隔离沙箱执行环境通过创建具有严格 sandbox 属性的 iframe 实现。iframe 的 sandbox 属性设置为 "allow-scripts"，仅允许脚本执行，故意不设置 allow-same-origin、allow-top-navigation、allow-popups 等属性，从浏览器层面阻断沙箱代码访问主页面 DOM、Cookie、localStorage 及其他同源资源的能力。

沙箱 iframe 加载一个预编译的执行器页面（executor page），该页面内置代码执行运行时。运行时提供以下能力：（1）接收并执行父页面传入的 LLM 生成代码，代码被包装为异步箭头函数执行；（2）为每个已注册命名空间注入 Proxy 代理对象，代理对象的 get 陷阱拦截属性访问，将 toolName 和调用参数序列化为结构化 RPC 请求，通过 MessageChannel 发送至主页面；（3）拦截 console.log、console.warn、console.error 调用，将日志输出收集到日志缓冲区，随执行结果一并返回；（4）设置全局执行超时定时器，超时后通过 Promise.race 机制使执行 Promise 拒绝并返回超时错误。

与已有的服务端 DynamicWorkerExecutor 不同，浏览器侧沙箱不依赖 WorkerLoader 或 Cloudflare Workers 运行时。它利用浏览器原生 iframe sandbox 属性和 MessageChannel 实现进程级隔离，无需额外服务器资源。代码执行在 iframe 的独立 JavaScript 上下文中进行，与主页面共享同一个浏览器进程但遵循同源策略的隔离约束。

### 工具调度协议

工具调度协议定义了沙箱执行环境与主页面之间的结构化通信格式。采用 MessageChannel 双工通道，沙箱侧持有 port2，主页面侧持有 port1。所有工具调用遵循请求-响应模式。

请求消息格式为：{ id: string, namespace: string, toolName: string, args: unknown }。其中 id 为单调递增的唯一请求标识（UUID），namespace 为目标命名空间，toolName 为规范化后的工具名称，args 为序列化后的调用参数（对象或数组）。响应消息格式为：{ id: string, result?: unknown, error?: { message: string, code: string } }。id 与原请求 id 对应，成功时返回 result，失败时返回 error 对象，error.code 区分错误类型："TOOL_NOT_FOUND"（工具不存在）、"TOOL_EXECUTION_ERROR"（工具执行异常）、"TIMEOUT"（工具调用超时）、"INVALID_ARGS"（参数校验失败）。

主页面侧的代理执行器接收请求后按以下流程处理：（1）校验 namespace + toolName 是否存在于工具注册中心；（2）若存在，用工具注册的输入 JSON Schema 校验 args 参数；（3）调用工具注册时提供的执行函数，传入校验后的参数；（4）捕获执行过程中的异常，包装为 error 响应；（5）将结果或错误通过 port1 发送回沙箱。每个工具调用的代理执行均在主页面上下文中进行，例如调用 page.getSelection() 时，实际调用 window.getSelection() 的是主页面的执行函数，而非沙箱代码。

协议同时支持以下控制消息：{ type: "ready" }（沙箱就绪信号，表示执行器已准备好接收代码）；{ type: "abort" }（主页面主动中止执行，沙箱收到后清理并停止）；{ type: "log", entries: string[] }（沙箱批量推送日志到主页面）。每轮执行结束后，主页面关闭当前 MessageChannel 并清理相关资源。

### 安全控制机制

安全控制采用纵深防御策略，在多个层面阻止 LLM 生成代码直接访问或污染主页面对象。

第一层——浏览器沙箱属性：iframe 的 sandbox="allow-scripts" 从浏览器层面阻止沙箱代码访问父页面 DOM（缺少 allow-same-origin 导致无法通过 parent.document 访问）、阻止导航（缺少 allow-top-navigation）、阻止弹窗（缺少 allow-popups）、阻止表单提交（缺少 allow-forms）。沙箱代码的全局对象与主页面完全不同，无法通过原型链或闭包泄漏访问主页面对象。

第二层——通信通道白名单：沙箱与主页面之间仅存在一个 MessageChannel 通道，所有跨边界通信必须经过此通道。沙箱代码无法创建额外的通信通道（如 WebSocket、fetch、Worker），因为这些 API 在 sandbox iframe 中被浏览器限制或由执行器运行时显式屏蔽。主页面侧在接收消息时校验消息来源（event.source === sandboxIframe.contentWindow），拒绝非预期来源的消息。

第三层——代理执行边界：所有已注册工具的实际执行函数始终在主页面上下文中运行，而不是在沙箱中运行。沙箱代码只能通过命名空间代理发起工具调用请求，请求经序列化-反序列化后到达主页面，主页面代理执行器在校验工具名、参数 schema 后才调用真实的执行函数。沙箱代码无法访问执行函数的闭包、无法修改执行函数的行为、无法获取主页面作用域中的变量引用。即使沙箱代码尝试构造恶意参数，JSON Schema 校验层也会拦截不符合预期结构的输入。

第四层——工具注册白名单：主页面仅暴露已显式注册的工具。未注册的工具名在代理执行器校验阶段被拒绝，返回 TOOL_NOT_FOUND 错误。不存在「万能调用」或反射型工具调用机制。注册工具时可以设置只读标记（readOnlyHint），代理执行器在日志中标注只读工具调用以便审计。

### 执行生命周期管理

每次代码执行具有明确定义的生命周期阶段：准备（prepare）、就绪（ready）、执行（execute）、结果回传（result）、错误处理（error）、超时处理（timeout）、清理（cleanup）。

准备阶段：主页面创建新的 sandbox iframe 和 MessageChannel，将 port2 通过 iframe 的 postMessage 传递至执行器页面。执行器页面完成初始化后发送 { type: "ready" } 信号。若 iframe 加载超时（默认 10 秒），进入错误处理流程。

执行阶段：主页面将 LLM 生成的代码字符串、执行超时时间、可用命名空间列表通过 MessageChannel 发送至执行器。执行器将代码包装为标准异步箭头函数执行体，注入命名空间代理对象，启动超时定时器，通过 Promise.race 同时等待执行结果和超时信号。执行过程中的日志由被拦截的 console 方法收集。

结果回传阶段：代码正常执行完毕后，执行器将 { result, logs } 通过 MessageChannel 发送回主页面。主页面代理执行器将结果返回给上层调用方（如 agent chat 工具调用链）。

错误处理阶段：区分三种错误来源——（1）代码执行异常（编译错误或运行时异常），捕获后返回 { error: { message, code: "EXECUTION_ERROR" }, logs }；（2）工具调用异常（代理执行器中工具执行函数抛出异常），返回 { error: { message, code: "TOOL_EXECUTION_ERROR" } } 给沙箱侧，沙箱代码可捕获该错误或让其向上传播；（3）非法工具调用（工具不存在或参数校验失败），代理执行器直接返回错误响应，不执行任何工具逻辑。所有错误均携带结构化错误码，便于上层系统区分和处理。

超时处理阶段：执行超时分为两个层级——（1）全局执行超时：从代码开始执行计时，超时后通过 Promise.race 拒绝执行 Promise，返回 { error: { code: "EXECUTION_TIMEOUT" }, logs }；（2）单次工具调用超时：代理执行器为每个工具调用设置独立超时（默认 30 秒），超时后返回 { error: { code: "TOOL_CALL_TIMEOUT" } }。两级超时可独立配置。

清理阶段：无论执行成功、失败还是超时，均执行以下清理动作——（1）关闭 MessageChannel 两端端口；（2）从 DOM 中移除 sandbox iframe；（3）清理代理执行器中的临时状态；（4）若设置了 onCleanup 回调则触发通知。清理操作具有幂等性，多次调用不会产生副作用。

### 工具描述与类型生成

工具描述与类型生成模块从工具注册中心读取已注册工具的元数据（名称、描述、输入 JSON Schema、输出 JSON Schema），自动生成供 LLM 使用的 TypeScript 类型声明和工具描述文本。生成的类型声明遵循每个命名空间一个 declare namespace 块的格式输出。

类型生成过程：（1）遍历工具注册中心中的所有命名空间和工具；（2）对每个工具的输入 JSON Schema，通过 JSON Schema 到 TypeScript 类型转换器生成输入类型定义；对输出 JSON Schema 同样生成输出类型；（3）将工具名规范化为合法的 TypeScript 函数名；（4）生成如下形式的类型声明——每个命名空间输出为 declare namespace { toolName(args: InputType): Promise<OutputType> } 的集合。转换器支持 JSON Schema 的基本类型、对象、数组、联合类型、枚举、可选属性等构造，对不支持的高级构造降级为 unknown 类型并附加注释。

工具描述文本以结构化方式呈现给 LLM，包含：命名空间名称、每个工具的规范化名称、原始名称（若有差异）、人类可读描述、参数类型摘要。当工具注册中心的工具集合因动态注册或注销发生变化时，类型生成模块重新生成声明并通知上层，使 LLM 在下一轮对话中使用最新的工具视图。这一机制与已有 codemode 的 ToolProvider 类型生成（generateTypes 函数）保持一致的输出格式，确保 LLM 提示词可复用。

### 与现有体系集成

本方案设计为与项目已有的 codemode / agent chat / client tool 体系协同工作，而非替代。

与 Code Mode 体系的集成：浏览器侧沙箱执行器实现与已有 Executor 接口兼容的执行契约——接收代码字符串与工具提供者列表，返回 ExecuteResult（含 result、error、logs）。这使得上层 createCodeTool 可以在服务端 DynamicWorkerExecutor 和浏览器侧沙箱执行器之间透明切换：当工具提供者包含浏览器侧工具时，自动路由到浏览器沙箱；当工具提供者全部为服务端工具时，使用已有的 Worker 隔离执行。这种路由对 LLM 透明，LLM 始终看到统一的工具视图和一致的代码编写模式。

与 Agent Chat 体系的集成：在 agent chat 的工具调用流程中，浏览器侧工具通过 agentTool 机制注册为可调用的子 agent 工具。当 LLM 决定调用浏览器侧工具时，工具调用请求经 agent chat 的消息管道传递至浏览器侧代理执行器，代理执行器创建沙箱、执行代码、收集结果，将最终结果通过 agent chat 的流式响应通道返回。浏览器侧工具支持 streaming 模式——执行过程中的日志可以实时推送到 agent chat 的消息流中。

与 Client Tool 体系的集成：已有的客户端工具（client tools）机制允许页面侧注册工具供 agent 调用。本方案与之互补：client tools 提供的是面向 agent 的逐个工具调用模式（一次调用一个工具），而浏览器侧 codemode 提供的是面向 LLM 的批量编排模式（一次生成代码批量调用多个工具并组合结果）。两者共享同一个工具注册中心，开发者一次注册即可同时支持两种调用模式。与 WebMCP / navigator.modelContext 的集成：通过统一的工具注册中心，已注册的浏览器侧工具可以同时暴露给 navigator.modelContext（供浏览器内置 AI 使用）和沙箱执行环境（供 LLM 生成代码使用），实现「一次注册，多处可用」。

### 风险与待确认问题

以下为当前设计中需要后续确认和验证的风险点：

（1）iframe sandbox 兼容性：sandbox iframe 中某些浏览器 API 的行为可能因浏览器而异（如 MessageChannel 在 sandbox iframe 中的序列化能力、console 方法拦截的可靠性）。需要在 Chrome、Firefox、Safari 等主流浏览器中进行兼容性验证。（2）大数据序列化性能：工具调用参数和返回值通过 MessageChannel 的结构化克隆算法传递，对于大体积数据（如截图的 base64 编码、大量 DOM 节点数据），序列化性能可能成为瓶颈。可考虑引入分块传输或引用传递机制。（3）并发沙箱管理：同时创建多个沙箱执行环境时的浏览器资源消耗（内存、DOM 节点数）需要评估上限。建议实现沙箱池化复用机制，而非每次执行创建/销毁 iframe。（4）跨域场景：当主页面与 iframe 执行器页面不同源时，sandbox 属性的行为和安全模型可能不同。需要明确执行器页面的部署策略（同源托管 vs CDN 托管）。（5）与 navigator.modelContext 的互操作：当浏览器侧工具同时通过 WebMCP 暴露给 navigator.modelContext 和通过沙箱执行环境暴露给 LLM 生成代码时，两个通道的工具状态同步需要协调机制，避免状态不一致。（6）调试与可观测性：沙箱内代码的执行过程对开发者不透明，需要在执行器中内置调试信息收集机制（如执行轨迹、工具调用时序），并通过日志通道回传至主页面。
