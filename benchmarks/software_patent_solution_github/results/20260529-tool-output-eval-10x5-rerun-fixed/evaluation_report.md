# 软件专利技术方案 benchmark 批量评估报告

- 批次：`20260529-tool-output-eval-10x5-rerun-fixed`
- 重复次数：`5`
- Subject 模型：`deepseek` / `deepseek-v4-pro`
- Base URL：`https://api.deepseek.com/v1`
- Thinking：`enabled`；reasoning_effort：`high`；max_completion_tokens：`8192`
- 压缩配置：max_tokens=`128000`，threshold_ratio=`0.8`，reserved_output=`8000`，token_char_coefficient=`0.5`
- Runtime：llm_timeout=`45.0`，llm_max_retries=`2`，compression_timeout=`180.0`
- 运行参数：workers=`mixed(original=10, rerun_batch=4, rerun_single=1)`，round_timeout=`1800`，judge_timeout=`900`，skip_judge=`False`

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 001 | 5 | 5 (100%) | 5 | 73.6 | 72 | 80 | 3.2 | scored:5 |
| 002 | 5 | 5 (100%) | 5 | 75.6 | 72 | 82 | 3.88 | scored:5 |
| 003 | 5 | 5 (100%) | 5 | 82.2 | 80 | 84 | 1.83 | scored:5 |
| 004 | 5 | 5 (100%) | 5 | 89.6 | 89 | 92 | 1.2 | scored:5 |
| 005 | 5 | 5 (100%) | 5 | 72.6 | 68 | 78 | 3.98 | scored:5 |
| 006 | 5 | 5 (100%) | 5 | 85.2 | 78 | 89 | 3.97 | scored:5 |
| 007 | 5 | 5 (100%) | 5 | 80 | 74 | 82 | 3.1 | scored:5 |
| 008 | 5 | 5 (100%) | 5 | 84.4 | 82 | 88 | 2.24 | scored:5 |
| 009 | 5 | 5 (100%) | 5 | 83 | 78 | 91 | 4.82 | scored:5 |
| 010 | 5 | 5 (100%) | 5 | 81.2 | 78 | 82 | 1.6 | scored:5 |

## 逐项结果

### Case 001

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：72, 72, 80, 72, 72
- 主要扣分点：
  - API 契约同步不足，未说明 OpenAPI、API index/schema、前端类型或测试夹具如何同步新增 opencode 能力字段。
  - UI 集成主要停留在会话列表品牌和按钮，未充分落到 conversation list/chat workspace/session detail 的完整路径。
  - continue 失败处理只说通用错误处理，缺少 binary missing、provider/auth/model/session missing 等可诊断失败边界。
- 主要缺失机制：
  - /api/pty/attach、TerminalView/chat workspace 对 OpenCode PTY/terminal 不支持的显式禁用或错误返回。
  - API contract synchronization across OpenAPI/API index/schema and frontend session capability fields.
  - API/E2E tests covering session list, transcript, continue success/failure, and PTY unsupported behavior with real fixtures and binary stub.

### Case 002

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：72, 78, 82, 74, 72
- 主要扣分点：
  - MCP 工具层设计不足，只说 MCP 工具可通过 API key 访问，没有给出 run list/detail/provenance/eval attach/leaderboard 等 MCP 工具契约。
  - eval 附着主要依赖 agent_name + workspace_id + 时间窗口，缺少 run 级 eval attach/update 契约、pass/fail/benchmark 元数据与稳定 run_id 绑定，方案自己也承认时间窗口可能不准。
  - eval 附着设计存在边界不清：一处说 eval_results 查询时 JOIN 聚合，另一处说同步生成不可变快照；当前 eval_runs 也缺少 run_id，方案没有完整说明迁移、版本或 attach/update 语义。
- 主要缺失机制：
  - /api/v1/runs、run detail、run provenance、run eval attach、run stream 等稳定 API，及对应 MCP server 工具。
  - AgentRun/RunRecord 等等价的统一运行对象，至少包含 runId、agentId、taskId、sessionId、spawnId、状态、时间、结果、错误、成本、tokenUsage、metadata。
  - MCP server 中面向 run list/detail/provenance/eval attach/leaderboard 的工具入口。

### Case 003

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：84, 84, 80, 83, 80
- 主要扣分点：
  - 下行同步采用最后写入者胜出，虽然可实施，但对外部 agent 与 Mission Control 并发修改的非破坏性冲突保护不足。
  - 与当前源码接入点结合不足，只泛称同步模块、事件总线和项目级配置，没有落到现有任务创建/更新/删除 API、配置模块、GitHub sync 相关模块或 UI 面板边界。
  - 任务看板或管理 UI 可见反馈不足，没有说明同步状态、任务数量、最近同步时间、错误信息或手动触发入口如何展示。
- 主要缺失机制：
  - 任务看板或管理 UI 中的同步可见反馈。
  - 任务看板或管理 UI 中的同步状态、最近同步时间、错误和手动同步可见入口。
  - 任务看板或管理 UI 可见反馈：同步状态、错误提示、手动同步按钮或任务级镜像状态。

### Case 004

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：89, 89, 89, 89, 92
- 主要扣分点：
  - JSON 文件内 version 字段的乐观锁描述不够可实施；没有文件锁、CAS 存储或串行 updater 时，读版本后再 rename 仍可能发生丢失更新。
  - content_hash 不匹配时直接丢弃信号并回退默认值，可能损失本机累计信号；自愈/重算规则还不够明确。
  - sidecar store 仍偏具体为单一 _scoring.json 文件，未抽象出完整最小接口边界，例如 getMany、update/updater、batchUpdate、delete、list/scan 的清晰 API 契约。
- 主要缺失机制：
  - manifest 构建、memory-symbol-tree、source/shared origin、多种删除/重命名场景的统一融合和清理策略。
  - manifest、memory symbol tree、dream consolidate/prune/synthesize、archive candidate selection 等路径统一通过融合对象读取信号的接入设计。
  - service initializer 或启动迁移钩子，确保新版本一开始就创建 sidecar 并切换新写入路径。

### Case 005

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：68, 68, 78, 74, 75
- 主要扣分点：
  - cleanup 只在风险中提出幂等键保留窗口，没有形成明确 API 或状态策略。
  - reset/clear conversation 与 submission ledger 的同步处理不足，只提到 TurnQueue generation 跳过，但没有把未执行或不可恢复任务持久标记 skipped/aborted/error。
  - reset/clear conversation 与 submission ledger 的同步处理缺失；cleanup 还提出删除已写入 Session 的用户/助理消息，这与参考边界中“删除 submission 通常不应删除已写入 Session 消息，除非显式策略”存在风险。
- 主要缺失机制：
  - clear/reset conversation 与 submission ledger 的同步 skipped/aborted/error 规则。
  - clear/reset conversation 与 submission 状态的同步规则。
  - list/inspect API、observability lifecycle events、幂等键保留窗口和自动清理策略。

### Case 006

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：84, 89, 88, 78, 87
- 主要扣分点：
  - cancelIntent 依赖 stop 前设置同步标记来区分 reader.cancel 来源，方案自己也列为待确认，说明实现边界尚未完全闭合。
  - durabilityMode 主要描述 WebSocket close 和 beforeunload，但没有说明如何改造当前 reader.cancel、AI SDK stop abortSignal、stream.cancel 这些真正会触发取消帧的核心路径。
  - late chunk/done 过滤主要依赖 activeRequestIds 的概念性描述，缺少更明确的状态机或 terminal/cancel_requested 后的处理规则。
- 主要缺失机制：
  - attached local stream cleanup 与 active server turn id 的分离登记，支持本地 reader/listener 释放但保留服务端 turn。
  - fallback/observed turn 的注册与取消入口，尤其是非当前 attached stream 但由 hook 观察到的服务端 turn。
  - observed turn id 与 local transport request id 的区分和登记规则，尤其是 cross-tab STREAM_RESUMING/broadcastTransition 路径下如何让 stop 找到正确 requestId。

### Case 007

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：82, 74, 82, 82, 80
- 主要扣分点：
  - AIChatAgent 适配表述前后不一致：前文说框架已为 Think 和 AIChatAgent 提供内部适配器、子类自动符合，后文又说 AIChatAgent 适配器尚未实现；且缺少 AIChatAgent 执行、恢复、取消的具体差异说明。
  - Think 与 AIChatAgent 的适配边界只说 AIChatAgent 是后续里程碑，没有分别说明两者在执行、恢复、取消、chunk 读取上的具体 adapter 差异。
  - 客户端层只描述接口和去重，没有形成当前项目期望的聚合 reducer/hook、按 parentToolCallId 与 displayOrder 稳定渲染的机制。
- 主要缺失机制：
  - AIChatAgent 与 Think 分别接入 start/inspect/chunks/cancel/recovery 的具体适配机制
  - AIChatAgent 适配器的具体协议接入点、恢复读取和取消传播细节。
  - Think 与 AIChatAgent adapter 的分阶段实现边界及各自恢复/取消细节。

### Case 008

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：88, 85, 85, 82, 82
- 主要扣分点：
  - cleanup 只写移除 iframe 和释放内存，没有明确移除 message listener、清理 timer、reject/释放 pending tool calls 和调用映射。
  - iframe 加载失败、ready 超时、postMessage 发送失败等非代码异常状态没有像执行错误一样细化。
  - iframe+Web Worker 双层隔离可行但实现复杂，部分 CSP、blob/data URL、Worker 创建和动态代码执行约束需要更精确的工程设计。
- 主要缺失机制：
  - event.source === iframe.contentWindow 的消息来源校验，以及 opaque origin 场景下的校验策略。
  - execution-result 终态消息，包含 result/error/logs，并与 execute 生命周期明确对应。
  - iframe load/error、sandbox unavailable 等初始化失败路径。

### Case 009

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：84, 78, 91, 84, 78
- 主要扣分点：
  - PDF pageCount、PDF 直接 data URI 可消费等能力被作为可选设想提出，但没有说明当前项目中如何可靠获取或适配具体 provider。
  - PDF 与图片的“模型可直接消费”表述偏乐观，缺少对不支持多模态或不支持 PDF 输入模型的工具层降级输出设计。
  - WebP/SVG 等格式在正文识别表中覆盖不足，风险段又提出扩展名回退，弱化了“不只依赖扩展名”的约束。
- 主要缺失机制：
  - AI SDK toModelOutput 或等价的普通结果到 model text/image-data/file-data 的转换层。
  - 不同模型/provider 对 PDF/file part 支持差异的适配表或统一降级封装，而不是仅提示“可能不支持”。
  - 图片/PDF 内容块旁的简短文本说明，用于说明文件路径、媒体类型、大小和来源。

### Case 010

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：78, 82, 82, 82, 82
- 主要扣分点：
  - MCP 代理缺少可序列化 tool descriptor、子侧 AI SDK wrapper、父侧 callTool RPC 的明确边界；当前表述容易被理解为跨 DO 传递 ToolSet/execute 闭包。
  - MCP 代理链还缺少明确的可序列化 tool descriptor、子侧 AI SDK wrapper、父侧 callTool 边界，容易让实现者误传带 execute 闭包的 ToolSet。
  - Workspace proxy 机制不够完整，只写了读写、目录、glob 等概念，没有覆盖 Think workspace tools 和 codemode state backend 需要的完整文件接口。
- 主要缺失机制：
  - MCP 受控代理的序列化边界：父级持有连接和 OAuth，子级每轮获取纯描述，构造本地 wrapper，模型调用时 wrapper 调父级 callTool，结果再回到 Think 生命周期钩子。
  - MCP 工具描述与调用的可序列化边界：父级保存 registry/OAuth/connection/tool descriptors，子级按轮拉取 descriptors 并构造 wrapper，真实 callTool 只在父级执行。
  - MCP 工具描述的序列化格式、每轮获取时机、子侧 wrapper execute 实现、父侧 callTool 权限校验和结果返回协议。
