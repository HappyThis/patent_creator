# 参考技术方案

## 方案概述

该方案面向同一用户下多个 agent 会话共享长期资源的场景，提出一种父目录 agent 与子会话 agent 分层协作机制。父目录 agent 作为用户级实体，维护会话索引、共享 workspace、共享 MCP 连接、OAuth 回调、定时任务和客户端广播；每个聊天会话作为子 agent，保留独立消息、记忆、配置和扩展。子会话通过受控代理访问父级共享资源，从而在隔离会话上下文的同时实现用户级文件和工具连接共享。

## 核心组件

1. 用户级父目录 agent

系统为每个认证用户创建一个父目录 agent。父目录维护聊天会话列表、标题、更新时间、预览信息和子会话注册表，并对外提供创建、重命名、删除和列出会话的接口。父目录也是共享资源的唯一真实持有者。

2. 子会话 agent

每个聊天会话对应一个子 agent 实例，拥有独立 Durable Object 存储、消息历史、分支、记忆、扩展和会话配置。浏览器切换会话时连接对应子 agent，但该子 agent 的 workspace 和 MCP 工具可通过代理访问父目录资源。

3. 严格注册表访问门禁

父目录在转发子 agent 请求前检查目标会话是否存在于其子会话注册表中。只有通过父目录创建的子会话才允许访问；未知会话 id 在唤醒子 agent 前即被拒绝，防止猜测 id 导致越权访问或意外创建。

4. 共享 workspace 代理

父目录持有真实 workspace。子会话把自身 workspace 字段替换为代理对象，该代理实现 workspace 所需文件接口，并将 read、write、list、grep、mkdir、rm、字节读取、复制移动等操作通过父子 agent RPC 转发给父目录。内置 workspace 工具和代码执行沙箱的 state 文件接口都基于该代理工作。

5. 共享 MCP 代理

父目录持有 MCP 服务器注册表、OAuth 凭据、连接状态和工具描述。子会话在每轮推理前向父目录请求当前可用 MCP 工具描述，并构造本轮工具集；当模型调用 MCP 工具时，子会话通过代理请求父目录执行真实 MCP 调用，再把结果返回给模型。

6. 跨会话客户端同步

父目录监听共享 workspace 的文件变化，并向连接到父目录的所有客户端广播变化事件。前端会话列表 hook 维护一个 workspace revision 计数，多个打开标签页或不同会话面板收到广播后刷新文件浏览器或相关视图。

7. 父级定时任务调度

由于子会话是父目录下的 facet 或等价子实体，跨会话定时任务由父目录统一调度。父目录触发任务后，根据会话更新时间或策略选择目标子会话，通过 RPC 注入摘要、提醒或其他后台任务。

## 执行流程

1. 用户完成认证后，请求被路由到该用户的父目录 agent。
2. 前端连接父目录获取会话列表和共享资源状态。
3. 用户创建会话时，父目录生成会话 id，创建子 agent，并写入会话元数据。
4. 用户切换会话时，前端通过父目录的子 agent 路由连接目标子会话。
5. 子会话处理聊天请求时，保留自己的消息、记忆、分支和扩展。
6. 子会话调用文件工具时，请求进入共享 workspace 代理，由父目录执行真实文件操作。
7. 子会话每轮推理前从共享 MCP 代理获取父目录可用 MCP 工具，并把工具调用转发回父目录执行。
8. 父目录接收到文件变化后广播事件；前端根据 revision 更新文件浏览器。
9. 父目录定时任务触发时，选择最近活跃或符合条件的子会话并发送后台提示。
10. 删除会话时，父目录删除子会话和会话元数据，但保留用户级共享 workspace 和 MCP 连接，除非用户显式清理。

## 隔离与共享边界

- 每个子会话独立保存消息历史、响应分支、记忆块、扩展和会话配置。
- 父目录统一保存用户级 workspace、MCP 服务器连接、OAuth 凭据、会话索引和跨会话调度状态。
- 子会话不能直接任意访问父目录内部状态，只能通过 workspace 代理和 MCP 代理暴露的受控方法访问。
- 浏览器可调用的接口仅限会话管理和 MCP 管理等用户级操作；文件 IO 和 MCP 工具执行等模型工具路径不直接作为浏览器 callable 暴露，避免绕过 agent 生命周期钩子。

## 技术效果

该方案使同一用户的多会话 assistant 同时具备“会话级隔离”和“用户级资源连续性”。用户可以在多个聊天中围绕同一项目文件和同一组外部工具继续工作，而不会混淆各会话消息历史；系统通过父目录集中管理共享资源、访问门禁和定时任务，降低状态分散和越权访问风险。

## 目标能力边界

必须解决的是“同一用户多个 chat session 的隔离上下文与共享长期资源之间的协同”。会话消息、分支、记忆、扩展和配置应保留在子会话；workspace、MCP server registry、OAuth token、跨会话 schedule 和客户端同步信号应由用户级父目录持有。

方案不是简单在一个 agent 里加多个 chat id，也不是让所有会话共享同一消息表。高分答案会明确父子 Durable Object 或等价分层实体，并说明哪些能力共享、哪些能力隔离。

## 核心数据结构与状态模型

父目录 agent 应维护：

- 会话索引：`chatId`、`title`、`createdAt`、`updatedAt`、`lastMessagePreview`。
- 子会话注册表：由父级创建/删除，用于路由门禁。
- 共享 workspace 实例和文件变化广播计数。
- MCP server registry、OAuth credentials、connection state、tool descriptors。
- 父级 schedule 记录和目标会话选择策略。

子会话 agent 应维护：

- 自身消息历史、分支、memory/context、extensions、agent config。
- workspace proxy 和 MCP proxy，而非真实共享资源状态。
- 对父目录的 best-effort `recordChatTurn` 或等价 preview 更新。

关键状态：

- chat exists in registry / missing / deleted。
- MCP server connecting / connected / auth_required / error。
- workspace revision monotonically increasing for UI refresh。
- schedule fired / target selected / child prompt posted。

## 共享代理细节

Workspace proxy 应覆盖 Think built-in tools 和 codemode `state.*` 所需的文件接口，包括文本读写、字节读写、append、exists、stat/lstat、mkdir、readDir、rm、cp、mv、symlink/readlink、glob 等。代理调用父目录真实 workspace，父级单线程语义自然串行化同一路径写入。

MCP proxy 应在每轮推理前从父目录获取当前工具描述，构造 AI SDK tool set。模型调用工具时，子会话通过父级执行真实 MCP call。浏览器不应直接 callable 这个 raw MCP tool invocation，否则会绕开 beforeToolCall/afterToolCall 等 agent 生命周期钩子。

## 访问控制与同步细节

- Worker 根据认证用户只路由到该用户的父目录 agent。
- 父目录 `onBeforeSubAgent` 或等价门禁检查 child class/name 是否存在于注册表，未知 id 直接 404。
- 删除会话应删除 child facet 和元数据，但不默认删除用户级 workspace/MCP。
- workspace onChange 由父目录 broadcast 给所有连接到目录的客户端；前端用 revision 触发文件浏览器刷新。
- 父级定时任务应选择最近活跃或策略指定子会话执行，不应让每个子会话各自 schedule 同一全局任务。

## 项目集成点

方案应连接 agent sub-routing、认证 Worker 路由、React `useAgent` sub 参数、workspace/shell 文件接口、Think workspace tools、codemode state backend、MCP callback handling、client chat sidebar hook 和 schedule 机制。只实现前端侧边栏不构成完整技术方案。

## 必须命中的评分锚点

- 用户级父目录 + 会话级子 agent 的分层架构。
- 会话私有状态与用户共享资源边界清晰。
- 共享 workspace 通过代理进入子会话和 codemode。
- 共享 MCP/OAuth 由父级持有，子级按轮获取工具和转发调用。
- 严格注册表门禁防止猜测 chat id。
- workspace 变化广播到多标签页/多会话 UI。
- 父级负责跨会话 schedule。

## 常见错误方案

- 多个会话只是同一个 agent 表里的不同 `chatId`，没有独立子 agent 上下文。
- workspace 和 MCP 都放在每个子会话里，导致用户级资源不连续。
- 所有状态都放父级，导致消息历史、记忆和扩展互相污染。
- 浏览器可直接调用父级 MCP tool execution，绕开 agent 工具生命周期。
- 无注册表门禁，访问未知 child id 会隐式创建或唤醒子会话。

## 与源码实现的对应参考

该参考方案对应 `cloudflare/agents` 的 `feat(example): multi-session assistant — sub-agent routing, shared workspace, shared MCP (#1384)`：

- 方案提交：`a7059d4a5a1071a10c60be0e777968fc7ff5d36c`
- 快照提交：`d31ec18dd70500bccda7944c63b70ef9fad49bda`
- 关键能力：用户级 `AssistantDirectory`、子会话 `MyAssistant` facet、`onBeforeSubAgent` 注册表门禁、共享 workspace 代理、`WorkspaceFsLike`、共享 MCP 代理、workspace change broadcast、父级 schedule。
