## 技术方案

本方案提出一种浏览器侧隔离执行LLM生成编排代码的方法与系统。核心思路是：在浏览器主页面中构建一个与主页面执行上下文严格隔离的代码执行沙箱（Sandbox），LLM生成的编排代码在该沙箱中运行；沙箱内部不直接调用浏览器工具，而是通过结构化的跨边界调度协议，向主页面侧的Tool Proxy发出工具调用请求；主页面侧的Tool Proxy对请求进行合法性校验后，代表沙箱执行已注册的浏览器工具，并将执行结果回传沙箱。整个过程中，生成代码无法访问主页面DOM、全局变量、Cookie、Storage等对象，工具的实际执行始终由主页面代理完成。

### 整体架构

系统在浏览器侧由以下核心模块组成：（1）Sandbox Manager，负责沙箱实例的创建、配置、生命周期管理和销毁；（2）Sandbox Runtime，即隔离执行环境，接收并执行LLM生成的编排代码，拦截代码中的工具调用并转为协议消息；（3）Tool Registry，在主页面维护已注册浏览器工具的命名空间、工具名、输入输出schema和来源标识；（4）Tool Proxy，接收沙箱发出的工具调用请求，完成校验后代理执行工具并回传结果；（5）Code Generator，根据Tool Registry中的工具元数据，为LLM生成工具描述（function declarations），供LLM生成编排代码时引用。各模块之间通过结构化消息协议通信。

### 隔离执行环境的构建

隔离执行环境采用以下机制构建：使用sandboxed iframe作为执行容器，设置sandbox属性为"allow-scripts"（不设置allow-same-origin、allow-top-navigation、allow-forms等权限），使iframe获得独立的null origin，彻底阻断对主页面DOM、Cookie、localStorage、sessionStorage、IndexedDB等浏览器存储和页面对象的访问。同时设置严格的Content-Security-Policy响应头，限制iframe内只能执行内联脚本（通过Blob URL注入），禁止加载外部资源、禁止eval（使用间接方式控制）、禁止内联样式和外部样式表。编排代码通过Blob URL + importScripts或直接new Function方式注入沙箱执行，不在沙箱中预置任何主页面对象引用。

### 工具注册与命名空间管理

每个可被编排代码调用的浏览器工具在注册时必须提供：命名空间（namespace）、工具名（toolName）、语义化版本（version）、来源标识（sourceId，标识工具由哪个页面组件或模块提供）、输入schema（JSON Schema格式，描述参数名、类型、是否必填、默认值和约束）、输出schema（JSON Schema格式，描述返回值结构）。Tool Registry以"namespace:toolName@version"作为工具的唯一标识键。当多个来源注册同名工具时，采用以下冲突处理策略：若namespace不同，视为不同工具；若namespace相同但version不同，默认使用最新兼容版本；若namespace、toolName、version完全相同但sourceId不同，按注册优先级（后注册覆盖先注册）处理并发出冲突告警。

### 工具描述生成与Schema管理

Code Generator遍历Tool Registry中所有已注册工具，为每个工具生成符合LLM function calling规范的描述结构，包含：name字段（格式为"namespace__toolName"，使用双下划线将命名空间与工具名连接以生成安全的扁平函数名）、description字段（从工具注册时的元数据中提取）、parameters字段（直接复用工具的输入schema）。生成的工具描述列表作为LLM的系统提示或工具声明注入，LLM据此生成调用这些工具的编排代码。工具描述生成时对工具名进行规范化：去除首尾空格、将非ASCII标识符字符替换为下划线、截断至最大长度（如64字符），确保生成的函数名在JavaScript中合法。

### 跨边界工具调度协议

沙箱与主页面之间的通信采用结构化消息协议，基于postMessage通道但对其进行了严格约束和封装。协议定义以下消息类型：（1）SANDBOX_READY：沙箱初始化完成，可以接收编排代码；（2）EXECUTE_CODE：主页面向沙箱注入编排代码；（3）TOOL_CALL：沙箱向主页面发起工具调用请求，载荷包含调用唯一标识callId、规范化的工具名normalizedToolName、参数arguments对象和请求时间戳；（4）TOOL_RESULT：主页面回传工具执行成功结果，载荷包含callId、result数据；（5）TOOL_ERROR：主页面回传工具执行失败信息，载荷包含callId、error对象（含name、message、stack等可序列化字段）；（6）EXECUTION_DONE：编排代码执行完毕；（7）EXECUTION_TIMEOUT：执行超时通知；（8）SANDBOX_ERROR：沙箱内部错误。所有消息均包含messageId用于去重和日志追踪。

### Tool Proxy校验与代理执行

Tool Proxy在收到TOOL_CALL消息后，执行以下校验流程再代理执行工具：（1）消息来源校验：验证messageEvent.origin为沙箱iframe的null origin或指定标识，拒绝其他来源的消息；（2）工具名解析与校验：将normalizedToolName按"namespace__toolName"格式反解析出namespace和toolName，在Tool Registry中查找对应工具；若工具不存在，返回TOOL_ERROR并标记为"unknown_tool"；（3）参数Schema校验：使用工具注册时的输入schema对arguments进行JSON Schema校验，不符合schema的调用返回TOOL_ERROR并标记为"invalid_arguments"；（4）执行：通过校验后，Tool Proxy调用工具的原始实现函数，传入经过校验的参数。工具的实际执行完全在主页面上下文中进行，沙箱无法感知或干预工具的内部执行过程。

### 安全控制机制

安全控制分为三个层面。第一层：沙箱隔离——sandboxed iframe的null origin + 严格CSP确保生成代码无法通过任何DOM API、BOM API或存储API触及主页面对象；沙箱内不注入window.parent、window.top、document、fetch、XMLHttpRequest等引用。第二层：消息通道安全——只监听来自沙箱iframe的message事件；对每条TOOL_CALL消息校验origin、校验工具名合法性、校验参数schema；拒绝未注册工具的调用请求和参数不合规的调用请求。第三层：工具执行边界——工具的实际执行函数始终运行在主页面上下文，沙箱只获得序列化后的返回值或错误信息；工具函数内部自行负责对自身操作的权限控制（如某些工具仅读取、某些工具可修改页面状态），Tool Proxy不干预工具内部逻辑。生成代码不能直接访问或污染主页面对象的保证由第一层和第二层共同实现。

### 生命周期管理

每次编排代码执行具有明确的六个阶段生命周期。准备阶段：Sandbox Manager创建sandboxed iframe，设置sandbox属性和CSP，注入沙箱基础运行时（包含消息收发、工具调用拦截和基础API shim），等待沙箱发送SANDBOX_READY消息；若在readyTimeout（默认5秒）内未收到就绪消息，进入错误终止流程。注入阶段：主页面向沙箱发送EXECUTE_CODE消息，消息体包含编排代码字符串和本次执行的executionId。执行阶段：沙箱执行编排代码，代码中的工具调用被拦截并转为TOOL_CALL消息发出；主页面Tool Proxy收到TOOL_CALL后同步或异步执行工具并返回TOOL_RESULT或TOOL_ERROR；沙箱中的代码等待工具结果后继续执行。超时控制：从注入阶段开始计时的executionTimeout（默认30秒）到期后，主页面强制发送EXECUTION_TIMEOUT，Sandbox Manager终止沙箱。结果阶段：代码执行完毕或异常退出后，沙箱发送EXECUTION_DONE（携带返回值或未捕获异常信息）。清理阶段：Sandbox Manager销毁iframe、清空消息监听器、清理临时Blob URL、在日志收集器中标记本次执行结束。

### 异常传播与日志收集

异常传播分为三类路径。工具执行异常：工具函数抛出异常时，Tool Proxy捕获异常对象，提取name、message、stack等可序列化字段，通过TOOL_ERROR消息回传沙箱；沙箱将TOOL_ERROR转为JavaScript Error对象抛出给编排代码，编排代码可选择捕获处理或让其向上传播。沙箱内代码异常：编排代码中的语法错误、运行时错误或未捕获异常被沙箱的全局error事件和unhandledrejection事件捕获，通过EXECUTION_DONE消息的error字段回传主页面。沙箱基础设施异常：如Blob URL创建失败、iframe加载失败等，由Sandbox Manager通过SANDBOX_ERROR消息上报。日志收集方面，Sandbox Manager维护一个执行日志缓冲区，每条消息（含messageId、时间戳、类型、方向）按executionId分组记录；沙箱内代码可以通过一个特殊的console shim（将console.log等调用转为LOG消息发送主页面）实现沙箱内日志的集中收集。

### 与现有codemode体系协作

本方案作为浏览器侧codemode的补充实现，与现有服务端/Worker侧codemode共享相同的抽象接口。在codemode的统一抽象层中，存在一个CodeExecutor接口，定义execute(code, tools, options)方法。服务端侧由ServerCodeExecutor实现（在隔离VM或沙箱进程中执行），浏览器侧由BrowserCodeExecutor实现（即本方案的Sandbox Manager + Sandbox Runtime）。Agent Chat系统通过统一的ToolProvider接口获取可用工具列表，浏览器侧由BrowserToolProvider实现，它从Tool Registry读取已注册工具并生成工具描述。Client Tool体系中的工具通过ToolRegistry.register(namespace, toolName, schema, impl)接口注册到主页面，同时被BrowserToolProvider感知。这种设计使得LLM无需感知代码将在服务端还是浏览器侧执行：工具描述生成逻辑一致，编排代码生成逻辑一致，差异仅在于CodeExecutor的具体实现由部署环境决定。

### 技术效果说明

本方案的技术效果包括：（1）通过sandboxed iframe的null origin + 严格CSP + 消息通道三层安全控制，确保LLM生成的编排代码无法直接访问或污染主页面对象，所有工具执行由主页面代理完成并在可校验的边界内进行；（2）通过命名空间+工具名+版本的三元组唯一标识和输入输出schema校验，解决了浏览器侧动态工具命名冲突和调用安全的问题；（3）通过六阶段生命周期管理和超时控制，保证每次执行的可预测性和资源的及时释放；（4）通过统一的CodeExecutor和ToolProvider抽象接口，实现了浏览器侧codemode与服务端codemode的无缝协作，LLM无需感知执行位置差异。

### 风险与待确认问题

以下为需要后续确认的风险和待确认问题：（1）sandboxed iframe的null origin在某些浏览器版本中的行为差异需要测试验证，特别是跨origin消息传递的可靠性；（2）工具函数如果涉及异步操作（如返回Promise），Tool Proxy需要支持异步工具的等待和超时控制，当前方案中Tool Proxy的异步处理策略需要进一步细化；（3）沙箱内编排代码如果发起无限循环或阻塞操作，仅靠executionTimeout可能无法中断（JavaScript单线程特性），可能需要考虑Web Worker作为替代或补充执行载体；（4）部分浏览器工具可能依赖DOM上下文（如读取选择范围需要Range对象），其返回值中可能包含不可序列化的DOM节点引用，需要在Tool Proxy层面对返回值进行安全序列化处理；（5）与现有codemode体系中服务端工具和浏览器工具的联合编排场景（即同一段编排代码同时调用服务端工具和浏览器工具）需要后续设计跨端调度协议。
