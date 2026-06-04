# Patent Technical Solution Benchmark

This benchmark is the main track for evaluating the quality of the `技术方案`
section in a patent disclosure.

It differs from `software_patent_solution_github`: that older benchmark remains a
software-engineering diagnostic track. This track evaluates whether the generated
technical solution is reasonable, deep, implementable, and protectable at patent
disclosure granularity.

The benchmark does not reward implementation paths, validation plans, interface
catalogs, or delivery checklists unless they are themselves part of the
technical means being disclosed.

Current cases are software-project cases: the evaluator clones the repository
described by `snapshot.json` at the frozen commit and materializes it under
`prepared_environment/project_snapshot/`. The subject agent must explore that
project snapshot before drafting the `技术方案` section, and the judge scores the
result by relative technical depth against the hidden reference solution and
rubric.
