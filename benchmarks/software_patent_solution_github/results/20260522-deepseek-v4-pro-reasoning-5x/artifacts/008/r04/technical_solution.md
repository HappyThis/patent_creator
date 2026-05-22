## 技术方案

本方案在现有服务端 codemode 能力的基础上，扩展出浏览器侧代码执行模式（Browser Codemode），使 LLM 生成的编排代码可在浏览器页面的隔离沙箱中执行，并通过安全协议调用页面注册的动态工具。方案围绕六个核心模块展开：隔离执行环境、动态工具注册与命名空间管理、工具调度协议、安全控制、生命周期管理、工具描述与模式生成。

### 技术问题与场景约束

现有 codemode 能力（@cloudflare/codemode 包）基于 Cloudflare Workers 的 DynamicWorkerExecutor 实现：LLM 生成 JavaScript 代码，在服务端隔离 Worker 中执行，通过 Workers RPC 的 ToolDispatcher 将工具调用回传至宿主。但该模式无法覆盖浏览器侧工具场景：读取页面选择内容、访问页面状态（如 Zustand/Redux store）、调用前端组件暴露的能力、组合浏览器动态客户端工具等。这些工具存在于浏览器页面中，无法也不应全部搬到服务端。

本方案需解决的核心约束包括：（1）执行环境必须在浏览器侧，以避免将浏览器私有能力搬运到服务端；（2）执行环境必须与主页面隔离，LLM 生成的代码不能直接访问或修改主页面对象；（3）工具调用需要从隔离环境安全传递至主页面，由主页面代理实际执行；（4）需支持根据当前可用工具自动生成 LLM 可理解的工具描述和输入输出结构；（5）每次执行应有完整的生命周期管理；（6）方案应尽量复用现有 codemode、agent chat、client tool 体系。

### 整体架构

方案总体分为三层：浏览器侧隔离沙箱层（Sandbox Layer）、主页面工具代理网关层（Tool Proxy Gateway Layer）、以及服务端编排协调层（Server Orchestration Layer）。沙箱层负责在浏览器中创建与主页面完全隔离的 JavaScript 执行环境，注入经过命名空间规范化后的工具代理对象。工具代理网关层驻留在主页面中，负责接收来自沙箱的工具调用请求、校验工具名与输入模式、代理执行已注册的工具函数、并将结果序列化回传。服务端编排层接收 LLM 的 codemode tool call，将生成代码、可用工具的类型描述和模式信息下发给浏览器侧执行。

### 浏览器侧隔离执行环境

隔离执行环境的核心是一个沙箱化的浏览器上下文，不与主页面共享 JavaScript 执行环境或 DOM 访问权限。具体方案采用以下机制协同实现隔离。

第一，沙箱 iframe 创建时使用严格的 sandbox 属性：仅启用 allow-scripts，明确禁止 allow-same-origin、allow-top-navigation、allow-forms、allow-popups 等权限。不设置 allow-same-origin 是关键安全决策——这使得沙箱内代码运行在 null origin 下，无法通过 document.cookie、localStorage、sessionStorage 或 DOM 遍历访问主页面数据。

第二，沙箱内部注入一份精简的沙箱运行时（Sandbox Runtime）。该运行时替代了完整的浏览器全局对象，提供受控的 console 实现（所有日志通过 MessageChannel 回传至主页面，而非直接输出到浏览器控制台）、Promise.race 实现的超时定时器、以及基于 Proxy 的工具命名空间代理。LLM 生成的代码被包裹在一个异步函数中在该运行时内执行。

第三，沙箱内的网络访问能力被内容安全策略（CSP）完全阻断。沙箱 iframe 的 CSP 头禁止 connect-src、禁止 fetch 和 XMLHttpRequest 对外发起请求，确保 LLM 生成代码不能绕过工具调度协议直接访问外部资源。网络隔离在浏览器层面强制执行，而非依赖代码层面的变量遮蔽。

### 动态工具注册与命名空间管理

动态工具是指由主页面在运行时注册、可被沙箱中 LLM 生成代码调用的函数。每个工具需要声明其来源命名空间、工具名、描述、输入 JSON Schema、可选输出 Schema 以及实际执行函数。主页面通过 BrowserToolRegistry 进行注册管理。

命名空间与来源标识：每个工具组归属一个命名空间，命名空间同时作为来源标识。命名空间类型包括：（a）page——页面自身注册的 DOM 操作、页面状态读写、浏览器 API 工具；（b）extension——浏览器扩展注册的工具；（c）remote——通过 WebMCP 适配器从服务端 McpAgent 桥接的工具（对应现有的 agents/experimental/webmcp）；（d）自定义命名空间——由开发者指定的任意标识符。命名空间在工具注册时声明，并在沙箱内作为一级对象暴露。

工具名规范化：工具原始名称可能包含连字符、点号、空格等非 JavaScript 标识符字符（尤其在 MCP 工具场景）。规范化的步骤包括：（1）将连字符、点号和空格统一替换为下划线；（2）去除其他非标识符字符；（3）若结果以数字开头，前缀下划线；（4）若结果为 JavaScript 保留字，后缀下划线。此规则与现有 codemode 的 sanitizeToolName 保持一致，确保 LLM 生成的代码中工具调用为合法 JavaScript 标识符访问。

冲突处理：当两个工具组注册了经过规范化后同名的工具时，系统执行以下策略：（1）同一命名空间内同名工具，后注册者覆盖前者，并发出警告；（2）不同命名空间间不存在冲突，因为每个命名空间在沙箱中为独立代理对象（如 page.getSelection() 与 extension.getSelection() 不冲突）；（3）命名空间名本身若冲突（两个来源注册了同名命名空间），拒绝后注册者并抛出错误。此外，保留命名空间名集合（如 __sandbox、__system）禁止外部工具组使用。

### 工具调度协议

沙箱与主页面之间的所有通信通过单一 MessageChannel 进行。该通道承载两类消息：工具调用消息和生命周期消息。所有消息均为结构化 JSON 对象，不存在函数引用或对象引用传递。

工具调用请求消息结构包含：type（固定为 "tool:call"）、requestId（唯一请求标识，用于匹配响应）、namespace（命名空间名）、toolName（原始工具名，非规范化后的名称）、args（JSON 序列化后的参数对象）和 timestamp。工具调用响应消息结构包含：type（"tool:result" 或 "tool:error"）、requestId（对应请求的标识）、result（成功时的返回值，JSON 可序列化）、error（失败时的错误信息，包含 message 和可选的 stack）、logs（该次工具调用产生的控制台输出数组）。

主页面侧工具代理网关（ToolProxyGateway）负责处理来自沙箱的 tool:call 消息。处理流程为：（1）查找请求中 namespace 对应的已注册工具组，不存在则返回 "tool:error" 并提示命名空间未注册；（2）在工具组内查找 toolName，不存在则返回 "tool:error" 并提示工具未找到；（3）使用工具注册时声明的 inputSchema 对 args 进行 JSON Schema 校验，校验失败则返回 "tool:error" 附带校验错误详情；（4）校验通过后，执行注册的 execute 函数，捕获执行结果或异常；（5）将结果 JSON 序列化后发送 "tool:result"，或将异常信息封装为 "tool:error"。注意，非法工具调用（未注册的命名空间或工具名）与执行期异常被区分为不同的错误类型，前者在调度层拦截，后者来自工具函数本身。

### 安全控制机制

安全控制贯穿沙箱创建、代码执行、工具调度和结果回传四个阶段，采用纵深防御策略，不依赖单一机制。

沙箱层安全：（1）iframe sandbox 属性仅启用 allow-scripts，禁止 allow-same-origin，确保沙箱运行在 null origin 下，无法访问主页面任何同源存储和 DOM；（2）CSP 头完全阻断网络请求，connect-src 'none'；（3）沙箱内不注入任何主页面对象的引用——window、document、location、parent、top 等均不可用或被替换为安全存根；（4）沙箱内 console 方法被重写，输出不直接写入浏览器控制台而是通过 MessageChannel 回传，经主页面过滤后再决定是否输出。

调度层安全：主页面 ToolProxyGateway 是沙箱代码访问主页面能力的唯一路径，构成代理边界。网关对每次工具调用进行以下强制检查：（1）命名空间白名单校验——仅接受已在 BrowserToolRegistry 中注册的命名空间；（2）工具名校验——在注册的工具集合中查找，不存在则拒绝；（3）输入模式校验——使用注册时声明的 JSON Schema 对参数进行验证，不符合模式的调用被拒绝；（4）返回值序列化——工具执行返回的任何对象必须通过结构化克隆算法（structured clone）或 JSON 序列化/反序列化后才能传递给沙箱，防止对象引用泄漏。

代码层安全：LLM 生成的代码在注入沙箱前经过 AST 归一化处理（复用 codemode 的 normalizeCode 流程），包括：（1）去除 markdown 代码围栏；（2）将非箭头函数形式统一包装为 async 箭头函数；（3）检测并拒绝包含危险全局变量访问的代码模式（如 import()、eval()、Function() 构造函数）。注意 eval 和 Function 在沙箱 CSP 下已不可用，但额外增加静态检测作为纵深防御。

### 执行生命周期管理

每次浏览器侧代码执行具有明确的生命周期状态机，包含六个阶段：准备（preparing）、就绪（ready）、执行中（running）、完成（completed）、错误（error）、超时（timeout）、清理（cleanup）。生命周期由主页面侧的 ExecutionController 管理。

准备阶段（preparing）：创建沙箱 iframe 元素（不挂载到可见 DOM，使用 display:none 或 off-screen 方式），设置 sandbox 属性和 CSP 策略。注入沙箱运行时脚本（Sandbox Runtime），包含工具命名空间代理、console 劫持、超时定时器。通过 MessageChannel 的 port2 传递给沙箱，port1 在主页面侧由 ExecutionController 持有。沙箱运行时初始化完成后发送 "sandbox:ready" 生命周期消息，ExecutionController 收到后状态切换为 ready。

执行阶段（running）：ExecutionController 将 LLM 生成的代码（经 AST 归一化后）和可用工具的类型声明通过 "sandbox:execute" 消息发送至沙箱。沙箱运行时将代码包裹在 async 函数中执行，同时启动超时定时器。执行期间，沙箱可通过 MessageChannel 发送 "tool:call" 消息，由主页面 ToolProxyGateway 处理并返回 "tool:result" 或 "tool:error"。所有 console 输出通过 "log:append" 消息实时回传。

结果与终止阶段：代码正常执行完毕时，沙箱发送 "sandbox:result" 消息（包含返回值 result 和收集的 logs 数组），状态切换为 completed。代码抛出未捕获异常时，沙箱发送 "sandbox:error" 消息（包含 error.message 和 error.stack），状态切换为 error。超时定时器触发时（默认 30 秒，可配置），沙箱发送 "sandbox:timeout" 消息，状态切换为 timeout，ExecutionController 立即切断 MessageChannel 并进入清理流程。清理阶段（cleanup）：关闭 MessageChannel 的所有 port，从 DOM 中移除沙箱 iframe，释放 ExecutionController 持有的所有引用，注销与该次执行关联的所有临时监听器。清理完成后状态切换为 terminated。

### 工具描述与输入输出模式生成

为使 LLM 能正确生成调用工具编排的代码，系统需要在 codemode tool 的描述中提供可用工具的类型声明和模式信息。该机制直接复用并扩展 codemode 包中已有的 generateTypes 函数。

对于每个注册的工具命名空间，系统遍历其中的工具定义，提取每个工具的 inputSchema（JSON Schema 格式）和可选的 outputSchema，调用 jsonSchemaToTypeString 将其转换为 TypeScript 类型声明字符串。工具名经过 sanitizeToolName 规范化后作为沙箱内可调用的方法名，参数类型命名为 {ToolName}Input，返回值类型命名为 {ToolName}Output。生成结果形如：type GetSelectionInput = { }; type GetSelectionOutput = string; declare const page: { getSelection: (input: GetSelectionInput) => Promise<GetSelectionOutput>; }。

工具描述信息（description）和输入参数字段描述被提取并嵌入 JSDoc 注释中，使 LLM 能够理解每个工具的用途和每个参数的含义。当工具的 inputSchema 来自 Zod schema 或 AI SDK 的 jsonSchema 包装时，系统通过 extractDescriptions 函数从 schema 的 .shape 描述或 JSON Schema 的 .properties[field].description 中提取字段级描述信息。对于未提供 inputSchema 的简单工具（如 SimpleToolRecord 类型），系统生成一个接受 unknown 参数的通用签名，仅保留工具名和描述供 LLM 参考。

### 与现有体系的集成

本方案设计为与现有 Cloudflare Agents 体系中多个组件协作，而非替代它们。集成点包括以下方面。

与 codemode 包的集成：浏览器侧执行器（BrowserCodeExecutor）实现与现有 Executor 接口语义对等的契约，使得 createCodeTool 可以同时接受服务端 DynamicWorkerExecutor 和浏览器侧 BrowserCodeExecutor。LLM 在生成代码时不感知执行位置差异——服务端工具和浏览器端工具通过不同的命名空间区隔（如 codemode.* 和 page.*）。ToolProvider 的 name 字段复用为命名空间标识，types 字段自动生成。

与 AIChatAgent / Client Tool 的集成：当前 AIChatAgent 的 client tool（无 execute 函数的工具）通过 onToolCall 回调在浏览器侧执行。Browser Codemode 提供一种补充模式：当 LLM 需要对多个 client tool 进行编排时（含条件、循环、错误处理），它可以生成代码，由浏览器沙箱统一执行。client tool 可自动注册为 page 命名空间下的浏览器工具——系统读取 AIChatAgent 的 tools 定义，将其中无 execute 函数的工具自动注册到 BrowserToolRegistry 的 page 命名空间中，并生成对应的类型声明。

与 WebMCP 的集成：现有的 agents/experimental/webmcp 适配器将服务端 McpAgent 工具桥接到浏览器。在 Browser Codemode 方案中，WebMCP 桥接的工具被注册到 remote 命名空间下。沙箱代码可以通过 remote.* 调用这些服务端工具，同时调用 page.* 的浏览器端工具，实现一次编排跨越前后端。工具列表变更的监听（watch 模式）同样适用于沙箱场景——当服务端工具变更时，BrowserToolRegistry 更新 remote 命名空间的工具列表，并触发 LLM 工具描述重新生成。

### 风险与待确认问题

本方案存在以下需要在实施阶段进一步确认的风险和技术决策点。

沙箱方案的跨浏览器兼容性：iframe sandbox 属性的 allow-scripts 和禁止 allow-same-origin 在现代浏览器中均受良好支持，但 null origin 下部分浏览器 API 行为存在差异（如 Blob URL、Web Workers 的可用性），需要针对目标浏览器进行兼容性验证。

工具审批（needsApproval）在沙箱中的处理：现有 codemode 不支持 needsApproval 工具（此类工具在沙箱中直接执行而非暂停等待审批）。浏览器沙箱场景中，若某工具标记为需要审批，则沙箱侧需支持暂停执行并等待主页面用户确认的流程。这可能需要在 MessageChannel 协议中增加 approval:required 和 approval:response 消息类型。

Web Worker 替代方案：iframe sandbox 方案以外的另一可行路径是使用 Web Worker（或内部再创建 iframe 的 Worker），以获取更轻量的隔离执行环境。Web Worker 天然不具备 DOM 访问能力，且可通过 CSP 限制网络访问。但 Worker 内无法直接使用部分浏览器 API（如 navigator.geolocation），这需要主页面代理提供。两种方案的取舍需根据实际工具类型分布进行决策。

工具描述生成的性能：generateTypes 依赖 zod-to-ts 和 JSON Schema 到 TypeScript 的类型转换，在工具数量较多（如 50 个以上）时可能产生较大的类型描述字符串，影响 LLM 上下文窗口利用率和 token 消耗。可考虑引入工具描述缓存、增量更新和按需生成策略。

安全边界的形式化验证：当前方案的安全性依赖于浏览器 sandbox 属性、CSP 策略和 MessageChannel 的结构化克隆机制，但缺乏对沙箱逃逸场景的形式化分析。建议在实施阶段进行穿透测试，验证沙箱代码是否可能通过 Spectre 类侧信道、postMessage 滥用或 Blob URL 等方式绕过隔离。
