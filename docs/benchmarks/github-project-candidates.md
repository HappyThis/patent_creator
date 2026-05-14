# GitHub 中型项目候选清单

## 文档定位

本文档记录软件专利技术方案评测基准的 GitHub 项目初筛结果。

筛选目标是找到适合构建 `project_snapshot + request.md + reference_solution.md` 测试项的中型开源项目。

## 依赖文档

- [评测基准规范索引](README.md)
- [GitHub 中型项目软件专利技术方案评测基准](software-patent-solution-github.md)

## 筛选口径

本轮按以下口径初筛：

- GitHub 开源项目。
- star 数约 `1k - 5k`。
- 仓库体量接近中型项目，优先考虑可裁剪到 `1w - 10w` 行有效代码的项目。
- 优先选择 TypeScript、Python、Go、Rust、Java 项目。
- 优先选择机制明确的软件系统，例如构建工具、工作流引擎、数据处理、观测性、缓存/同步、权限、AI 工具链。
- 优先选择有真实 issue / PR / design discussion，可提炼出技术方案需求和参考方案的项目。

本轮使用 GitHub API 初筛项目元数据，并抽查近期 merged PR 和 closed issue 标题。筛选日期：2026-05-12。

## 优先候选

### 1. google/mtail

- 仓库：<https://github.com/google/mtail>
- 规模信号：约 4k stars，Go，仓库 size 约 6.9MB。
- 项目类型：日志提取、指标采集、编译器/VM、Prometheus 监控。
- 适配原因：机制清晰，容易形成软件专利方案，例如日志程序编译、运行时指标提取、store GC、bucket observe、内存增长控制。
- 候选测试项方向：
  - 指标 store 过期数据的增量 GC 与一致性清理机制。
  - 面向日志程序的 observe / bucket 写入语义校验机制。
  - fuzz 触发的解析器或 VM 容错机制。
- 风险：项目相对底层，需要测试项作者理解日志指标模型和 mtail 程序语义。

### 2. unionai-oss/pandera

- 仓库：<https://github.com/unionai-oss/pandera>
- 规模信号：约 4.3k stars，Python，仓库 size 约 6MB。
- 项目类型：DataFrame schema、数据验证、统计数据测试。
- 适配原因：issue 质量较好，技术问题常围绕验证策略、schema 序列化、错误聚合、跨后端类型语义，适合转成软件技术方案。
- 候选测试项方向：
  - 聚合 check constraints 以减少 filter chain 的验证策略。
  - regex column 场景下 failure cases 的列映射恢复机制。
  - lazy validation 中错误数据保留与结构化回放机制。
- 风险：部分问题可能偏 bug fix，需要挑选具有机制扩展价值的 issue / PR。

### 3. Netflix/maestro

- 仓库：<https://github.com/Netflix/maestro>
- 规模信号：约 3.8k stars，Java，仓库 size 约 2.8MB。
- 项目类型：工作流编排、DAG、调度、Kubernetes step。
- 适配原因：工作流系统天然适合专利化技术方案，例如运行实例查询、步骤状态聚合、K8s step 生命周期、命令参数兼容。
- 候选测试项方向：
  - 工作流实例跨运行的最新 step view 聚合查询机制。
  - Kubernetes step 的 PreStop 生命周期暴露与兼容执行机制。
  - Kubernetes job command / args 数组兼容迁移机制。
- 风险：issue 暴露较少，可能需要更多依赖 PR 讨论和代码 diff 还原参考方案。

### 4. ucbepic/docetl

- 仓库：<https://github.com/ucbepic/docetl>
- 规模信号：约 3.7k stars，Python，仓库 size 约 65MB。
- 项目类型：LLM 文档处理、agentic ETL、语义数据处理。
- 适配原因：与本项目关注的 agent、文档处理、语义流水线高度相关，容易产生上下文检索、算子分解、token 统计、重试容错类技术方案。
- 候选测试项方向：
  - pipeline runner 的 token usage 分层统计机制。
  - map operation 的快速分解与执行计划生成机制。
  - ReduceOperation 在重试耗尽时的空结果容错机制。
- 风险：LLM 相关方案容易写空泛，rubric 需要强约束“具体算子、状态、执行计划、失败处理”。

### 5. web-infra-dev/rsbuild

- 仓库：<https://github.com/web-infra-dev/rsbuild>
- 规模信号：约 3.3k stars，TypeScript，仓库 size 约 42MB。
- 项目类型：前端构建工具、bundler、Rspack / Webpack 生态。
- 适配原因：构建工具适合生成依赖图、缓存、热更新、压缩、dev server、插件管线等技术方案。
- 候选测试项方向：
  - SSE 压缩保护与 dev server 事件流稳定性机制。
  - 插件配置合并与构建阶段约束传播机制。
  - 增量构建中的依赖失效传播机制。
- 风险：近期 PR 多为依赖或安全配置，需要深挖历史 issue / PR 找到高质量测试项。

### 6. analogjs/analog

- 仓库：<https://github.com/analogjs/analog>
- 规模信号：约 3.1k stars，TypeScript，仓库 size 约 27MB。
- 项目类型：Angular fullstack meta-framework、Vite plugin、SSR/SSG。
- 适配原因：近期 PR/issue 中有 sourcemap、CSS package export condition、Angular dep resolution、linker plugin 等较具体机制。
- 候选测试项方向：
  - 测试流水线中 TypeScript sourcemap 保真机制。
  - CSS import 的 package export condition 作用域限定机制。
  - Angular linker plugin 与 deps optimizer 的依赖接入机制。
- 风险：部分测试项需要熟悉 Angular/Vite 内部机制，参考方案整理成本较高。

### 7. viewflow/viewflow

- 仓库：<https://github.com/viewflow/viewflow>
- 规模信号：约 2.9k stars，Python，仓库 size 约 29MB。
- 项目类型：Django workflow library、BPMN、权限、流程引擎。
- 适配原因：issue 具备典型流程引擎问题，例如 subprocess 步骤重复触发、多数据库权限重建、BPMN export 信息丢失。
- 候选测试项方向：
  - subprocess 无 long-lived step 时的后续步骤去重触发机制。
  - 多数据库场景下的流程权限创建幂等机制。
  - exclusive gateway BPMN export 的名称/标题保持机制。
- 风险：项目活跃度略低于其他候选，但测试项机制非常清晰。

### 8. flox/flox

- 仓库：<https://github.com/flox/flox>
- 规模信号：约 4k stars，Rust，仓库 size 约 24MB。
- 项目类型：确定性开发环境、Nix、package manager、virtual environments。
- 适配原因：开发环境、锁定、发布、激活、认证状态提示等机制具备专利方案潜力。
- 候选测试项方向：
  - 环境激活时的系统 override 可见性与状态隔离机制。
  - 包 catalog cross-reference 的构建/发布提示机制。
  - 用户认证流程中的上下文感知提示抑制机制。
- 风险：Nix 生态上下文较重，需要精心裁剪 project_snapshot。

## Agent 方向优先候选

### 9. builderz-labs/mission-control

- 仓库：<https://github.com/builderz-labs/mission-control>
- 规模信号：约 4.8k stars，TypeScript，仓库 size 约 15MB。
- 项目类型：AI agent orchestration、任务调度、监控、MCP、自托管。
- 适配原因：非常贴近 agent orchestration 方向，存在多个较大的 runtime、session、dispatch、MCP、audit 和 run protocol 功能迭代，适合提炼软件机制型专利技术方案。
- 候选测试项方向：
  - OpenCode 原生 runtime 与本地会话接入机制。
  - 多 provider direct dispatch 与 gateway fallback 机制。
  - AgentRun protocol 的运行记录、溯源、评测附着和事件流机制。
  - MCP audit receipt 的防篡改签名与批量验证机制。
  - Hermes multi-agent profile provisioning 机制。
- 风险：项目与当前 agent 生态很近，可能存在产品形态相似导致评测偏向；应避免选择局部 bugfix、小型安全修补或题目过细的 PR。

### 10. campfirein/byterover-cli

- 仓库：<https://github.com/campfirein/byterover-cli>
- 规模信号：约 4.7k stars，TypeScript，仓库 size 约 22MB。
- 项目类型：coding agent memory、CLI、MCP、knowledge management。
- 适配原因：agent memory 与上下文管理高度相关，适合评估系统能否生成“上下文/记忆/工具模式”类软件专利技术方案。
- 候选测试项方向：
  - context tree 共享 Markdown 与本机运行时信号 sidecar 分层机制。
  - query log 的查询过程结构化记录、汇总与回放机制。
  - curate / dream 流程中的摘要生成批处理、延迟级联和成本优化机制。
  - tool-mode write-time overwrite guard。
  - single-topic render command。
  - curate session protocol 与 prompt-builder correction 机制。
- 风险：部分 PR 很大且混合多个优化点，需要优先选择边界清晰的机制型改动；公开 issue 较少，参考方案可能更多依赖 PR、代码 diff 和文档整理。

### 11. bytebase/dbhub

- 仓库：<https://github.com/bytebase/dbhub>
- 规模信号：约 2.7k stars，TypeScript，仓库 size 约 2.9MB。
- 项目类型：database MCP server、SQL 工具调用、数据库上下文暴露。
- 适配原因：项目体量小、机制清晰，适合构建 agent 工具调用中的上下文描述、SQL 策略、连接安全、token-efficient schema 暴露等测试项。
- 候选测试项方向：
  - 将 source description 注入 MCP tool description 的连接上下文提示机制。
  - SQL execution policy enforcement。
  - PostgreSQL sslmode verify-ca / verify-full 与 sslrootcert 支持机制。
  - Host Header validation 防 DNS rebinding 机制。
- 风险：部分问题偏安全或配置，需要挑选能体现 agent 工具调用链路的测试项。

### 12. cloudflare/agents

- 仓库：<https://github.com/cloudflare/agents>
- 规模信号：约 4.9k stars，TypeScript，仓库 size 约 20MB。
- 项目类型：AI agent framework、Cloudflare Durable Objects、workflows。
- 适配原因：近期 PR/issue 包含 durable submissions、durable cancellation、structured tool output truncation、stale sub-agent schedules 等 agent 运行时核心机制。
- 已落地测试项：
  - `005`：Think agent 面向外部调用方的 durable submission 与幂等恢复机制。
  - `006`：useAgentChat 的 durable turn cancellation 与 client stream disposal 解耦机制。
  - `007`：保留式流式子 agent 工具编排机制。
  - `008`：codemode browser iframe executor。
  - `009`：multimodal workspace read support。
  - `010`：multi-session assistant 中会话隔离与用户级共享 workspace / MCP 机制。
- 候选测试项方向：
  - Think turn telemetry / reasoning controls。
- 已评估但暂不纳入：
  - structured tool outputs 在上下文截断时的保持机制：有技术点，但提交形态更接近修复，暂不作为核心 case。
  - stale sub-agent schedules 的清理与调度一致性机制：范围较小，偏局部清理，不符合“较大功能迭代”优先原则。
- 风险：部分实现强依赖 Cloudflare 平台，需要将 project_snapshot 裁剪到 agent runtime 相关上下文。

### 13. gptme/gptme

- 仓库：<https://github.com/gptme/gptme>
- 规模信号：约 4.3k stars，Python，仓库 size 约 49MB。
- 项目类型：terminal agent、local tools、web browsing、subagents。
- 适配原因：与 coding agent / terminal agent 评测方向接近，近期 PR 有 typed subagent role、verifier profile、running chat prompt queue。
- 候选测试项方向：
  - subagent typed role 的工作姿态约束机制。
  - verifier profile 的子 agent review / validation 机制。
  - running chat 中 prompt queue 的串行化与状态恢复机制。
- 风险：agent 行为容易偏产品体验，测试项需要聚焦工具调用、子 agent 调度、队列状态等技术机制。

## 备选候选

### 14. ergo-services/ergo

- 仓库：<https://github.com/ergo-services/ergo>
- 项目类型：Go actor model、分布式系统、supervisor、workflow。
- 适配原因：actor、supervisor、tracing、Raft 等机制专利化空间大。
- 候选测试项方向：
  - actor supervisor termination logic 的一致性修正机制。
  - 分布式 actor tracing 上下文传播机制。
  - registered process name list 的节点内发现机制。
- 暂列备选原因：issue 数较少，部分大 PR 可能难以拆成中等难度 测试项。

### 15. uptrace/uptrace

- 仓库：<https://github.com/uptrace/uptrace>
- 项目类型：APM、OpenTelemetry traces / metrics / logs。
- 适配原因：metrics、histogram、权限访问控制等技术问题明确。
- 候选测试项方向：
  - native OTel histogram 低样本分位数异常的指数尺度校正机制。
  - 项目权限 direct link 绕过的访问控制校验机制。
  - YAML 邮件配置中的 SSL port 推断机制。
- 暂列备选原因：仓库 size 很小但项目可能依赖外部组件和服务上下文，快照裁剪需确认。

### 16. observablehq/framework

- 仓库：<https://github.com/observablehq/framework>
- 项目类型：数据 app / dashboard 静态站生成器。
- 适配原因：SSG、数据加载、构建缓存、workspace、preview 等机制适合评测。
- 候选测试项方向：
  - 数据 app 构建中的依赖失效传播机制。
  - workspace 登录后列表展示的上下文恢复机制。
  - 输入绑定文档/运行时的一致性机制。
- 暂列备选原因：近期 issue/PR 质量混杂，需要深挖历史讨论。

### 17. langchain-ai/langgraphjs

- 仓库：<https://github.com/langchain-ai/langgraphjs>
- 项目类型：resilient language agents as graphs、checkpoint、SDK。
- 适配原因：agent graph、checkpoint、message projection、cron metadata filter 等问题很适合技术方案评测。
- 候选测试项方向：
  - interrupt checkpoint 写入中的 `has_writes` 状态保持机制。
  - message projection 中 AI content blocks 保持机制。
  - cron search/count metadata filter。
- 暂列备选原因：项目可能存在较高生态记忆污染，且需要理解 LangGraph checkpoint 语义。

### 18. matt1398/claude-devtools

- 仓库：<https://github.com/matt1398/claude-devtools>
- 项目类型：Claude Code devtools、session logs、tool calls、subagents、context window、token usage。
- 适配原因：非常贴近 agent 可观测性，可用于构建 session discovery、worktree session listing、SSH self-diagnosis、性能退化定位类 测试项。
- 候选测试项方向：
  - git worktree 下 session discovery 与展示机制。
  - SSH connection robust self-diagnosing。
  - agent session log 性能退化的索引/分页机制。
- 暂列备选原因：仓库 size 约 107MB，略超理想范围，需要裁剪。

### 19. octodns/octodns

- 仓库：<https://github.com/octodns/octodns>
- 项目类型：DNS infrastructure as code、多 provider 同步。
- 适配原因：配置 schema、provider 差异、record normalization、同步计划机制适合技术方案。
- 候选测试项方向：
  - 主配置文件 JSON schema 生成与校验机制。
  - TXT/SPF 记录 null value 的 provider 兼容处理机制。
  - 多 provider DNS record 差异归一化机制。
- 暂列备选原因：部分测试项可能偏配置校验，专利化价值需要筛选。

## 暂缓候选

### kubero-dev/kubero

- 仓库：<https://github.com/kubero-dev/kubero>
- 暂缓原因：近期 issue/PR 偏维护、依赖、翻译、文档和外部 chart 变更，技术机制测试项需要更深挖。

### microsoft/retina

- 仓库：<https://github.com/microsoft/retina>
- 暂缓原因：eBPF / Kubernetes observability 很有技术价值，但项目上下文较重，第一批黄金测试项成本偏高。

### amalshaji/portr

- 仓库：<https://github.com/amalshaji/portr>
- 暂缓原因：公开 issue 较少，近期 PR 多为依赖、UI 移除和日志默认配置，测试项稳定性不足。

### sayanarijit/xplr

- 仓库：<https://github.com/sayanarijit/xplr>
- 暂缓原因：项目规模合适，但近期问题偏测试依赖、文档链接和 TUI 性能优化；可作为 Rust CLI 备选，不适合作为第一批主力。

### flipt-io/flipt

- 仓库：<https://github.com/flipt-io/flipt>
- 暂缓原因：功能机制合适，但仓库 size 已超过当前中型上界，近期 PR 多为依赖更新。后续可针对 feature flag、GitOps、audit、OpenTelemetry 深挖。

## 第一批建议

建议准备以下 10 个首批项目，并从中筛出 3 个黄金测试项试运行。

通用软件机制方向：

1. `google/mtail`
2. `unionai-oss/pandera`
3. `Netflix/maestro`
4. `ucbepic/docetl`
5. `viewflow/viewflow`

agent / AI 工具链方向：

6. `builderz-labs/mission-control`
7. `campfirein/byterover-cli`
8. `bytebase/dbhub`
9. `cloudflare/agents`
10. `gptme/gptme`

原因：

- 机制足够明确。
- 项目上下文相对可裁剪。
- issue / PR 更容易整理成技术方案需求。
- 参考方案可以从真实修复或功能实现中提炼，而不是靠凭空设计。
- 其中 5 个项目直接覆盖 agent orchestration、agent memory、MCP tool context、agent runtime、terminal agent 等方向。

第二批扩展可加入：

- `web-infra-dev/rsbuild`
- `analogjs/analog`
- `flox/flox`
- `uptrace/uptrace`
- `langchain-ai/langgraphjs`
- `matt1398/claude-devtools`

## 下一步

1. 对第一批 10 个项目逐个深挖 3-5 个候选 issue / PR。
2. 按测试项质量筛出 3 个黄金测试项。
3. 为每个黄金测试项固化解决方案合入前的项目快照。
4. 编写 `request.md`、`reference_solution.md`、`rubric.md`、`metadata.json`。
5. 试跑现有 agent，记录基线表现。
