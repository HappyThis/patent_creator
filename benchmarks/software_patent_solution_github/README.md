# GitHub 中型项目软件专利技术方案评测基准

本评测基准用于评估本系统中的技术方案生成 agent，是否能够在已准备好的项目环境中，根据给定需求生成合理的软件专利技术方案草稿。

这里的“被测 agent”指专利交底书系统内负责生成技术方案草稿的 agent 能力。Codex、Claude Code、Cursor 等外部 coding agent 或模型接口只能作为执行后端、调试工具或对照组；正式主评测不应把目标偷换为评估这些外部工具本身。

规范文档：

- [GitHub 中型项目软件专利技术方案评测基准](../../docs/benchmarks/software-patent-solution-github.md)
- [GitHub 中型项目候选清单](../../docs/benchmarks/github-project-candidates.md)
- [测试项难度复评](./difficulty_review.md)

## 概念模型

本评测基准区分“来源项目”和“测试项”。

- 来源项目：被选作测试项来源池的 GitHub 仓库。
- 测试项：从某个来源项目的具体 issue、PR 或设计讨论中提炼出的正式评测样本。
- 一个来源项目可以产出 0 个、1 个或多个测试项。

本地项目源码可 clone 到 `projects/`。该目录已配置 git ignore，不作为正式 benchmark 数据提交。

正式可运行测试项存放在 `cases/`。

## 测试项契约

每个正式测试项包含：

- `snapshot.json`：固定项目快照描述，记录仓库、commit、拉取策略和维护复核用关注路径。
- `request.md`：agent 可见的技术方案需求。
- `reference_solution.md`：隐藏参考方案，用于评分。
- `rubric.md`：隐藏的测试项级评分标准。
- `metadata.json`：机器可读的测试项元数据，只记录来源、难度、标签、可见性等评测信息；checkout 信息以 `snapshot.json` 为准。

benchmark runner 根据 `snapshot.json` 拉取仓库并 checkout 到指定 commit，然后将该 checkout 目录设置为被测 agent 的当前工作目录，或显式告诉 agent 项目环境路径。正式评测时，被测 agent 的文本输入只包含 `request.md` 和评测基准级运行任务说明。

`snapshot.json` 和 `focus_paths` 是隐藏维护信息，不作为用户输入，也不提供给被测 agent。`focus_paths` 只能用于人工复核、case 维护或诊断性分析，不能进入正式 agent 输入。

正式评测走本系统完整主 agent 对话接口。评估器不直接调用子 agent，也不把主 agent 的最终聊天回复当作答案。主 agent 完成本轮后，评估器只从 `disclosure.json` 中抽取“技术方案”章节内容作为被评分产物。

## Agent 输入契约

正式评测时，输入给被测 agent 的内容必须严格固定为一个输入包：

1. 系统或任务说明：`runner.md` 的全文。
2. 环境位置：将项目 checkout 目录设置为当前工作目录；如果执行框架无法设置当前工作目录，只能额外提供一句“项目环境路径：<path>”。
3. 用户需求：当前 case 的 `request.md` 全文。

输入包不得包含：

- `snapshot.json` 的内容。
- `focus_paths` 或任何建议阅读路径。
- `metadata.json`。
- `reference_solution.md`。
- `rubric.md`。
- 来源 issue、PR 或最终实现 diff 的链接和摘要。
- case 难度、标签、评分维度或维护说明。

`request.md` 自身也不得包含精确源码路径、目标类名、目标函数名或已知实现方案；除非这些信息是普通用户需求中自然会出现的产品概念，而不是 benchmark 维护者提供的解题提示。

`runner.md` 是 agent 可见文本，因此也不得出现 `snapshot.json`、`focus_paths`、参考方案、rubric、最终 PR diff 等评测侧概念；这些约束只应写在 benchmark 规范和 runner 实现中。

## 评估产物契约

评估对象只有一个：主 agent 写入 `disclosure.json` 的技术方案章节内容。

评估器应在主 agent 写作结束后读取当前交底书文档，抽取 `technical_solution` 章节，生成 `evaluated_artifact.md` 并交给评分器。最终聊天回复、工具调用轨迹、子 agent proposal、session event、文档 diff 等不作为评分输入。

评估器不限制主 agent 是否编辑其他章节，也不把中途可恢复的工具错误计入评分；只要最终能从 `disclosure.json` 中提取有效的 `technical_solution` 内容，就进入内容评分。

如果 `technical_solution` 章节为空，或内容明显不是技术方案，评估器可以继续向同一主 agent 会话发送固定补充指令，要求“继续充实技术方案章节”。补充次数由 runner 配置控制；超过次数后仍未形成有效技术方案的 case 记为 `skipped_no_solution_artifact`，不对聊天回复进行兜底评分。

## 运行评估器

推荐使用 `bench.py` 作为日常入口。该脚本会自动加载项目根目录 `.env`，默认使用 `backend/.venv/bin/python`，并把单 case 和小批量运行的 timeout 默认设为 `900` 秒。

直接进入交互式 CLI dashboard：

```bash
benchmarks/software_patent_solution_github/bench.py
```

或显式打开：

```bash
benchmarks/software_patent_solution_github/bench.py ui
```

查看 case 列表：

```bash
benchmarks/software_patent_solution_github/bench.py list
```

推荐先从单个 case 跑通 subject + judge 的完整闭环。真实主 agent 会进行源码阅读、长文本写作、上下文压缩、工具调用自恢复和 Codex-as-judge 评分；实测 `600` 秒已接近复杂 case 的完成边界，`900` 秒更适合作为单 case、低并发小批量验证和全量首轮运行的默认实践。

单个 case（推荐）：

```bash
benchmarks/software_patent_solution_github/bench.py run 001
```

如果需要显式写出全量首轮实践标准，建议使用单 worker 和 `900` 秒 timeout：

```bash
benchmarks/software_patent_solution_github/bench.py batch \
  --workers 1 \
  --round-timeout 900 \
  --judge-timeout 900
```

只运行主 agent 并抽取技术方案，不调用 Codex judge。该命令用于先验证模型 key、subject agent、项目 checkout 和 artifact 抽取链路，不消耗 Codex-as-judge 调用：

```bash
benchmarks/software_patent_solution_github/bench.py subject 001
```

复用已经抽取的 `evaluated_artifact.md`，只运行 judge。未传 `--run-id` 时会自动选择该 case 最新的 artifact：

```bash
benchmarks/software_patent_solution_github/bench.py judge 001
```

批量运行前应先确认单 case 的 `status` 为 `scored`，再从单 worker 开始：

```bash
benchmarks/software_patent_solution_github/bench.py batch 001 002 003
```

多次重复运行用于观察同一 case 的 artifact 稳定性和 judge 分数波动。建议先使用低并发，确认没有 provider quota、超时或 judge 鉴权问题后再提高 `--workers`：

```bash
benchmarks/software_patent_solution_github/bench.py batch 001 002 003 --repeats 2 --workers 1
```

查看最新运行状态：

```bash
benchmarks/software_patent_solution_github/bench.py status
benchmarks/software_patent_solution_github/bench.py status <run_id> --case 001
```

运行产物写入 `runs/<run_id>/`，该目录不进入版本控制。评估器会根据 `snapshot.json` 准备项目快照，将绝对路径注入给完整主 agent 对话接口；主 agent 完成后只抽取 `disclosure.json` 的 `technical_solution` 章节作为 `evaluated_artifact.md`。评估器不限制主 agent 是否编辑其他章节，也不把中途可恢复的工具错误计入评分；只要最终能提取有效技术方案，就进入内容评分。

Codex-as-judge 使用本机 `codex exec`，在同一个项目快照目录中以只读方式阅读源码并打分。

运行中会在终端输出 `[benchmark]` 状态行，批量运行也会实时转发单 case 子进程输出，不再等整个 case 结束后一次性打印。多 worker 并发时，终端中的每条转发日志都会带来源前缀，例如 `[worker=bench-worker_0 run=r01-001 case=001 repeat=1/2] stdout ...` 或 `[worker=bench-worker_1 run=r02-003 case=003 repeat=2/2] stderr ...`，用于区分是哪一个 worker/case/repeat 产生的输出；落盘的 `run_case_stdout.txt` 和 `run_case_stderr.txt` 仍保留原始内容，不额外写入终端前缀。若终端中断或需要从文件判断卡点，可查看 `runs/<run_id>/cases/<case_id>/progress.json` 的最新阶段，或用 `tail -f runs/<run_id>/cases/<case_id>/progress.jsonl` 查看完整状态流。状态阶段包括 `prepare`、`subject`、`subject_round`、`artifact`、`judge`、`judge_codex` 和 `result`。其中 `judge_codex` 来自 `codex exec --json` 的事件流，会记录 Codex thread、turn、item 和 token usage；原始事件保存到 `runs/<run_id>/cases/<case_id>/judge/codex_judge_events.jsonl`。

需要观察 subject agent 实际发给大模型的 system prompt、messages 和 tools schema 时，可临时开启 `PATENT_CREATOR_LOG_LLM_PAYLOAD=true`。开启后普通 `app.log` 只记录轻量索引和 payload 文件路径，完整请求按“一次模型调用一个 JSON 文件”写入 `logs/llm_payloads/`，并追加 `logs/llm_payloads/index.jsonl`。该目录可能包含用户输入、交底书正文和工具返回结果，只用于本地调试，不应提交到版本控制。

每个 case 会额外写入 `runs/<run_id>/cases/<case_id>/diagnostics.json`，只记录 subject 状态、补充轮次、产物抽取、round 硬失败和 judge 状态。完整运行轨迹仍保留在 `subject/session_events.jsonl`，用于调试，不作为技术方案质量评分依据。批量运行会写入轻量 `runs/<run_id>/run_summary.json`，其中只保留解析结果、诊断和 `run_case_stdout.txt` / `run_case_stderr.txt` 路径，不再内嵌完整 stdout/stderr；多次重复运行会额外写入 `evaluation_summary.json` 和 `evaluation_report.md`，并在 benchmark 根目录更新 `latest_run_report.md`，用于观察产物成功率、平均分、分数波动和失败类型。运行报告只呈现 agent 的运行与评分结果，不生成 case 建议、case 分级或 benchmark 自评结论。

确认某次运行结果值得保留后，使用发布脚本整理出可提交的评估历史：

```bash
backend/.venv/bin/python benchmarks/software_patent_solution_github/evaluator/publish_result.py \
  --run-id <run_id> \
  --name <result_id> \
  --subject-model <model> \
  --judge-model codex
```

发布脚本会从 `runs/<run_id>/` 重新生成 `evaluation_summary.json`、`evaluation_report.md`，并提取每次运行的技术方案正文和 judge 结构化结果，写入 `results/<result_id>/` 并更新 `results/index.jsonl`。脚本不会执行 `git add` 或 `git commit`，也不会把 `prepared_repo/`、`session_events.jsonl`、Codex 原始事件、stdout/stderr、完整 `disclosure.json` 或本机绝对路径归档。

## 当前状态

研究某个来源项目时，可以把源码 clone 到 `projects/`，例如：

```text
projects/
  builderz_labs_mission_control/
```

`cases/` 目录用于存放已经选定具体 issue、PR 或设计讨论，并补齐快照、参考方案和评分标准的正式测试项。

正式测试项优先选择较大的功能迭代、新功能支持、新特性机制、能力扩展或架构级改造。局部 bug 修复、小参数调整、依赖升级、文案或样式修改原则上不进入正式 benchmark。

`request.md` 应模拟普通用户或产品侧提出的粗粒度场景需求，只描述能力目标、场景问题和关键约束，不提前给出关键技术机制或方案骨架。

当前已生成的正式测试项：

- `001`：基于 `builderz-labs/mission-control` 的 OpenCode 原生 agent runtime 会话接入能力。
- `002`：基于 `builderz-labs/mission-control` 的 agent 执行过程结构化记录与评估附着能力。
- `003`：基于 `builderz-labs/mission-control` 的 agent 任务 Git-native 协作镜像同步能力。
- `004`：基于 `campfirein/byterover-cli` 的共享知识文件与运行时动态信号分层存储能力。
- `005`：基于 `cloudflare/agents` 的外部调用方触发 agent 对话任务持久接收能力。
- `006`：基于 `cloudflare/agents` 的 durable agent chat 客户端清理与服务端取消解耦能力。
- `007`：基于 `cloudflare/agents` 的保留式流式子 agent 工具编排能力。
- `008`：基于 `cloudflare/agents` 的浏览器侧隔离执行 LLM 生成代码并调度动态客户端工具能力。
- `009`：基于 `cloudflare/agents` 的 agent workspace 多模态文件读取与模型输出转换能力。
- `010`：基于 `cloudflare/agents` 的多会话 assistant 会话隔离与用户级共享资源协同能力。
