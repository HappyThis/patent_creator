# Representation Semantics Track

This track measures whether an agent selects and uses figures and formulas appropriately while drafting the patent technical-solution section.

## Policy model

Figure and formula policies are independent for every case:

- `recommended`: omission is penalized; correct use receives full credit; partial or incorrect use is penalized.
- `optional`: omission and correct use both receive full credit; only technically wrong or misleading use is penalized.

Optional content is not penalized merely for being simple, decorative, or unnecessary. Visual aesthetics and rendering quality are outside the scoring scope.

## Case distribution

| Cases | Figure | Formula | Count |
|---|---|---:|---:|
| 008, 009, 010 | recommended | optional | 3 |
| 011, 012 | optional | recommended | 2 |
| 013, 014 | recommended | recommended | 2 |
| 001, 004, 006 | optional | optional | 3 |

Cases 001, 004, 006, 008, 009, and 010 are calibration cases reused from `general_solution`. Cases 011 through 014 are additional frozen pre-implementation snapshots.

## Scoring

- `solution_score`: the existing general technical-solution score.
- `representation_score`: the mean of the independent figure and formula channel scores.
- `total_score = 0.7 × solution_score + 0.3 × representation_score`.

The structured conclusion records policy, actual use, verdict, score, and assessment for each channel. Batch aggregation reports use rates, recommended omissions, partial-use counts, and incorrect-use counts separately.

## Run

```bash
backend/.venv/bin/python benchmarks/patent_technical_solution/bench.py batch \
  --track representation_semantics \
  --workers 10 \
  --repeats 1
```

The track disables subject web search, hides snapshot repository/commit provenance, strips `.git`, and rejects recorded web/network tool calls. These controls reduce implementation-answer leakage; they are cooperative benchmark controls rather than an operating-system network sandbox.
