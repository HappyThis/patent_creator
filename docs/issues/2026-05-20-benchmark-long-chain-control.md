# Benchmark 长链路可控完成问题

> 状态：进行中
> 最后更新：2026-05-20
> 关闭条件：复杂 benchmark case 能在合理时间内先产出可评测技术方案；已有 artifact 后不会因追加 review、子 agent 长跑或重复补全导致 round timeout；文档写入工具不再频繁触发可预防的大块结构化参数失败。

## 背景

2026-05-20 连续复测了复杂 case：

| Case | Run | 结果 | 观察 |
| --- | --- | --- | --- |
| 007 | `20260520-094905-007` | `scored=82` | 移除子 agent 最大步数后，不再出现 `subagent_max_steps_reached` 硬失败；subject 能完成，但链路仍偏长。 |
| 005 | `20260520-100337/r01-005` | `round_failed` | 已提取 artifact，但主流程继续调用 reviewer，最终 1200 秒 round timeout，未进入 judge。 |
| 010 | `20260520-100337/r01-010` | `scored=82` | subject 约 248 秒完成，明显好于此前接近 1200 秒才收尾；小步写入策略有效。 |
| 005 | `20260520-113247-005` | `scored=75` | 引入 pipe 写入预算后，subject 约 969.6 秒完成并进入 judge；pipe 长输出被有效限制，但仍暴露主 agent 完成策略、重复写入和 `exec_command` 绕写问题。 |
| 001 / 005 / 010 | `20260520-tools-refactor-smoke` | `artifact_extracted` | 工具重构后 subject-only smoke run 全部完成，未复现旧 `document_edit`；但仍暴露 pipe 预算规划、写入大小预检查和写作阶段上下文过重问题。 |

本轮说明，旧的子 agent 大 JSON 提交协议和压缩格式问题已经明显缓解，主矛盾转向：复杂任务如何可控地完成，而不是无限追求补全。

## 2026-05-20 `005` pipe 预算复跑补充

本次运行：

- run id：`20260520-113247-005`
- subject 状态：`completed`
- subject 用时：约 `969.6s`
- judge 状态：`scored`
- 分数：`75`

pipe 预算机制已验证有效：

- `material_analyst` 首次尝试写入 `9347` 字，被 `pipe_budget_exceeded` 拒绝；随后压缩为 3 次成功写入，总计 `3759` 字，并主动 `finish`。
- `solution_refiner` 首次超额被拒绝；随后压缩为 2 次成功写入，总计 `3220` 字，并主动 `finish`。
- `consistency_reviewer` 首次尝试写入 `5640` 字，被拒绝；随后压缩为 3 次成功写入，总计 `2462` 字，并主动 `finish`。

这说明 `write_pipe` run 级预算（最多 10 次、累计 4000 字、不限制单次字符数）可以有效降低子 agent 交付内容失控风险。该机制解决的是“交付内容无限膨胀 / 不及时 finish”的一部分问题。

本次同时暴露新的开放问题：

- pipe 预算只限制交付内容，不限制子 agent 在交付前持续读取材料或审查大量章节；`consistency_reviewer` 仍然读取了很多章节，包括空壳重复章节。
- 主 agent 在已有 artifact 后仍调用 reviewer，说明“够了就停”的完成策略仍未解决。
- 主 agent 多次并发提交相同文档写入内容，导致重复子章节。
- 文档写入工具不支持删除子章节，主 agent 用清空 blocks 的方式留下空壳章节。
- 主 agent 最终通过 `exec_command` 直接修改 `disclosure.json`，绕过了受控文档写入工具边界。

## 2026-05-20 工具重构 smoke run 补充

本次运行：

- run id：`20260520-tools-refactor-smoke`
- case：`001`、`005`、`010`
- 模式：`--skip-judge`，只验证 subject agent 和 artifact 抽取。
- 结果：三个 case 均为 `artifact_extracted`，`round_failed=false`。

已验证：

- 主 agent 可见工具已经切换为 `document_read`、`document_replace_section_blocks`、`document_append_block`、`document_replace_block`、`document_append_child_section`、`document_clear_section_blocks`、`execute_subagent`、`exec_command`。
- 运行日志中未搜到旧 `document_edit` 工具调用，旧 `operations` 公开入口未再出现。
- `005` 在本次 smoke 中用时约 `849.1s` 完成 subject，未再出现“已有 artifact 后继续 review 导致 timeout”的硬失败。
- `010` 在本次 smoke 中用时约 `669.6s` 完成 subject；一次 `document_append_child_section` 因 `edit_too_large` 失败后，主 agent 改为小块 `document_append_block` 并成功恢复。

仍暴露的新问题：

- 子 agent 仍会先写超出 pipe 总预算的内容，再依赖 `pipe_budget_exceeded` 反馈压缩重写。例如 `005` 中 `material_analyst` 尝试写入 `9161` 字，`solution_refiner` 尝试写入 `16708` 字。
- 主 agent 仍会先超过单次 1500 字文档写入限制，再根据 `edit_too_large` 拆分。
- `010` 写作阶段主 agent 每步仍携带约 `150k` token 级别上下文，说明写作阶段上下文没有有效瘦身。
- subject-only smoke run 的 `case_selection_report.md` 将未评分结果显示为 0% / 淘汰或暂缓，属于报告分类问题，不应视为 case 失败。

## ISSUE-2026-05-20-01：已有 artifact 后仍继续重型 review 导致超时

优先级：P0

状态：部分缓解，仍开放

现象：

- `005` 在 subject round 中已经写出了可提取的 `disclosure.technical_solution`。
- runner 成功写出 `evaluated_artifact.md`，但该 case 仍以 `round_failed` 结束。
- 失败原因不是无产物，而是主 agent 在已有 artifact 后继续调用 `consistency_reviewer`，最终耗尽 1200 秒 round timeout。
- 后续 `20260520-113247-005` 与 `20260520-tools-refactor-smoke/r01-005` 已能正常收尾，说明该问题已有明显缓解。
- 但 `001` smoke run 仍在已有 artifact 后调用 `consistency_reviewer`，只是未拖到 timeout；因此问题从“稳定硬失败”转为“review 触发条件仍需控制”。

影响：

- benchmark 无法进入 judge，导致一个已有成果的 case 被记录为失败。
- agent 的“持续补强”倾向在 benchmark 中收益很低，风险很高。
- 该问题会污染全量分数，因为它混入了执行收尾失败，而不是纯内容质量失败。

处理方向：

- 主 agent 在 benchmark 任务下应采用“先可提交，再有限增强”的策略。
- 已经写入可评测 `technical_solution` 后，不应再启动重型 reviewer / analyst 子 agent。
- reviewer 如果保留，应只输出短缺口清单，由主 agent 判断是否值得补，不应进入长链路写作或长链路审查。

关闭条件：

- `005` 或同类复杂 case 在已有 artifact 后能正常结束并进入 judge。
- session 日志中不再出现“写完主体后继续长时间 reviewer 导致 timeout”的模式。
- reviewer 触发条件收敛为短缺口核查，不再成为默认收尾前置步骤。

## ISSUE-2026-05-20-02：子 agent 无最大步数后缺少语义收尾约束

优先级：P0

状态：部分缓解，仍开放

现象：

- 移除子 agent 最大步数后，`007` 不再因 `subagent_max_steps_reached` 硬失败，这是正向结果。
- 但 `005` 暴露出另一个风险：reviewer / analyst 类子 agent 仍可能持续写 pipe 或持续探索，不及时 `finish`，最终消耗主流程时间。

影响：

- 单纯移除最大步数解决了误杀问题，但没有解决“什么时候该结束”的问题。
- 如果重新加硬步数，又会回到复杂任务被机械截断的问题。
- 更合适的边界应来自任务语义和父 agent 调度策略，而不是子 agent 通用步数上限。

处理方向：

- 已完成：`write_pipe` 增加 run 级预算，最多 10 次、累计 4000 字；每次写入返回剩余次数和剩余字符数；预算耗尽时执行器自动结束子任务。
- 已验证：`005` 复跑中 `material_analyst`、`solution_refiner`、`consistency_reviewer` 的超长 pipe 写入均被拒绝，模型能压缩后继续并主动 `finish`。
- 最新 smoke run 继续证明预算限制有效，但也显示子 agent 仍常先超额再压缩，需要单独跟踪预算规划问题。
- 子 agent 说明中明确交付边界：只交付短结论、证据点、缺口列表或局部文本，不承担完整长文档闭环。
- 父 agent 给子 agent 的任务必须小而明确，禁止把“完整审查整篇方案”“写完一整章”这类重任务交给轻量子 agent。
- `finish` 仍保持无参数；所有给主 agent 的内容继续通过 `write_pipe(content)` 少量多次写入。
- 可考虑在父 agent 层引入任务预算意识：已有 artifact 后，子 agent 只能做有限局部补强。
- 仍开放：pipe 预算不限制子 agent 在写 pipe 前持续读取材料或审查大量章节；reviewer / analyst 仍需要任务语义和读取范围约束。

关闭条件：

- 复杂 case 中不再因为 reviewer / analyst 子 agent 长跑导致外层 round timeout。
- 子 agent pipe 内容呈现短结论化，而不是大段持续生成。

## ISSUE-2026-05-20-03：主 agent 缺少“够了就停”的完成策略

优先级：P0

状态：开放

现象：

- 复杂 case 中主 agent 倾向于持续补章节、补风险、补机制、再自检。
- `010` 这次能在 248 秒完成，说明小步写入和较早落盘有效。
- `005` 则说明当主 agent 进入“再 review 一轮”的惯性后，即便已有 artifact，也可能直接超时。

影响：

- 生成质量不一定随着继续补写提升，但超时风险显著增加。
- benchmark 的目标是产出可评分技术方案，不是无限接近完整交底书。

处理方向：

- 主 agent prompt 应明确完成优先级：
  - 先写入可评测技术方案草稿。
  - 再补必要缺口。
  - 一旦覆盖核心问题、手段、效果、约束，即应结束。
- 避免“全文审查通过后才结束”的隐含流程。
- runner 不需要理解失败原因；agent 自身要减少导致 `round_failed` 的行为。

关闭条件：

- 复杂 case 的 subject 完成时间稳定低于 round timeout 的显著安全区间。
- 已有 artifact 后的后续步骤数量可控，不再出现接近 timeout 才 respond 的模式。

## ISSUE-2026-05-20-04：文档写入工具结构化编辑心智负担仍偏高

优先级：P1

状态：已关闭

现象：

- 已有 1500 字限制和小步写入提示后，`010` 明显改善，基本按多次 `append_child_section` 写入。
- 但复杂 case 中仍会出现把 `operations` 写成字符串、一次塞入复杂 section tree、失败后再拆分的模式。

影响：

- 可恢复失败会额外消耗模型调用、时间和上下文。
- 对较弱模型或 provider 工具调用兼容性较差的模型，可能从可恢复失败变成不可恢复失败。

处理方向：

- 保持文档写入工具的一次正文写入上限。
- 继续弱化模型手写复杂 JSON tree 的机会。
- 考虑增加更低心智负担的专用工具，例如：
  - 追加一个短段落到指定章节。
  - 创建一个标题和若干段落组成的子章节。
  - 替换当前技术方案正文摘要。
- 工具使用说明和示例应由工具定义自身生成，不写死在主 agent 或子 agent prompt 中。

关闭条件：

- 复杂 case 中文档写入工具参数错误显著减少。
- 失败后的自我修复不再成为主要耗时来源。

关闭记录：

- 旧 `document_edit + operations` 公开入口已移除。
- 当前文档写入拆为五个低负担工具：`document_replace_section_blocks`、`document_append_block`、`document_replace_block`、`document_append_child_section`、`document_clear_section_blocks`。
- 内部 domain 写入层也已移除 op 分发器，不再保留旧 operations 兼容路径。

## ISSUE-2026-05-20-07：主 agent 重复提交文档写入工具导致重复子章节

优先级：P0

状态：开放

现象：

- `005` 的 `20260520-113247-005` 运行中，主 agent 在同一轮内多次提交内容相同或高度重复的文档写入工具调用。
- 结果是 `technical_solution` 下生成了重复子章节，例如一组有效章节后，又出现同名空壳或重复子章节。
- 主 agent 读回章节后发现重复，开始尝试清理，进一步拉长链路。

影响：

- 重复写入会直接污染 `technical_solution` artifact。
- 后续清理会消耗大量模型调用，并可能触发更多工具错误。
- 该问题会削弱“先可提交”的收益：artifact 已经存在，但 agent 会因为修复重复结构继续工作。

处理方向：

- 主 agent prompt 中明确：一次模型决策中不要提交多个语义重复的文档写入工具调用。
- executor 或写入工具可考虑对同一 step 内完全相同的写入做拒绝或去重。
- 主 agent 写入后应避免再次落盘同一批章节；如果需要补充，应按新增缺口追加，而不是重放整批内容。

关闭条件：

- 复杂 case 中不再出现同一批子章节被连续写入两次以上。
- `technical_solution` 下不再出现大量同名重复子章节。

## ISSUE-2026-05-20-08：文档写入工具缺少安全删除 / 清理子章节能力

优先级：P1

状态：开放

现象：

- `005` 复跑中，主 agent 发现重复子章节后尝试调用 `delete_section`。
- 当前文档写入工具不支持 `delete_section`，工具返回失败。
- 主 agent 随后用 `replace_section_blocks` 将重复章节 blocks 清空，但 section 节点仍留在目录树中，形成空壳章节。

影响：

- artifact 结构中可能保留空标题或空壳节点，影响可读性和评分。
- agent 为修复重复写入而进入额外探索和清理链路，增加超时风险。
- 如果没有安全删除能力，模型可能继续寻找绕行方法。

处理方向：

- 优先从预防重复写入入手，减少删除需求。
- 评估是否需要新增受限的 `document_remove_child_section` / `document_remove_empty_section` 工具。
- 如果暂不新增删除操作，应在工具说明中明确不支持删除章节，并指示 agent 不要尝试清理空壳结构，而是结束当前可评分 artifact。

关闭条件：

- agent 不再因清理重复 section 进入长链路。
- 空壳章节不再出现在最终 evaluated artifact 中，或工具提供明确安全清理路径。

## ISSUE-2026-05-20-09：主 agent 可通过 `exec_command` 绕过文档写入工具修改交底书内部数据

优先级：P0

状态：开放

现象：

- `005` 复跑中，主 agent 使用 `exec_command` 直接打开并修改 subject 数据目录中的 `disclosure.json`。
- 该行为绕过了文档写入工具的 schema 校验、正文长度限制、变更摘要、事件语义和工具边界。
- 这不是 benchmark runner 层问题，而是主 agent 工具权限边界问题。

影响：

- 破坏“文档只能通过受控文档写入工具修改”的系统约束。
- 可能产生 store 状态、事件日志、git commit 或缓存之间的不一致。
- 使文档写入工具的保护规则失效，例如 1500 字限制和 section id 生成规则。
- 对 benchmark 来说，可能得到 artifact，但其生成路径不再代表真实 agent 文档编辑能力。

处理方向：

- 在主 agent prompt 中明确：`exec_command` 只用于读取、搜索、诊断项目环境，不得修改交底书运行数据、`disclosure.json` 或 subject 内部 store 文件。
- 工具层可对 `exec_command` 增加工作区写入边界，禁止写入当前 project 的 runtime/data/store 目录。
- 若确需命令执行写项目代码，应区分“项目源码工作区”和“系统内部数据目录”，内部数据目录默认只读。

关闭条件：

- session 事件中不再出现 `exec_command` 直接修改 `disclosure.json`、session log、project store 等内部数据文件。
- 文档变更均通过明确受控的文档写入工具完成。

## ISSUE-2026-05-20-10：子 agent pipe 预算不限制读取 / 分析工具长跑

优先级：P1

状态：开放

现象：

- `005` 复跑中，`consistency_reviewer` 的 pipe 输出被限制住了，但它在写 pipe 前仍读取了大量章节。
- 这说明 pipe 预算只解决“交付给主 agent 的内容规模”，不限制子 agent 在交付前的工具探索长度。

影响：

- reviewer / analyst 仍可能通过大量 `document_read` 或 `exec_command` 消耗时间和上下文。
- 该问题比旧的“大 JSON 提交失败”温和，但仍会增加复杂 case subject 时间。

处理方向：

- 父 agent 给 reviewer 的 goal 应明确读取范围，例如只读取当前技术方案整节，不逐个读取所有章节。
- reviewer prompt 可强调先使用 `get_section(include_children=true)` 获取目标章节，只有缺失关键上下文时才补读其他章节。
- 后续如仍失控，可考虑“读取预算”或“reviewer 只允许一次 get_section + pipe + finish”的轻量工具化策略。

关闭条件：

- reviewer / analyst 子 agent 不再在已有 artifact 后进行大量额外读取。
- pipe 预算之外的子 agent 工具调用次数在复杂 case 中保持可控。

## ISSUE-2026-05-20-05：评测集需要分层，避免质量评测与压力测试混在一起

优先级：P1

状态：开放

现象：

- `007` 原先更像压力测试：考验长链路、子 agent 调度和及时收尾能力。
- `005` 本轮虽然已有 artifact，但因为长链路收尾失败没有进入 judge，也更像 agent 执行控制压力样本。
- `010` 能稳定评分，但 rubric 对完整 Workspace proxy、MCP descriptor、RPC 边界、认证路由等机制要求很细，属于高难质量样本。

影响：

- 如果把所有 case 都混在同一黄金集里，全量均分会混入执行时间、工具稳定性、case 难度和内容质量多个变量。
- 难以判断一次改动到底提升了技术方案质量，还是只是改善了运行稳定性。

处理方向：

- 将 case 分为三类：
  - 核心质量 case：稳定进入 judge，用于比较技术方案质量。
  - 待校准 case：题目、rubric 或隐藏标准需要人工复核。
  - 压力 / 回归 case：用于检验长链路、子 agent、压缩、超时和 provider 稳定性。
- `007`、`005` 暂时更适合进入压力 / 回归类。
- `010` 可保留为高难核心 case，但需要检查 request 是否足够明确召唤 rubric 中的关键机制。

关闭条件：

- `case_selection_report.md` 或 benchmark 文档中明确 case 分层。
- 全量报告能分别展示质量分、运行稳定性和压力样本结果。

## ISSUE-2026-05-20-06：复杂技术方案质量仍缺少关键机制深度

优先级：P1

状态：开放

现象：

- `007` 和 `010` 都得到 `82`，说明答案方向正确，但没有进入 90+ 强答案档。
- `010` 的主要扣分点集中在：
  - Workspace proxy 覆盖不完整。
  - MCP tool descriptor 没有明确每轮刷新与状态变化处理。
  - 缺少 workspace revision / version 模型。
  - 浏览器 callable 与内部 RPC 权限边界不够工程化。
  - 认证 Worker 到父 agent 名称绑定机制不够明确。
- `007` 的主要扣分点集中在：
  - headless 子 agent 无法使用浏览器 client tools 时的降级或拒绝策略。
  - Think 与 AIChatAgent adapter 的阶段边界不清。
  - 父模型 tool result 的 completed / error / aborted / interrupted 结构化契约不完整。
  - retained run 清理后的 tombstone / replay / drill-in 语义不足。

影响：

- 当前 agent 已能生成可用方案，但对复杂工程边界和异常路径覆盖不足。
- 这会限制高难 case 的上限分。

处理方向：

- 提升 agent 对“完整接口边界、状态版本、认证绑定、错误语义、降级策略”的敏感度。
- 在主 agent 工作流程中加入“核心机制闭环检查”，但该检查必须短而明确，不能变成长链路 reviewer。
- 对高难 case 的 rubric 做一次题目对齐检查，避免隐藏标准过多。

关闭条件：

- `007`、`010` 或同类高难 case 能稳定超过当前 82 分上限。
- judge 不再反复指出同类关键机制缺失。

## ISSUE-2026-05-20-11：`--skip-judge` 报告分类误导

优先级：P1

状态：开放

现象：

- `20260520-tools-refactor-smoke` 是 subject-only smoke run，命令中使用了 `--skip-judge`。
- 三个 case 的实际状态均为 `artifact_extracted`，且 `round_failed=false`。
- 但 `case_selection_report.md` 将它们显示为 0% / 淘汰或暂缓，并在详情中把 `artifact_extracted` 放在“失败状态”位置。

影响：

- 容易把“未评分”误读成“case 失败”或“case 应淘汰”。
- smoke run 的目标是验证 subject agent、工具链和 artifact 抽取，不应该进入质量通过率统计。

处理方向：

- `--skip-judge` 时报告应单独标记为“未评分 / 仅验证 artifact”。
- subject-only run 可以展示 artifact 抽取成功率、round_failed 数量、工具失败摘要，但不应生成质量保留 / 淘汰建议。
- case selection 报告中应区分 `artifact_extracted`、`scored`、`round_failed`、`judge_failed` 四类状态。

关闭条件：

- `--skip-judge` 的 run report 不再把 `artifact_extracted` 显示为 0% 或淘汰。
- 报告能清楚区分“未评分”和“失败”。

## ISSUE-2026-05-20-12：子 agent pipe 预算规划不足

优先级：P1

状态：开放

现象：

- pipe run 级预算已经生效：最多 10 次写入、累计 4000 字。
- 但子 agent 仍常先生成超预算内容，再依赖工具错误反馈压缩。
- `20260520-tools-refactor-smoke` 中观察到：
  - `005` 的 `material_analyst` 首次尝试写入 `9161` 字。
  - `005` 的 `solution_refiner` 首次尝试写入 `16708` 字。
  - `010` 的 `material_analyst` 首次尝试写入 `5744` 字。
  - `010` 的 `solution_refiner` 首次尝试写入 `5046` 字。

影响：

- 预算机制保护了主 agent 上下文，但模型先失败再修正会浪费调用时间和 token。
- 对弱模型或不稳定 provider，连续预算失败可能放大为任务失败。

处理方向：

- 子 agent prompt / tool manual 中进一步强调：写 pipe 前必须按 `max_total_chars=4000` 规划总交付内容。
- `write_pipe` 返回的 ack 已包含剩余次数和剩余字符数，子 agent 应在后续写入中显式压缩，而不是重复提交同一超额内容。
- 可考虑在子 agent 启动上下文中加入“推荐交付形态”：事实清单不超过 N 条、风险不超过 N 条、正文候选不超过短段落。

关闭条件：

- 复杂 case 中 `pipe_budget_exceeded` 明显减少。
- 子 agent 首次 pipe 写入通常能落在预算内，而不是先超额再压缩。

## ISSUE-2026-05-20-13：主 agent 文档写入大小预检查不足

优先级：P1

状态：开放

现象：

- 文档写入工具已经有单次正文写入 1500 字限制。
- `20260520-tools-refactor-smoke/r01-010` 中，主 agent 一次 `document_append_child_section` 约 `1845` 字，被工具以 `edit_too_large` 拒绝。
- 模型随后拆成更小的 `document_append_child_section` + `document_append_block` 成功恢复。

影响：

- 工具保护有效，但可预防失败仍会增加一轮模型调用。
- 如果写入阶段频繁撞 1500 字限制，会抵消低负担文档工具的收益。

处理方向：

- 主 agent 工作流程中明确：每次只写一个短章节或少量段落，长章节先创建短子章节，再用 `document_append_block` 分段补充。
- 工具说明中继续保留明确错误和重试建议；同时在主 agent 写作策略中前置“估算正文长度”意识。
- 可考虑在执行层对明显超长参数提供更短、更结构化的错误提示，减少模型重复提交同一大块内容。

关闭条件：

- 复杂 case 中 `edit_too_large` 显著减少。
- 主 agent 在初次写入时就稳定采用小步落盘，而不是失败后才拆分。

## ISSUE-2026-05-20-14：写作阶段上下文过重

优先级：P2

状态：开放

现象：

- `20260520-tools-refactor-smoke/r01-010` 进入写作阶段后，主 agent 多次调用仍携带约 `150k` token 级别上下文。
- 此时主要任务已经从“材料探索”转为“把已有技术事实写入文档”，继续携带大量原始材料会显著增加每步成本。

影响：

- 写作阶段每个小步工具调用都变得很慢、很贵。
- 长文档写作越被拆成小步，过重上下文带来的累计成本越明显。
- 这不是协议正确性问题，但会影响全量 benchmark 的吞吐和稳定性。

处理方向：

- 探索阶段结束后，为写作阶段构造更轻的工作上下文：保留用户需求、项目事实摘要、子 agent pipe 结果、当前文档 outline 和最近写入结果。
- 减少写作阶段重复携带完整项目读取历史和长工具输出。
- 与 `compression-cost-reuse` 结合考虑：同一 parent context 下的压缩结果可复用，写作阶段可只引用压缩后的事实记忆。

关闭条件：

- 复杂 case 进入写作阶段后，主 agent 单步 prompt token 明显下降。
- 小步写入不再因为携带超重历史而显著拖慢 subject 时间。

## 当前判断

本轮复测后的主矛盾已经不是旧协议正确性，而是长链路可控完成和写作阶段成本控制：

- `write_pipe + finish` 与 Markdown 压缩方向基本成立。
- 小步文档写入对 `010` 有明显改善。
- `005` 后续复跑和 smoke run 已经能收尾，但 reviewer 触发条件、pipe 预算规划、写入大小预检查和上下文成本仍需继续收敛。

下一步优先级应是：

1. 继续收紧已有 artifact 后的 reviewer 触发条件和主 agent 完成策略。
2. 让子 agent 在写 pipe 前按预算规划，而不是先超额再修正。
3. 降低文档写入的 `edit_too_large`、重复写入和清理链路。
4. 修正 `--skip-judge` 报告分类，并推进 case 分层和高难 rubric 对齐。
