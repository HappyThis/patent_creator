# Experiment Conclusion: Main Prompt Complex Cases 5x

## Scope

- Run id: `20260525-main-prompt-complex-5cases-5x`
- Cases: `004`, `005`, `006`, `007`, `010`
- Repeats: `5` per case
- Workers: `5`
- Subject model: `deepseek-v4-pro`
- Judge model: `codex-as-judge`

## Result

All `25` runs completed with scored artifacts.

- Scored runs: `25/25`
- Artifact success: `25/25`
- Max elapsed time: `662.6s`
- Round timeout: `900s`
- Overall average score: `82.4`
- Overall minimum score: `74`
- Overall score standard deviation: `5.91`

Compared with the existing repeated-run baseline for the same cases:

- Overall average score changed from `81.08` to `82.4`
- Overall minimum score changed from `68` to `74`
- Overall score standard deviation changed from `7.0` to `5.91`

## Per-Case Comparison

| Case | Baseline scores | New scores | Baseline avg | New avg | Baseline min | New min | Baseline std | New std | Decision signal |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `004` | `82, 82, 88, 90, 95` | `89, 88, 82, 95, 88` | `87.4` | `88.4` | `82` | `82` | `4.92` | `4.13` | Improved average and variance; minimum unchanged. |
| `005` | `75, 68, 68, 74, 68` | `75, 75, 78, 75, 78` | `70.6` | `76.2` | `68` | `75` | `3.2` | `1.47` | Improved average, minimum, and variance. |
| `006` | `88, 82, 76, 89, 90` | `85, 94, 88, 84, 84` | `85.0` | `87.0` | `76` | `84` | `5.37` | `3.79` | Improved average, minimum, and variance. |
| `007` | `86, 80, 84, 84, 80` | `74, 80, 80, 74, 80` | `82.8` | `77.6` | `80` | `74` | `2.71` | `2.94` | Regressed average, minimum, and variance. |
| `010` | `78, 78, 80, 78, 84` | `80, 88, 78, 82, 86` | `79.6` | `82.8` | `78` | `78` | `1.96` | `3.71` | Improved average; minimum unchanged; variance regressed. |

## Issue Decision

Issue `#4` can be closed from this benchmark evidence. The close condition focuses on bounded completion behavior for complex cases. This run completed all `25` executions without timeout, with max elapsed time `662.6s` under the `900s` round timeout, while preserving or improving the overall score profile.

Issue `#6` should remain open. The overall score profile improved, but repeatability did not improve uniformly. Case `007` regressed on average score, minimum score, and variance, and case `010` regressed on variance.

Previously closed task-size issues `#5`, `#13`, and `#14` remain supported by the targeted `005` repeated-run result.
