# Published results

Publish a reviewed run with:

```bash
backend/.venv/bin/python benchmarks/patent_representation_semantics/publish_result.py \
  --run-id <run_id> \
  --name <result_id>
```

Raw run artifacts remain ignored; only reviewed lightweight result snapshots should be committed.
