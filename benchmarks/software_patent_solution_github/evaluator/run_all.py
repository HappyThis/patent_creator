from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
RUN_CASE = Path(__file__).resolve().parent / "run_case.py"


def main() -> None:
    args = parse_args()
    benchmark = json.loads((BENCHMARK_DIR / "benchmark.json").read_text(encoding="utf-8"))
    case_ids = args.cases or benchmark.get("case_ids", [])
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    results = []
    for case_id in case_ids:
        command = [
            sys.executable,
            str(RUN_CASE),
            "--case",
            str(case_id).zfill(3),
            "--run-id",
            run_id,
            "--runs-dir",
            args.runs_dir,
        ]
        if args.skip_judge:
            command.append("--skip-judge")
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        parsed_result = parse_case_result(completed.stdout)
        result = {
            "case_id": str(case_id).zfill(3),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "result": parsed_result,
            "diagnostics": parsed_result.get("diagnostics") if isinstance(parsed_result, dict) else None,
        }
        results.append(result)
        print(completed.stdout)
        if completed.returncode != 0:
            print(completed.stderr, file=sys.stderr)
    summary_path = Path(args.runs_dir).resolve() / run_id / "run_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_case_result(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all software patent solution benchmark cases.")
    parser.add_argument("--cases", nargs="*", help="Optional case ids. Defaults to benchmark.json case_ids.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"))
    parser.add_argument("--skip-judge", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
