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
| 010 | `20260520-after-reviewer-removal-010` | `scored=78` | 移除 `consistency_reviewer` 后不再复现 reviewer timeout；subject 约 687 秒完成并进入 judge。但主 agent 已写出主体后又重复调用 `material_analyst`，并出现多次 `pipe_budget_exceeded`，说明重复委派和子 agent 任务边界仍需处理。 |
| 001-010 | `20260520-full-10cases-5workers-after-reviewer-removal` | 9 个 `scored`，`008` 为 `judge_failed` | 5 worker 全量运行中 10 个 subject 均完成并抽取 artifact，说明 reviewer timeout 路径已消失；但 `004/006/008/009` 仍有明显长链路，`008` subject 约 734 秒才完成，judge 失败来自 Codex 插件同步 403/Cloudflare challenge。 |

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

本次当时暴露以下问题；其中 reviewer 专项问题后续已通过移除 `consistency_reviewer` 关闭，其他问题继续拆分跟踪：

- pipe 预算只限制交付内容，不限制子 agent 在交付前持续读取材料或审查大量章节；当时 `consistency_reviewer` 仍然读取了很多章节，包括空壳重复章节。
- 主 agent 在已有 artifact 后仍调用 reviewer；该 reviewer 路径已关闭，但“够了就停”的完成策略仍未完全解决。
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

## 2026-05-20 10 case / 5 worker 全量运行补充

本次运行：

- run id：`20260520-full-10cases-5workers-after-reviewer-removal`
- 命令：`bench.py batch 001 002 003 004 005 006 007 008 009 010 --workers 5 --round-timeout 1200 --judge-timeout 900`
- subject 结果：10/10 完成，均成功抽取 `evaluated_artifact.md`
- judge 结果：9/10 scored，`008` 因 judge 基础设施失败未评分

分数与耗时：

| Case | Subject 用时 | 最终状态 | 分数 |
| --- | ---: | --- | ---: |
| 001 | 512.2s | `scored` | 72 |
| 002 | 308.0s | `scored` | 82 |
| 003 | 363.5s | `scored` | 82 |
| 004 | 628.3s | `scored` | 78 |
| 005 | 476.3s | `scored` | 74 |
| 006 | 711.2s | `scored` | 89 |
| 007 | 417.1s | `scored` | 80 |
| 008 | 734.0s | `judge_failed` | - |
| 009 | 521.5s | `scored` | 78 |
| 010 | 223.5s | `scored` | 78 |

本轮结论：

- `consistency_reviewer` 移除后，`005` 从历史“已有 artifact 后继续 review 超时”恢复为 `scored=74`，reviewer timeout 问题可继续保持关闭。
- `007` 这类原先容易卡住的复杂子 agent case 能在约 417 秒完成并得分 80，说明 `write_pipe + finish`、Markdown 压缩和小步写入路径整体有效。
- `006`、`008` 仍然耗时较长，尤其 `008` subject 约 734 秒，说明长文档写作收尾、子 agent 任务规模和写作阶段上下文成本仍是主要风险。
- `004` 仍调用 `section_writer` 承接技术方案章节候选正文，且 `material_analyst`、`solution_refiner`、`section_writer` 均触发超过 200k token 的 subagent compression，说明“写作子 agent 轻量化”没有完全落地。
- `document_append_block` / `document_append_child_section` 仍会收到字符串化的 `block` / `blocks` 参数，失败后模型通常能改为对象或数组重试成功；该问题不再是旧 `operations` 协议，但仍是文档工具参数成功率问题。
- `008` judge 失败不是 subject agent 失败，而是 Codex judge 启动时远程插件同步访问 `chatgpt.com/backend-api/plugins/featured` 返回 403/Cloudflare challenge，随后 MCP 初始化失败。

## ISSUE-2026-05-20-01：已有 artifact 后仍继续重型 review 导致超时

优先级：P0

状态：已关闭

现象：

- `005` 在 subject round 中已经写出了可提取的 `disclosure.technical_solution`。
- runner 成功写出 `evaluated_artifact.md`，但该 case 仍以 `round_failed` 结束。
- 失败原因不是无产物，而是主 agent 在已有 artifact 后继续调用 `consistency_reviewer`，最终耗尽 1200 秒 round timeout。
- 后续 `20260520-113247-005` 与 `20260520-tools-refactor-smoke/r01-005` 已能正常收尾，说明该问题已有明显缓解。
- `001` smoke run 仍在已有 artifact 后调用过 `consistency_reviewer`，只是未拖到 timeout；该风险已通过移除 reviewer 子 agent 处理。

影响：

- benchmark 无法进入 judge，导致一个已有成果的 case 被记录为失败。
- agent 的“持续补强”倾向在 benchmark 中收益很低，风险很高。
- 该问题会污染全量分数，因为它混入了执行收尾失败，而不是纯内容质量失败。

处理方向：

- 主 agent 在 benchmark 任务下应采用“先可提交，再有限增强”的策略。
- 已经写入可评测 `technical_solution` 后，不应再启动重型 reviewer / analyst 子 agent。
- reviewer 如果保留，应只输出短缺口清单，由主 agent 判断是否值得补，不应进入长链路写作或长链路审查。

关闭记录：

- `consistency_reviewer` 已从当前子 agent 注册表和提示词路由中移除。
- `20260520-after-reviewer-removal-010` 验证：`010` 不再出现 reviewer timeout，subject 约 `687s` 完成，最终进入 judge 并 scored=`78`。
- `20260520-full-10cases-5workers-after-reviewer-removal` 验证：10 个 case 的 subject 均完成；`005` 正常进入 judge 并 scored=`74`，未再出现 reviewer timeout 路径。
- 残留的“已有主体后继续调用 analyst / refiner”不再归入 reviewer timeout，改由 `ISSUE-2026-05-20-03`、`ISSUE-2026-05-20-15` 和 `ISSUE-2026-05-20-16` 继续跟踪。

关闭条件：

- `005` 或同类复杂 case 在已有 artifact 后能正常结束并进入 judge。
- session 日志中不再出现“写完主体后继续长时间 reviewer 导致 timeout”的模式。
- reviewer 触发条件收敛为短缺口核查，不再成为默认收尾前置步骤。

## ISSUE-2026-05-20-02：子 agent 无最大步数后缺少语义收尾约束

优先级：P0

状态：已关闭

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
- 最新 smoke run 和 `20260520-after-reviewer-removal-010` 继续证明预算限制有效，子 agent 最终均能通过 `finish` 结束。
- 子 agent 说明中明确交付边界：只交付短结论、证据点、缺口列表或局部文本，不承担完整长文档闭环。
- 父 agent 给子 agent 的任务必须小而明确，禁止把“完整审查整篇方案”“写完一整章”这类重任务交给轻量子 agent。
- `finish` 仍保持无参数；所有给主 agent 的内容继续通过 `write_pipe(content)` 少量多次写入。
- pipe 预算规划不足、读取过多、任务过大、重复委派等残留问题已拆到独立 issue，分别由 `ISSUE-2026-05-20-10`、`ISSUE-2026-05-20-12`、`ISSUE-2026-05-20-15`、`ISSUE-2026-05-20-16` 跟踪。

关闭记录：

- 旧风险是“子 agent 不及时 finish 或无限写 pipe”。当前 `write_pipe` 预算、预算 ack 和无参 `finish` 已能兜住该类协议收尾问题。
- `20260520-after-reviewer-removal-010` 中 `material_analyst`、`solution_refiner`、`section_writer` 均成功 `finish`；第二次 `material_analyst` 也能 finish，问题转为“是否应该再次委派”和“委派任务是否过大”。

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
- `20260520-after-reviewer-removal-010` 中，主 agent 第二次调用 `material_analyst` 后，该子 agent 在写 pipe 前多次 `document_read`，并触发 context compression，`used_tokens=118436`。
- `20260520-full-10cases-5workers-after-reviewer-removal` 中，`004` 的 `material_analyst`、`solution_refiner`、`section_writer` 均触发超过 `200k` token 的 subagent compression；`006/008/009` 也出现长时间 subagent 运行和压缩，说明读取 / 分析长跑仍然存在。
- 这说明 pipe 预算只解决“交付给主 agent 的内容规模”，不限制子 agent 在交付前的工具探索长度。

影响：

- analyst / refiner 仍可能通过大量 `document_read` 或 `exec_command` 消耗时间和上下文。
- 该问题比旧的“大 JSON 提交失败”温和，但仍会增加复杂 case subject 时间。

处理方向：

- 父 agent 给 analyst / refiner 的 goal 应明确读取范围，例如只分析一个模块、一个流程或一个问题，不逐个读取所有章节。
- 子 agent prompt 可强调先使用已有 goal 摘要和必要的单次 `get_section(include_children=true)`，只有缺失关键上下文时才补读其他材料。
- 后续如仍失控，可考虑读取预算，或者在 `execute_subagent` 层限制同一子任务的 `document_read` / `exec_command` 次数。

关闭条件：

- analyst / refiner 子 agent 不再在已有 artifact 后进行大量额外读取。
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
- `20260520-after-reviewer-removal-010` 中仍观察到多次 `pipe_budget_exceeded`：`material_analyst`、`solution_refiner` 和第二次 `material_analyst` 都出现先超额、再压缩重写的模式。
- `20260520-full-10cases-5workers-after-reviewer-removal` 中，`008` 的 `solution_refiner` 曾单次生成 `6788` completion 后才继续写 pipe，后续又有多次较长 completion；预算限制了最终 pipe 内容，但没有阻止子 agent 在写入前长写。

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

## ISSUE-2026-05-20-15：子 agent 仍收到过大任务

优先级：P0

状态：开放

最新证据：

- `20260520-complex-after-reflection` 复跑 `007/009/010` 后确认：即使主 agent prompt 已要求调度前评估工作量，主 agent 仍会把多主题、全项目、跨模块任务交给子 agent。
- `007` 的 `material_analyst` goal 覆盖 Agent/Think/AIChatAgent、subAgent routing、tool registry、streaming event protocol、recovery、cancellation、access control、cleanup、concurrency 等多个方向，触发 subagent context compression，`used_tokens=278528`。
- `010` 的 `material_analyst`、`solution_refiner`、`consistency_reviewer` 均触发 context compression；其中 reviewer 之后主 agent 进入重复 `document_replace_block` 链路，最终 `1200s` round timeout。
- `20260520-after-reviewer-removal-010` 验证：移除 `consistency_reviewer` 后，`010` subject 阶段从此前 `1200s` timeout 改为约 `687s` 完成，最终进入 judge 并 scored=`78`。
- 同一次验证仍显示主 agent 在已经写入主要技术方案并自检后，再次调用 `material_analyst`，且 goal 与第一次大体重复；第二次调用触发 context compression，`used_tokens=118436`。
- `20260520-full-10cases-5workers-after-reviewer-removal` 中，`004/006/008/009` 仍体现“大任务交给子 agent，再由主 agent 长时间写作”的模式；`008` subject 约 `734s` 才完成，是本轮最慢 subject。
- `material_analyst` 和 `solution_refiner` 仍有多次 `write_pipe` 因 `pipe_budget_exceeded` 失败后再压缩重写，说明预算兜底有效，但 agent 对预算的前置规划仍不足。
- pipe 预算能限制子 agent 最终交付内容，但不能阻止子 agent 在写 pipe 前进行过重阅读、分析和压缩。
- 重复调用同一类子 agent 的问题已拆为 `ISSUE-2026-05-20-16`，本 issue 继续聚焦单次 goal 的任务规模。

影响：

- 子 agent 虽然不会再无限返回大 JSON，但仍会把大量时间和 token 消耗在重任务内部。
- 主 agent 仍然可能把“完整架构分析”“完整方案骨架”“跨多个模块的综合判断”外包给子 agent，导致复杂 case 运行成本高、收尾风险大。
- `write_pipe` 剩余额度和 `finish` 协议解决的是交付边界，不是任务规模边界。

已采取处理：

- 移除 `consistency_reviewer` 子 agent，避免主流程在已有 artifact 后把 review 当作默认收尾前置步骤。
- 一致性检查改为主 agent 的轻量自检职责，不再通过独立 reviewer 子 agent 执行。
- 已用 `20260520-after-reviewer-removal-010` 验证：旧 reviewer timeout 路径消失，case 能正常完成并评分。

后续处理方向：

- `execute_subagent` 层面对 goal 做任务规模预检查：过宽 goal 应拒绝或要求主 agent 拆分。
- 主 agent 调度规则从提示词约束升级为工具层约束：只允许局部、单目标、短交付物任务进入子 agent。
- 对 `material_analyst` 和 `solution_refiner` 分别定义可接受任务形态，例如“最多一个模块/一个问题/一个流程”，而不是“分析整个项目架构”。

关闭条件：

- 复杂 case 中不再出现单个子 agent goal 覆盖多个大型主题。
- 子 agent context compression 次数显著下降。
- `007/010` 这类高难 case 不再因子 agent 重任务或 review 后编辑循环接近 round timeout。

## ISSUE-2026-05-20-16：主 agent 重复委派同类子 agent

优先级：P0

状态：开放

现象：

- `20260520-after-reviewer-removal-010` 中，主 agent 已经完成主体技术方案写入、读回自检并进行多次 `document_replace_block` 补强后，又再次调用 `material_analyst`。
- 第二次 `material_analyst` 的 goal 与第一次高度相似，仍是围绕 Cloudflare Durable Objects 多会话助手系统抽取技术事实、术语、架构对应关系和风险点。
- 该重复委派触发 context compression，`used_tokens=118436`，并带来多次 `document_read`、多次 `write_pipe` 和后续替换块补写。
- 该行为没有导致本次 timeout，但把 subject 用时拉到约 `687s`；若叠加更慢 provider 或更大 case，仍可能重新接近 round timeout。

影响：

- 即使单次子 agent 任务被限制为轻量，重复委派也会让轻任务累积成重链路。
- 主 agent 会在已有可评测 artifact 后重新进入材料分析阶段，破坏“先可提交、有限增强、及时停止”的策略。
- 重复委派使 pipe 预算、读取预算和压缩成本被重复消耗，降低全量 benchmark 吞吐。

处理方向：

- `execute_subagent` 层记录本轮已经调用过的 `(agent_id, goal_fingerprint)`，对高度相似的重复 goal 返回拒绝或要求主 agent 说明新增缺口。
- 主 agent 在再次调用子 agent 前必须明确“本次委派解决哪个尚未覆盖的具体缺口”，不能重复执行材料抽取、整体架构分析、整体方案骨架这类已完成任务。
- 已有 `technical_solution` 后，子 agent 只能用于局部短补强，例如“补一个认证路由边界检查点”，而不是重新做全局分析。
- 重复委派被拒绝时，工具返回应提示主 agent：直接基于已有 pipe 内容和当前文档做有限编辑，或结束本轮。

关闭条件：

- 复杂 case 中，同一 round 不再出现同一子 agent 对高度相似 goal 的重复调用。
- 已有 artifact 后的子 agent 调用都能对应一个明确、局部、尚未覆盖的缺口。
- `010` 复跑不再出现写完主体后再次调用 `material_analyst` 做全局材料抽取。

## ISSUE-2026-05-20-17：文档写入工具对象 / 数组参数仍会字符串化

优先级：P1

状态：开放

现象：

- 旧 `document_edit.operations` 公开入口已经移除，但新文档工具的对象 / 数组字段仍会被模型错误地作为字符串传入。
- `20260520-full-10cases-5workers-after-reviewer-removal` 中多次出现：
  - `document_append_block.block` 被传成 JSON 字符串，例如字符串末尾多出 `]`，工具失败后模型再改为对象重试成功。
  - `document_append_child_section.blocks` 被传成 JSON 字符串，失败后模型再改为数组或压缩段落重试成功。
- 合法 JSON 字符串可被参数归一化救回一部分，但畸形字符串、混入 markdown 代码块或额外括号时仍会先失败。

影响：

- 这类失败通常可恢复，但会多消耗一次模型调用、几秒到几十秒时间和额外上下文。
- 在复杂 case 中，参数失败与 1500 字写入限制叠加，会放大长链路收尾问题。
- 该问题说明工具 schema 已经简化，但弱模型 / OpenAI-compatible provider 对嵌套对象字段的稳定性仍不足。

处理方向：

- 保持工具 schema 简单，不回到旧 `operations` 大 JSON。
- 参数归一化层继续对声明为对象 / 数组的字段做窄口径解析：字符串是合法 JSON 时解析；解析失败时返回明确错误和示例。
- 工具描述中强调 `block` 必须是对象、`blocks` 必须是数组，不要用引号包住整段 JSON。
- 主 agent 写作策略继续优先使用短段落 `document_append_block`，减少一次传入复杂数组的机会。

关闭条件：

- 复杂 case 中 `document_append_block status=failed` / `document_append_child_section status=failed` 因字符串化参数导致的失败显著减少。
- 参数失败不再成为 subject 时间的主要可恢复损耗。

## 当前判断

本轮复测后的主矛盾已经不是旧协议正确性，而是长链路可控完成和写作阶段成本控制：

- `write_pipe + finish` 与 Markdown 压缩方向基本成立。
- 小步文档写入对 `010` 有明显改善。
- `005` 后续复跑和 smoke run 已经能收尾，但复杂 case 证明子 agent 任务规模、pipe 预算规划、写入大小预检查和上下文成本仍需继续收敛。
- 10 case / 5 worker 全量运行中 subject 全部完成，说明可运行性显著改善；但 `008` judge 受 Codex 插件同步 403 影响失败，评测基础设施稳定性需要单独处理。
- `consistency_reviewer` 已从当前子 agent 集合中移除；review 不再作为独立子 agent 能力维护。

下一步优先级应是：

1. 将子 agent 任务规模边界和重复委派检查从提示词约束升级为工具层约束。
2. 让子 agent 在写 pipe 前按预算规划，而不是先超额再修正。
3. 降低文档写入的 `edit_too_large`、字符串化参数、重复写入和清理链路。
4. 修正 `--skip-judge` 报告分类和 judge 启动偶发失败，并推进 case 分层和高难 rubric 对齐。
