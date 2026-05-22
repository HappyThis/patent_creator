# 技术方案工程边界深度不足

> 状态：进行中
> 最后更新：2026-05-22
> 关闭条件：主 agent 在软件专利技术方案中能稳定给出完整工程边界，覆盖接口契约、状态机、异常处理、权限边界、资源清理、迁移和上下游集成路径，并在高难 case 中稳定进入高分段。

## 背景

`20260522-deepseek-v4-pro-reasoning-5x` 已证明系统能稳定产出技术方案：10 个 case 每个重复 5 次，总计 50 次全部 `scored`。但评分显示，agent 往往能抓住总体方向，却会漏掉工程闭环中的关键边界。

结果快照：

- `benchmarks/software_patent_solution_github/results/20260522-deepseek-v4-pro-reasoning-5x/`

## 现象

多个 case 的主要扣分点都不是“大方向错误”，而是工程边界不足：

- `001`：OpenCode runtime 方案缺少 OpenAPI、API index、前端 store、session kind、UI capability 的同步更新。
- `002`：AgentRun 方案缺少 run list/detail/provenance/eval attach/leaderboard 等外部工具面。
- `004`：SignalStore / RuntimeStore 方案缺少 list/scan、update、batchUpdate、失败日志和字段归属边界。
- `007`：retained subagent 方案缺少 AIChatAgent 与 Think 的差异化执行、恢复、取消、stream replay 适配。
- `008`：浏览器沙箱方案缺少限制性 CSP、execution-result 协议字段、useAgentChat/onToolCall/addToolOutput 接入链路。
- `010`：多会话 agent 方案缺少 MCP serializable descriptors -> child wrapper -> parent callTool 的完整边界。

## 影响

- 技术方案看起来完整，但真正实现时仍会遇到接口、状态和权限断点。
- 专利交底中“技术手段”不够具体，保护范围和可实施性都会受影响。
- 高难 case 的分数容易停留在 78-85 分，而不是稳定进入 90 分以上。

## 处理方向

- 主 agent 生成技术方案时，应强制检查以下工程边界：
  - 输入输出接口与 schema。
  - 状态机与终态写入。
  - 并发、幂等、重试和取消。
  - 权限、认证、跨边界 RPC。
  - 资源清理、超时和失败恢复。
  - 迁移、兼容、索引和可观察性。
  - 上下游消费路径与 UI/API/工具同步。
- 对高难 case 的 reference/rubric 中已经指出的关键边界，主 agent 应优先转化为方案主体机制，而不是放到“风险/待确认”。

## 验证方式

- 对 `007`、`008`、`010` 这类高难 case 重跑 5 次。
- 观察 `missing_key_mechanisms` 中工程边界类缺口是否明显减少。
- 目标是高难 case 均分提升且低分尾部减少。
