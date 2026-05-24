# GitHub 中型项目软件专利技术方案评测基准

## 文档定位

本文档定义一个基于 GitHub 中型开源项目的软件专利技术方案生成评测基准。

该评测基准用于评估本系统中的技术方案生成 agent，能否在已准备好的项目环境中，以用户提出的技术方案需求文本作为任务输入，主动搜索和理解项目上下文，并生成合理、可实施、具备专利保护价值的软件技术方案草稿。

本文档中的“被测 agent”指专利交底书系统内负责生成技术方案草稿的 agent 能力。Codex、Claude Code、Cursor 等外部 coding agent 或模型接口可以作为执行后端、调试工具或对照组，但正式主评测的目标不是评估这些外部工具本身，而是评估本系统在同一输入契约下产出技术方案的能力。

## 依赖文档

- [评测基准规范索引](README.md)
- [Agent 基本设计原则](../core/agent-principles.md)
- [子 Agent 定义](../core/subagents.md)
- [Tools 设计](../core/tools.md)

## 相关文档

- [GitHub Issues](https://github.com/HappyThis/patent_creator/issues)

## 评测目标

该评测基准重点评估：

- agent 是否能在当前项目环境中自行搜索、阅读和定位相关上下文。
- agent 是否能从粗粒度需求中抽象出真实的软件技术问题。
- agent 是否能将技术方案需求转化为软件专利中的技术问题。
- agent 是否能提出具体的软件技术手段，而不是空泛地扩写功能描述。
- agent 是否能在缺少人工指定源码路径提示的情况下，提出合理的软件机制、数据结构、状态流转和边界处理。
- agent 是否能主动说明仍需确认的不确定性，而不是编造当前项目环境中不存在的源码事实。
- agent 是否能形成问题、手段、效果之间的闭环。

不评估：

- 生成完整专利交底书的能力。
- 生成代码 patch 的能力。
- 开放 web search 能力。
- 对整个 GitHub 项目生态的泛化记忆。

## 项目筛选标准

候选 GitHub 项目应满足：

- star 数：`1k - 5k`。
- 有效代码量：`1w - 10w` 行。
- 项目规模：中型项目，具备真实架构和持续维护记录。
- 语言优先级：TypeScript、Python、Go、Rust、Java。
- 活跃度：近一年有提交、issue 或 PR 活动。
- 资料质量：有 README、关键文档、测试、issue 或 PR 讨论。
- 项目类型：优先选择开发工具、构建工具、文档系统、工作流引擎、数据处理、权限系统、同步系统、缓存系统、可观测性、AI 工具链等软件机制明确的项目。

排除：

- 巨型知名项目，避免阅读成本过高和模型记忆污染。
- 玩具项目、教程项目、模板项目。
- 主要价值在 UI 视觉、内容运营或配置封装的项目。
- 主要测试项只涉及依赖升级、文案修改、样式调整、简单 bug fix 的项目。
- 缺少 issue/PR 讨论或难以还原技术约束的项目。

## 测试项来源

每个测试项应尽量来自真实项目中的一个问题、改进或设计讨论。

优先来源：

- feature request + design discussion。
- 较大的功能迭代 + merged PR。
- 架构机制增强 + accepted implementation。
- performance / reliability / security problem + accepted implementation。
- maintainer 明确否定某些方案的讨论。

辅助来源：

- bug report + root cause + merged fix。

bug 修复类材料只有在能抽象出可复用的软件机制时，才可以进入正式测试项。单点修补、边界条件修复、参数调整、文案修正、依赖升级、样式调整、测试补齐等材料不得作为正式测试项。

正式测试项应优先选择“较大的功能迭代”或“机制设计型改进”，而不是小修改。一个合格测试项通常应满足：

- 需要 agent 阅读多个模块或跨层上下文，而不是只定位一个局部函数。
- 参考方案包含明确的软件机制、状态流转、数据结构、调度策略、缓存策略、权限策略、同步策略或 agent 编排策略。
- 技术效果能被表述为系统能力提升，而不是仅仅消除某个报错。
- 方案具有一定可泛化性，可以转化为专利交底书中的技术构思。

有效性判断标准：

- 如果一个测试项删掉“bug 修复”表述后，仍然能被表述为“系统新增或增强了一种能力、特性或机制”，则可以作为正式测试项候选。
- 如果一个测试项只能被表述为“修好了某个错误”，则不应作为正式 benchmark 测试项。
- 第一批正式测试项应优先来自新功能支持、新特性机制、能力扩展或架构级改造。
- 缺陷修复材料可以作为辅助样本或反例样本，但不应主导正式长期回归基线。

测试项可按来源性质标注为：

- `feature_iteration`：功能迭代型，作为正式测试项优先保留。
- `mechanism_enhancement`：机制增强型，作为正式测试项优先保留。
- `systemic_bugfix`：系统性缺陷修复型，仅在机制价值充分时保留。
- `local_bugfix`：局部 bug 修复型，原则上不进入正式 benchmark。

测试项不是原始 issue 的复制，而是基于项目上下文整理出的技术方案需求。

## 测试项三件套

每个测试项的核心结构为：

1. `snapshot.json`
2. `request.md`
3. `reference_solution.md`

其中：

- `snapshot.json` 是隐藏的项目快照描述，记录仓库、快照 commit、拉取策略和建议关注路径。它用于 runner 准备项目环境、评测维护、溯源、人工复核和必要时的隐藏评审参考，不作为 agent 文本输入。
- `request.md` 是给 agent 的技术方案需求。
- `reference_solution.md` 是隐藏示例技术方案，用于评分，不给 agent。

同时配套：

- `rubric.md`
- `metadata.json`

注意：项目候选不等于测试项。一个 GitHub 项目只是测试项来源池，一个项目可以产出多个测试项，也可能在深挖后不产出正式测试项。

## 目录结构

```text
benchmarks/software_patent_solution_github/
  benchmark.json
  runner.md
  judge.md
  projects/
    README.md
    .gitignore
    <locally_cloned_repo>/
  cases/
    <case_id>/
      snapshot.json
      request.md
      reference_solution.md
      rubric.md
      metadata.json
```

当前运行目录已初始化：

- [评测运行目录](../../benchmarks/software_patent_solution_github/README.md)

其中 `projects/` 是本地源码 clone 工作区，目录内容默认被 git 忽略；`cases/` 只存放已经从具体 issue、PR 或设计讨论中提炼出来的正式测试项。

`snapshot.json` 不直接保存源码。它只保存可复现拉取项目快照所需的信息。正式评测时，runner 根据该文件准备项目 checkout，并将 checkout 目录作为被测 agent 的当前工作目录，或显式告知项目环境路径；但 runner 不应把 `snapshot.json` 或 `focus_paths` 作为输入文本提供给被测 agent。

## 快照规则

项目快照应固定在解决方案合入之前的 commit。

原因：

- agent 应基于问题发生时的项目状态生成技术方案。
- agent 不应直接读到最终实现。
- 参考方案可以基于后续已合并 PR 和讨论整理，但不得放入 agent 可见上下文。

快照应记录：

- 仓库地址
- 快照提交
- 关联 issue
- 关联 PR
- 快照日期
- 代码行数
- 排除路径
- 纳入路径

其中纳入路径或 `focus_paths` 只是 case 维护者使用的隐藏阅读线索，用于确认参考方案是否有项目依据。它不模拟真实用户输入，正式评测时不得暴露给 agent；agent 需要自己在当前项目环境中搜索定位。

## request.md 规范

`request.md` 是 agent 可见的用户需求。

它应包含：

- 项目背景的简要说明。
- 用户希望新增、增强或解决的能力目标。
- 用户可感知的场景问题、业务目标或使用约束。
- 与项目相关的关键约束。
- 需要生成技术方案草稿的明确要求。

它不应包含：

- 参考方案的核心实现细节。
- 关键技术机制名称或方案骨架，例如直接点名状态机、索引、缓存失效、single-flight、TTL、调度身份、权限矩阵等。
- 评分标准。
- 明确的输出结构模板。
- 对 agent 搜索路径的直接提示。

`request.md` 的粒度应模拟普通用户或产品侧提出的粗粒度场景需求。它可以说明“想支持什么能力”“当前体验或系统能力不足在哪里”“有哪些已知约束”，但不应把实现路径提前喂给 agent。否则评测会退化为技术方案改写能力，而不是技术方案生成能力。

推荐写法：

- 描述用户目标和业务场景。
- 描述现有系统已经具备的相关能力或模块边界。
- 描述当前还不能很好支持的场景。
- 要求 agent 基于需求生成技术方案草稿。

不推荐写法：

- 直接列出应采用的关键算法、数据结构或并发控制方式。
- 直接给出模块拆分、流程步骤或缓存/索引/状态字段。
- 把参考 PR 的设计结论改写成 agent 可见需求。

示例结构：

```md
# 技术方案需求

当前项目希望支持 <新能力或新特性>。

用户在 <场景> 下需要 <目标>，但现有系统还不能很好支持。项目中已经存在 <相关能力或使用约束>，需要在不破坏现有约束的前提下扩展该能力。

请生成一个可用于专利交底书的技术方案草稿。方案应说明系统如何实现该能力，以及相比现有方式能带来什么技术效果。
```

## `reference_solution.md` 规范

`reference_solution.md` 是隐藏示例方案。

它应基于真实 issue、PR、代码修改和人工专利化整理形成。

它应包含：

- 技术问题。
- 核心技术构思。
- 必要技术特征。
- 目标能力边界，说明哪些属于必须解决，哪些属于可选增强。
- 核心数据结构、状态模型、协议字段或配置项。
- 关键流程或模块关系。
- 异常、恢复、幂等、并发、权限、清理等边界处理。
- 与项目现有模块的集成点，避免变成脱离代码库的空泛方案。
- 必须命中的评分锚点。
- 常见错误方案或反例，用于稳定区分中低分答案。
- 技术效果。
- 与项目约束的对应关系。
- 可选技术特征。

它不是唯一标准答案。评分时应允许 agent 提出不同但合理的方案。
但参考方案不应只给“方向”，而应达到隐藏技术设计说明书级别：评审者即使不阅读真实 PR，也能据此判断一个候选方案是否覆盖了关键机制、状态流转和工程边界。

## `rubric.md` 规范

`rubric.md` 是测试项级评分标准。

它应包含：

- 本测试项的关键观察点。
- 高分方案应覆盖的机制。
- 明确扣分项。
- 禁止或不鼓励的方案。
- 与参考方案的比较重点。

示例：

```md
# 评分标准

## 关键观察点

- 是否识别出问题本质是派生内容依赖失效，而不是普通文件变更检测。
- 是否提出依赖记录、反向索引、摘要比对和失效传播机制。
- 是否避免退化为全量构建。
- 是否避免要求用户手动声明依赖。

## 扣分项

- 只说使用缓存提高效率。
- 只说使用 AI 判断需要重建的页面。
- 要求用户手动配置每个页面依赖。
- 没有说明如何识别未修改但受影响的页面。
```

## `metadata.json` 规范

`metadata.json` 用于评测运行器管理测试项。

建议字段：

```json
{
  "id": "case_001_incremental_build_invalidation",
  "title": "增量构建失效传播机制",
  "benchmark_id": "software_patent_solution_github",
  "case_version": "1.0.0",
  "snapshot_version": "1.0.0",
  "rubric_version": "1.0.0",
  "source": {
    "repo": "owner/repo",
    "repo_url": "https://github.com/owner/repo",
    "snapshot_commit": "<commit>",
    "issue_urls": [],
    "pr_urls": [],
    "snapshot_date": "YYYY-MM-DD"
  },
  "project": {
    "primary_language": "TypeScript",
    "stars_at_collection": 2300,
    "effective_loc": 42000,
    "included_paths": [],
    "excluded_paths": []
  },
  "case": {
    "difficulty": "medium",
    "context_shape": "prepared_project_environment",
    "expected_capabilities": [
      "codebase_reading",
      "requirement_understanding",
      "technical_problem_abstraction",
      "technical_solution_generation"
    ],
    "tags": [
      "incremental_build",
      "dependency_graph",
      "cache_invalidation"
    ]
  },
  "visibility": {
    "agent_visible": [
      "request.md",
      "prepared project checkout as current working directory or environment path"
    ],
    "hidden_from_agent": [
      "snapshot.json",
      "focus_paths",
      "reference_solution.md",
      "rubric.md",
      "metadata.json"
    ]
  }
}
```

## 被测 agent 运行规则

runner 应向 agent 提供且只能提供以下输入包：

1. 系统或任务说明：`runner.md` 的全文。
2. 环境位置：已 checkout 到指定快照 commit 的项目目录，优先作为当前工作目录；如果执行框架不能设置当前工作目录，只能额外提供一句“项目环境路径：<path>”。
3. 用户需求：当前测试项的 `request.md` 全文。

runner 不应向 agent 提供：

- 参考方案。
- rubric。
- `snapshot.json`。
- `focus_paths`。
- `metadata.json`。
- related PR 的最终代码改动。
- 来源 issue、PR 或最终实现 diff 的链接和摘要。
- case 难度、标签、评分维度或人工筛选说明。
- 评分器输出。

输入包的推荐顺序为：

```text
<runner.md>

项目环境路径：<仅当无法设置 cwd 时出现>

<request.md>
```

其中 `request.md` 不应包含精确源码路径、目标类名、目标函数名或已知实现方案；除非这些信息是普通用户需求中自然会出现的产品概念，而不是 benchmark 维护者提供的解题提示。

`runner.md` 本身是 agent 可见文本，因此也不应提及 `snapshot.json`、`focus_paths`、参考方案、rubric、最终 PR diff 等评测侧概念。隐藏材料的隔离规则只能写在本规范、机器可读配置和 runner 实现中，不能写进 agent-facing prompt。

agent 可执行：

- 搜索和阅读当前项目环境。
- 将技术方案落实到当前交底书文档的技术方案章节。

agent 不需要执行：

- 修改当前项目环境中的源码、文档、测试或配置。
- 运行测试。
- 生成完整交底书。

## 输出要求

被测 agent 的目标不是在最终聊天回复中输出技术方案，而是通过完整主 agent 对话链路，把技术方案落实到当前交底书文档中。

技术方案章节内容应尽量包含：

- 技术问题。
- 核心技术方案。
- 必要技术特征。
- 关键模块或流程。
- 技术效果。
- 与需求场景的对应关系。
- 风险或待确认问题。

测试项的 `request.md` 不单独规定输出结构，以避免每个测试项风格漂移。

## 评估产物提取

评估器只评估一个产物：主 agent 写入 `disclosure.json` 的技术方案章节内容。

具体规则：

1. runner 通过完整主 agent 对话接口提交输入。
2. 主 agent 本轮结束后，runner 读取当前交底书文档。
3. runner 只抽取 `technical_solution` 章节，生成 `evaluated_artifact.md`。
4. judge 仅基于 `evaluated_artifact.md`、隐藏参考方案和 rubric 评分。

最终聊天回复、工具调用轨迹、子 agent pipe 内容、session event、文档 diff 等不作为评分输入。它们可以用于 runner 调试和失败排查，但不能参与正式评分。

runner 应在工具层限制文档写入工具的目标章节：正式评测中只允许写入 `technical_solution`。如果主 agent 尝试编辑技术领域、背景技术、现有技术方案、技术问题、技术效果等其他章节，工具应返回失败结果，促使主 agent 回到技术方案章节。

如果 `technical_solution` 章节为空，或内容明显不是技术方案，runner 可以继续向同一主 agent 会话发送固定补充指令，要求继续充实技术方案章节。补充次数由 runner 配置控制；超过次数后仍未形成有效技术方案的 case 记为 `skipped_no_solution_artifact`，不得使用最终聊天回复兜底评分。

## 评分维度

建议总分 100 分：

- 环境阅读与需求理解：15 分。
- 技术问题识别：15 分。
- 技术手段具体性：20 分。
- 必要技术特征完整性：10 分。
- 问题-手段-效果闭环：15 分。
- 需求约束遵守：10 分。
- 可实施性：10 分。
- 专利化价值：5 分。

每个维度应采用 1-5 档描述，再折算为分数。

## 评分方式

第一版建议采用三层评分：

1. 规则检查。
2. LLM judge。
3. 人工抽检校准。

规则检查关注：

- 是否产生技术方案草稿。
- 是否引用或体现项目上下文。
- 是否包含技术问题、技术手段、技术效果。
- 是否明显违反 request 中的约束。

LLM judge 关注：

- 分项打分。
- 扣分原因。
- 与参考方案的差距。
- 是否存在空泛表达或错误假设。

人工抽检关注：

- LLM judge 是否过松或过严。
- 参考方案是否过窄。
- rubric 是否需要调整。

## 结果报告

每次评测运行应保存：

- agent 输出。
- agent 搜索/阅读轨迹。
- judge 分项分数。
- judge 扣分原因。
- 总分。
- 与上一版本 agent 的对比。

报告不应只看平均分，还应关注：

- 哪些测试项退步。
- 哪些维度退步。
- 是否更容易生成空泛方案。
- 是否更好地利用项目上下文。
- 是否更遵守约束。

## 最小完整版本

该评测基准的第一个完整版本建议包含 20 个测试项：

- 12 个真实项目改进型测试项。
- 4 个架构机制型测试项。
- 4 个陷阱或反例型测试项。

每个测试项都应具备：

- 隐藏来源项目快照。
- 技术方案需求。
- 隐藏示例技术方案。
- 测试项级评分标准。
- 完整元数据。

少于 20 个测试项可以用于试运行，但不应作为正式长期回归基线。

## 质量门槛

一个测试项进入正式评测基准前，应满足：

- 仅凭需求也能评估 agent 的技术方案生成能力。
- 隐藏项目快照和真实实现能够支撑参考方案与评分标准。
- 参考方案不是简单复述 PR，而是完成了专利化重构。
- 评分标准能区分空泛方案和具体技术方案。
- 测试项不依赖开放网络。
- 测试项可在未来重复运行。
