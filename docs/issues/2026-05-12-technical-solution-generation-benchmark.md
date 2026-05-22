# 建立技术方案生成能力评测基准

> 状态：进行中
> 最后更新：2026-05-22
> 关闭条件：正式 benchmark 规范稳定，完成足够数量的正式 case，并能稳定跑通 subject 与 Codex-as-judge 内容评分链路。

## 背景

系统后续需要重点提升技术方案生成能力。为了判断每次 prompt、agent 职责、输出结构或调度策略调整是否真的带来提升，需要建立一套评测基准。

该评测基准不应评估完整交底书字数或格式完整度，而应评估：在给定软件项目上下文和技术方案需求的情况下，系统能否生成合理、可实施、具备专利保护价值的软件技术方案。

## 状态总览

文件级状态仍为 `进行中`。原因是 benchmark 建设本身尚未完全闭环：`20260520-full-10cases-5workers-after-reviewer-removal` 已经证明 10 个 case 的 subject 均可完成并抽取 artifact，其中 9 个进入 Codex-as-judge 并评分；但仍有 `008` 因 Codex judge 启动阶段插件同步 403 / Cloudflare challenge 而 `judge_failed`，还需要沉淀稳定回归流程、judge 失败重试和结果报告规范。

已关闭子问题：

- 已明确本 benchmark 只评价最终 `disclosure.technical_solution` 中提取出的技术方案内容。
- 已移除 runner 对 `document_edit` 的章节 guard。
- 已移除可恢复工具异常统计，`subagent_plain_response`、字符串化 `operations`、中途 `document_edit` 失败等过程问题不再进入内容评估指标。
- 已保留 `session_events.jsonl` 作为 debug 材料。
- 已跑通 `001` 的 subject 与 Codex-as-judge 完整评分链路。
- 已跑通 `001`、`002`、`003` 的 subject 与 Codex-as-judge 完整评分链路，满足“至少 3 个正式 case”的最低闭环验证要求。
- 已废弃黄金 case 与 case 分层概念。正式纳入 benchmark 的 case 一视同仁；运行分数只评价 agent 在该 case 上的表现，运行器不再输出 case 分级、case 建议或 benchmark 自评结论。
- 已移除批量报告中的 benchmark 自评逻辑：`run_all.py` 不再生成 recommendation，也不再输出 `case_selection_summary.json` / `case_selection_report.md`；当前输出为 `evaluation_summary.json` / `evaluation_report.md`。
- `010` 在单独提高 `round-timeout` 到 `1200` 秒后可完成闭环，说明该 case 本身不是不可运行样本。
- `010` 在 Markdown memory 压缩改造后复跑成功：`20260518-compression-md-010-rerun2` 完成 subject + artifact + Codex-as-judge，得分 `72`。
- `005`、`007` 已完成单独排查与复跑：`007` 在 `20260520-094905-007` 中 `scored=82`，`005` 在 `20260520-113247-005` 中 `scored=75`，历史 `benchmark-failed-cases` 子问题关闭。
- `20260520-full-10cases-5workers-after-reviewer-removal` 全量运行中，10 个 case 的 subject 全部完成，9 个 scored；历史“复杂 case 无法产出 artifact”已不再是当前主阻断。

仍开放子问题：

- 将复杂 case 的低并发、timeout 和失败复跑策略沉淀为稳定回归流程。
- 处理 Codex-as-judge 启动阶段的偶发基础设施失败，例如插件同步 403 / Cloudflare challenge 导致的 `judge_failed`。
- 维护正式 case 的来源标准和题目编写规范；运行器不再自动输出 case 建议或 benchmark 自评结论。
- 沉淀批量运行、汇总和横向比较流程。

## 开放问题优先级

P0：暂无 benchmark 建设层面的阻断项。`005`、`007` 未写出 result 的历史问题已经完成复跑确认，后续转为稳定回归流程和长链路控制问题。

P1：

- 继续筛选正式 case 来源，优先覆盖 agent / AI 工具链、开发工具、同步系统、权限系统等机制型软件项目。
- 统一 case 质量门槛，避免把 bug fix、小补丁、过细需求或已经暴露答案的需求放入正式 benchmark。
- Codex-as-judge 运行失败应与 subject agent 失败区分。`008` 本轮 subject 已完成并抽取 artifact，但 judge 因插件同步访问 `chatgpt.com/backend-api/plugins/featured` 返回 403 / Cloudflare challenge 而失败。
- 批量报告只展示运行次数、产物成功、评分次数、状态分布、分数和 judge 摘要，不生成 case 建议或 benchmark 自评结论。

P2：

- 从现有正式候选扩展到 20 个左右的回归集。
- 建立长期批量运行、结果趋势对比和模型横向比较流程。

## 2026-05-20 全量 10 case / 5 worker 运行

本次运行：

- run id：`20260520-full-10cases-5workers-after-reviewer-removal`
- 并发：`--workers 5`
- timeout：`--round-timeout 1200 --judge-timeout 900`
- subject：10/10 完成并抽取 `evaluated_artifact.md`
- judge：9/10 scored，`008` 为 `judge_failed`

结果：

| Case | Status | Score | 备注 |
| --- | --- | ---: | --- |
| 001 | `scored` | 72 | 方向正确但 transcript / continue / PTY gating 等关键机制不足。 |
| 002 | `scored` | 82 | Run Record 方向较完整，但 provenance / MCP / leaderboard 等机制不足。 |
| 003 | `scored` | 82 | Git 镜像方向正确，但初始化、UI、冲突和 tombstone 不足。 |
| 004 | `scored` | 78 | 仍暴露重型子 agent 链路；质量扣分来自字段分层和清理一致性等机制不足。 |
| 005 | `scored` | 74 | reviewer timeout 已消失；质量仍偏中档。 |
| 006 | `scored` | 89 | 本轮最高分，但 subject 用时约 711 秒，仍属长链路。 |
| 007 | `scored` | 80 | 原压力样本已能跑通；仍缺 headless client tools 降级等关键边界。 |
| 008 | `judge_failed` | - | subject 约 734 秒完成；judge 失败来自 Codex 插件同步 403 / Cloudflare challenge，不是无 artifact。 |
| 009 | `scored` | 78 | 多模态 read 方向正确，但模型输出转换层和 PDF/file-data 不足。 |
| 010 | `scored` | 78 | 父子 agent 方向正确，但 Workspace proxy、MCP proxy 和 RPC 权限边界不完整。 |

本次结论：

- benchmark 主链路可运行性比此前明显改善，复杂 case 基本能产出可评测 artifact。
- 全量均分若只算 scored case 为 `79.22`，但 `008` 因 judge 基础设施失败缺分，因此这不是完整质量均分。
- `008` 的失败应归入 `benchmark-judge-plugin-sync-failure`，不应归入 subject agent 能力失败。
- 不再推进 case 分层：`006/007/008` 等样本对长链路、子 agent 和 judge 稳定性压力较大，但这些现象应作为 agent/runner 稳定性问题单独记录，不作为 case 分级依据。

## 当前决策

第一优先级不做轻量纯场景评测，而是建设一套基于 GitHub 中型开源项目的软件专利技术方案评测基准。

测试项核心结构确定为：

1. `project_snapshot`
2. `request.md`
3. `reference_solution.md`

其中：

- `project_snapshot` 是固定项目快照，供 agent 搜索、阅读和理解项目上下文。
- `request.md` 是基于该项目快照提出的技术方案需求。
- `reference_solution.md` 是隐藏示例技术方案，用于评分和人工校准。

配套文件：

- `rubric.md`：测试项级评分标准。
- `metadata.json`：测试项级机器可读信息。

重要修正：

- GitHub 项目不是测试项。
- GitHub 项目只是测试项来源池。
- 一个项目可以产出多个正式测试项。
- 首批 10 个候选项目用于后续深挖测试项，不代表评测基准只有 10 个测试项。

项目筛选口径：

- GitHub 开源项目。
- star 数约 `1k - 5k`。
- 有效代码量约 `1w - 10w` 行。
- 优先选择开发工具、构建工具、文档系统、工作流引擎、数据处理、权限系统、同步系统、缓存系统、可观测性、AI 工具链等软件机制明确的中型项目。

## 历史讨论：纯场景设计

早期曾讨论过纯场景评测：每个测试项只以一个具体场景为题目。

场景背景一般应比较详细，以模拟真实用户在专利交底过程中的输入方式。真实用户通常不会一次给出规范技术方案，而是会描述业务场景、现有问题、产品目标、已有尝试、约束条件和纠偏信息。

该形态仍可作为后续评测子类型保留，但不作为当前第一优先级。

## 纯场景测试项结构建议

每个测试项可以包含：

- 场景：业务场景、现有做法、痛点、目标、已知约束、已有尝试、纠偏信息。
- 评估关注点：该测试项特别需要观察的技术方案质量要点。

测试项本身不包含“初始用户输入”“多轮补充或纠正”“期望输出结构”等输入项。

## 场景中的纠偏信息

如果需要模拟真实用户的纠正，可以把纠偏信息直接写入场景背景中。例如：

- 用户曾尝试过某种方案，但效果不好。
- 用户明确不希望采用某类实现方式。
- 某个容易被误解的点需要在场景中说明。
- 某些约束来自真实业务或工程条件。

这样既能保留真实交互中的复杂性，又能保证每个测试项都是稳定、可复现的单输入样本。

## 统一评估维度

每个测试项可以从以下维度评分：

- 项目上下文阅读：是否主动搜索、阅读并利用项目快照中的关键上下文。
- 技术问题识别：是否将背景中的业务问题转化为真实技术问题。
- 技术手段具体性：是否给出模块、流程、数据结构、算法步骤、控制逻辑等具体手段。
- 问题-手段-效果闭环：技术效果是否能由技术手段合理推出。
- 必要技术特征完整性：是否提炼出方案成立所需的核心必要特征。
- 可实施性：方案是否工程上可实现，是否缺少关键输入、输出、流程或组件。
- 创新点聚焦：是否形成可保护的技术构思，而不是常规功能堆叠。
- 约束吸收能力：是否正确采纳项目上下文、场景中的约束和纠偏信息，避免采用已被否定或不适用的设计。
- 不确定性处理：信息不足时是否提出待确认问题，而不是硬编。

## 运行任务

评测基准中系统的目标输出应是技术方案草稿，而不是完整交底书。

当前第一优先级评测基准中，所有测试项使用同一个运行任务：基于给定项目快照和技术方案需求生成软件专利技术方案草稿。

测试项文件中不单独规定期望输出结构。输出结构可以由被测 agent 的系统 prompt、评测运行器或统一任务提示词控制。

测试项筛选和题目编写新增约束：

- 正式测试项应优先选择较大的功能迭代、新功能支持、新特性机制、能力扩展或架构级改造。
- 小修改、局部 bug 修复、依赖升级、参数调整、文案或样式修改不应作为正式 benchmark 测试项。
- 缺陷修复材料只有在能抽象为系统新增或增强某种能力、特性或机制时，才可作为辅助样本或候选样本。
- `request.md` 应模拟普通用户或产品侧提出的粗粒度场景需求，不应提前给出关键技术机制、方案骨架或参考 PR 的设计结论。
- 如果题目输入已经直接点名关键技术手段，评测会退化为技术方案改写能力，不符合当前 benchmark 目标。

## 下一步任务

1. 已建立 `benchmarks/software_patent_solution_github/` 运行目录。
2. 已定义 `benchmark.json`、`runner.md`、`judge.md` 模板。
3. 已挑选首批 10 个候选 GitHub 中型项目，其中重点覆盖 agent / AI 工具链方向。
4. 已将 `projects/` 定位为本地源码 clone 工作区，候选项目源码不进入 git。
5. 下一步从候选项目中筛选适合转化为技术方案需求的 issue / PR / design discussion。
6. 下一步将选中的具体 issue / PR / design discussion 转化为正式 `cases/<case_id>/`。
7. 下一步为每个正式测试项固化解决方案合入前的项目快照。
8. 下一步补齐 `reference_solution.md`、`rubric.md`、`metadata.json` 的真实内容。
9. 先完成 3 个正式测试项试运行，再扩展到正式 20 个测试项。

## 2026-05-13 试运行发现

对 `software_patent_solution_github` 的 `001` case 进行首次 E2E 试运行时，暴露出以下问题：

- 主 agent 走完整对话接口后，默认倾向于推动整份交底书演进，而不是只写 `technical_solution` 章节。
- 原始 `runner.md` 虽然要求“只充实技术方案相关内容”，但同时列出“技术问题、技术效果”等建议覆盖项，容易被理解为要从技术领域、背景、现有技术、技术问题等章节开始写。
- 评估器如果只在 prompt 层约束目标章节，无法防止主 agent 调用 `document_edit` 写入其他章节。
- `001` 试运行中，主 agent 先写入了 `technical_field`、`background_technology`、`existing_solution`、`existing_solution_defects`、`technical_problem`，但 `technical_solution` 仍为空，因此该 run 不能作为有效评测结果。
- 试运行中还出现了子 agent 输出契约错误、模型接口兼容错误、长轮次超时和上下文压缩失败，这些属于完整主 agent 链路稳定性问题，会影响 benchmark 可运行性。

已采取的 runner 层修正：

- `runner.md` 明确要求只编辑“技术方案”章节。
- 评估器曾在工具层限制 `document_edit` 只写入 `technical_solution`；该策略已在 2026-05-15 调整为不再 guard，最终只按 `technical_solution` artifact 内容评分。
- 评估产物只从 `disclosure.json` 的 `technical_solution` 章节抽取，不使用最终聊天回复兜底。
- 运行失败或中断时，评估器应清理临时 benchmark project 的 busy 状态，避免污染后续运行。

后续待办：

- 重新运行 `001`，确认章节写入约束生效。
- 将 `run_case.py --skip-judge` 跑通至少 3 个正式 case，再接入 Codex-as-judge。
- 记录每个 case 的 `skipped_no_solution_artifact`、模型错误、超时和 judge 失败状态，供后续统计。
- 评估是否需要为 benchmark 模式增加更短、更强约束的主 agent 任务上下文，避免完整交底书写作惯性影响技术方案评测。

## 2026-05-13 复跑发现

对 `software_patent_solution_github` 的 `001` case 复跑后，主 agent 能够完成当前轮次，且评估器成功从 `disclosure.json` 的 `technical_solution` 章节抽取技术方案产物。该轮说明工具层章节限制已经基本生效：最终 `document_edit` 只改动了 `technical_solution`，没有写入技术领域、背景技术、技术问题或技术效果等其他顶层章节。

本次仍暴露出一个文档写入稳定性问题：

- `document_edit` 第一次写入 `technical_solution` 时失败，错误为 `duplicate_section_id`，提示 `section_id 已存在：technical_effects`。
- 原因是 agent 将“技术效果”作为 `technical_solution` 的内部子章节生成时，使用了与交底书模板中已有顶层章节相同的 section id。
- agent 随后重试并改用不冲突的内部 section id，最终写入成功，因此本次运行没有失败。

影响：

- 该问题当前是可恢复失败，但会增加轮次耗时和不确定性。
- 如果后续模型没有自动重试，或者同类冲突发生在更复杂的章节结构中，可能导致有效技术方案无法写入，从而被 runner 误判为 `skipped_no_solution_artifact`。
- 这说明 benchmark 模式下不仅要限制“只能写技术方案章节”，还要避免让 agent 负责生成任何 section id。

后续待办补充：

- 交底书结构协议已调整为系统生成 section id：`section.id` 只表示机器身份，`section.type` 表示标准章节语义，`section.title` 表示展示标题。
- benchmark runner 抽取技术方案时已按 `section.type == "technical_solution"` 定位，而不是按语义化 section id 定位。
- `document_edit` 对字符串化 `operations` 的窄口径防御和 `append_child_section` 唯一参数协议已完成。
- 历史上曾在 session 诊断中单独统计 `duplicate_section_id` 这类可恢复工具失败；2026-05-15 后，内容 benchmark 不再统计可恢复工具异常，相关过程问题保留在 `session_events.jsonl` 中用于 debug。

## 2026-05-14 MiMo 试运行补充

使用 MiMo 模型运行 `software_patent_solution_github` 的 `001` case，并跳过 judge 后，subject 阶段已经跑通：

- run id：`20260514-205018-001`
- subject 状态：`completed`
- 总状态：`artifact_extracted`
- 评估产物来源：`disclosure.technical_solution`

本次运行同时暴露两个需要继续跟踪的问题：

1. `section_writer` 子 agent 连续直接回复长文本，没有按协议调用 `submit_result`。
   - 诊断码：`subagent_plain_response`
   - 现象：子 agent 实际生成了技术方案正文，但没有通过工具协议提交，导致主 agent 只能收到工具失败。
   - 当前处理：executor 已增加 fail-fast，连续直接回复后立即失败，避免长时间空转。
   - 后续关注：需要判断不同模型是否稳定支持子 agent 的工具提交协议；如果不稳定，应加强子 agent tool choice、提示词约束或主 agent 的失败恢复策略。

2. 主 agent 第一次写入 `document_edit` 时仍出现不合规 `operations`。（历史记录；后续该类可恢复工具异常不再进入 benchmark 评分指标。）
   - 诊断码：`benchmark_forbidden_section_edit`
   - 具体禁止对象：`<invalid_operations>`
   - 现象：本次不是实际跨章节写入，而是 `operations` 被模型作为无法解析的字符串传入，benchmark 的章节保护层在调用真实 `document_edit` 前拦截。
   - 当前结果：主 agent 随后改用合法的数组格式，并通过 `replace_section_blocks` 与 `append_child_section` 成功写入技术方案。
   - 后续调整：该 benchmark 已改为只评价最终技术方案 artifact，不再通过章节 guard 统计此类可恢复工具异常。

本次运行说明：benchmark 约束、v2 section id 体系和 artifact 抽取链路已经能够得到有效产物，但仍需要把“工具协议稳定性”纳入主 agent 能力评估，而不能只看最终是否提取到技术方案。

## 2026-05-15 完整评分链路复跑补充

使用 `software_patent_solution_github` 的 `001` case 跑完整链路后，subject 与 judge 均已跑通，最终状态为 `scored`。

- run id：`20260515-094548-001`
- artifact 来源：`disclosure.technical_solution`
- judge 状态：`scored`

后续设计调整：

- 本 benchmark 只评价最终 `disclosure.technical_solution` 中提取出的技术方案内容。
- 已移除 benchmark runner 对 `document_edit` 的“只允许编辑技术方案章节”guard；agent 是否写过其他章节不作为本 benchmark 的评价对象。
- 已移除可恢复工具异常统计，`subagent_plain_response`、字符串化 `operations`、中途 `document_edit` 失败等过程问题不进入 `result.json` 的内容评估指标。
- `session_events.jsonl` 继续保留，作为 debug 和链路排障材料，不作为评分依据。
- `diagnostics.json` 只保留 subject 状态、补充轮次、artifact 抽取、round 硬失败和 judge 状态。

该调整后，benchmark runner 的目标收敛为：能提取有效技术方案则进入 Codex-as-judge 内容评分；无法提取有效技术方案则记为 `skipped_no_solution_artifact` 或相应硬失败状态。

## 2026-05-18 正式 case 与全量首轮补充

2026-05-18 已完成至少 3 个正式 case 的完整闭环验证：

| Case | 状态 | 分数 |
| --- | --- | ---: |
| 001 | `scored` | 72 |
| 002 | `scored` | 76 |
| 003 | `scored` | 82 |
| 010 | `scored` | 72 |

因此，历史最低闭环验证目标已满足：不再是“只有 `001` 完整跑通”。后续 benchmark 重点从“能否跑通 3 个 case”转为“全量回归是否稳定、失败 case 是否能被准确排查、结果是否可横向比较”。`benchmark-golden-cases` 作为旧概念关闭，不再用于命名或管理正式 case。

## 2026-05-20 复杂 case 复测补充

本轮复测结果：

| Case | Run | 状态 | 分数 | 运行观察 |
| --- | --- | --- | ---: | --- |
| 007 | `20260520-094905-007` | `scored` | 82 | 能完成评分；长链路、及时收尾和复杂调度问题另记为 agent/runner 稳定性信号。 |
| 005 | `20260520-100337/r01-005` | `round_failed` | - | 该轮已产出 artifact 但后续 review 超时；后续复跑证明可评分，不能据此判定 case 无效。 |
| 010 | `20260520-100337/r01-010` | `scored` | 82 | 能完成评分，需继续检查 request 与 rubric 是否都被源码证据支撑。 |
| 005 | `20260520-113247-005` | `scored` | 75 | 引入 pipe 预算后能进入 judge，但 subject 仍耗时约 969.6 秒，暴露 agent 执行控制问题。 |

本轮暴露的 benchmark 层问题：

- `005` 证明 runner 只看最终状态是合理的：评测集不应该关心 agent 为什么失败，失败就是失败；但工程分析需要另行记录“已有 artifact 后继续 review 导致 timeout”。
- 运行耗时、timeout、子 agent 调度和长链路稳定性噪声不应作为 case 分级依据；这些问题应单独记录为 agent/runner 稳定性问题。
- `010` 的 rubric 要求非常细，后续需要人工确认 request 是否足够明确，避免隐藏标准过多。
- `005` 在 pipe 预算后可评分，说明单轮失败不应被运行器扩展解释为 benchmark 样本问题。

专项跟踪文档见：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)。

同日执行过一次全量首轮：

- `001`、`002`、`003`、`004`、`006`、`008`、`009` 在全量批次中完成评分。
- `010` 在全量并发批次中曾因 `900` 秒 round timeout 未写出 result；单独复跑并将 `round-timeout` 提高到 `1200` 秒后完成评分，说明该 case 可运行，但复杂 case 对并发和 timeout 更敏感。
- `005`、`007` 后续已经单独排查和复跑：`007` 得分 `82`，`005` 在 pipe 预算后得分 `75`。因此 `benchmark-failed-cases` 关闭；剩余风险转入长链路控制和稳定回归流程。

Markdown memory 压缩改造后，`010` 再次完成单 case 闭环：

- run id：`20260518-compression-md-010-rerun2`
- subject：`completed`
- artifact：`artifact_extracted=true`
- judge：`scored`
- score：`72`
- 观察：压缩协议稳定，2 次 `context_summary` 均为 `compression_mode=markdown_memory` 且 `warnings=[]`。中途仍出现两次可恢复 `document_edit` 失败，但 runner 不将其作为内容评分指标。

2026-05-20 工具重构 smoke run 补充：

- run id：`20260520-tools-refactor-smoke`
- 范围：`001`、`005`、`010`
- 模式：`--skip-judge`，只验证 subject agent 和 artifact 抽取，不进行内容评分。
- 结果：三个 case 均为 `artifact_extracted`，`round_failed=false`。
- 旧报告问题：`case_selection_report.md` 曾将 subject-only 未评分结果显示为 0% / case 建议，容易误解为 case 失败。当前批量报告已改为 `evaluation_report.md`，只展示运行状态、产物成功和评分结果。

当前 benchmark 侧开放 issue：

- `benchmark-stable-full-run`：沉淀全量低并发、复杂 case timeout、失败 case 单独复跑和结果汇总规范。
- `benchmark-run-transient-failures`：记录 provider streaming `ReadError`、quota、timeout 等外部或长轮次不稳定因素，避免将偶发运行失败误判为 case 不可运行。
- `skip-judge-report-classification`：已关闭。批量报告不再生成 case 建议，`--skip-judge` 结果只通过状态分布、产物成功和已评分次数呈现。

## 暂不处理

第一版评测基准暂不追求完全自动化评分。

可以先采用人工评分为主、LLM 辅助评分为辅的方式，重点沉淀稳定测试项和评分标准。待测试项稳定后，再接入自动化回归测试。

## 规范沉淀

本 issue 后续落到正式评测基准规范中维护：

- [评测基准规范索引](../benchmarks/README.md)
- [GitHub 中型项目软件专利技术方案评测基准](../benchmarks/software-patent-solution-github.md)
