# Benchmark 发布结果元数据不足

> 状态：进行中
> 最后更新：2026-05-22
> 关闭条件：发布到 `results/` 的每次实验都能自包含记录模型、运行参数、补跑历史、整体统计和源码状态，后续不依赖聊天记录才能复盘实验条件。

## 背景

`publish_result.py` 已经能把运行结果整理为可提交快照，且不会自动 git add / commit。当前发布内容粒度合理：包含报告、聚合 summary、case_results、技术方案产物和 judge 结构化结果，没有导入 prepared repo、session events、stdout/stderr 或完整 disclosure。

## 现象

`20260522-deepseek-v4-pro-reasoning-5x` 导入结果正确，但 `manifest.json` 主要记录：

- result id
- source run id
- case ids
- runs / scored runs / artifact success runs
- benchmark git commit / dirty 状态
- included / excluded 文件

它没有结构化记录：

- provider：DeepSeek
- model：deepseek-v4-pro
- thinking/reasoning：enabled
- repeats：5
- workers：5
- round timeout / judge timeout
- 补跑历史：`004/007/010` 只重跑 judge，`008` 第 5 次完整重跑
- 整体均分、最低、最高、标准差

## 影响

- 后续回看结果时，需要依赖聊天记录或运行目录推断实验条件。
- 模型横评或趋势对比时，无法仅靠 results 快照做可靠过滤。
- 补跑过的结果无法清楚表达“最终有效结果”和“原始失败恢复过程”的关系。

## 处理方向

- 扩展 `publish_result.py` 的 manifest schema。
- 支持通过参数或 run_summary 派生以下字段：
  - `model.provider`
  - `model.name`
  - `model.thinking`
  - `run.repeats`
  - `run.workers`
  - `run.round_timeout`
  - `run.judge_timeout`
  - `reruns`
  - `score_summary`
  - `notes`
- 保持发布脚本只整理结果，不提交 git。

## 验证方式

- 发布一次新结果后，仅阅读 `results/<result_id>/manifest.json` 即可复盘主要实验条件。
- index 中保留轻量字段，完整字段保留在 manifest。
