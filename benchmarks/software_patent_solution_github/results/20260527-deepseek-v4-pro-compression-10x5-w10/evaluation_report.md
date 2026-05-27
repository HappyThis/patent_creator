# 软件专利技术方案 benchmark 批量评估报告

- 批次：`20260527-deepseek-v4-pro-compression-10x5-w10`
- 重复次数：`5`
- Subject 模型：`deepseek` / `deepseek-v4-pro`
- Base URL：`https://api.deepseek.com/v1`
- Thinking：`enabled`；reasoning_effort：`high`；max_completion_tokens：`8192`
- 压缩配置：max_tokens=`128000`，threshold_ratio=`0.8`，reserved_output=`8000`，token_char_coefficient=`0.5`
- Runtime：llm_timeout=`45.0`，llm_max_retries=`2`，compression_timeout=`180.0`
- 运行参数：workers=`10`，round_timeout=`900`，judge_timeout=`900`，skip_judge=`False`

## Baseline 对比结论

对比对象：`20260522-deepseek-v4-pro-reasoning-5x`，同为 10 个 case、每个 5 次、共 50 个已评分样本。

总体结论：本次压缩机制在运行稳定性上通过验证，但 benchmark 写作质量没有证明整体提升。当前平均分为 79.86，老 baseline 平均分为 80.20，整体变化为 -0.34。压缩链路本次触发 21 次、完成 21 次、接受 21 次、失败 0 次；429 为 0，timeout 为 0。

| 指标 | 老 baseline | 本次结果 | 变化 |
| --- | ---: | ---: | ---: |
| 总样本 | 50 | 50 | 0 |
| 已评分 | 50 | 50 | 0 |
| 平均分 | 80.20 | 79.86 | -0.34 |
| 最低分 | 68 | 68 | 0 |
| 最高分 | 95 | 93 | -2 |

| Case | 老均分 | 本次均分 | 变化 |
| --- | ---: | ---: | ---: |
| 001 | 72.4 | 73.0 | +0.6 |
| 002 | 75.6 | 76.4 | +0.8 |
| 003 | 81.4 | 82.2 | +0.8 |
| 004 | 87.4 | 85.0 | -2.4 |
| 005 | 70.6 | 74.8 | +4.2 |
| 006 | 85.0 | 82.2 | -2.8 |
| 007 | 82.8 | 81.5 | -1.3 |
| 008 | 85.4 | 84.3 | -1.1 |
| 009 | 81.8 | 78.8 | -3.0 |
| 010 | 79.6 | 80.4 | +0.8 |

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 001 | 5 | 5 (100%) | 5 | 73 | 72 | 77 | 2 | scored:5 |
| 002 | 5 | 5 (100%) | 5 | 76.4 | 72 | 84 | 4.27 | scored:5 |
| 003 | 5 | 5 (100%) | 5 | 82.2 | 79 | 84 | 2.23 | scored:5 |
| 004 | 5 | 5 (100%) | 5 | 85 | 80 | 93 | 4.82 | scored:5 |
| 005 | 5 | 5 (100%) | 5 | 74.8 | 68 | 82 | 4.45 | scored:5 |
| 006 | 5 | 5 (100%) | 5 | 82.2 | 78 | 89 | 4.49 | scored:5 |
| 007 | 5 | 5 (100%) | 5 | 81.5 | 78.5 | 87 | 2.97 | scored:5 |
| 008 | 5 | 5 (100%) | 5 | 84.3 | 82 | 88.5 | 2.86 | scored:5 |
| 009 | 5 | 5 (100%) | 5 | 78.8 | 72 | 84 | 4.66 | scored:5 |
| 010 | 5 | 5 (100%) | 5 | 80.4 | 78 | 82 | 1.5 | scored:5 |

## 逐项结果

### Case 001

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：72, 72, 72, 72, 77
- 主要扣分点：
  - FrameworkAdapter/eventBus 段落更像通用 agent 框架适配，不足以替代 Mission Control 本地 session runtime 的发现、transcript 和 continue 链路。
  - continue 方案只有命令模板和输出捕获，缺少 binary missing、provider/model missing、session missing、命令失败透传等诊断性失败处理。
  - continue 机制仍偏假设性，直接写出 opencode continue --session --prompt，但没有规定本地命令失败时如何透传或分类为 binary missing、session missing、provider/model/auth missing 等可诊断错误。
- 主要缺失机制：
  - /api/sessions/continue 对 opencode 的专属命令构造、可配置 binary、命令替身测试和失败分类。
  - /api/sessions/transcript 对 kind=opencode 的路由、错误返回和不可解析载荷降级。
  - /api/sessions/transcript 对 runtime kind=opencode 的路由接入。

### Case 002

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：84, 72, 78, 74, 74
- 主要扣分点：
  - AgentRun 被定义为关系聚合视图，但没有给出清晰的 canonical run 持久化位置，CREATED/DISPATCHED/IN_FLIGHT/ARCHIVED 等状态缺少可靠落盘字段和终态保护机制。
  - eval 仍主要按 agent/layer 描述，缺少可信的 run 级 attach/update 契约、benchmark 元数据和与单次执行的稳定绑定。
  - eval 附着机制偏向执行完成后内部评估写入，缺少外部评测系统或人工评审对指定 run 附着、更新、标注 benchmark 元数据的契约。
- 主要缺失机制：
  - MCP server 层的 run 查询、provenance 查询、eval attach/update、leaderboard 工具。
  - MCP 工具层和 API 层的具体 run 查询、provenance 查询、评估附着、leaderboard 聚合接口。
  - MCP 工具层的 run list、run detail、provenance、eval attach、leaderboard 能力。

### Case 003

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：79, 84, 84, 84, 80
- 主要扣分点：
  - UI 可见性只提到 conflict/failed 图标，缺少同步状态、最近同步时间、任务数量、手动同步按钮等管理反馈。
  - 任务看板或管理 UI 的可见反馈不够完整，只提到同步状态标记和外部修改提示，没有任务数量、最近同步时间、错误详情、手动同步按钮等闭环。
  - 冲突处理采用数据库覆盖远端的描述较粗，且 git pull --rebase 与本地提交/覆盖顺序容易产生破坏性覆盖风险，需要更明确的非破坏性策略或人工介入状态。
- 主要缺失机制：
  - 仓库初始化 API、状态查询 API、手动同步/重试 API。
  - 仓库初始化流程：创建目录结构、版本文件、agent 描述文件、初始 commit、远端/分支检查。
  - 任务看板或管理 UI 中的同步可见反馈。

### Case 004

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：93, 80, 88, 82, 82
- 主要扣分点：
  - SignalStore 最小接口不完整。文中接口只有 getByPath、save、deleteByPath、migrateFromMarkdown，缺少 getMany、batchUpdate、update(updater)、list/scan 等对批量检索融合、批量访问回写和孤儿记录修复很关键的能力。
  - dream/consolidate/prune/synthesize 的接入只间接出现，未像 search、curate、archive 一样展开流程。
  - sidecar store 的接口边界不够完整，只概括为加载、查询、更新、删除和批量刷盘，未明确 getMany、batchUpdate、list/scan 等最小接口能力。
- 主要缺失机制：
  - SearchKnowledgeService/buildFreshIndex、memory-symbol-tree、dream consolidate/prune/synthesize、archive candidate 判断等路径如何统一改为融合对象的更具体接入细节。
  - dream、prune、synthesize、consolidate 等自动整理路径中哪些操作只更新 sidecar、哪些操作更新 Markdown 的更细流程边界。
  - sidecar failure logging 和可观测性设计。

### Case 005

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：74, 82, 75, 68, 75
- 主要扣分点：
  - EXECUTING 恢复规则偏乐观，依赖“检查是否存在完整 assistant 回复”或“根据 parent_id 判断”重新推理，缺少 requestId、streamId、fiber/recovery 证据绑定和无法安全恢复时标错的规则。
  - reset/clear conversation 与外部任务 ledger 的同步处理不足，只泛泛提到 queued turn stale，没有说明 pending/running 提交在 reset 后应如何标记 skipped/aborted/error 并防止恢复重放。
  - 取消 pending 任务的语义不够具体；通过 requestId/AbortRegistry 更适合 running turn，但 received/pending 尚未生成有效 requestId 时如何阻止后续执行说明不足。
- 主要缺失机制：
  - accepted 后可靠唤醒后台 drain 的具体机制，以及 pending -> running 与 requestId/turnId 绑定。
  - completed/error/aborted/skipped 的条件终态更新，避免迟到结果覆盖已取消或已跳过任务。
  - durable messagesAppliedAt、applied version、applied message ids 或等价的会话应用 checkpoint。

### Case 006

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：78, 89, 78, 80, 86
- 主要扣分点：
  - request-lifetime 模式中标签页关闭或组件卸载发送取消的可靠性没有展开，容易受 WebSocket 关闭时机限制。
  - request-lifetime 模式把组件 cleanup/WebSocket 断开映射为客户端自动发送取消，但浏览器刷新或断网时客户端发送 cancel 的可靠性和服务端 close-side 策略没有展开。
  - turnOwnership 配置、ownership 上下文、connectionId 取消字段等是合理设计扩展，但没有精确定义如何落到现有 WebSocketChatTransport/useAgentChat API 和协议类型。
- 主要缺失机制：
  - active request id 在 local detach、explicit cancel、done/replayComplete、late chunk 场景下的保留与清理规则。
  - fallback/observed server turn 的 active id 注册、取消入口和多标签权限/竞态处理。
  - observed/fallback stream 的取消登记机制，以及跨标签场景下某一标签关闭不影响其他观察者的精确规则。

### Case 007

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：80, 82, 87, 78.5, 80
- 主要扣分点：
  - Think 与 AIChatAgent 的适配边界说明不够严谨：一方面声称四种组合均可用，另一方面又说 AIChatAgent 后续补齐，缺少分别的执行、恢复、取消差异。
  - 基本只按 Think/HelperAgent 路径展开，未分别说明 AIChatAgent 的执行、恢复、取消、流式 chunk 读取与适配边界，也没有说明混合 Think/AIChatAgent 组合。
  - 完全缺失 headless 子 agent 中浏览器 client tools 不可用、降级或父侧中介执行策略，触发 rubric 上限。
- 主要缺失机制：
  - AIChatAgent 与 Think 分别作为父/子时的具体适配差异、阶段边界和恢复/取消语义。
  - AIChatAgent 子运行的输入消息、流式输出、保存消息、恢复和取消适配细节。
  - AIChatAgent 适配方案，包括其 onChatMessage/saveMessages、流存储和取消恢复如何接入 retained child run。

### Case 008

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：82, 88.5, 82, 82, 87
- 主要扣分点：
  - CSP 与代码注入表述存在张力：一方面说禁止 eval，另一方面又说 new Function；还提到 iframe 中 importScripts，普通 iframe window 并不支持该 API。
  - pending tool calls 的终止语义不够完整：超时或 iframe 销毁时如何 reject 未完成的 tool_call Promise、移除 listener、清空 pending call map 没有展开。
  - 与 agent chat/client tool 的协作还不够闭环：现有 createCodeTool 的 execute 语义偏服务端执行，浏览器侧 executor 如何通过 onToolCall/addToolOutput 或专门 client-side code tool 回传结果没有具体到可接入流程。
- 主要缺失机制：
  - execution-result 中 result、error、logs 的统一结构，以及 iframe 内 console.log/warn/error 捕获。
  - provider 与 tool name 在消息协议中的分离传递，或与现有 ToolProvider/ResolvedProvider 完全一致的路由结构。
  - 主页面侧强制终止执行实例的路径，用于处理同步阻塞或 sandbox 未能主动返回的情况。

### Case 009

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：78, 84, 84, 72, 76
- 主要扣分点：
  - MAX_READABLE_SIZE 默认 10 MB、MAX_IMAGE_SIZE 默认 20 MB 的顺序存在逻辑含混；若先检查总上限，图片专项上限实际不会生效。
  - PDF 只返回结构化描述并预留扩展，没有把 PDF 字节转换为模型可消费的 file 内容块，未满足图片/PDF 都应尽量传入模型的需求。
  - PDF 处理中提到页数、首页光栅化和原始 PDF 内容块，但缺少当前项目可落地的转换失败路径、依赖边界和能力探测细节。
- 主要缺失机制：
  - AI SDK tool toModelOutput 或等价模型输出转换层：text -> model text，image -> image-data，PDF/file -> file-data，binary/error -> 结构化文本。
  - PDF 或其他可传递文件的 file-data 内容块生成机制，而不只是 PDF 元信息摘要。
  - PDF/可传递文件到模型 file-data 内容块的转换路径。

### Case 010

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：80, 82, 82, 80, 78
- 主要扣分点：
  - MCP 代理偏向单个通用 mcp_proxy 工具，未明确每轮从父级获取可序列化 tool descriptors，并在子侧构造逐工具 AI SDK wrapper 后由父级 callTool 执行。
  - MCP 工具代理没有清楚说明可序列化 tool descriptor、子侧 wrapper、父侧 callTool 的边界，只是笼统说返回工具定义并 invoke。
  - MCP 工具获取流程称父级直接返回 AI SDK ToolSet 给子会话并注入 streamText，缺少可序列化 tool descriptor、子侧 wrapper、父侧 callTool 的明确边界，存在跨 Durable Object RPC 传递 execute 闭包的实现风险。
- 主要缺失机制：
  - MCP 可序列化工具描述、子侧逐工具 wrapper、父侧 callTool 的清晰边界。
  - MCP 工具描述的序列化 schema、子侧 AI SDK wrapper 构造方式、父侧 callTool 执行和错误返回规范。
  - MCP 工具的可序列化描述边界：父级返回 descriptor，子级构造 wrapper，实际 execute 再走父级 callTool。
