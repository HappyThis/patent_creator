#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


BENCHMARK_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCHMARK_DIR.parents[1]
SHARED_PUBLISHER = (
    REPO_DIR
    / "benchmarks"
    / "patent_technical_solution"
    / "evaluator"
    / "publish_result.py"
)


def main() -> None:
    os.execve(
        sys.executable,
        [
            sys.executable,
            str(SHARED_PUBLISHER),
            "--benchmark-dir",
            str(BENCHMARK_DIR),
            "--runs-dir",
            str(BENCHMARK_DIR / "runs"),
            "--results-dir",
            str(BENCHMARK_DIR / "results"),
            *sys.argv[1:],
        ],
        os.environ.copy(),
    )


if __name__ == "__main__":
    main()
