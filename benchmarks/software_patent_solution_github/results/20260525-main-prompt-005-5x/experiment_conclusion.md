# Experiment Conclusion: Main Agent Prompt Workflow on Case 005

## Scope

This result records the case 005 benchmark after removing sub-agent execution paths and strengthening the main agent prompt for complex technical tasks.

Source run:

- `runs/20260525-main-prompt-005-5x`

Published result:

- `results/20260525-main-prompt-005-5x`

## Verification

- Backend regression test: `uv run pytest` -> `65 passed`.
- Case 005 repeated benchmark: 5 runs, 5 scored, 100% artifact success.
- Scores: `75, 85, 82, 75, 75`.
- Average score: `78.4`.
- Minimum score: `75`.
- Maximum score: `85`.
- Score standard deviation: `4.27`.
- Subject runs completed within the 900 second timeout; recorded case elapsed values were approximately `287.6s` to `414.5s`.
- Session event scan showed all subject tool calls used `scope=main`; no sub-agent execution scope was present.

## Baseline Comparison

Previous case 005 baseline:

- `results/20260522-deepseek-v4-pro-reasoning-5x`
- Scores: `75, 68, 68, 74, 68`.
- Average score: `70.6`.
- Minimum score: `68`.

Intermediate main-only run before prompt workflow strengthening:

- Scores observed across four case 005 runs: `68, 68, 68, 74`.
- Average score: `69.5`.
- Minimum score: `68`.

Current run:

- Scores: `75, 85, 82, 75, 75`.
- Average score: `78.4`.
- Minimum score: `75`.

The benchmark evidence indicates that the current main-agent direct execution design, with the complex-task prompt workflow, improves case 005 average and low-tail quality relative to both the previous published baseline and the intermediate main-only run.

## Issue Closure Basis

The following issues are considered resolved by the current design and benchmark evidence:

- `#5 [P0] Prevent oversized goals from being delegated to subagents`
- `#13 [P1] Make subagents plan pipe output within budget before writing`
- `#14 [P1] Limit subagent reading and analysis before pipe delivery`

Closure rationale:

- The runtime no longer exposes sub-agent delegation, pipe delivery, or sub-agent scoped execution.
- The case 005 repeated benchmark shows no quality regression from removing those paths; the current result improves the average score to `78.4` and the minimum score to `75`.
- Subject event logs for the current benchmark runs show only `main` scope tool calls.

The following issues should remain open for now:

- `#4 [P0] Add a main-agent completion policy for stopping once the artifact is sufficient`: case 005 supports improvement, but this is broader than sub-agent removal and should be confirmed across additional complex cases.
- `#6 [P1] Stabilize technical-solution quality across repeated runs`: average and minimum improved on case 005, but variance did not clearly improve and the issue is a broader repeatability policy.
