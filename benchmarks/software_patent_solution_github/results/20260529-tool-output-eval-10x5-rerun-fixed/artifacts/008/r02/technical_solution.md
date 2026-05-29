## 技术方案

### 技术问题概述

当前项目的 codemode 能力允许 LLM 生成代码并在服务端隔离 Worker 中执行，通过 Workers RPC 将工具调用从沙箱派发到宿主。但这种模式无法覆盖浏览器侧场景：某些工具仅存在于浏览器页面中（如读取页面选择内容、访问页面状态、调用前端组件暴露的能力）。若将这些浏览器私有能力全部搬到服务端，不仅增加网络延迟和服务端负担，还会丢失浏览器上下文的实时性。若让 LLM 生成代码直接在主页面执行，则存在安全风险——生成代码可能访问或污染主页面对象（如 window、document、全局状态等）。

### 整体架构

方案在浏览器侧引入「浏览器端隔离执行环境」（Browser Sandbox），作为 LLM 生成代码的执行容器。该环境与主页面隔离，无法直接访问主页面对象。主页面作为「工具宿主」（Tool Host），注册实际可用的浏览器侧工具（如读取选择文本、获取页面状态等），并生成工具的类型描述供 LLM 参考。执行流程为：LLM 生成编排代码 → 注入隔离环境 → 代码通过代理对象调用工具 → 代理将调用序列化并通过安全通道（postMessage / MessageChannel）发送到主页面 → 主页面执行实际工具并返回结果 → 结果回传隔离环境。

整体架构由以下关键部分组成：（1）隔离执行容器——承载 LLM 生成代码的浏览器侧沙箱；（2）工具注册与描述生成——将浏览器侧动态工具转换为 LLM 可理解的类型声明；（3）工具调度协议——隔离环境与主页面之间的工具调用与结果回传通道；（4）生命周期管理器——控制沙箱的准备、执行、超时、异常和清理全过程。

### 隔离执行环境设计

隔离执行环境采用沙箱化 iframe（sandbox attribute + null origin）或专用 Web Worker 实现。选择 iframe 方案时，iframe 设置 sandbox="allow-scripts" 且不设置 allow-same-origin，使内部 JavaScript 运行在与主页面完全隔离的 null origin 上下文中。iframe 内部无法通过 window.parent / window.top 访问主页面对象，无法读取主页面 DOM，无法访问主页面 cookie 或 localStorage。

隔离环境内部注入一组 Proxy 代理对象，每个代理对应一个工具命名空间（如默认命名空间 codemode 或自定义命名空间）。代理的 get 陷阱拦截对 codemode.toolName(args) 的访问，将工具名称和调用参数序列化为 JSON，通过 MessageChannel 或 postMessage 向主页面发送调用请求。主页面侧的工具调度器接收请求、匹配已注册工具、执行并返回结果。隔离环境内的代码无法绕过该代理直接访问主页面任何对象。

与现有服务端 DynamicWorkerExecutor 的 globalOutbound: null 机制类似，浏览器沙箱默认不暴露 fetch、XMLHttpRequest、WebSocket 等网络 API。如确需受控网络访问，可通过主页面代理转发——主页面工具宿主提供可选的 httpFetch 工具，沙箱通过该工具间接发起网络请求，所有请求经过主页面审核。

### 工具描述、命名空间与来源标识

浏览器侧工具分为两类：内置工具（由 SDK 提供，如读取页面选择、访问剪贴板）和开发者自定义工具（由应用注册，如调用前端组件暴露的能力）。每个工具包含以下元数据：（1）名称——原始工具名，可能包含连字符、点号或特殊字符；（2）来源标识——标记工具由哪个组件或模块注册，用于命名空间分组和冲突消解；（3）输入 JSON Schema——描述工具接受的参数结构；（4）输出 JSON Schema——描述工具返回值的结构；（5）执行函数——在主页面上下文中运行的实际实现。

工具名规范化：借鉴现有 codemode 的 sanitizeToolName 机制，将原始工具名中的连字符、点号、空格替换为下划线，去除其他非标识符字符。若名称以数字开头则添加下划线前缀；若名称与 JavaScript 保留字冲突则追加下划线后缀。主页面维护「原始名→规范化名」的映射表，工具调度器收到调用请求时，通过映射表反向查找原始工具名并路由到正确的执行函数。

命名空间与冲突处理：沿用现有 ToolProvider 的 namespace 设计，每个工具来源（如不同前端组件、不同页面区域）可声明独立的命名空间名称。调用时以 namespace.toolName() 形式区分。若多个来源注册了同名工具，命名空间机制自然消解冲突。若同一命名空间内出现同名，后注册者覆盖前者并输出警告。系统预留命名空间名 __dispatchers、__logs、__lifecycle 用于内部协议。

输入输出 Schema 与类型生成：工具的输入参数由 JSON Schema（或 Zod schema）定义。系统从工具元数据中提取 JSON Schema，通过 json-schema-to-type 转换器生成 TypeScript 类型声明字符串（如 type SelectTextInput = { selector: string }），注入 LLM 工具的 description 中作为 {{types}} 占位符内容。输出 schema 同样被转换为类型声明，供 LLM 了解返回值结构。这一生成机制与现有服务端 codemode 的 generateTypes 保持一致，使 LLM 无需区分工具来自服务端还是浏览器侧。

### 工具调度协议与安全代理边界

隔离环境与主页面之间的工具调用采用基于 postMessage 的结构化消息协议，每条消息包含以下字段：type（消息类型：'call' / 'result' / 'error' / 'ready' / 'timeout' / 'cleanup'）、callId（调用唯一标识，用于请求-响应配对）、namespace（工具命名空间名称）、toolName（规范化后的工具名）、args（JSON 序列化的调用参数，仅 call 消息携带）、result / error（仅 result / error 消息携带）。

安全代理边界：（1）工具调用方向单向——隔离环境只能发起工具调用请求，主页面只能响应结果；隔离环境不能接收来自主页面的主动调用；（2）参数序列化——工具参数在隔离环境侧被 JSON.stringify 序列化，在主页面侧被 JSON.parse 反序列化后传入工具执行函数，返回结果同样经历序列化/反序列化过程，确保隔离环境无法通过原型链污染、闭包引用等方式获取主页面对象引用；（3）非法工具调用——若请求的工具名不在已注册工具集合中，主页面返回 type='error' 消息，携带 'Tool "xxx" not found' 错误信息，隔离环境将错误作为异常抛出；（4）异常传播——主页面工具执行函数若抛出异常，异常信息（message、stack 摘要）被序列化并通过 error 消息回传，隔离环境重新构造 Error 对象抛出。

### 执行生命周期管理

每次代码执行遵循严格的生命周期阶段，由生命周期管理器统一控制：

（1）准备阶段（prepare）：创建隔离 iframe/Worker，注入基础运行时脚本（Proxy 代理 stub、console 日志拦截、Promise.race 超时机制）。主页面将已注册工具的命名空间映射和工具名列表发送给隔离环境。隔离环境完成初始化后发送 type='ready' 消息。

（2）执行阶段（execute）：LLM 生成的代码被注入隔离环境并执行。工具调用通过代理对象的 get 陷阱拦截，构造 call 消息发送到主页面。主页面侧工具调度器匹配工具、调用执行函数、回传 result 或 error。执行期间的 console.log/warn/error 输出被拦截并收集到 __logs 数组中。

（3）超时控制（timeout）：执行通过 Promise.race 与超时定时器竞速。超时配置默认 30 秒，可由调用方指定。超时到达时，主页面向隔离环境发送 type='timeout' 消息，隔离环境终止当前执行的 Promise，捕获超时异常，收集已产生的日志后返回结果。同时主页面取消所有等待中的工具调用。

（4）错误处理（error）：两类错误被区分处理——工具调用错误（工具不存在、参数校验失败、工具执行异常）通过 error 消息回传，不终止整体执行，LLM 代码可捕获并处理；沙箱级错误（语法错误、运行时异常、超时）导致执行终止，错误信息和已收集日志一并返回给 LLM。

（5）清理阶段（cleanup）：执行结束后（无论成功、失败或超时），主页面发送 type='cleanup' 消息。隔离环境收到后：移除事件监听器、清空 __logs 数组、释放 Proxy 引用。主页面侧：移除该 iframe/Worker、取消所有待处理工具调用 Promise、从调度器注册表中移除本次执行关联的临时工具。日志在 cleanup 前已回传给主页面并上报给 LLM，清理阶段不丢失日志。

### 与现有体系的协作

本方案与现有体系的三层协作关系：

与 codemode 体系：方案引入 BrowserSandboxExecutor，实现与 DynamicWorkerExecutor 相同的 Executor 接口（execute(code, providers) → ExecuteResult）。createCodeTool 接受 executor 参数，因此开发者可在服务端和浏览器端之间透明切换——仅需将 executor 从 DynamicWorkerExecutor 替换为 BrowserSandboxExecutor，工具定义的其余部分不变。工具类型生成流程（generateTypes）、代码规范化流程（normalizeCode）、工具名规范化流程（sanitizeToolName）全部复用。

与 Agent Chat / Client Tool 体系：现有的 clientTools 机制（通过 useAgentChat 的 clientTools 参数定义浏览器侧工具，通过 onToolCall 回调执行）继续用于简单单步工具调用场景。Browser Codemode 作为其增强：当 LLM 需要多步编排多个浏览器侧工具时，使用 codemode 工具生成代码；当只需单步调用时，使用标准 client tool 路径。两者的工具注册可以共享同一套工具元数据定义，通过新增的 toClientToolProvider() 适配器将浏览器工具批量转换为 codemode 命名空间。

与 Think / WebSocket 通道：执行结果回传复用现有 WebSocket 通道的 CF_AGENT_TOOL_RESULT 消息格式。当浏览器侧 codemode 执行完成，结果通过 WebSocket 发送至服务端 Think Agent，Think 持久化结果并可选地触发 auto-continuation。与现有 client tool 的 autoContinueAfterToolResult 机制保持一致。

### 风险与待确认问题

（1）沙箱兼容性：不同浏览器的 sandbox iframe 行为存在细微差异（如 null origin 下的某些 API 限制），需在主流浏览器上进行兼容性验证。（2）执行性能：LLM 生成的代码可能包含死循环或无限递归，超时机制虽可兜底，但无法阻止代码在超时前耗尽 CPU 资源。可考虑在 Web Worker 方案中结合 Worker.terminate() 实现硬件级终止。（3）跨域限制：若主页面与隔离 iframe 非同源，MessageChannel 通信受浏览器安全策略约束，需确认 null origin iframe 与主页面之间的 postMessage 通道是否在所有目标浏览器中可用。（4）工具注册时序：工具可能在 LLM 生成代码之后动态注册（如延迟加载的前端组件），需要支持工具热注册和增量类型描述更新，当前方案假设工具在执行前已完整注册。

### 关键处理流程

完整执行流程如下：

S1. 开发者注册浏览器工具：通过 registerBrowserTool({ name, namespace, inputSchema, outputSchema, execute }) 或批量注册 clientToolProvider 向工具注册表添加工具。注册表为主页面侧单例。

S2. 生成工具描述：调用 generateBrowserTypes(registry) 遍历注册表中所有工具，按命名空间分组，通过 jsonSchemaToType 转换器生成 TypeScript 类型声明字符串，注入 codemode 工具的 description 字段。

S3. LLM 决策使用 codemode：LLM 看到 codemode 工具描述及所有浏览器侧工具的类型声明，决定生成编排代码。

S4. 创建隔离环境：BrowserSandboxExecutor 创建 sandbox iframe，注入 Proxy stub、console 拦截和超时竞速代码。主页面将已解析的 ResolvedProvider 列表（命名空间→工具函数映射）注册到工具调度器。

S5. 隔离环境就绪：隔离环境完成初始化后发送 ready 消息。主页面收到 ready 后将 LLM 代码（经 normalizeCode 规范化后的 async arrow function）发送至隔离环境。

S6. 代码执行与工具调度：隔离环境执行代码。遇到 codemode.toolName(args) 时，Proxy 拦截 get 和 apply，构造 { type:'call', callId, namespace, toolName, args } 消息通过 postMessage 发送。主页面 MessageChannel.onmessage 处理：查找命名空间对应工具调度器、反序列化参数、调用实际 execute 函数、序列化结果、发送 { type:'result', callId, result } 或 { type:'error', callId, error } 回隔离环境。Proxy 的 Promise 解析或拒绝。

S7. 结果返回与清理：代码执行完成（正常 return 或异常/超时），隔离环境将 { result, error, logs } 发送到主页面。主页面返回 ExecuteResult 给 createCodeTool 的 execute 回调。若存在 error，createCodeTool 抛出包含日志上下文的异常给 LLM。随后主页面触发 cleanup 流程：通知隔离环境清理、移除 iframe/Worker、取消待处理调用、清理调度器中的临时注册。

### BrowserSandboxExecutor 设计

BrowserSandboxExecutor 是实现 Executor 接口的浏览器侧执行器，对应服务端的 DynamicWorkerExecutor。其核心职责：创建和管理隔离 iframe/Worker 实例、通过 MessageChannel 建立与沙箱的双向通信、将 ResolvedProvider 列表转换为沙箱内 Proxy stub 代码。关键配置项包括：timeout（执行超时，默认 30 秒）、sandboxType（'iframe' | 'worker'）、allowedOrigins（允许接收消息的来源白名单）、onLog（日志回调，用于将沙箱内 console 输出透传到应用层日志系统）。与 DynamicWorkerExecutor 的关键差异在于：不使用 WorkerLoader 和 Workers RPC，而使用浏览器原生 postMessage/MessageChannel。

### 技术效果

（1）零额外依赖：方案完全基于浏览器原生能力（iframe sandbox、postMessage、MessageChannel），无需引入第三方沙箱库或 Worker 运行时。（2）安全隔离不打折：生成代码在任何时刻都无法访问主页面 window、document、DOM、存储或网络；所有与主页面的交互均通过序列化消息通道。（3）与现有服务端 codemode 统一：通过 Executor 接口抽象，浏览器侧执行与服务端执行共享同一套工具定义、类型生成和代码规范化机制。（4）渐进增强：简单单步浏览器工具调用继续使用现有 client tool 路径，复杂多步编排走 browser codemode，开发者无需二选一。（5）可观察：所有工具调用、执行异常和 console 输出均通过日志通道回传，支持调试和审计。（6）资源安全释放：每次执行结束执行强制性 cleanup，避免 iframe/Worker 泄漏和事件监听器残留。
