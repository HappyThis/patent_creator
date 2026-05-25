# 软件专利技术方案 benchmark 批量评估报告

- 批次：`20260525-main-prompt-complex-5cases-5x`
- 重复次数：`5`

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 004 | 5 | 5 (100%) | 5 | 88.4 | 82 | 95 | 4.13 | scored:5 |
| 005 | 5 | 5 (100%) | 5 | 76.2 | 75 | 78 | 1.47 | scored:5 |
| 006 | 5 | 5 (100%) | 5 | 87 | 84 | 94 | 3.79 | scored:5 |
| 007 | 5 | 5 (100%) | 5 | 77.6 | 74 | 80 | 2.94 | scored:5 |
| 010 | 5 | 5 (100%) | 5 | 82.8 | 78 | 88 | 3.71 | scored:5 |

## 逐项结果

### Case 004

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：89, 88, 82, 95, 88
- 主要扣分点：
  - IScoringProvider 接口列出了 get/record/batch，但没有把后文依赖的 removeEntry、migrateEntry、list/scan、batchUpdate 或 updater/CAS 作为明确最小接口，生命周期边界略松。
  - JSON sidecar 全量加载和 debounced 异步刷盘可行，但对崩溃窗口、跨进程文件锁、写入失败后的重放语义还不够工程化。
  - createdAt/updatedAt 被完全归为动态信号层有争议；这些字段在知识生命周期中也可能属于可审阅的语义/内容变更元数据。
- 主要缺失机制：
  - curate ADD/UPDATE/MERGE、MarkdownWriter.generateContext/mergeContexts、memory symbol tree 和 manifest 读取融合的具体改造流程不足。
  - dream、synthesize、prune、memory-symbol-tree 等自动整理和展示路径的明确读取融合接入点。
  - manifest、memory symbol tree、archive candidate selection 等所有现有 frontmatter scoring 消费路径的具体融合改造点。

### Case 005

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：75, 75, 78, 75, 78
- 主要扣分点：
  - QUEUED 状态被列出但执行领取流程直接从 RECEIVED 到 RUNNING，排队状态和并发限制的状态转换不一致。
  - accepted -> running 的描述存在危险顺序：称状态更新发生在消息注入之后、推理之前；若消息注入后但 running 更新前崩溃，恢复扫描会把 accepted 当作未注入而重复执行。
  - reset/clear conversation 与任务 ledger 的同步处理不足：只提到 generation 失败标记 SKIPPED，但状态机未包含 SKIPPED，也未覆盖 pending/received/running 任务的批量同步规则。
- 主要缺失机制：
  - Think 现有执行入口的具体复用方式：应说明后台 drain 如何调用 saveMessages 或等价内部路径，并继承 waitUntilStable、turn queue、stream/recovery/cancel 语义。
  - clear/reset conversation 时同步更新 submission/task ledger 的完整规则。
  - completed/failed/aborted/skipped/error 的条件终态更新规则，防止迟到完成覆盖取消、删除或 reset 结果。

### Case 006

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：85, 94, 88, 84, 84
- 主要扣分点：
  - durable 模式下本地清理是否删除或保留 activeRequestIds 表述不够一致，可能影响迟到 done/chunk 过滤和后续显式取消。
  - fallback/observed turn 的取消能力不足。方案描述了跨标签观察，但没有说明非当前 transport stream 创建或已 detached 的 observed turn 如何注册 active id 并被显式 stop 取消。
  - request-lifetime 模式下将 WebSocket 断开、ReadableStream.cancel 与服务端取消的触发链描述得较直接，实际实现时还需要明确 onClose、stream cancel、caller abort 三者各自的触发点，避免把网络波动误归为用户取消。
- 主要缺失机制：
  - durable local cleanup、explicit cancel、request-lifetime abort、late done/chunk 的完整行为矩阵和精确状态迁移。
  - fallback observer / cross-tab observed stream 与显式取消入口的登记和取消路径。
  - fallback/observed turn 注册到统一取消入口的具体客户端状态结构。

### Case 007

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：74, 80, 80, 74, 80
- 主要扣分点：
  - AIChatAgent 只作为待确认风险提及，没有分别说明 AIChatAgent 的执行、恢复、取消、流式 chunk 读取和适配边界，难以满足 chat/Think 兼容性的高分要求。
  - 事件协议只列 started/chunk/finished/error，没有独立 aborted/interrupted 事件语义；清理 retained run 时也未明确 running 子运行应先取消再删除。
  - 事件持久化描述为子 DO 通过 DO 之间的“存储绑定”写入主 DO 事件日志，缺少当前项目或 Durable Object API 层面的明确依据。
- 主要缺失机制：
  - AIChatAgent 与 Think 分别适配的工程边界，尤其是 AIChatAgent 的 programmatic turn、恢复 chunk 获取、取消信号和 final result synthesis。
  - AIChatAgent 与 Think 子适配的具体执行、恢复和取消差异。
  - AIChatAgent 与 Think 的具体 child adapter：start、inspect、stored chunks replay、cancel、summary/output extraction。

### Case 010

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：80, 88, 78, 82, 86
- 主要扣分点：
  - MCP 代理机制较接近目标，但没有明确每轮推理前从父级获取当前可序列化 tool descriptors 并在子侧构造 wrapper；“创建时传递 schema 列表”不足以处理连接状态和工具动态变化。
  - MCP 代理没有明确可序列化 tool descriptor、子侧 wrapper、父侧 callTool 的边界，也没有说明子会话每轮推理前获取工具描述并构造工具集；仅描述工具调用时转发给父级，工程边界不够细。
  - MCP 代理没有说明子会话每轮获取可序列化 tool descriptors、构造子侧 wrapper、再由父侧 callTool 执行的工程边界。
- 主要缺失机制：
  - MCP proxy 缺少可序列化工具描述、子侧工具 wrapper 和父侧真实 callTool 的边界设计。
  - MCP tool descriptor 的按轮同步、可序列化边界、子侧 wrapper 与父侧 callTool/invoke 的严格链路。
  - MCP 可序列化工具描述边界：父级保存 tool descriptors，子会话按轮获取 descriptor，子侧构造 wrapper，模型调用后通过父侧 callTool 执行真实 MCP 调用。
