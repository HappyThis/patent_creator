# 评分 Rubric

总分 100 分。

## 1. 环境阅读与场景抽象（15 分）

- 结合当前项目环境识别多会话 assistant 需要同时处理隔离与共享：5 分
- 结合现有 agent/session/workspace 上下文，识别用户级资源与会话级上下文的边界：5 分
- 识别 workspace、MCP、OAuth、定时任务、实时刷新等跨会话资源：5 分

## 2. 父子 agent 架构（20 分）

- 设计用户级父目录 agent 或等价上层实体：5 分
- 设计每个会话独立子 agent：5 分
- 说明父目录维护会话索引和元数据：4 分
- 说明子会话通过路由连接和切换：3 分
- 说明父子实体的存储边界：3 分

## 3. 共享 workspace 机制（15 分）

- 父级持有真实 workspace：3 分
- 子会话通过代理访问父级 workspace：4 分
- 覆盖文本、字节、目录、搜索、删除等文件接口：3 分
- 说明内置工具和代码执行文件接口可复用代理：3 分
- 说明并发写入或父级串行化语义：2 分

## 4. 共享 MCP 机制（15 分）

- 父级持有 MCP 注册表、OAuth 凭据和连接：4 分
- 子会话按轮次获取工具描述并合并工具集：4 分
- 工具调用经由父级执行并返回结果：3 分
- 说明 OAuth 回调归属父级：2 分
- 避免浏览器绕过 agent 生命周期直接调用 MCP 工具：2 分

## 5. 访问控制与安全边界（15 分）

- 通过注册表校验子会话存在性：4 分
- 防止猜测会话 id 唤醒或创建子会话：4 分
- 区分浏览器 callable 与内部父子 RPC：3 分
- 说明同一用户范围内共享、不同用户隔离：2 分
- 说明删除会话与共享资源保留/清理策略：2 分

## 6. 实时同步与调度（10 分）

- 设计 workspace 变化广播或等价同步机制：4 分
- 支持多个标签页或会话视图刷新：2 分
- 父级统一负责跨会话定时任务：2 分
- 能选择目标会话执行后台任务：2 分

## 7. 专利技术方案表达质量（10 分）

- 技术问题、技术手段、技术效果闭环清晰：4 分
- 方案体现父子 agent、共享代理、访问门禁、广播调度的组合机制：4 分
- 避免只描述聊天 UI 或侧边栏操作：2 分

## 扣分项

- 把多个会话简单存在同一个消息数组中，无独立 agent 上下文：最多扣 20 分
- 未区分共享资源与会话私有状态：最多扣 18 分
- 未设计访问门禁：最多扣 15 分
- MCP 或文件工具允许浏览器直接绕过 agent 生命周期调用：最多扣 12 分
- 只写产品交互，不写父子结构、代理和状态流：最多扣 20 分

## 源码级评分补充

- `MCP proxy` 是参考中的表达，不限定命名。高分方案应体现“父级持有 MCP 注册表、OAuth 凭据、connection state 和 tool descriptors；子会话按轮获取描述并经父级执行真实工具调用”的受控代理链。
- Workspace proxy 必须覆盖 Think built-in tools 和 codemode state 所需的完整文件接口，至少说明文本/字节读写、stat/lstat、readDir/glob、rm、cp/mv、symlink/readlink 或等价能力。
- 只做多 chat id、共享一张消息表，或让浏览器/子会话绕过父级直接执行 raw MCP invocation 的方案应显著扣分。

## 档位锚点

- 90-100 分强答案：必须同时具备用户级父目录 agent、每会话独立子 agent、父级会话索引/门禁、防猜测唤醒、共享 workspace 完整代理、共享 MCP 可序列化 tool descriptor + 子侧 wrapper + 父侧 callTool、OAuth 父级归属、浏览器 callable 与内部 RPC 隔离、workspace revision/onChange 广播、多标签刷新、跨会话调度、认证身份绑定和删除会话资源策略。
- 75-85 分可用但不完整：父子 agent 架构和共享资源边界正确，能说明 workspace/MCP/OAuth/调度，但 workspace proxy、MCP proxy 可序列化边界、RPC 权限隔离、实时 revision 或认证路由中缺一到两个关键工程机制。
- 60-74 分弱答案：只是多 chat id 或父级索引设计，缺完整共享 workspace/MCP 代理或访问门禁，可能让浏览器/子会话绕过父级直接执行 raw tool。
- 0-59 分不合格：多个会话共享同一消息数组、无独立 agent 上下文、无共享/私有状态边界、无访问控制或直接暴露 raw MCP/file IO。

## 分数上限规则

- 如果 workspace proxy 没有覆盖 Think built-in tools 与 codemode/shell state 所需的完整文件接口，包括字节读写、append/exists/lstat、cp/mv、symlink/readlink、search/diff 或等价能力，总分通常不得高于 82 分。
- 如果方案把包含 execute 函数闭包的 AI SDK ToolSet 直接跨 Durable Object RPC 传给子会话，而没有可序列化 tool descriptor + 子侧 wrapper + 父侧 callTool 的边界，总分通常不得高于 80 分。
- 如果方案没有区分浏览器 callable 与父子内部 RPC，导致 raw MCP invocation 可能被前端绕过 agent 生命周期调用，总分通常不得高于 82 分。
- 如果方案没有 workspace revision/onChange 或等价单调变更广播，无法说明文件浏览器和多标签页何时刷新，总分通常不得高于 84 分。
- 如果方案没有说明 Worker 路由如何绑定认证身份并拒绝伪造 userId/sessionId，总分通常不得高于 82 分。
- 如果同时缺失完整 workspace proxy、可序列化 MCP 工具代理、RPC/callable 权限隔离、认证路由绑定中任意两个关键机制，总分不得高于 78 分；缺失三个或更多时，总分不得高于 72 分。
