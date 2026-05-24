# 评测基准规范索引

## 文档定位

本文档定义本项目后续评测基准的分类方式、命名方式和通用测试项结构。

评测基准的目标不是评估模型是否会写长文本，而是评估 agent 在专利交底书协作过程中的关键能力是否稳定提升。

## 依赖文档

- [Agent 基本设计原则](../core/agent-principles.md)
- [子 Agent 定义](../core/subagents.md)
- [Tools 设计](../core/tools.md)

## 相关文档

- [GitHub Issues](https://github.com/HappyThis/patent_creator/issues)
- [GitHub 中型项目软件专利技术方案评测基准](software-patent-solution-github.md)
- [GitHub 中型项目候选清单](github-project-candidates.md)
- [GitHub 评测运行目录](../../benchmarks/software_patent_solution_github/README.md)

## 评测基准分类

后续评测基准按能力目标和上下文形态分类。

### 1. 技术方案生成类

评估系统能否从用户需求、项目上下文或资料中生成合理技术方案。

适合评估：

- `solution_refiner` 或后续技术方案类子 agent
- 主 agent 是否正确调用方案生成能力
- 技术问题、技术手段、技术效果之间是否闭环

子类型：

- 纯场景技术方案评测：只有场景输入，不提供项目上下文。
- 项目快照技术方案评测：提供固定项目快照，要求 agent 搜索、阅读并生成方案。
- 检索增强技术方案评测：允许 agent 使用 web search 或外部资料检索。

### 2. 资料理解类

评估系统能否从用户材料、项目文档、issue、PR、代码片段中抽取技术事实、术语、约束和待确认问题。

适合评估：

- `material_analyst`
- 上下文阅读质量
- 事实、推断、待确认项的区分能力

### 3. 交底书写作类

评估系统能否在技术方案已经明确的情况下，将方案展开为交底书章节正文。

适合评估：

- `section_writer`
- 章节结构组织
- 专利表达质量
- 是否避免把过程性讨论写入最终正文

### 4. 一致性自检类

评估系统能否检查技术方案、章节正文、术语、模块关系和效果描述之间的一致性。

适合评估：

- 主 agent 的轻量自检和完成态判断
- 术语一致性
- 问题-手段-效果闭环
- 章节之间是否互相矛盾

### 5. 端到端协作类

评估从用户输入到文档修改的一整轮或多轮协作质量。

适合评估：

- 主 agent 决策
- 子 agent 调度
- 工具调用顺序
- 文档落盘质量
- 用户反馈吸收能力

## 上下文形态分类

每个评测基准应明确上下文形态：

- `scenario_only`：只有场景输入。
- `fixed_snapshot`：提供固定项目或资料快照。
- `fixed_corpus_retrieval`：提供固定资料库，允许搜索/阅读。
- `open_web_retrieval`：允许开放 web search，适合产品实验，不适合作为第一版稳定回归。
- `interactive_session`：包含多轮用户输入，适合端到端协作评估。

## 通用目录结构

评测基准目录建议采用：

```text
benchmarks/
  <benchmark_id>/
    benchmark.json
    runner.md
    judge.md
    cases/
      <case_id>/
        request.md
        metadata.json
        snapshot.json
        reference_solution.md
        rubric.md
```

如果测试项不是 Git 仓库快照，而是固定资料库检索，可以增加：

```text
        corpus/
```

## 文件职责

- `benchmark.json`：评测基准级元数据，包括名称、版本、上下文形态、评分维度。
- `runner.md`：被测 agent 的统一任务说明。
- `judge.md`：LLM judge 的统一评分说明。
- `request.md`：给被测 agent 的用户需求或场景。
- `snapshot.json`：Git 项目快照描述，记录仓库、commit、拉取策略和建议关注路径。
- `corpus/`：给被测 agent 可搜索、可阅读的固定资料库。
- `reference_solution.md`：隐藏参考方案，用于评分和人工校准。
- `rubric.md`：测试项级评分标准。
- `metadata.json`：测试项级机器可读信息。

## 可见性规则

被测 agent 可见：

- `request.md`
- `snapshot.json` 以及 runner 根据它 checkout 出来的仓库，或 `corpus/`
- 评测运行器提供的统一任务说明

被测 agent 不可见：

- `reference_solution.md`
- `rubric.md`
- `metadata.json` 中的参考答案字段
- judge prompt

评分器可见：

- 被测 agent 输出
- `request.md`
- `reference_solution.md`
- `rubric.md`
- 必要的 `metadata.json`

## 版本规则

评测基准应显式记录版本，避免不同版本结果不可比较。

建议版本字段：

- `benchmark_version`
- `case_version`
- `snapshot_version`
- `rubric_version`
- `judge_version`

如果测试项输入、项目快照、参考方案或评分标准发生实质变化，应提升对应版本。

## 第一优先级

当前第一优先级评测基准是：

[GitHub 中型项目软件专利技术方案评测基准](software-patent-solution-github.md)

它用于评估系统能否基于真实软件项目快照和技术方案需求，生成合理的软件专利技术方案草稿。
