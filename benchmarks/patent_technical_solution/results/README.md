# Benchmark 评估历史

该目录保存人工确认后可以入库的技术方案 Benchmark 结果快照。完整运行证据位于 `../runs/<run_id>/`，不进入版本控制。

新版发布命令：

```bash
backend/.venv/bin/python benchmarks/patent_technical_solution/evaluator/publish_result.py \
  --run-id <run_id> \
  --name <result_id>
```

新版结果快照只包含：

- `manifest.json`：来源 run、运行配置、模型、聚合数据、Case 状态和结论引用。
- `conclusions/<case_id>/result.json` 或 `rNN.json`：Codex 原样输出的总分与综合评价报告。

发布脚本不会复制 `subject/`、冻结环境、Agent/Codex 日志、技术方案正文或图片，也不会执行 `git add`、`commit` 或 `push`。原有 v1 历史快照保持不变；新版脚本不迁移、不兼容旧运行目录。
