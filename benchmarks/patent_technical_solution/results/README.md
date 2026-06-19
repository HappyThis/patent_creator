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

## Latest Published Result

| Result ID | Subject model | Judge | Scale | Average score | Completion | Notes |
| --- | --- | --- | --- | ---: | --- | --- |
| `20260619-gpt55-baseline-10cases-5x-w10` | `gpt-5.5` | `codex` | 10 cases x 5 repeats, workers=10 | 93.14 | 50/50 scored, artifact_success=50/50 | min=82, max=98, stdev=3.43; published from run `20260619-gpt55-baseline-10cases-5x-w10` |

Previous published baseline: `20260605-patent-tech-solution-10cases-5x-w10`, `deepseek-v4-pro`, average 87.06, min 70, max 96. The current result is +6.08 points higher on the same 10 cases x 5 repeats x 10 workers shape.
