# 建立技术方案生成能力评测基准

> 状态：进行中
> 最后更新：2026-05-15
> 关闭条件：核心 benchmark 规范稳定，完成足够数量的正式 case，并能稳定跑通 subject 与 Codex-as-judge 内容评分链路。

## 背景

系统后续需要重点提升技术方案生成能力。为了判断每次 prompt、agent 职责、输出结构或调度策略调整是否真的带来提升，需要建立一套评测基准。

该评测基准不应评估完整交底书字数或格式完整度，而应评估：在给定软件项目上下文和技术方案需求的情况下，系统能否生成合理、可实施、具备专利保护价值的软件技术方案。

## 状态总览

文件级状态仍为 `进行中`。原因是 benchmark 建设本身尚未完成：正式 case 数量、项目快照固化、参考方案、rubric 和批量回归流程仍需要继续完善。

已关闭子问题：

- 已明确本 benchmark 只评价最终 `disclosure.technical_solution` 中提取出的技术方案内容。
- 已移除 runner 对 `document_edit` 的章节 guard。
- 已移除可恢复工具异常统计，`subagent_plain_response`、字符串化 `operations`、中途 `document_edit` 失败等过程问题不再进入内容评估指标。
- 已保留 `session_events.jsonl` 作为 debug 材料。
- 已跑通 `001` 的 subject 与 Codex-as-judge 完整评分链路。

仍开放子问题：

- 继续扩充和筛选正式 case。
- 为正式 case 固化项目快照版本、参考方案、rubric 和 metadata。
- 至少跑通 3 个黄金 case 后，再扩大到正式回归集。
- 沉淀批量运行、汇总和横向比较流程。

## 开放问题优先级

P0：

- 完成至少 3 个黄金 case。每个 case 需要有稳定项目快照、粗粒度需求、参考方案、rubric 和 metadata。当前只有单个 case 跑通，无法判断系统改动是否具有普遍提升。
- 跑通 3 个黄金 case 的完整 subject + Codex-as-judge 链路，并形成可横向比较的结果汇总。没有这个闭环，benchmark 很难用于后续 prompt、agent 或模型适配回归。

P1：

- 继续筛选正式 case 来源，优先覆盖 agent / AI 工具链、开发工具、同步系统、权限系统等机制型软件项目。
- 统一 case 质量门槛，避免把 bug fix、小补丁、过细需求或已经暴露答案的需求放入核心 benchmark。

P2：

- 从黄金 case 扩展到正式 20 个左右的回归集。
- 建立长期批量运行、结果趋势对比和模型横向比较流程。

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

- 核心测试项应优先选择较大的功能迭代、新功能支持、新特性机制、能力扩展或架构级改造。
- 小修改、局部 bug 修复、依赖升级、参数调整、文案或样式修改不应作为核心 benchmark 测试项。
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
9. 先完成 3 个黄金测试项试运行，再扩展到正式 20 个测试项。

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
- 将 `run_case.py --skip-judge` 跑通至少 3 个黄金 case，再接入 Codex-as-judge。
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

## 暂不处理

第一版评测基准暂不追求完全自动化评分。

可以先采用人工评分为主、LLM 辅助评分为辅的方式，重点沉淀稳定测试项和评分标准。待测试项稳定后，再接入自动化回归测试。

## 规范沉淀

本 issue 后续落到正式评测基准规范中维护：

- [评测基准规范索引](../benchmarks/README.md)
- [GitHub 中型项目软件专利技术方案评测基准](../benchmarks/software-patent-solution-github.md)
