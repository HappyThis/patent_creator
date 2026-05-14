# 参考技术方案

## 方案概述

该方案面向浏览器侧动态工具的 codemode 执行场景，提出一种基于临时隔离页面容器的浏览器端代码执行与工具调度机制。系统将 LLM 生成的代码放入受限 iframe 中执行，iframe 只能运行编排逻辑，不能直接访问主页面；当代码需要调用浏览器工具时，通过结构化消息协议请求主页面执行对应工具，主页面校验工具名和参数后返回结果。执行完成或超时后，系统回收 iframe 和事件监听器，并将结果、错误与日志返回给 agent chat 的客户端工具层。

## 核心组件

1. 浏览器端 codemode 工具描述生成器

系统读取当前页面注册的客户端工具，提取工具名称、描述、输入结构和输出结构，生成供 LLM 使用的单一代码编排工具描述。若工具名不符合 JavaScript 标识符规则，则生成可调用的规范化名称，并维护规范化名称到原工具的映射。

2. 隔离执行容器

每次执行创建一个隐藏 iframe，并设置仅允许脚本运行的 sandbox 属性。iframe 使用受控文档内容加载运行时脚本，并附加限制性 CSP，默认禁止外部资源加载和主页面访问。执行完成、失败或超时后，iframe 被销毁。

3. 运行时注入模块

iframe 内部运行时接收待执行代码与可用工具提供者列表，构造代理对象，使生成代码可以通过类似本地函数调用的方式调用工具。代理对象不真正执行工具，而是把调用转换为消息发送给主页面。

4. 双向消息协议

主页面与 iframe 通过浏览器消息通道传递结构化消息。主要消息包括：

- sandbox ready：iframe 运行时准备完成。
- execute request：主页面发送待执行代码和可用工具提供者。
- tool call：iframe 请求调用某个工具，携带调用 id、工具提供者、工具名和参数。
- tool result：主页面返回某次工具调用的成功结果或错误。
- execution result：iframe 返回整段生成代码的最终结果、错误和日志。

5. 主页面工具调度器

主页面维护已注册工具集合。当收到工具调用消息时，先校验消息来源、工具提供者、工具名和参数形态，再调用对应浏览器工具。工具执行完成后，主页面以同一调用 id 返回结果或错误，供 iframe 内部 Promise 恢复。

## 执行流程

1. agent 或前端 chat 组件根据当前客户端工具集合生成 codemode 工具描述。
2. LLM 调用 codemode 工具并输出一段异步编排代码。
3. 浏览器端执行器对代码进行规范化处理。
4. 执行器创建隐藏 iframe，写入受限运行时文档。
5. iframe 发送 ready 消息后，主页面发送 execute request。
6. iframe 执行代码，代码调用工具代理时产生 tool call 消息。
7. 主页面调度真实浏览器工具，将结果或错误通过 tool result 返回。
8. iframe 继续执行生成代码，直至返回最终结果或抛出异常。
9. 主页面收到 execution result 后清理 iframe、定时器和监听器，并把执行结果返回给 agent chat 的客户端工具输出。
10. 若超时或 iframe 加载失败，主页面中止执行并返回结构化错误。

## 安全控制

- iframe 使用 sandbox 隔离，默认只授予脚本执行能力。
- iframe 文档设置严格 CSP，默认不加载外部资源。
- 主页面只接受来自当前 iframe contentWindow 的消息。
- iframe 只能通过已声明的工具提供者和工具名发起调用。
- 主页面在执行工具前校验工具是否存在，非法工具名返回错误。
- 每次执行结束后销毁 iframe，避免状态跨执行泄漏。
- 执行器设置超时，防止生成代码长期占用资源。
- 日志通过运行时收集并作为结果字段返回，避免直接污染主页面控制台。

## 异常处理

- iframe 未准备好、加载失败或无法发送消息时，返回 sandbox unavailable 类错误。
- 工具执行抛错时，主页面将错误文本绑定调用 id 返回，iframe 内部对应 Promise reject。
- 生成代码抛错时，iframe 返回 execution result 中的 error 字段。
- 工具提供者名称冲突、非法或使用保留名称时，执行前直接拒绝。
- 工具名规范化后找不到映射时，返回工具不存在错误。

## 技术效果

该方案使 LLM 生成代码能够在浏览器端编排动态客户端工具，同时通过 iframe、CSP、消息协议、工具白名单和执行后销毁实现安全边界。相比直接在主页面 eval 代码，该方案降低了页面污染和越权访问风险；相比把客户端工具搬到服务端，该方案保留了浏览器私有上下文和动态工具能力。

## 目标能力边界

必须解决的是“在浏览器端安全执行 LLM 生成的工具编排代码”，不是让模型直接调用浏览器工具。生成代码应运行在隔离容器中，真实工具只在主页面注册表中执行。方案可以采用 iframe、Worker 或等价浏览器沙箱，但必须提供隔离、消息协议、超时和清理。

该方案不要求支持需要人工审批的工具在同一 codemode 执行中暂停恢复；可以将审批型工具排除或回退到普通 client tool 流程，但必须说明边界。

## 核心数据结构与消息协议

工具描述对象应包含：

- 工具名、描述、input schema、output schema 或等价结构。
- 规范化后的 JavaScript 可调用名。
- 真实工具名映射和 provider 名称。

消息协议至少包含：

- ready：隔离运行时已加载。
- execute：主页面发送规范化代码和 provider 列表。
- tool-call：隔离环境请求调用 provider/tool，携带 call id、name、args。
- tool-result：主页面返回同一 call id 的 result 或 error。
- execution-result：隔离环境返回最终 result、error、logs。

执行状态包括 `creating_sandbox/ready/running/waiting_tool/completed/error/timeout/cleaned`。超时、加载失败、消息发送失败都应进入 terminal 并清理 iframe。

## 安全与清理细节

- 隔离容器不得拥有主页面 DOM 访问权；iframe 方案应使用 sandbox 和限制性 CSP。
- 主页面只接受当前 iframe/window 或等价隔离上下文发出的消息。
- 主页面只执行已注册、白名单内、schema 匹配的工具。
- provider 名称和工具名称需要检查保留字、重复名、非法标识符。
- 每次执行创建独立容器，结束后移除容器、timer、message listener 和 pending calls。
- 日志应作为执行结果收集，不直接污染主页面。

## 项目集成点

方案应复用 codemode 的类型生成/工具描述、代码规范化、executor 接口、client tool registration 和 `useAgentChat` 的工具结果回传。高分答案会说明浏览器 executor 与现有 Worker executor 是同一 executor 抽象的不同实现。

## 必须命中的评分锚点

- 不在主页面直接 eval 生成代码。
- 有隔离容器和 CSP/sandbox 或等价安全边界。
- 有双向消息协议和 call id 关联。
- 主页面负责真实工具执行，隔离环境只发请求。
- 有动态工具描述和工具名规范化。
- 有超时、错误传播和资源清理。

## 常见错误方案

- 直接把模型代码放到浏览器主线程执行。
- 只把工具 schema 发给模型，但没有代码执行隔离和调度协议。
- 允许隔离代码直接访问 `window` 或 DOM。
- 没有 call id，多个并发工具调用结果可能串线。
- 没有超时和 cleanup，iframe/listener 泄漏。

## 与源码实现的对应参考

该参考方案对应 `cloudflare/agents` 的 `feat(codemode): add browser iframe executor (#1468)`：

- 方案提交：`186a2a45700fbd9680b69e8b72ea062fd325d077`
- 快照提交：`ab2b1db31971ac2d2ddab9d962986f208c69a422`
- 关键能力：`IframeSandboxExecutor`、`createBrowserCodeTool()`、iframe `sandbox="allow-scripts"`、限制性 CSP、`postMessage` 工具调用协议、执行超时和清理。
