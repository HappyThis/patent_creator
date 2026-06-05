# Benchmark 评估历史

该目录用于保存已经人工确认可以入库的专利交底书技术方案评估结果快照。原始运行目录 `../runs/` 是临时调试产物，不进入版本控制。

发布结果使用：

```bash
backend/.venv/bin/python benchmarks/patent_technical_solution/evaluator/publish_result.py \
  --run-id <run_id> \
  --name <result_id>
```

发布脚本只整理结果，不执行 `git add`、`git commit` 或任何提交操作。是否提交由开发者人工检查后决定。

每个结果快照包含：

- `manifest.json`：本次发布的来源、模型备注、case 列表、运行数量和 git 状态。
- `evaluation_summary.json`：机器可读汇总。
- `evaluation_report.md`：人可读评估报告。
- `case_results.jsonl`：每个 case/repeat 一行的轻量结果。
- `artifacts/`：被评估的技术方案正文。
- `judge_results/`：Codex-as-judge 的结构化评分结果。

发布脚本不会归档：

- `prepared_environment/project_snapshot/`
- `subject/session_events.jsonl`
- `judge/codex_judge_events.jsonl`
- `run_case_stdout.txt`
- `run_case_stderr.txt`
- `subject/disclosure.json`
- 本机绝对路径
