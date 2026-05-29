## 技术方案

### 整体架构

本技术方案在浏览器侧构建一个安全的隔离代码执行沙箱，使 LLM 能够生成 JavaScript 编排代码，在沙箱中调用页面注册的动态客户端工具，而无需将浏览器私有能力迁移到服务端。方案复用现有 codemode 的 ToolProvider 命名空间模型、工具名规范化机制和 Schema 驱动类型生成能力，将其执行层从服务端 DynamicWorkerExecutor（基于 Cloudflare Workers 动态隔离体）适配到浏览器侧基于 sandbox iframe 的隔离执行环境。

系统由三个核心层组成：（1）主页面工具注册与代理层，负责注册浏览器侧动态工具、生成工具描述 Schema、接收来自沙箱的工具调用请求并代理执行；（2）隔离沙箱执行层，基于 sandbox iframe 实现，承载 LLM 生成的编排代码，将工具调用请求通过结构化消息协议发送到主页面；（3）通信协议层，定义主页面与沙箱之间的消息格式、调用/响应匹配、错误传播和生命周期信号。

### 隔离执行环境构建

隔离沙箱基于 HTML sandbox iframe 构建。iframe 设置 sandbox 属性为"allow-scripts"，不包含 allow-same-origin、allow-top-navigation、allow-popups、allow-forms 等权限。这使得沙箱内的代码运行在 null origin 下，无法通过 DOM API 访问主页面对象、无法读取主页面 cookie 或 localStorage、无法发起同源网络请求。

沙箱内部注入执行骨架代码（sandbox harness），该骨架代码初始化后通过 postMessage 向主页面发送 ready 信号。骨架代码为每个已注册的工具命名空间创建一个 Proxy 对象，拦截对 `namespace.toolName(args)` 的调用，将其序列化为结构化消息通过 postMessage 发送到主页面，并等待对应的响应消息。沙箱内的 LLM 生成代码通过标准 JavaScript 异步语法调用这些 Proxy 对象，无需感知底层通信细节。

### 工具命名空间与来源标识

每个工具归属于一个命名空间（namespace），命名空间同时作为工具来源标识。命名空间由工具注册时显式指定，未指定时使用默认命名空间。工具在沙箱中通过 `namespace.toolName()` 的形式调用，使 LLM 生成代码时能够区分不同来源的工具（如 `page.getSelection()` 与 `storage.read()`）。命名空间名称必须是合法的 JavaScript 标识符，系统在注册阶段进行校验，拒绝包含特殊字符或与保留名称冲突的命名空间。

### 工具名规范化与冲突处理

工具原始名称可能包含连字符、点号、空格等不构成合法 JavaScript 标识符的字符（如 `get-selection`、`page.scroll`）。系统在注册阶段对每个工具名执行规范化转换：将连字符、点号和空格替换为下划线；移除所有非标识符字符；以数字开头的名称前添加下划线前缀；对于 JavaScript 保留字（如 `delete`、`class`、`return`），在名称末尾追加下划线避免语法冲突。规范化后的工具名作为 Proxy 上的属性名暴露给沙箱代码。

当不同来源注册了规范化后同名的工具时，系统优先采用先注册者，后续同名工具注册时触发冲突告警并被拒绝。命名空间前缀机制从根本上减少了跨来源冲突的可能性：即使两个工具原始名称相同（如都叫 `search`），只要它们属于不同的命名空间（如 `github.search` 与 `docs.search`），在沙箱中即为不同的调用路径。此外，系统保留一组内部名称（如 `__dispatchers`、`__logs`），禁止命名空间使用这些保留名称。

### 工具调度协议

主页面与沙箱之间的工具调度基于 postMessage 实现，采用请求-响应匹配模式。每条工具调用请求消息包含以下字段：`id`（唯一调用标识，由沙箱生成）、`type`（固定为 `tool:call`）、`namespace`（工具命名空间）、`tool`（规范化后的工具名）、`args`（调用参数，JSON 可序列化对象）。主页面工具代理层收到请求后，根据 namespace 和 tool 查找已注册的工具执行函数，传入 args 执行，将执行结果或错误封装为响应消息回传。

响应消息包含：`id`（匹配对应请求的 id）、`type`（固定为 `tool:result`）、`result`（成功时的返回值，JSON 可序列化）、`error`（失败时的错误信息对象，包含 message 和可选的 stack 字段）。沙箱内 Proxy 收到响应后，若存在 error 则作为 JavaScript 异常抛出，否则将 result 作为异步函数返回值返回给 LLM 生成的代码。主页面工具执行异常（包括工具不存在、参数校验失败、执行超时、工具函数自身抛出的异常）均通过 error 字段传播，保留原始异常的 message 和 stack 信息。

### 输入输出 Schema 与类型生成

每个注册的工具需提供 JSON Schema 形式的输入参数结构描述（inputSchema），可选提供输出结构描述（outputSchema）。系统将工具集合的 Schema 信息自动生成为适合 LLM 理解的类型声明文本，包括工具函数的参数类型、返回值类型和字段注释。类型声明按命名空间分组，注入到 LLM 的代码生成提示中，使 LLM 能够生成正确的工具调用代码。

工具执行前，主页面代理层对入参进行 Schema 校验。校验由 AI SDK 的 asSchema 机制驱动，支持 Zod Schema、Standard Schema 协议和 JSON Schema 三种描述格式。校验失败时返回结构化错误，包含字段级别的错误路径和原因描述，使 LLM 能够根据错误信息修正后续调用代码。校验通过后的参数值才被传递给实际的工具执行函数，确保沙箱代码不会以非法参数调用主页面工具。

### 生命周期管理

每次代码执行具有明确的七阶段生命周期：（1）准备阶段：系统根据当前注册的工具列表生成沙箱 HTML 文档和注入骨架代码，创建 sandbox iframe 并挂载到 DOM 中；（2）就绪阶段：沙箱内骨架代码初始化完成后发送 `sandbox:ready` 消息，主页面收到后标记沙箱可用；（3）注入阶段：主页面将 LLM 生成的用户代码通过 `sandbox:execute` 消息注入沙箱；（4）执行阶段：沙箱执行用户代码，期间工具调用通过 Proxy 经 postMessage 代理到主页面；（5）结果返回阶段：代码执行完成后沙箱发送 `sandbox:result` 消息携带返回值和日志；（6）错误阶段：执行过程中任何未捕获异常或超时，沙箱发送 `sandbox:error` 消息；（7）清理阶段：主页面移除 iframe、注销消息监听器、取消未完成请求。

超时控制由沙箱内部通过 Promise.race 实现：用户代码与一个定时拒绝的 Promise 竞速。默认超时时间为 30 秒，可通过配置调整。超时发生后，沙箱内部停止等待用户代码、发送超时错误消息，但用户代码中已发起的工具调用不受影响地继续完成（结果被丢弃）。清理阶段确保每次执行后释放所有资源：移除 iframe DOM 节点、移除所有 postMessage 事件监听器、清理未完成的工具调用 Promise 映射表、中止关联的 AbortController。系统支持在执行过程中通过 AbortController 从主页面主动取消执行。

### 安全控制

安全控制分为三个层面。第一层：iframe 沙箱属性控制。sandbox iframe 仅开启 `allow-scripts`，不开启 `allow-same-origin`，使沙箱代码运行在 null origin，无法通过 DOM API 访问或修改主页面对象（包括 document、window、localStorage、sessionStorage、cookie、indexedDB）。沙箱内代码不能操作主页面 DOM、不能读取主页面存储、不能访问主页面全局变量或函数。

第二层：消息通道白名单。沙箱与主页面之间的 postMessage 通道进行严格的消息来源校验和目标校验。主页面只处理来自已知沙箱 iframe 且 event.source 匹配的消息；沙箱只处理来自 event.source 为 parent 的消息。消息 type 字段进行白名单校验，非白名单内的消息类型被静默丢弃。第三层：工具代理边界。沙箱代码不直接持有任何工具函数的引用，所有工具调用必须通过 Proxy → postMessage 序列化 → 主页面反序列化 → 工具执行 → 结果序列化 → postMessage → 沙箱反序列化的完整往返路径。这意味着即使沙箱代码试图访问或修改工具函数引用，它只能接触到 Proxy 对象而无法触及主页面真实的函数闭包和执行上下文。

### 与现有体系的协作

本方案与现有三个体系协同工作。与 codemode 体系的协作：浏览器沙箱执行器（BrowserSandboxExecutor）实现与 DynamicWorkerExecutor 相同的 Executor 接口，接受相同的 ResolvedProvider 数组作为工具提供者，使现有 createCodeTool 和 ToolProvider 模型无需修改即可在浏览器侧使用。当 createCodeTool 检测到运行环境为浏览器时，自动选择 BrowserSandboxExecutor 而非 DynamicWorkerExecutor。

与 Agent Chat 体系的协作：浏览器侧注册的工具通过现有的 ClientToolSchema 格式（name、description、parameters）上报给服务端 Agent，服务端 Agent 在 LLM 推理时将这些工具与服务器端工具合并为统一工具列表。当 LLM 调用客户端工具时，工具调用通过现有的 cf_agent_tool_result 协议回传浏览器执行。当 LLM 选择使用 codemode 生成代码编排多个工具时，生成的代码在浏览器沙箱中执行，工具调用不经过服务端往返而直接在浏览器侧本地完成调度。与 Client Tool 体系的协作：浏览器侧注册的工具同时支持两种调用路径——LLM 直接逐个调用（通过 chat 协议回传）和 LLM 生成代码批量编排（通过沙箱 Proxy 本地调用），两种路径共享同一套工具注册和 Schema 描述。

### 日志收集与异常传播

沙箱骨架代码劫持 console.log、console.warn、console.error 方法，将日志输出收集到内存数组而非输出到浏览器控制台。执行完成后日志随 `sandbox:result` 或 `sandbox:error` 消息一同返回给主页面。非法工具调用（调用未注册的工具名）不会导致沙箱崩溃，而是返回包含 `Tool "xxx" not found` 的结构化错误，使 LLM 能够感知并修正调用。

### 技术效果

本方案在浏览器侧实现了与现有 codemode 服务端方案对等的代码编排执行能力，同时具有以下技术效果：（1）安全隔离：通过 sandbox iframe + null origin + Proxy 代理三层安全控制，确保 LLM 生成的代码无法直接访问或污染主页面对象；（2）零依赖：方案仅使用浏览器原生 API（iframe、postMessage、Proxy、AbortController），不引入额外第三方运行时或沙箱库；（3）工具热插拔：浏览器侧工具在运行时动态注册和注销，工具列表变化时自动重新生成 Schema 描述供 LLM 使用；（4）低延迟：工具调用在浏览器本地完成主页面-沙箱往返，不经过网络，单次工具调用延迟为微秒级；（5）可观测：完整的日志收集、异常堆栈保留和结构化错误返回，便于调试和 LLM 自我修正；（6）体系兼容：复用现有 ToolProvider 命名空间模型、sanitizeToolName 规范化、Schema 类型生成和 Executor 接口，与 codemode、Agent Chat 和 Client Tool 三个体系无缝协作。

### 风险与待确认问题

（1）sandbox iframe 的 `allow-scripts` 在部分旧版浏览器中可能不被完整支持，需评估目标浏览器兼容性矩阵。（2）null origin iframe 中某些浏览器 API（如 performance.now()、console 方法）的行为可能存在差异，需在目标浏览器上进行充分测试。（3）postMessage 消息大小受浏览器限制（通常约 64KB-1MB），大体积工具调用参数或返回值需要分片传输或使用 SharedArrayBuffer 等替代通道。（4）sandbox iframe 中的代码无法使用 ES Module import，所有依赖需通过骨架代码预注入或工具调用间接获取，这限制了生成代码的模块化能力。（5）多个沙箱同时存在时的资源占用（内存、事件监听器）需制定并发上限策略。
