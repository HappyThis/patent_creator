# Patent Technical Solution Benchmark

该 Benchmark 只评价专利交底书中最终“技术方案”的综合质量，包括技术内容本身，以及实际使用的公式、配图是否适用且表达正确。

它不再提供 figure mode 或 combined mode，也不评价图片的视觉美观、排版精细度和渲染质量。公式和配图不是强制交付物：没有使用时不会自动扣分；使用时，Codex 会评价其必要性、语义正确性、与正文的一致性及对方案理解的帮助。

## 执行原则

- 每个 Case 只给主 Agent 一次完整任务，不做自动补写轮次。
- Agent 必须先探索 `prepared_environment/project_snapshot/`，再把最终正文写入 `subject/` 中交底书的“技术方案”章节。
- 评估程序不抽取技术方案副本、不复制 disclosure、不生成中间评价材料。
- 评估程序不做语义规则检查。空泛、过短或质量较差的方案仍交给 Codex，由 Codex 在统一规则下给低分。
- Codex 的工作目录是当前 Case 运行目录。提示词只说明各类原始材料的位置和关系，由 Codex 自行读取需求、冻结环境、最终交底书、公式及实际引用的配图。
- Codex 最终只返回总分和一份综合评价报告，不输出程序评分维度。

## Case 输入

每个 `cases/<case_id>/` 包含：

- `request.md`：给 Agent 的技术方案需求。
- `snapshot.json`：冻结项目仓库及 commit。
- `reference_solution.md`：仅供 Codex 使用的隐藏参考路径。
- `rubric.md`：仅供 Codex 使用的 Case 评分校准。

运行时会根据 `snapshot.json` 创建：

```text
prepared_environment/
├── ENVIRONMENT.md
└── project_snapshot/       # 冻结 Git checkout，Agent 和 Codex 均可读取
```

## 原始运行目录

单 Case：

```text
runs/<run_id>/
├── run.json
└── cases/<case_id>/
    ├── execution.json
    ├── prepared_environment/
    ├── subject/
    │   └── data/projects/<project_id>/
    │       ├── disclosure.json
    │       ├── sessions/*.jsonl
    │       └── assets/figures/...       # 仅在 Agent 实际创建配图时存在
    ├── agent_logs/
    │   ├── stdout.log
    │   └── stderr.log
    └── judge/
        ├── codex_logs/
        │   ├── prompt.md
        │   ├── schema.json
        │   ├── events.jsonl
        │   └── stderr.log               # 仅在异常时存在
        └── conclusion/result.json
```

批量运行在 Case 下增加 repeat 层：`cases/<case_id>/rNN/`，其内部结构与单 Case 相同。整个批次只有一个顶层 `run.json`。

文件职责：

- `run.json`：整次运行的开始/结束时间、状态、配置、Agent/Judge 模型、各 Case 引用和分数聚合。
- `execution.json`：单次 Case 的 Agent 与 Codex 开始/结束时间、状态、模型、token usage、SDK/runtime 版本、错误和结论路径。
- `subject/`：Agent 的原始输出工作区，也是唯一评价对象；会话语义日志位于其中的 `sessions/*.jsonl`。
- `agent_logs/`：Agent 运行进程的 stdout/stderr，供失败诊断。
- `judge/codex_logs/`：Codex 的实际输入、结构化 schema、事件流和异常日志。
- `judge/conclusion/result.json`：唯一评价结论，固定为 `status + total_score + evaluation_report`。

## 安装与运行

先同步 Benchmark 专用依赖：

```bash
cd backend
uv sync --group benchmark
```

常用命令：

```bash
# 查看 Case
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py list

# 运行一个 Case：Agent + Codex
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py run 001

# 只运行 Agent，之后对同一 run 单独评价
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py subject 001 --run-id example-001
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py judge 001 --run-id example-001

# 并发执行多个 Case，每个重复 2 次
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py batch 001 002 003 \
  --workers 2 --repeats 2 --run-id example-batch

# 查看状态
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py status example-batch
```

Codex Judge 使用官方 `openai-codex` Python SDK，以只读沙箱运行。可通过 `BENCHMARK_JUDGE_MODEL`、`BENCHMARK_JUDGE_PROVIDER`、`BENCHMARK_JUDGE_REASONING_EFFORT` 和 `BENCHMARK_JUDGE_SERVICE_TIER` 覆盖本机 Codex 配置。

SDK 会优先使用 `BENCHMARK_CODEX_BIN` 指定的 Codex binary；未指定时，会探测 PATH、ChatGPT/Codex App 和本机 plugin runtime 中可正常启动的 binary，均不可用时才使用 SDK 自带 runtime。实际 binary、SDK 版本和 runtime 版本都会记录到运行元数据中。

## 发布结果

`runs/` 保存完整原始证据且不进入版本控制。需要把确认后的评分结果作为轻量历史快照入库时，运行：

```bash
backend/.venv/bin/python benchmarks/patent_technical_solution/evaluator/publish_result.py \
  --run-id <run_id> \
  --name <result_id>
```

新版快照只包含一个溯源 `manifest.json` 和各次 Codex 原始结论 `conclusions/`，不再复制技术方案、图片、日志或生成重复汇总文件。旧运行目录和旧发布快照不迁移，也不由新版脚本兼容。
