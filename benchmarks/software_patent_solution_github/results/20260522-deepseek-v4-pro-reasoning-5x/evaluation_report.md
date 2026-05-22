# 软件专利技术方案 benchmark 批量评估报告

- 批次：`20260522-deepseek-v4-pro-reasoning-5x`
- 重复次数：`5`

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 001 | 5 | 5 (100%) | 5 | 72.4 | 68 | 78 | 3.2 | scored:5 |
| 002 | 5 | 5 (100%) | 5 | 75.6 | 72 | 78 | 2.24 | scored:5 |
| 003 | 5 | 5 (100%) | 5 | 81.4 | 79 | 84 | 1.74 | scored:5 |
| 004 | 5 | 5 (100%) | 5 | 87.4 | 82 | 95 | 4.92 | scored:5 |
| 005 | 5 | 5 (100%) | 5 | 70.6 | 68 | 75 | 3.2 | scored:5 |
| 006 | 5 | 5 (100%) | 5 | 85 | 76 | 91 | 5.37 | scored:5 |
| 007 | 5 | 5 (100%) | 5 | 82.8 | 80 | 88 | 2.71 | scored:5 |
| 008 | 5 | 5 (100%) | 5 | 85.4 | 80 | 89 | 3.67 | scored:5 |
| 009 | 5 | 5 (100%) | 5 | 81.8 | 78 | 86 | 3.25 | scored:5 |
| 010 | 5 | 5 (100%) | 5 | 79.6 | 78 | 82 | 1.96 | scored:5 |

## 逐项结果

### Case 001

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：78, 72, 68, 72, 72
- 主要扣分点：
  - UI 集成不足，未具体说明 conversation list、chat workspace、session branding、settings/onboarding、terminal view 如何基于 OpenCode runtime kind 和能力字段展示或禁用入口。
  - continue 失败处理主要覆盖未配置命令时的 501，缺少 binary missing、provider/model/session missing 或本地命令非零退出等诊断性失败分类。
  - continue 方案只有统一 501，不包含 OpenCode 命令发现、命令替身、条件满足时的继续路由，也缺少 binary missing、provider missing、session missing 等可诊断失败分类。
- 主要缺失机制：
  - API 契约同步：openapi.json、/api/index、相关 CLI/MCP 客户端、前端 store sessionKind 类型和测试应同步包含 opencode。
  - OpenAPI/API index/schema 与前端能力字段同步。
  - OpenAPI、/api/index、MCP/CLI schema 对 opencode kind 和真实能力边界的同步更新。

### Case 002

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：78, 78, 72, 75, 75
- 主要扣分点：
  - MCP 外部访问只泛泛提到注册查询工具，缺少 run list/detail/provenance/eval attach/leaderboard 等具体工具面。
  - Run 主要被设计为跨表传播的标识符，缺少结构化 provenance/lineage 字段和生成流程，不能充分说明一次执行从哪个触发源、输入摘要、输出摘要、artifact 或外部调用演化而来。
  - Run 生命周期主要依赖 agent_output 之后异步抽取，缺少在 spawn 创建、运行中、完成、失败、取消路径自动创建和更新 run 的桥接机制，因此 failed/timeout/cancelled 的状态来源不稳。
- 主要缺失机制：
  - MCP server 工具层暴露 run list/detail/provenance/eval attach/leaderboard，而不只是 REST API。
  - MCP server 工具层的 run 查询、provenance 查询、eval 附着和 leaderboard 访问能力。
  - MCP 工具层的具体 run list、run detail、provenance 查询、eval 附着和 leaderboard 工具定义。

### Case 003

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：82, 79, 84, 80, 82
- 主要扣分点：
  - Git 操作偏 GitHub Contents API/默认分支文件操作，未充分说明本地 Git-native repo 的 pull/add/commit/push、分支、远端、工作区未提交变更处理，跨 GitLab/Gitea/本地仓库可携带性不足。
  - outbox 表和同步状态追踪器是可行设计，但未说明如何落到当前 SQLite/migration/route 结构，也未给出事件状态字段、重试次数、last_error、next_retry_at 等持久化细节。
  - 入向同步用 mc_synced_at 判断外部文件是否晚于数据库 updated_at 的逻辑不严谨；外部 agent 修改文件时不一定会更新 MC 写入时间戳，可能导致外部变更被跳过。
- 主要缺失机制：
  - GET/POST 管理 API：启用状态、初始化、状态查询、手动同步、手动重试。
  - GNAP/外部协作镜像仓库的初始化、目录结构创建、初始 Git commit 和远端配置校验流程。
  - Git 侧删除任务文件的入站删除同步策略，以及任务 project_id 变更时旧路径清理和新路径迁移策略。

### Case 004

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：82, 95, 89, 82, 89
- 主要扣分点：
  - RuntimeStore 的接口边界没有完整展开，缺少明确的 getMany、batchUpdate、delete、list/scan 等最小接口定义；检索场景的批量读取只被隐含在 ContextReader 中，没有专门说明。
  - SQLite 作为实现选择可行，但当前项目依赖和架构说明中没有 SQLite 基础设施，落地时需要补充依赖、初始化、测试和打包策略。
  - SignalStore 最小接口缺少 update/updater、batchUpdate、list/scan 等能力；后文要求孤儿扫描和批量衰减，但接口只列出 get、set、getBatch、delete、migrateFromFrontmatter。
- 主要缺失机制：
  - SignalStore 的 list/scan、update、batchUpdate 接口及其失败日志策略。
  - createdAt、updatedAt 的最终字段归属和旧文件迁移优先级规则。
  - memory-symbol-tree、synthesize、dream initializer 等未被完整列入的源码消费/写入路径改造。

### Case 005

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：75, 68, 68, 74, 68
- 主要扣分点：
  - deleteTask 删除关联用户消息和助手消息的设计风险较高，可能破坏 Session 的树状历史和普通聊天可见性；参考边界更倾向于删除 submission 记录而不是默认删除已应用会话消息。
  - reset/clear conversation 与任务 ledger 的同步处理不足。只泛泛引用 TurnQueue generation stale，没有说明清空会话时如何批量标记未执行或不可恢复 submission。
  - reset/clear conversation 与任务 ledger 的同步处理只被列为待确认风险，没有给出 skipped/aborted/error 状态同步规则。
- 主要缺失机制：
  - clear/reset conversation 与 submission ledger、active requestId/turnId/recoveryId 的同步规则需要更工程化。
  - completed/aborted/skipped/error 的条件终态更新，防止迟到完成覆盖取消、清理或 reset 后状态。
  - list/delete/inspect API 或等价可观察接口，以及 submission lifecycle observability events。

### Case 006

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：88, 88, 91, 76, 82
- 主要扣分点：
  - intentRef 如何可靠传入 transport 的 onAbort 路径描述偏概念化。若 AI SDK stop、AbortSignal、ReadableStream.cancel 和 React cleanup 共用底层 abort 路径，需要更明确的接口或状态传递方案来避免竞态。
  - request-lifetime 模式下依赖卸载或网络断开时发送取消帧，但浏览器刷新、连接已断开、代理超时等场景下取消帧不一定可靠；方案只在风险中提到，主机制缺少确认、重试或服务端租约类兜底。
  - request-lifetime 模式描述为 WebSocket close 时先发送 CANCEL，但真正的网络断开或 close 事件发生后同一 socket 往往已不可发送，方案缺少可靠替代通道或服务端超时策略。
- 主要缺失机制：
  - chatRecovery/fiber 恢复时的取消状态持久化或校验机制，防止已被用户取消的 turn 在恢复路径中被 continueLastTurn 重新启动。
  - fallback observer 或跨标签观察到的 turn 的统一注册与显式取消入口。
  - hook intentRef 与 transport abort handler 的具体接口，例如 transport.cancelActiveServerTurn()、localDetach() 或 cancelOnClientAbort 策略函数，避免单靠隐含 ref 造成竞态。

### Case 007

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：88, 80, 82, 82, 82
- 主要扣分点：
  - AIChatAgent 适配被描述为后续跟进，缺少与 Think 同等级的执行、恢复、取消和结构化输出适配细节。
  - late live-tail reattach 和父 LLM 轮次恢复主要作为风险列出，没有给出可实施的 V1 以外补偿机制。
  - 实时续接 live-tail 只作为未来能力，V1 标记 interrupted 是可接受边界，但缺少更完整的事件游标或观察者重附加扩展机制。
- 主要缺失机制：
  - AIChatAgent 与 Think 分别在执行、恢复、取消、stream 存储和工具结果生成上的具体适配路径。
  - AIChatAgent 子运行的具体启动、流捕获、stream replay、取消和恢复适配契约。
  - AIChatAgent 子适配器的具体 start/cancel/inspect/getChunks 接入方式，以及与 Think 适配的差异处理。

### Case 008

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：82, 80, 88, 89, 88
- 主要扣分点：
  - CSP 与 new Function 的关系处理不够稳：正文以 new Function 作为执行方式，但限制性 CSP 中未包含 unsafe-eval，后文虽列为风险，仍削弱主方案落地性。
  - agent chat 集成方向偏泛，提到在 AIChatAgent.onChatMessage 中创建 BrowserSandboxExecutor，但浏览器执行器需要 DOM/页面端桥接，未具体落到 useAgentChat/onToolCall/addToolOutput 的现有链路。
  - execution result 消息没有像 tool_call/tool_result/ready/error 一样形成清晰协议字段，最终结果回传描述略散。
- 主要缺失机制：
  - pending tool call 清理机制：超时、iframe load/error、销毁时 pending Promise、调用表、message listener 和 timer 的终态处理。
  - provider/tool 规范化后的映射冲突拒绝策略，尤其是保留名、重复 provider 和 sanitize 后重名。
  - sandbox load error、ready timeout、postMessage/port 发送失败的状态机分支。

### Case 009

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：83, 78, 84, 78, 86
- 主要扣分点：
  - MIME 优先级设计有风险：方案称后端 MIME 可覆盖 magic bytes，但当前需求更适合在 MIME 缺失或泛化时嗅探，并对不可信 MIME 做校验。
  - PDF 处理偏向文本提取或首页渲染为图片，缺少将 PDF/文件作为模型 file-data 内容块传递的明确机制，可能不能满足支持文件内容块的模型路径。
  - PDF 方案偏模糊：一方面提出可选 PDF.js 渲染分页，另一方面回退为完整 base64 PDF JSON，未落到稳定的 file-data 内容块机制。
- 主要缺失机制：
  - AI SDK tool 的 toModelOutput 或等价转换层：text -> model text，image -> text note + image-data，PDF/file -> text note + file-data，binary/error -> 结构化文本。
  - AI SDK 或等价模型输出边界的具体实现：普通 result 中 text/image/file/binary/error 如何转换为 model text、image-data、file-data 或结构化错误。
  - PDF/通用文件内容块传递机制及其 provider 不支持时的降级策略。

### Case 010

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：82, 82, 78, 78, 78
- 主要扣分点：
  - MCP proxy 缺少关键工程边界说明：没有清楚描述父级可序列化 tool descriptors/connection state、子侧按轮构造 AI SDK wrapper、父侧 callTool 执行的链路。
  - MCP 代理只描述 listMcpTools 和 executeMcpTool，缺少可序列化 tool descriptor、子侧 AI SDK wrapper、父侧 callTool 的明确边界，也没有说明如何避免跨 DO 传递带 execute 闭包的 ToolSet。
  - MCP 代理没有形成完整的可序列化 tool descriptor -> 子侧 AI SDK wrapper -> 父侧 callTool 边界，关键 ToolSet 集成被放在待确认问题中。
- 主要缺失机制：
  - MCP 可序列化代理边界：父级返回 tool descriptors，子级构造 AI SDK wrapper，wrapper 调父级 callTool，避免跨 RPC 传递 execute 闭包。
  - MCP 可序列化代理链：父级保存 registry、connection state、tool descriptors；子会话每轮获取 descriptors 构造本地 tool wrappers；模型调用 wrapper 后由父级执行真实 callTool 并返回结果。
  - MCP 可序列化描述符加子侧 wrapper 加父侧 callTool 的工程边界。
