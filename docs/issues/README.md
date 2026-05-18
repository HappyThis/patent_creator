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

- `solution-quality-validation`：技术方案质量验证不足。系统已经能产出技术方案，但还没有足够样本证明它能稳定生成合理、可实施、具备保护价值的方案。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `provider-stream-readerror-recovery`：长轮次仍可能因 provider streaming `httpx.ReadError` / `llm_stream_error` 中断，导致已完成大量分析但未落地产物。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)

P1：

- `case-selection-quality-gate`：case 筛选标准需要继续落实。需要避免 bug fix、小补丁、过细需求或已经暴露技术手段的需求进入核心 benchmark。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)
- `benchmark-failed-cases`：全量首轮中 `005`、`007` 仍需单独排查和复跑；复杂 case 在并发下可能需要更高 timeout 或低并发策略。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)
- `late-artifact-write`：主 agent 可能在长时间阅读和多次子 agent 调用后才写入 `technical_solution`，一旦 round 超时就没有可评测 artifact。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `document-edit-segmented-write-policy`：复杂技术方案仍可能先尝试一次性大块 `document_edit`，失败后才拆分；应默认采用主体摘要 + 子章节逐段追加。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `document-edit-argument-preflight`：`document_edit` 仍可能出现字符串化 `operations`、未转义引号或过大操作体等可预防参数错误。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `benchmark-run-transient-failures`：provider streaming `ReadError`、quota、timeout 等偶发运行失败需要与 case 不可运行区分开。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)

P2：

- `operations-stringified-json`：主 agent 仍可能把 `document_edit.operations` 输出成字符串化 JSON。真实工具已有窄口径防御，短期不阻塞主线，但会增加轮次和弱模型失败概率。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `compression-cost-reuse`：Markdown memory 压缩已经稳定，但复杂 case 中重复压缩相同主上下文仍会带来耗时和 token 成本。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `benchmark-regression-scale`：正式回归集、批量运行、趋势对比和模型横向比较还没建设起来。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)

已关闭：

- `reasoning-content-replay`：`reasoning_content` 已按 provider/profile 保存和回放。
- `append-child-section-contract`：`append_child_section` 参数协议已统一。
- `duplicate-section-id`：章节 id 语义混用问题已通过 v2 文档结构解决。
- `benchmark-runtime-error-diagnostics`：技术方案 benchmark 已改为只评价最终技术方案内容，不再统计可恢复过程异常。
- `section-writer-submit-result`：旧的 `submit_result` 结构化提交协议已移除，子 agent 当前统一通过 `write_pipe(content)` + `finish({})` 交付结果。
- `context-compression-invalid-json`：旧 JSON / preserved tool call 压缩协议已移除，当前使用 Markdown memory 压缩并通过弱校验与 fallback 保底。
- `subagent-task-boundary`：`section_writer` 阻断级边界问题已关闭；复杂正文已转为主 agent 直接落盘或轻量局部写作。
- `tool-failure-recovery`：工具失败后绕行内部文件的问题已关闭为阻断级问题；`010` 复跑中主 agent 能修正同一 `document_edit` 调用并继续。
- `benchmark-golden-cases`：`001`、`002`、`003` 已跑通完整 subject + Codex-as-judge 链路，满足至少 3 个黄金 case 的最低闭环要求。
