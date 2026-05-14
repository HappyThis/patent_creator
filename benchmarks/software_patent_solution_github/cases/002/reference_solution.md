# 隐藏参考方案

## 技术问题

Mission Control 已经能够派发 agent 任务、记录部分 spawn history、展示任务结果和统计成本，但这些信息分散在任务、会话、agent 历史和评估接口中，缺少统一的 agent run 抽象。系统难以稳定回答一次 agent 执行从哪里来、由谁执行、产出了什么、成本是多少、是否被评测、是否可被外部工具查询等问题。

如果只把执行过程写成普通日志，会缺少结构化查询、来源证明、评测附着、事件通知和外部工具访问能力；如果另建完全割裂的记录体系，又会破坏现有任务调度、spawn history 和 MCP 工具链。

因此，需要一种将 agent 执行过程结构化为可追踪、可评估、可订阅、可被工具查询的运行记录机制。

## 核心技术构思

在 Mission Control 中建立“AgentRun 结构化运行记录 + provenance 溯源 + eval 附着 + 事件流 + 工具化访问”的 agent 执行过程沉淀机制。

系统把每次 agent 执行抽象为一个 AgentRun 对象，并将其与现有 agent、task、session、spawn history 和成本统计关联。AgentRun 记录执行状态、来源、输入摘要、输出结果、耗时、成本、错误和评估结果。系统在 agent spawn 创建和完成时自动生成或更新运行记录，同时保留外部系统主动上报运行记录的能力。运行记录通过 API、事件流和 MCP 工具暴露给上层界面、评测系统和自动化 agent，使运行结果能够被查询、评分、排行和审计。

## 必要技术特征

1. 统一运行对象：定义 AgentRun 数据模型，记录 run id、agent、task、session、状态、开始结束时间、结果、成本、错误和元数据。
2. 来源溯源：为运行记录生成 provenance 信息，描述运行来源、关联对象、输入输出摘要和可校验的 lineage。
3. 自动桥接：在现有 spawn history 创建和完成路径中自动创建、更新 AgentRun，避免要求调用方重复上报。
4. 评估附着：允许后续把 eval 结果附着到某个 run 上，记录评分、通过状态、评估说明或评测维度。
5. 排行聚合：根据已附着的 eval 结果、成功率、成本或 agent 维度生成 leaderboard 或统计视图。
6. 事件通知：在 run 创建、更新、完成和评估附着时写入事件总线，并提供流式订阅能力。
7. 工具访问：通过外部工具接口暴露 run list、run detail、provenance、eval attach 和 leaderboard 等能力，使 agent 或评测工具可以读写运行记录。
8. 向后兼容：不替换现有任务和会话系统，而是把既有 task、agent、session、spawn history 作为运行记录的来源和关联对象。

## 关键流程

### 运行记录创建流程

1. 调度器或工具触发 agent 执行。
2. 系统创建 spawn history 或等价执行入口记录。
3. AgentRun 模块根据执行入口创建统一 run 对象，记录来源、agent、task、session、启动时间和初始状态。
4. 系统为 run 生成 provenance 记录，保留执行来源和关联对象摘要。

### 运行完成更新流程

1. agent 执行结束后，现有 spawn history 更新状态、输出和错误信息。
2. AgentRun 模块同步更新 run 状态、结果、耗时、错误和成本。
3. 系统发布 run updated / completed 事件。
4. API 和外部工具可以查询更新后的 run detail。

### 评估附着流程

1. 评测系统或人工评审针对某个 run 提交 eval 结果。
2. 系统把 eval 与 run 绑定，记录评分、结果和评估元数据。
3. leaderboard 聚合逻辑根据 eval、成本和成功率更新统计结果。
4. 系统发布 eval attached 事件，供前端或自动化流程订阅。

## 技术效果

- 每次 agent 执行从非结构化历史变成可查询、可追踪、可评估的运行对象。
- 任务、agent、session、spawn history、成本和评估结果被统一关联，减少信息割裂。
- 外部 agent 和评测工具可以通过统一接口查询运行记录、附着评估和构建排行榜。
- provenance 信息提升运行记录的可审计性和可复现分析能力。
- 事件流让 UI、自动化评测和监控系统可以实时感知 run 生命周期变化。
- 该机制为后续 agent 质量回归、成本优化、自动化评测和跨系统报告奠定基础。

## 目标能力边界

必须解决的是“每次 agent 执行形成可查询、可评估、可订阅的结构化 run”，而不是新增普通日志表。方案不需要把所有历史数据一次性补齐，也不需要替代 task/session/spawn-history，但必须把这些既有对象与 run 建立稳定关联。

该机制应支持自动桥接现有 spawn history，也保留外部系统主动创建或更新 run 的入口。高分方案会把自动采集、人工评估、外部 MCP 工具访问和 leaderboard 聚合视为同一 run 模型上的不同视图，而不是多套独立数据。

## 核心数据结构与状态模型

`AgentRun` 至少应包含：

- 身份字段：`runId`、`agentId`、`taskId`、`sessionId`、`spawnId`、`workspaceId`。
- 来源字段：`sourceType`、`triggeredBy`、`inputSummary`、`provenance`、`lineage`。
- 生命周期字段：`status`，建议覆盖 `queued/running/completed/failed/cancelled` 或等价状态，配套 `startedAt/endedAt/durationMs`。
- 结果字段：`outputSummary`、`artifactRefs`、`errorCode`、`errorMessage`、`cost`、`tokenUsage`。
- 评估字段：`evals[]`、`score`、`pass/fail`、`rubricVersion`、`evaluator`、`attachedAt`。
- 扩展字段：metadata、tags、external ids。

状态转移应明确：创建后进入 queued/running，完成后进入 terminal；eval 可以附着到 terminal run，也可以标记 pending review。迟到的 spawn 完成事件不得覆盖已经人工标记的取消或错误终态，除非有明确版本号或条件更新。

## 项目集成点

方案应接入 `spawn-history` 创建/完成路径、任务 outcome/regression/evals API、event bus、migrations、MCP server 和新的 `/api/v1/runs` 查询接口。run 不是替代任务表，而是从任务、会话和 agent 执行历史中抽出可审计运行层。

## 必须命中的评分锚点

- 有统一 run 对象，而不是散落日志。
- 有 provenance，可以解释 run 从哪个任务、会话、agent 或外部调用产生。
- spawn-history 自动桥接，避免调用方重复上报。
- eval 附着和 leaderboard 基于 run 聚合。
- run 生命周期事件进入 event bus 或 stream。
- MCP/API 可以查询 run、附着 eval、读取 provenance。

## 常见错误方案

- 只增加日志打印或审计表，没有 run 状态、关联对象和查询接口。
- 只做 leaderboard，没有解释运行记录如何产生和更新。
- eval 与 run 用文本字段弱关联，缺少可追踪 id 和版本。
- 让外部工具直接读任务表推断运行过程，未建立稳定 run contract。
- 新系统与 spawn-history 割裂，导致同一次执行有两套互不一致的状态。

## 对应真实实现

真实 PR #487 采用了如下实现方向：

- 新增 `runs` 数据模型和 `src/lib/runs.ts`，提供运行记录 CRUD、provenance、eval 附着和 leaderboard。
- 在 `spawn-history` 流程中自动创建和更新 AgentRun。
- 新增 `/api/v1/runs`、`/api/v1/runs/:id`、`/api/v1/runs/:id/provenance`、`/api/v1/runs/:id/eval`、`/api/v1/runs/stream` 和 `/api/v1/evals/leaderboard`。
- 通过事件总线发布 `run.created`、`run.updated`、`run.completed`、`run.eval_attached`。
- 在 MCP server 中新增 run 查询、创建、更新、溯源、评估附着和排行榜工具。
