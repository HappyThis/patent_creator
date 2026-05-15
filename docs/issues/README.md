# Issues

本目录用于收集和跟踪当前项目问题。

建议文件名格式：

- `YYYY-MM-DD-short-title.md`

## 状态规范

每个 issue 文件应在标题下方声明文件级状态：

- `进行中`：问题仍有未完成工作，是默认状态。
- `已关闭`：该 issue 的关闭条件已经满足，不再作为开放事项跟踪。
- `暂缓`：当前不处理，但仍保留背景和决策记录。

如果一个 issue 中包含多个子问题，应在文档中区分“已关闭子问题”和“仍开放子问题”。只有当文件级关闭条件全部满足时，才将整个 issue 标记为 `已关闭`。

## 优先级规范

- `P0`：阻塞当前主线目标，应该优先处理。
- `P1`：重要但不阻塞当前闭环，可在 P0 后处理。
- `P2`：有价值但可以延后，通常是规模化、体验或稳定性增强。

## 当前开放问题一览

P0：

- `benchmark-golden-cases`：黄金 case 不足。目前只有 `001` 完整跑通，尚不能判断技术方案生成能力是否稳定提升。需要至少完成 3 个高质量黄金 case，并跑通完整 subject + Codex-as-judge 链路。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)
- `solution-quality-validation`：技术方案质量验证不足。系统已经能产出技术方案，但还没有足够样本证明它能稳定生成合理、可实施、具备保护价值的方案。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)

P1：

- `case-selection-quality-gate`：case 筛选标准需要继续落实。需要避免 bug fix、小补丁、过细需求或已经暴露技术手段的需求进入核心 benchmark。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)
- `section-writer-submit-result`：`section_writer` 子 agent 仍可能直接回复文本，而不是调用 `submit_result`，导致主 agent 只能把子 agent 调用视为失败后自行恢复。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `tool-failure-recovery`：工具失败后的恢复策略不稳定。主 agent 曾在 `document_edit` 参数错误后通过 `exec_command` 查找内部 `disclosure.json`，而不是优先修正同一工具调用重试。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)

P2：

- `operations-stringified-json`：主 agent 仍可能把 `document_edit.operations` 输出成字符串化 JSON。真实工具已有窄口径防御，短期不阻塞主线，但会增加轮次和弱模型失败概率。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `benchmark-regression-scale`：正式回归集、批量运行、趋势对比和模型横向比较还没建设起来。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)

已关闭：

- `reasoning-content-replay`：`reasoning_content` 已按 provider/profile 保存和回放。
- `append-child-section-contract`：`append_child_section` 参数协议已统一。
- `duplicate-section-id`：章节 id 语义混用问题已通过 v2 文档结构解决。
- `benchmark-runtime-error-diagnostics`：技术方案 benchmark 已改为只评价最终技术方案内容，不再统计可恢复过程异常。
