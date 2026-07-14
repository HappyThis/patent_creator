#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


BENCHMARK_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCHMARK_DIR.parents[1]
SHARED_BENCH = REPO_DIR / "benchmarks" / "patent_technical_solution" / "bench.py"


def main() -> None:
    env = os.environ.copy()
    env["PATENT_SOLUTION_BENCHMARK_DIR"] = str(BENCHMARK_DIR)
    env["PATENT_SOLUTION_BENCHMARK_ID"] = "patent_representation_semantics"
    os.execve(
        sys.executable,
        [sys.executable, str(SHARED_BENCH), *sys.argv[1:]],
        env,
    )


if __name__ == "__main__":
    main()
