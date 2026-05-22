# 低分 case 能力改进清单

> 状态：进行中
> 最后更新：2026-05-22
> 关闭条件：低分 case 的主要短板被拆解为可执行的能力改进项，并通过后续重复评测验证最低分和均分提升。

## 背景

`20260522-deepseek-v4-pro-reasoning-5x` 中所有 case 都能完成评分，但低分 case 暴露出清晰的 agent 能力短板。这些短板不应只留在评估报告里，应进入能力改进清单。

## 首批低分对象

| Case | 均分 | 分数 | 主要短板 |
| --- | ---: | --- | --- |
| 005 | 70.6 | 75, 68, 68, 74, 68 | submission ledger、消息应用边界、reset/clear conversation、终态条件更新、list/delete/inspect API。 |
| 001 | 72.4 | 78, 72, 68, 72, 72 | runtime kind、OpenAPI/API index/schema、前端能力字段、continue 失败分类、UI 禁用/提示边界。 |
| 002 | 75.6 | 78, 78, 72, 75, 75 | AgentRun/provenance、spawn history 自动桥接、eval attach、leaderboard、MCP run 工具面。 |

## 处理方向

- 对每个低分 case 单独建立“能力改进 checklist”：
  - 关键源码路径。
  - judge 反复指出的缺失机制。
  - 主 agent 应在方案中主动覆盖的工程边界。
  - 后续 prompt / tool / subagent 策略改进点。
- 不把低分解释为 case 不好；case 已确认有效，分数评价的是 agent 表现。
- 后续优先观察最低分是否提升，而不是只看最高分。

## 验证方式

- 针对 `001`、`002`、`005` 做改进后各重复 5 次。
- 目标先让最低分抬升到 75 以上，再观察均分。
- judge 的 `missing_key_mechanisms` 应明显减少，并且不增加 unsupported claims。
