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

- `solution-engineering-boundary-depth`：技术方案能覆盖大方向，但在接口边界、状态版本、认证绑定、异常语义、运行时约束等工程闭环上仍不稳定。
  - 跟踪文档：[技术方案工程边界与机制深度不足](./2026-05-22-solution-engineering-boundary-depth.md)
- `solution-source-grounding-unsupported-claims`：技术方案中仍会混入源码依据不足、把设计建议写成项目现状、或缺少证据路径支撑的断言。
  - 跟踪文档：[技术方案源码依据与无支撑断言问题](./2026-05-22-solution-source-grounding-unsupported-claims.md)
- `low-score-case-capability-backlog`：`005`、`001`、`002` 在 5 次重复评测中的平均分偏低，需要作为下一轮 agent 能力改进的重点样本。
  - 跟踪文档：[低分 case 能力改进清单](./2026-05-22-low-score-case-capability-backlog.md)
- `subagent-large-task-boundary`：主 agent 仍会把全项目、多主题、跨模块的大任务交给子 agent，pipe 预算只能限制交付内容，不能限制任务成本；`010` 移除 reviewer 后仍重复调用大粒度 `material_analyst`。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `agent-completion-policy`：主 agent 缺少“够了就停”的完成策略，复杂任务中容易持续补写、补查、补审查，直到接近 timeout。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)

P1：

- `solution-quality-repeatability`：同一 case 多次运行存在质量波动，需要把重复评测结果作为 agent 改进前后的对比基线。
  - 跟踪文档：[技术方案质量重复性与波动问题](./2026-05-22-solution-quality-repeatability.md)
- `benchmark-judge-timeout-cost-control`：Codex-as-judge 能完成评分，但部分 run 评估成本和耗时偏高，曾出现 judge timeout 后重跑成功的情况。
  - 跟踪文档：[Codex 评估耗时与成本控制问题](./2026-05-22-benchmark-judge-timeout-cost-control.md)
- `benchmark-result-manifest-metadata`：已导入结果缺少足够机器可读的实验元数据，不利于后续比较模型、推理模式、重复次数和 rerun 历史。
  - 跟踪文档：[Benchmark 发布结果元数据不足](./2026-05-22-benchmark-result-manifest-metadata.md)
- `provider-stream-readerror-recovery`：长轮次仍可能因 provider streaming `httpx.ReadError` / `llm_stream_error` 中断，导致已完成大量分析但未落地产物。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `late-artifact-write`：主 agent 可能在长时间阅读和多次子 agent 调用后才写入 `technical_solution`，一旦 round 超时就没有可评测 artifact。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `document-write-argument-preflight`：旧 `operations` 已移除，但新文档工具的 `block` / `blocks` 对象或数组字段仍可能被模型作为 JSON 字符串传入，需要继续降低参数错误概率。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `document-write-size-precheck`：主 agent 仍可能先超过 1500 字写入限制，再根据工具错误拆分，需减少这类可预防失败。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `subagent-pipe-budget-planning`：pipe 预算机制已生效，但子 agent 仍常先超额写入再压缩，需要更早按预算规划交付内容。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `subagent-read-budget`：pipe 预算限制了子 agent 交付内容，但不限制其在交付前大量读取/分析，analyst/refiner 仍可能拖慢复杂 case。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `exec-command-disclosure-mutation`：主 agent 具备通过 `exec_command` 直接修改 `disclosure.json` 的风险，应保持内部数据目录只读边界；最新 50 次运行未发现实际绕写。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)

P2：

- `compression-cost-reuse`：Markdown memory 压缩已经稳定，但复杂 case 中重复压缩相同主上下文仍会带来耗时和 token 成本。
  - 跟踪文档：[提高技术方案生成能力](./2026-05-12-technical-solution-generation-capability.md)
- `writing-phase-context-bloat`：复杂 case 进入写作阶段后主 agent 仍携带过重材料上下文，导致每次写入调用 token 成本过高。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)
- `document-write-section-cleanup`：如果未来再次出现重复或空壳章节，需要评估安全删除/清理工具；最新 50 个发布产物未发现重复标题。
  - 跟踪文档：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)

已关闭：

- `benchmark-v1-build`：GitHub 软件专利技术方案 benchmark v1 已闭环。`20260522-deepseek-v4-pro-reasoning-5x` 完成 10 个有效 case、每个 case 5 次重复运行，50/50 subject 完成，50/50 artifact 抽取，50/50 Codex-as-judge 评分，并已导入 `results/`。
- `benchmark-judge-plugin-sync-failure`：旧的插件同步 403 / Cloudflare challenge 已作为历史 judge 基础设施问题关闭；当前剩余的是 judge 耗时与成本控制，转入 `benchmark-judge-timeout-cost-control`。
- `solution-quality-validation`：旧的“样本不足，无法判断质量”问题已由 50 次评分基线关闭；质量改进拆分为工程边界、源码依据、重复性和低分 case 改进清单。
- `document-write-duplicate-sections`：最新 50 个发布 artifact 中未发现重复标题或重复章节污染，重复写入阻断问题关闭；安全清理能力保留为 P2 观察项。
- `subagent-duplicate-delegation`：最新 50 次运行未发现同一 run 内重复调用高度相似同类子 agent 的模式；残留问题转为单次子 agent 任务规模过大和完成策略。
- `benchmark-run-transient-failures`：旧的宽泛瞬时失败分类已关闭并拆分；provider 流式中断归入 `provider-stream-readerror-recovery`，judge timeout / 成本归入 `benchmark-judge-timeout-cost-control`，额度耗尽不作为 agent 或 benchmark 问题。
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
- `subagent-finish-boundary`：旧风险是子 agent 不及时 `finish` 或无限写 pipe；当前 `write_pipe` 预算、预算 ack 和无参 `finish` 已能兜住该类协议收尾问题，残留读取过多和任务过大已拆到独立 issue。
- `benchmark-artifact-after-review-timeout`：`consistency_reviewer` 已移除，`010` 复跑验证不再因 reviewer timeout 失败；残留的完成策略问题转入 `agent-completion-policy`。
- `document-edit-low-burden-api`：旧 `document_edit + operations` 公开入口已移除，当前改为 `document_replace_section_blocks`、`document_append_block`、`document_replace_block`、`document_append_child_section`、`document_clear_section_blocks` 五个低负担写入工具。
- `operations-stringified-json`：公开工具 schema 已不再暴露 `operations` 参数，内部 domain 写入层也已移除 op 分发器。
- `tool-system-refactor`：工具声明、schema、prompt 手册和执行入口已统一迁移到 `backend/app/tools`，旧 `agents/tools` 和 `runtime/executor/tools` 目录已删除。
- `benchmark-failed-cases`：`005`、`007` 已完成单独排查和复跑，均证明不是不可运行 case；剩余问题拆分到长链路控制和稳定回归流程中继续跟踪。
