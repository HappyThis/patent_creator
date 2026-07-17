# Patent Representation Semantics Benchmark

This standalone benchmark measures whether an agent selects and uses figures and formulas appropriately while drafting the patent technical-solution section.

It owns its cases, subject rules, Judge rules, run artifacts, and published results. The Python evaluator is shared with `patent_technical_solution` so process fixes and runtime behavior do not drift between the two patent benchmarks.

The benchmark is no longer a `patent_technical_solution` track. Use the commands in this directory directly; the former `--track representation_semantics` entry point now returns a migration hint.

## Layout

```text
patent_representation_semantics/
├── benchmark.json              # case membership, policies, default Judge
├── cases/<case_id>/            # self-contained frozen input and both rubrics
├── runner.md                   # shared technical-solution Agent contract
├── representation_runner.md    # representation-specific Agent contract
├── judge.md                    # shared solution Judge contract
├── representation_judge.md     # representation-specific Judge contract
├── runs/                       # raw evidence, ignored by Git
└── results/                    # explicitly published lightweight snapshots
```

## Policy model

Figure and formula policies are independent for every case:

- `recommended`: omission is penalized; correct use receives full credit; partial or incorrect use is penalized.
- `optional`: omission and correct use both receive full credit; only technically wrong or misleading use is penalized.

Optional content is not penalized merely for being simple, decorative, unnecessary, or visually ordinary. For recommended figures that are actually used, the figure score also considers explanatory value, legibility, and restrained black-and-white engineering style; technical semantics remain the dominant factor.

## Case distribution

| Cases | Figure | Formula | Count |
|---|---|---:|---:|
| 008, 009, 010 | recommended | optional | 3 |
| 011, 012 | optional | recommended | 2 |
| 013, 014 | recommended | recommended | 2 |
| 001, 004, 006 | optional | optional | 3 |

Cases 001, 004, 006, 008, 009, and 010 are calibration cases copied from the original general-solution corpus. Cases 011 through 014 are additional frozen pre-implementation snapshots. Every case is self-contained in this benchmark directory.

## Scoring

- `solution_score`: the existing general technical-solution score.
- `representation_score`: the mean of the independent figure and formula channel scores.
- `total_score = 0.7 × solution_score + 0.3 × representation_score`.

The structured conclusion records policy, actual use, verdict, score, and assessment for each channel. Batch aggregation reports use rates, recommended omissions, partial-use counts, and incorrect-use counts separately.

## Run

```bash
# Inspect the independent case list
backend/.venv/bin/python benchmarks/patent_representation_semantics/bench.py list

# Run all 10 cases once with concurrency 10
backend/.venv/bin/python benchmarks/patent_representation_semantics/bench.py batch \
  --workers 10 \
  --repeats 1
```

The default Codex Judge is `gpt-5.6-sol` with `xhigh` reasoning. CLI flags can still override the model, provider, or reasoning effort for an explicit comparison run.

Run artifacts are written to this benchmark's own `runs/` directory. Existing representation-track runs under `patent_technical_solution/runs/` remain historical artifacts and are not moved automatically.

Each `run_id` is immutable: reusing an existing id fails before any Case starts. A batch resolves the Codex Judge runtime once and reuses it for every Case. On `Ctrl-C` or `SIGTERM`, the launcher terminates active Case process groups and records the parent run and interrupted executions as `cancelled` instead of leaving stale `running` records.

To publish a reviewed run into this benchmark's own `results/` directory:

```bash
backend/.venv/bin/python benchmarks/patent_representation_semantics/publish_result.py \
  --run-id <run_id> \
  --name <result_id>
```

The benchmark disables subject web search, hides snapshot repository/commit provenance, strips `.git`, and rejects recorded web/network tool calls. These controls reduce implementation-answer leakage; they are cooperative benchmark controls rather than an operating-system network sandbox.
