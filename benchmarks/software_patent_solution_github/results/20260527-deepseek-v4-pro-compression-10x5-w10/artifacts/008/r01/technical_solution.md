## 技术方案

### 整体架构

本方案在浏览器侧构建一套隔离执行环境，用于安全运行 LLM 生成的编排代码（JavaScript/TypeScript），使 AI 生成的工具编排逻辑在沙箱中执行，工具的实际调用通过主页面代理完成，从而保护宿主应用的数据和接口安全。

系统由三层组成：（1）主页面宿主层，负责管理隔离环境生命周期、注册工具提供者、代理工具调用、接收执行结果；（2）隔离沙箱层，运行于 iframe sandbox 中，负责加载 LLM 生成的编排代码并提供标准化的工具调用接口；（3）消息协议层，连接主页面与沙箱，定义标准化的双向通信协议，包括代码注入、工具描述下发、工具调用请求、执行结果回传和控制指令。

主页面通过创建带有 sandbox 属性的 iframe 建立隔离环境，沙箱内部不具备任何直接访问宿主 DOM、网络、存储的能力。沙箱通过 postMessage 与主页面通信，所有对外能力均以「工具」形式由主页面代理执行。主页面维护一个 ToolDispatcher，根据工具的命名空间和来源标识将调用请求路由到对应的 ToolProvider，执行完成后将结果回传沙箱。这一架构将 codemode 服务端的 DynamicWorkerExecutor + WorkerLoader + ToolDispatcher（RpcTarget）模式映射到浏览器侧：iframe sandbox 替代 V8 isolate，postMessage 替代 Workers RPC 通道。

### 隔离执行环境

隔离执行环境基于 iframe 的 sandbox 属性构建。主页面动态创建 iframe 元素，设置 sandbox 属性为 allow-scripts，不开放 allow-same-origin、allow-popups、allow-top-navigation 等权限，确保沙箱内代码无法访问主页面 DOM、Cookie、localStorage 或发起网络请求。iframe 的 src 指向一个预编译的静态 HTML 引导页，该页面内联了最小化的 JavaScript 运行时引导脚本。

引导脚本在沙箱初始化时建立与主页面的 postMessage 通信链路，注册消息监听器，并暴露标准化的沙箱 API 接口供 LLM 生成的编排代码调用。编排代码通过主页面下发的代码字符串（经过 normalizeCode 处理）注入沙箱，由沙箱内的 eval 或 Function 构造器在隔离作用域中执行。沙箱内不提供 fetch、XMLHttpRequest、WebSocket 等网络 API 的可用实现，所有对外交互必须通过工具调用代理完成。

沙箱内部维护一个工具代理对象，该对象以命名空间为键组织可用工具。当编排代码调用某个工具时，沙箱将调用请求封装为标准消息，通过 postMessage 发送到主页面，并返回一个 Promise 等待主页面回传执行结果。这一模式与 codemode 中 ToolDispatcher（RpcTarget）通过 Workers RPC 代理工具调用的机制等价，区别在于传输层从 V8 isolate 的 RPC 通道变为浏览器的 postMessage。

### 动态工具注册与描述协议

工具注册采用 ClientToolSchema 格式，每个工具由 name、description 和 parameters（JSON Schema 7 格式）三个字段定义。主页面在创建隔离环境时，将当前已注册的工具描述集合序列化后通过 postMessage 下发至沙箱。工具描述支持动态增量更新：当主页面注册新的 ToolProvider 或移除已有工具时，通过增量消息同步至沙箱，沙箱更新本地工具代理对象。

工具名采用「命名空间.工具名」的点分格式进行来源标识，例如 github.list_issues、database.query。命名空间标识了工具的来源提供者，工具名标识具体操作。沙箱接收工具描述后，对每个工具名执行规范化处理（sanitizeToolName）：将连字符、点号、空格统一替换为下划线，检查是否为 JavaScript 保留字并在冲突时追加后缀 _，确保工具名在沙箱内作为合法的 JavaScript 标识符使用。规范化后的工具名同时保留原始名称的映射关系，在通过 postMessage 发送调用请求时使用原始名称，保证主页面 ToolDispatcher 可正确路由。

冲突处理规则：（1）同一命名空间内工具名重复时，后注册的工具覆盖先注册的工具；（2）不同命名空间下工具名相同但来源不同时，两者独立共存，由命名空间前缀区分；（3）保留命名空间（如 __system、__dispatchers、__logs）禁止业务工具使用，用于系统级控制消息。工具的输入输出 schema 以 JSON Schema 7 定义 parameters 和可选的 returns 字段，沙箱在执行工具调用前校验输入参数，主页面在工具执行完成后校验输出结果，schema 校验失败时统一抛出 TypedError 并终止当前编排代码执行。

### 工具调度与安全代理

工具调度的核心是主页面侧的 ToolDispatcher。它与 codemode 中的 ToolDispatcher（RpcTarget）功能对等，但传输层从 Workers RPC 变为 postMessage 消息通道。ToolDispatcher 维护一个工具注册表，以「命名空间:工具名」为键映射到对应的 ToolProvider 实例。每个 ToolProvider 封装了一组相关工具的实际执行逻辑，如数据库查询 Provider、GitHub API Provider、浏览器操作 Provider 等。

调度流程：沙箱通过 postMessage 发送工具调用请求，消息格式为 { type: 'tool_call', callId: string, namespace: string, toolName: string, args: any }。主页面接收后执行安全校验：（1）验证 origin，仅接受来自已知沙箱 iframe 的消息；（2）验证 namespace 是否已注册且未被禁用；（3）验证 toolName 是否属于该 namespace 的工具列表；（4）对 args 执行输入 schema 校验。校验通过后，ToolDispatcher 调用对应 ToolProvider 的 execute 方法，执行完成后将结果封装为 { type: 'tool_result', callId: string, result: any } 或 { type: 'tool_error', callId: string, error: SerializedError } 回传沙箱。

安全控制层面，主页面实行三层代理边界：（1）网络隔离——沙箱无直接网络访问能力，所有外部 API 调用由主页面在用户已认证的会话上下文中代理执行；（2）权限控制——每个 ToolProvider 可声明所需权限（如读写剪贴板、访问摄像头），主页面在执行前检查权限授权状态，未授权时拒绝调用并返回权限错误；（3）速率限制——ToolDispatcher 对每个 namespace 维护调用计数器，超出阈值时返回限流错误，防止 LLM 生成代码中的无限循环或恶意高频调用。

### 生命周期管理

隔离环境的生命周期由主页面侧的 SandboxManager 统一管理，包含五个核心状态：初始化（initializing）、就绪（ready）、执行中（executing）、错误（error）、已销毁（destroyed）。

状态转换规则如下：（1）initializing → ready：iframe 加载完成、引导脚本初始化完毕并向主页面发送 ready 消息后，主页面下发工具描述集合，沙箱构建工具代理对象，进入 ready 状态。（2）ready → executing：主页面通过 postMessage 下发编排代码字符串，沙箱执行代码，进入 executing 状态。代码执行期间的工具调用请求异步处理，不影响执行状态的维持。（3）executing → ready：代码正常执行完毕（包括所有异步工具调用完成），沙箱发送 complete 消息，携带返回值，回到 ready 状态，可接受下一次代码注入。（4）executing → error：代码抛出未捕获异常、工具调用 schema 校验失败、或主页面主动发送 abort 指令时，进入 error 状态。沙箱发送 error 消息，携带序列化的错误信息（message、stack、callId）。（5）任意状态 → destroyed：主页面调用 destroy 方法，移除 iframe 元素、清理事件监听器、取消所有待处理的工具调用 Promise，释放资源。

超时机制：每次代码执行设置独立超时时间（默认 30 秒），由主页面侧计时器控制。超时触发后主页面向沙箱发送 abort 消息，沙箱终止当前执行并进入 error 状态。若编排代码包含异步操作（如 await 工具调用），超时计时器在工具调用期间暂停，待结果回传后恢复，保证异步编排的合理执行时长。崩溃恢复：主页面通过定期心跳检测沙箱存活状态，若沙箱无响应则触发销毁并自动重建新的隔离环境，从就绪状态恢复。

### 与 codemode 体系的集成

本方案与现有 codemode 包保持接口兼容，最大化复用已有机制。createCodeTool 函数在浏览器侧生成编排工具时，沿用相同的流程：LLM 生成代码 → extractFns 提取 execute 函数 → normalizeCode 标准化代码字符串 → 注入通过 generateTypes 生成的 TypeScript 类型声明。区别在于执行路径：浏览器侧将代码通过 postMessage 下发至 iframe 沙箱执行，而非通过 WorkerLoader 创建 V8 isolate。

具体集成点包括：（1）sanitizeToolName 在浏览器侧工具注册时复用，处理工具名规范化；（2）ToolProvider 接口保持不变，浏览器侧新增 BrowserToolDispatcher 实现相同的 dispatch 语义，底层从 RpcTarget 切换为 postMessage 适配器；（3）ClientToolSchema（name/description/parameters）在 AIChatAgent 中已作为 client tools 的标准序列化格式，浏览器侧直接复用该格式进行工具描述下发；（4）DEFAULT_DESCRIPTION 中的 {{types}} 占位符替换逻辑不变，沙箱内编排代码同样获得完整的 TypeScript 类型提示。

扩展方案：对于需要持久化执行上下文的场景（如长时间运行的数据处理流水线），可在主页面侧引入 Service Worker 作为中间代理层，将工具调用请求从 iframe 沙箱经 Service Worker 代理到主页面，使编排代码在页面关闭后仍可继续执行（通过 Service Worker 生命周期）。对于需要多个隔离环境并行执行的场景，SandboxManager 支持创建多个 iframe 实例，每个实例独立维护命名空间和工具代理，互不干扰。
