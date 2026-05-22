# Codex 评估耗时与成本控制

> 状态：进行中
> 最后更新：2026-05-22
> 关闭条件：Codex-as-judge 能在合理时间内稳定完成评估；judge timeout 可自动或半自动恢复；评估结果能记录 token、耗时和补跑历史。

## 背景

本 benchmark 使用 Codex-as-judge，而不是简单 LLM-as-judge，是因为评估者需要读取源码并判断技术方案是否贴合项目环境。该方向成立，但本次实验暴露 judge 成本偏高。

## 现象

`20260522-deepseek-v4-pro-reasoning-5x` 初次全量运行中：

- `004`、`007`、`010` 的第 5 次均出现 Codex judge 1800 秒超时。
- 切换网络后，复用已有 `evaluated_artifact.md` 只重跑 judge，三个都成功：
  - `004`：89
  - `007`：82
  - `010`：78
- `008` 第 5 次 subject 曾失败，完整重跑后 scored=88。

judge token 量较大：

- `010` 补跑 judge input tokens 超过 150 万。
- `004` 补跑 judge input tokens 超过 120 万。

## 影响

- 评估成本高，批量回归耗时长。
- 失败项可能只是 judge timeout，不代表 subject agent 没有产物。
- 如果不记录补跑历史，后续难以复盘结果来源。

## 处理方向

- runner 支持只重跑 `judge_failed` 项，复用原始 `evaluated_artifact.md`。
- judge timeout、token usage、补跑次数、补跑原因写入发布 manifest。
- 探索 judge 限制策略：优先读重点路径、限制无关扫描、对大项目做源码阅读预算。
- 将 judge 基础设施失败、subject 失败、内容低分严格分开记录。

## 验证方式

- 连续跑 10 case x 5 repeats。
- judge_failed 经过补跑后应能稳定恢复，且补跑记录进入结果快照。
- judge 平均耗时和极端 token 输入应可观察、可比较。
