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

- `subagent-large-task-boundary`：主 agent 仍会把全项目、多主题、跨模块的大任务交给子 agent，pipe 预算只能限制交付内容，不能限制任务成本；`010` 移除 reviewer 后仍重复调用大粒度 `material_analyst`。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `subagent-duplicate-delegation`：主 agent 已写出主体并自检后，仍可能重复调用同类子 agent 做相似全局分析，导致压缩、读取和 pipe 预算重复消耗。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `agent-completion-policy`：主 agent 缺少“够了就停”的完成策略，复杂任务中容易持续补写、补查、补审查，直到接近 timeout。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `document-write-duplicate-sections`：主 agent 可能重复提交同一批文档写入工具调用，导致 `technical_solution` 下出现重复子章节，并触发后续清理长链路。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `exec-command-disclosure-mutation`：主 agent 可通过 `exec_command` 直接修改 `disclosure.json`，绕过受控文档写入工具的校验、事件和工具边界。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `solution-quality-validation`：技术方案质量验证不足。系统已经能产出技术方案，但还没有足够样本证明它能稳定生成合理、可实施、具备保护价值的方案。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `provider-stream-readerror-recovery`：长轮次仍可能因 provider streaming `httpx.ReadError` / `llm_stream_error` 中断，导致已完成大量分析但未落地产物。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)

P1：

- `case-selection-quality-gate`：case 筛选标准需要继续落实。需要避免 bug fix、小补丁、过细需求或已经暴露技术手段的需求进入正式 benchmark。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)
- `late-artifact-write`：主 agent 可能在长时间阅读和多次子 agent 调用后才写入 `technical_solution`，一旦 round 超时就没有可评测 artifact。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `document-write-segmented-policy`：复杂技术方案仍需要稳定采用主体摘要 + 子章节逐段追加的写入策略，避免长链路重复补写。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `document-write-argument-preflight`：旧 `operations` 已移除，但新文档工具的 `block` / `blocks` 对象或数组字段仍可能被模型作为 JSON 字符串传入，需要继续降低参数错误概率。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `document-write-size-precheck`：主 agent 仍可能先超过 1500 字写入限制，再根据工具错误拆分，需减少这类可预防失败。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `subagent-pipe-budget-planning`：pipe 预算机制已生效，但子 agent 仍常先超额写入再压缩，需要更早按预算规划交付内容。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `document-write-section-cleanup`：文档写入工具缺少安全删除/清理子章节能力，重复写入后只能清空 blocks，容易留下空壳章节。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `subagent-read-budget`：pipe 预算限制了子 agent 交付内容，但不限制其在交付前大量读取/分析，analyst/refiner 仍可能拖慢复杂 case。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `solution-quality-depth-high-difficulty`：`007`、`010` 等高难 case 已能到 82 分，但仍缺少完整工程边界、状态版本、认证绑定、异常语义等关键机制深度。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `benchmark-run-transient-failures`：provider streaming `ReadError`、quota、timeout 等偶发运行失败需要与 case 不可运行区分开。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)
- `benchmark-judge-plugin-sync-failure`：Codex-as-judge 启动时可能因远程插件同步 403 / Cloudflare challenge 导致 `judge_failed`，需要与 subject agent 失败区分并考虑重试或禁用非必要插件同步。
  - 跟踪文档：[建立技术方案生成能力评测基准](./2026-05-12-technical-solution-generation-benchmark.md)

P2：

- `compression-cost-reuse`：Markdown memory 压缩已经稳定，但复杂 case 中重复压缩相同主上下文仍会带来耗时和 token 成本。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `writing-phase-context-bloat`：复杂 case 进入写作阶段后主 agent 仍携带过重材料上下文，导致每次写入调用 token 成本过高。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
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
- `benchmark-case-layering`：case 分层与黄金 case 概念已废弃。正式纳入 benchmark 的 case 一视同仁；运行分数只评价 agent 表现，运行器不再输出 case 分级、case 建议或 benchmark 自评结论。
- `benchmark-golden-cases`：黄金 case 概念已废弃。`001`、`002`、`003` 已跑通完整 subject + Codex-as-judge 链路，历史最低闭环验证目标已满足。
- `skip-judge-report-classification`：批量报告已从 `case_selection_report.md` 改为 `evaluation_report.md`，只展示运行状态、产物成功和评分结果，不再生成 case 建议。
- `subagent-pipe-budget`：`write_pipe` 已增加 run 级预算（最多 10 次、累计 4000 字、不限制单次字符数），`005` 复跑验证超长 pipe 写入会被拒绝，子 agent 能压缩后主动 `finish`。
- `subagent-finish-boundary`：旧风险是子 agent 不及时 `finish` 或无限写 pipe；当前 `write_pipe` 预算、预算 ack 和无参 `finish` 已能兜住该类协议收尾问题，残留读取过多、任务过大和重复委派已拆到独立 issue。
- `benchmark-artifact-after-review-timeout`：`consistency_reviewer` 已移除，`010` 复跑验证不再因 reviewer timeout 失败；残留的已有主体后重复 analyst/refiner 调用已拆到 `subagent-duplicate-delegation` 和 `agent-completion-policy`。
- `document-edit-low-burden-api`：旧 `document_edit + operations` 公开入口已移除，当前改为 `document_replace_section_blocks`、`document_append_block`、`document_replace_block`、`document_append_child_section`、`document_clear_section_blocks` 五个低负担写入工具。
- `operations-stringified-json`：公开工具 schema 已不再暴露 `operations` 参数，内部 domain 写入层也已移除 op 分发器。
- `tool-system-refactor`：工具声明、schema、prompt 手册和执行入口已统一迁移到 `backend/app/tools`，旧 `agents/tools` 和 `runtime/executor/tools` 目录已删除。
- `benchmark-failed-cases`：`005`、`007` 已完成单独排查和复跑，均证明不是不可运行 case；剩余问题拆分到长链路控制和稳定回归流程中继续跟踪。
