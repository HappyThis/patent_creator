# 提高技术方案生成能力

> 状态：进行中
> 最后更新：2026-05-20
> 关闭条件：系统能够稳定把粗粒度软件需求转化为合理、可实施、具备保护价值的技术方案，并且主 agent / 子 agent / 文档写入链路不再因协议问题阻断技术方案落地。

## 背景

当前系统能够根据少量议题生成完整的专利交底书，输出字数较多，结构也较完整。但实际使用中发现，完整文档并不等于有效成果：如果核心技术方案不合理，后续生成的背景技术、发明内容、实施例等都会围绕一个不成立的方案展开，导致用户需要花费大量时间阅读和判断。

对于专利交底书而言，最关键的不是先生成长文档，而是先形成一个逻辑自洽、可实施、具备保护价值的技术方案。

## 状态总览

文件级状态仍为 `进行中`。原因是核心能力目标尚未完全达成：系统已经能在多个 case 中产出并评分技术方案，子 agent 的旧 `submit_result` 协议问题、复杂 JSON 压缩协议问题、旧 `document_edit + operations` 心智负担和工具失败后绕路恢复问题已经明显收敛；但复杂 case 中仍存在 provider 流式中断、写入策略成本、工具参数预防、写作阶段上下文成本和技术方案质量不足等链路稳定性与能力质量问题。

已关闭子问题：

- `reasoning_content` 保存与回放已按 provider/profile 处理。
- 旧 `document_edit + operations` 公开入口已移除，当前拆分为低负担文档写入工具，内部 op 分发器也已移除。
- `append_child_section` 参数协议已统一为 `parent_section_id` + `section`。
- `duplicate_section_id` 的根因已通过 v2 文档结构解决：`section.id` 由系统生成，章节语义迁移到 `section.type`。
- benchmark runner 已调整为只评价最终技术方案内容，不再把可恢复过程异常作为内容评分或诊断指标。
- `section-writer-submit-result` 旧协议问题已关闭：当前子 agent 不再使用复杂 `submit_result` envelope，统一通过 `write_pipe(content)` 少量多次传递内容，并通过 `finish({})` 结束。
- `context-compression-invalid-json` 旧压缩协议问题已关闭：上下文压缩不再要求模型输出 JSON 或历史 tool call 协议块，当前改为 Markdown memory，并由程序包装为单条历史记忆 message。
- `subagent-task-boundary` 的阻断级问题已关闭：`section_writer` 已被重新定位为轻量局部写作工具，实测中不再承担旧式整章大 JSON 提交；复杂正文主要由主 agent 基于分析结果直接落盘。
- `tool-failure-recovery` 已关闭为阻断级问题：`010` 复跑中两次 `document_edit` 失败后，主 agent 均优先修正同一工具调用并重试成功，未再绕行读取内部文档文件。
- `benchmark-failed-cases` 已关闭：`005`、`007` 均已完成单独排查和复跑，证明不是不可运行 case。

仍开放子问题：

- provider 流式响应仍可能在长轮次中断；`010` 首次复验在 `1056s` 处因 `httpx.ReadError` 结束为 `round_failed`。
- 主 agent 虽然能自恢复，但仍会先尝试超过 1500 字的文档写入，导致可预防的 `edit_too_large` 失败。
- 主 agent 仍可能在较晚阶段才写入 `technical_solution`；成功复跑中约 `585s` 才开始落盘，若 provider 在此之前中断仍可能无 artifact。
- 主 agent 已有 artifact 后仍可能继续重型 review / analyst 子 agent，导致已有产物无法进入 judge。
- 子 agent 移除最大步数后不再被机械截断，但 reviewer / analyst 类任务需要更强的语义收尾约束。
- 主 agent 缺少“够了就停”的完成策略，复杂任务中可能持续补查、补写、补审查，直到 round timeout。
- 复杂 case 中上下文压缩已稳定，但重复压缩和长上下文读取仍会带来明显耗时与 token 成本。
- 子 agent pipe 预算已经生效，但子 agent 仍常先超预算写入再压缩，预算规划不足。
- 写作阶段上下文仍可能过重，复杂 case 中小步写入仍携带大量材料历史，带来明显 token 成本。
- 技术方案质量本身仍需要通过更多正式 case 验证和提升。

## 开放问题优先级

P0：

- 技术方案质量验证不足。当前已通过至少 3 个 case 证明完整链路能跑通，但样本规模和失败 case 排查还不足以证明系统能稳定生成合理、可实施、具备保护价值的技术方案。
- provider 流式中断会直接破坏长轮次产物落地。需要设计对 `httpx.ReadError` / `llm_stream_error` 的恢复策略，尤其是已经有部分输出或工具调用历史时如何安全继续。
- 已有 artifact 后仍继续重型 review 的问题会导致本可评分的 case 变成 `round_failed`。主 agent 需要先保证可提交，再做有限增强。
- 子 agent 无最大步数后需要语义边界和父 agent 调度边界，避免 reviewer / analyst 长跑。
- 主 agent 需要明确“够了就停”的完成策略，避免复杂 case 持续补全到 timeout。

P1：

- 主 agent 过晚写入 artifact。复杂 case 中应优先形成可落盘的 `technical_solution` 草稿，再进行局部补强，避免超时或流式中断导致完全无产物。
- 默认分段写入策略不足。复杂技术方案应默认采用 `replace_section` 写摘要、再逐个 `append_child_section` 追加子章节，而不是先尝试一次性写完整大 section。
- 工具参数预防不足。旧 `operations` 问题已关闭，但模型仍可能超过单次 1500 字正文写入限制，需要从工具 schema、prompt 和可选预检查层降低可恢复失败出现概率。
- 子 agent pipe 预算规划不足。预算机制已能拒绝超长内容，但模型仍常先超额写入，再根据工具反馈压缩，增加调用成本。

P2：

- 子 agent 压缩复用与成本优化。当前 Markdown 压缩已稳定，但相同主上下文下反复启动子 agent 时可能重复压缩，后续可考虑按 parent context anchor 复用压缩记忆或减少压缩输入规模。
- 写作阶段上下文瘦身。复杂 case 进入写作阶段后不应继续携带完整项目读取历史，后续可考虑构造更轻的写作上下文。

## 问题

系统当前更偏向“交底书完整内容生成”，而不是“合理技术方案生成”。

这会带来两个主要问题：

- 用户阅读成本高：系统直接生成大量正文，用户需要从头阅读才能判断核心方案是否成立。
- 技术方案质量风险高：如果技术方案本身不合理，完整文档反而会掩盖问题，制造一种已经完成的错觉。

## 当前结论

现阶段先不急于设计固定流程或强制引导方式。

优先目标是：**提高系统生成合理技术方案的能力**。

也就是说，系统首先应具备把用户零散、不完整、业务化的描述转化为技术方案草案的能力。至于后续如何在交互中引导用户使用该能力，可以在能力建设之后再设计。

## 能力要求

系统至少需要具备以下能力：

- 从用户输入中识别可专利化的技术问题。
- 抽取装置、模块、步骤、数据结构、算法流程、控制逻辑等技术特征。
- 区分核心必要技术特征和可选/优选技术特征。
- 判断技术问题、技术手段和技术效果之间是否匹配。
- 识别只有业务目标、管理规则或效果描述但缺少技术手段的情况。
- 在信息不足时提出补全方向，而不是直接扩写完整交底书。
- 生成简明的技术方案草案，供用户先判断方案是否合理。

## 暂不处理

以下内容暂不作为本 issue 的重点：

- 是否强制用户先确认技术方案。
- 空项目下具体采用什么引导流程。
- 交底书完整章节的生成体验。
- 文档编辑器或页面交互优化。

这些问题可以在技术方案生成能力具备后再继续设计。

## 建议方向

可以先增加一个独立能力或任务类型：`生成技术方案草案`。

该能力的输出应短于完整交底书，重点包括：

- 技术问题
- 核心技术构思
- 必要技术特征
- 技术特征之间的协同关系
- 技术效果
- 当前不合理或缺失的信息
- 后续可展开为交底书的保护点

## 2026-05-13 E2E 试运行暴露的问题

在 GitHub 项目技术方案 benchmark 的 `001` case 试运行中，完整主 agent 链路暴露出以下能力问题：

- 主 agent 在“只落实技术方案章节”的任务下，仍倾向于从技术领域、背景技术、现有技术方案、技术问题等章节开始生成完整交底书内容。
- 主 agent 能够进行较充分的代码阅读，但在进入写作阶段后，目标章节控制不稳定，未优先完成 `technical_solution`。
- `section_writer` 子 agent 被调用来写技术方案，但提交结果格式不符合 `submit_result` 契约，导致 `subagent_invalid_submit_result`。
- 子 agent 失败后，主 agent 没有恢复到原目标章节，而是自行开始写完整文档前置章节。
- 长轮次代码阅读和写作导致模型调用超时，并触发上下文压缩；压缩阶段又出现无效 JSON，说明完整链路在长上下文任务下稳定性不足。

这些问题说明，技术方案生成能力不仅需要更好的 prompt，还需要更可靠的执行约束和错误恢复：

- 主 agent 需要更强的章节目标保持能力。
- 子 agent 输出契约需要进一步加固，避免无效 proposal 破坏整轮任务。
- 子 agent 失败后的主 agent 恢复策略需要回到用户目标，而不是改写其他章节。
- 长上下文任务需要降低无关章节读取和写作，减少超时和压缩失败概率。

## 2026-05-13 复跑补充

`001` case 复跑时，主 agent 最终成功把内容写入 `technical_solution`，说明章节目标保持能力有所改善。但过程中仍出现一次可恢复的 `document_edit` 失败：agent 在技术方案内部生成“技术效果”子章节时复用了交底书模板已有的 `technical_effects` section id，触发 `duplicate_section_id`。

该问题不属于技术方案内容质量本身，但会影响“技术方案能否稳定落地到交底书文档”。后续需要在文档编辑工具或技术方案写入策略中处理内部子章节 id 去重，避免技术方案内部结构与顶层交底书章节结构冲突。

## 2026-05-14 待修问题补充

本轮记录以下文档编辑链路问题，按修复状态维护：

已完成：

- `reasoning_content` 的保存与回放已按模型 provider/profile 处理：session log 保存模型原始 assistant message；实际请求回放时再由当前 provider profile 决定是否携带该字段，避免中途换模型后丢失历史信息或向不兼容模型发送协议字段。
- `document_edit` 对字符串化的 `operations` 已增加窄口径工具层防御：仅当 `operations` 本身是字符串时尝试解析为 JSON 数组；解析失败、解析后不是非空数组或数组项不是对象时返回明确错误。
- `append_child_section` 参数协议已统一为唯一格式：必须使用 `parent_section_id` 指定父章节，使用 `section` 指定新增子章节；提示词、schema 描述和工具错误信息均不兼容 `section_id`、`child` 或 `child_section` 旧写法。

已确定设计方向：

- `duplicate_section_id` 不再按“自动改名兼容”思路处理。根因是旧结构把 `section.id` 同时作为机器身份和章节语义使用，并要求 agent 生成全局唯一 id。新的目标结构改为：`section.id` 只作为系统生成的稳定身份，`section.type` 表达标准章节语义，`section.title` 表达展示标题；agent 不再为新增 section 手写 id。

已完成：

- 交底书 schema 已升级为目标 `v2`：新项目初始化使用 `sec_000001` 形式的系统生成章节 id，并在 `meta.id_counters` 中维护 `section` 与 `block` 计数器。
- 文档读写工具已改为按系统生成 `section_id` 定位，新增 section 时自动生成 id；`append_child_section` 与 `replace_section` 的 `section` 对象均不允许携带 id。
- 标准章节语义已迁移到 `section.type`，技术方案、技术效果等业务逻辑按 `type` 识别；普通子章节必须使用 `type:"custom"`，用 `title` 表达“整体架构”“处理流程”“技术效果”等展示语义。
- render AST、prompt 示例、benchmark artifact 抽取和相关测试已同步迁移到新 id 体系。

## 2026-05-14 MiMo 试运行补充

在 `software_patent_solution_github` 的 `001` case 中，主 agent 最终成功写入并提交了技术方案，但仍暴露出两个会影响技术方案生成稳定性的执行问题：

1. `section_writer` 子 agent 不按工具协议提交结果。
   - 现象：子 agent 直接回复长文本，而不是调用 `submit_result` 返回结构化 proposal。
   - 影响：主 agent 无法直接复用子 agent 的写作结果，只能把该次子 agent 调用视为工具失败；如果主 agent 没有良好恢复能力，技术方案可能无法落地到交底书。
   - 当前处理：executor 已将连续直接回复标记为 `subagent_plain_response` 并快速失败，避免无限纠正。
   - 后续方向：需要继续强化子 agent 的输出契约，或评估是否在技术方案生成 benchmark 中减少对子 agent 的依赖。

2. 主 agent 仍可能先给出不合规的 `document_edit.operations`。
   - 现象：主 agent 第一次把 `operations` 作为无法解析的字符串提交。历史 runner guard 曾将其拦截为 `<invalid_operations>`。
   - 影响：即使最终能够自我修正，也会增加轮次、token 消耗和失败不确定性；在更弱模型或更复杂 case 下，可能直接导致无法提取技术方案。
   - 当前处理：真实 `document_edit` 已有窄口径字符串解析防御；该内容 benchmark 不再把可恢复工具异常作为评分或诊断指标。
   - 后续方向：需要从主 agent 工具调用提示、模型 provider 差异继续收敛；该问题只在导致无法产出技术方案 artifact 时影响内容 benchmark 结果。

## 2026-05-15 完整评分链路复跑补充

`software_patent_solution_github` 的 `001` case 已经跑通 subject 与 judge。后续确认该 benchmark 只评价最终技术方案内容，不再把可恢复的工具异常、子 agent 协议失败或中途写入重试作为评分或诊断指标；这些过程问题保留在 `session_events.jsonl` 中供 debug。

以下问题仍属于系统执行链路稳定性问题，但不再作为该技术方案内容 benchmark 的评价目标：

1. 子 agent 协议失败仍然存在。
   - 失败原因：`section_writer` 连续直接回复文本，未调用 `submit_result`。
   - 诊断码：`subagent_plain_response`
   - 影响：主 agent 无法直接接收结构化 proposal，只能把子 agent 调用视为失败后自行恢复。
   - 后续方向：继续强化子 agent 输出契约；如果不同模型对 tool call 支持不稳定，需要评估是否让主 agent 在技术方案 benchmark 中直接写入，减少对子 agent 的强依赖。

2. 主 agent 工具调用参数仍会退化为字符串化 JSON。
   - 失败原因：`document_edit.operations` 被输出为字符串 JSON，而不是数组对象。
   - 影响：真实工具已有窄口径防御，主 agent 通常可恢复，但会增加轮次和 token 消耗。
   - 后续方向：继续收敛主 agent 的工具调用格式；该问题只在导致无法产出技术方案 artifact 时影响内容 benchmark 结果。

3. 工具失败后的恢复策略不稳定。
   - 现象：`document_edit` 参数错误后，主 agent 曾通过 `exec_command` 查找内部 `disclosure.json`，而不是直接修正 `document_edit` 参数重试。
   - 影响：该行为虽然没有导致本次失败，但会增加无关路径探索，并可能在更复杂 case 中绕开预期文档工具边界。
   - 后续方向：主 agent prompt 与工具错误信息应明确要求：工具参数错误时优先修正同一工具调用，不应通过文件系统直接读写内部交底书数据。

## 2026-05-18 管道协议落地与最难 case 复验补充

基于最新管道协议改造后，使用 `software_patent_solution_github` 的 `010` case 作为复杂样本复验。该 case 的技术主题、项目上下文和目标方案都更复杂，适合观察子 agent 调度、上下文压缩和长轮次写作稳定性。

本次观察结论：

1. 旧的 `section-writer-submit-result` 问题可以关闭。
   - 当前子 agent 已不再使用 `submit_result` 结构化 envelope。
   - `solution_refiner` 和两次 `section_writer` 调用均成功使用 `write_pipe` 写入内容，并通过 `finish({})` 结束。
   - 未观察到旧的 `submit_result` 大 JSON 协议失败，也未观察到 `section_writer` 直接回复文本导致的 `subagent_plain_response`。
   - 因此，历史 issue `section-writer-submit-result` 从开放列表移入已关闭列表。

2. 新暴露的问题是 `section_writer` 任务边界失效。
   - 主 agent 仍可能把两个大段落、多组要点或近似整章正文交给 `section_writer`。
   - 这与当前设计中的“轻量局部写作工具”定位冲突。
   - 影响是子 agent 虽然能通过 pipe 协议交付结果，但会消耗大量轮次和上下文预算，使复杂 case 更容易超时。
   - 后续方向：主 agent 调度策略应要求复杂技术方案正文由主 agent 自己写；`section_writer` 只处理一个短段落、一组局部 bullet 或一个待润色片段。执行层也可以在 `execute_subagent` 入口对 `section_writer` 的 goal 做轻量任务边界校验。

3. `material_analyst` 过度读取导致上下文压缩风险。
   - 复杂 case 中 `material_analyst` 进行了大量项目读取，触发多次上下文压缩。
   - 第三次压缩曾失败，错误为 `context_compression_invalid_output`，具体表现为压缩后的 assistant message 同时不满足 content / preserved tool call 约束。
   - 该问题不是 benchmark runner 的评分分类问题，而是 agent 执行器和上下文管理层的鲁棒性问题。
   - 后续方向：为重读型子 agent 设置更小的步数或读取预算；压缩层对 invalid output 增加一次重试或确定性裁剪 fallback。

4. 主 agent 写入 artifact 太晚。
   - 复杂 case 中主 agent 在多次子 agent 调用后仍未及时 `document_edit` 写入 `technical_solution`。
   - 一旦 round timeout，runner 无法抽取有效 artifact，judge 也无法进入。
   - 后续方向：benchmark 任务下主 agent 应优先落地一个可评测技术方案草稿，再使用子 agent 做局部补强；不要把“充分阅读和委派”放在第一次 artifact 写入之前无限延长。

当时打开的新增 issue：

- `subagent-task-boundary`：收紧 `section_writer` 使用边界，避免主 agent 把长写作任务委派给轻量子 agent。
- `subagent-overreading-compression`：限制重读型子 agent 的读取深度和压缩失败影响面。
- `late-artifact-write`：要求复杂 case 先形成可落盘草稿，再做局部增强，避免超时无 artifact。

后续状态见下一节；其中 `subagent-task-boundary` 已从阻断级开放问题移入关闭列表，`subagent-overreading-compression` 中的压缩格式失败部分已由 Markdown memory 方案关闭，剩余问题转为压缩成本与读取预算优化。

## 2026-05-18 Markdown 压缩复验与 issue 状态更新

在落地 Markdown memory 压缩方案后，使用 `software_patent_solution_github` 的 `010` case 连续复验：

- `20260518-compression-md-010`：subject 运行到 `1056s` 时因 provider streaming `httpx.ReadError` 失败为 `round_failed`，未抽取 artifact；但压缩链路稳定，11 次 `context_summary` 均为 `compression_mode=markdown_memory` 且 `warnings=[]`。
- `20260518-compression-md-010-rerun2`：完整跑通 subject + artifact + Codex-as-judge，最终 `scored=72`；2 次 `context_summary` 均为 `compression_mode=markdown_memory` 且 `warnings=[]`。

本轮可关闭的子 issue：

- `context-compression-invalid-json`：旧 JSON / preserved tool call 压缩协议已移除，弱 Markdown 校验 + fallback 后未再复现压缩格式阻断。
- `section-writer-submit-result`：继续保持关闭。当前子 agent 通过 `write_pipe` + `finish` 交付，未再复现旧 `submit_result` 或 `subagent_plain_response` 阻断。
- `subagent-task-boundary`：作为阻断级 issue 可关闭。复验中 `section_writer` 未再承担旧式整章大 JSON 提交；主 agent 已能基于分析结果直接写入文档，或只使用轻量局部写作。
- `tool-failure-recovery`：作为阻断级 issue 可关闭。`010-rerun2` 中两次 `document_edit` 失败后，主 agent 均修正同一工具调用并继续写入，没有绕开文档工具。

仍需保留或新增的 issue：

- `provider-stream-readerror-recovery`：长轮次仍可能因 provider streaming `httpx.ReadError` 失败。需要设计在已有部分上下文、部分子 agent 结果或部分文档写入时的安全续跑策略。
- `document-write-segmented-policy`：主 agent 仍需要稳定采用“主体摘要 + 子章节逐段追加”的写入策略，避免重复补写和长链路清理。
- `document-write-argument-preflight`：文档写入工具仍需要继续降低参数错误和重复调用概率。旧 `operations` 参数已移除，不再作为当前开放问题跟踪。
- `compression-cost-reuse`：Markdown 压缩已稳定，但复杂 case 中重复压缩相同主上下文仍可能造成明显耗时和 token 成本。后续可考虑基于 context anchor 复用压缩结果。
- `solution-quality-depth`：`010-rerun2` 得分 72，judge 指出缺少完整 Workspace proxy、MCP descriptor/wrapper、浏览器 callable 与内部 RPC 权限隔离、认证路由绑定和 revision 广播模型等关键机制。该问题属于技术方案质量提升，不是协议稳定性问题。

## 2026-05-20 复杂 case 复测补充

本轮重点复测 `007`、`005`、`010`，用于观察移除子 agent 最大步数、`document_edit` 单次写入限制和主 agent 小步写入策略后的实际效果。

结论：

1. 协议层问题明显缓解。
   - `007` 已能完成并进入 judge，最终 `scored=82`。
   - `010` subject 约 `248s` 完成，最终 `scored=82`，相比此前接近 `1200s` 才收尾有明显改善。
   - 当前更主要的问题不是 `write_pipe + finish` 是否能传回结果，而是长任务如何及时结束。

2. `005` 暴露出已有 artifact 后仍继续重型 review 的问题。
   - `005` 已写出并提取 `evaluated_artifact.md`，但 subject 状态仍为 `round_failed`。
   - 原因是主流程在已有技术方案后继续调用 `consistency_reviewer`，最终耗尽 `1200s` round timeout。
   - 这说明主 agent 需要“先可提交，再有限增强”的完成策略，不能把完整 review 作为结束前置条件。

3. 移除子 agent 最大步数后，不能再依赖机械截断保证收尾。
   - 该改动解决了 `007` 一类复杂子 agent 被硬切断的问题。
   - 但 reviewer / analyst 类子 agent 仍需要任务语义边界：只输出短结论、缺口列表或局部证据，不承担完整长链路闭环。

4. 文档写入工具小步写入有效，旧复杂 `operations` 入口已移除。
   - `010` 基本按多次追加子章节写入，说明单次写入限制和提示策略有效。
   - 后续重点从“复杂 JSON 参数失败”转向“重复写入、完成时机和工具边界控制”。

专项跟踪文档见：[Benchmark 长链路可控完成问题](./2026-05-20-benchmark-long-chain-control.md)。

## 2026-05-20 `005` pipe 预算复跑补充

在引入子 agent pipe 写入预算后，复跑 `005`：

- run id：`20260520-113247-005`
- subject 状态：`completed`
- subject 用时：约 `969.6s`
- judge 状态：`scored`
- 分数：`75`

已改善：

- `material_analyst`、`solution_refiner`、`consistency_reviewer` 的超长 pipe 写入均被 `pipe_budget_exceeded` 拒绝。
- 三个子 agent 均能根据剩余额度压缩输出，并最终主动 `finish`。
- 这说明当前 pipe 预算机制能有效抑制子 agent 交付内容失控，旧的“长 pipe / 不 finish”风险已有明显缓解。

新增或仍开放的问题：

- 主 agent 已有 artifact 后仍调用 `consistency_reviewer`，完成策略仍未收束。
- pipe 预算只限制交付内容，不限制 reviewer 在写 pipe 前大量读取章节。
- 主 agent 仍可能重复提交同一批文档写入工具调用，生成重复子章节。
- 当前文档写入工具不支持删除子章节，模型可能通过清空 blocks 留下空壳章节。
- 主 agent 通过 `exec_command` 直接修改 `disclosure.json`，绕过文档写入工具边界。这是新的 P0 工具权限问题。
