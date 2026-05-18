#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCHMARK_DIR.parents[1]
DEFAULT_PYTHON = REPO_DIR / "backend" / ".venv" / "bin" / "python"
DEFAULT_TIMEOUT = 900
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def main() -> None:
    args = parse_args()
    if args.command == "ui":
        run_ui(args)
        return
    if args.command == "list":
        list_cases()
        return
    if args.command == "status":
        show_status(run_id=args.run_id, case_id=args.case)
        return

    env = os.environ.copy()
    load_dotenv(REPO_DIR / ".env", env)
    python_bin = resolve_python(args.python)

    if args.command == "run":
        command = build_run_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=args.run_id,
            round_timeout=args.round_timeout,
            judge_timeout=args.judge_timeout,
        )
        run_command(command, env=env, dry_run=args.dry_run)
        return

    if args.command == "subject":
        command = build_subject_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=args.run_id,
            round_timeout=args.round_timeout,
        )
        run_command(command, env=env, dry_run=args.dry_run)
        return

    if args.command == "judge":
        run_id = args.run_id or latest_run_id_for_case(normalize_case(args.case))
        command = build_judge_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=run_id,
            judge_timeout=args.judge_timeout,
        )
        run_command(command, env=env, dry_run=args.dry_run)
        return

    if args.command == "batch":
        command = build_batch_command(
            python_bin=python_bin,
            cases=args.cases,
            run_id=args.run_id,
            workers=args.workers,
            repeats=args.repeats,
            round_timeout=args.round_timeout,
            judge_timeout=args.judge_timeout,
            skip_judge=args.skip_judge,
        )
        run_command(command, env=env, dry_run=args.dry_run)
        return

    raise SystemExit(f"unknown command: {args.command}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convenience runner for software patent solution benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--python", default=None, help="Python executable. Defaults to backend/.venv/bin/python.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ui", help="Open an interactive terminal dashboard.")

    subparsers.add_parser("list", help="List available benchmark cases.")

    status = subparsers.add_parser("status", help="Show latest progress for a run or case.")
    status.add_argument("run_id", nargs="?", help="Run id. Defaults to latest run directory.")
    status.add_argument("--case", help="Optional case id for selecting a case progress file.")

    run = subparsers.add_parser("run", help="Run one case with subject + judge.")
    add_case_arg(run)
    add_common_run_args(run)

    subject = subparsers.add_parser("subject", help="Run one case subject only, without Codex judge.")
    add_case_arg(subject)
    add_common_run_args(subject, judge=False)

    judge = subparsers.add_parser("judge", help="Reuse an evaluated artifact and run Codex judge.")
    add_case_arg(judge)
    judge.add_argument("--run-id", help="Existing run id. Defaults to latest run containing this case artifact.")
    judge.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
    judge.add_argument("--dry-run", action="store_true")

    batch = subparsers.add_parser("batch", help="Run multiple cases through run_all.py.")
    batch.add_argument("cases", nargs="*", help="Case ids. Defaults to benchmark.json case_ids.")
    batch.add_argument("--run-id", help="Optional batch run id.")
    batch.add_argument("--workers", type=int, default=1)
    batch.add_argument("--repeats", type=int, default=1)
    batch.add_argument("--round-timeout", type=int, default=DEFAULT_TIMEOUT)
    batch.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
    batch.add_argument("--skip-judge", action="store_true")
    batch.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.command is None:
        args.command = "ui"
    return args


def resolve_python(value: str | None) -> Path:
    python_bin = Path(value).expanduser() if value else DEFAULT_PYTHON
    return python_bin if python_bin.exists() else Path(sys.executable)


def build_run_command(
    *,
    python_bin: Path,
    case_id: str,
    run_id: str | None,
    round_timeout: int,
    judge_timeout: int,
) -> list[str]:
    command = [
        str(python_bin),
        str(BENCHMARK_DIR / "evaluator" / "run_case.py"),
        "--case",
        normalize_case(case_id),
        "--round-timeout",
        str(round_timeout),
        "--judge-timeout",
        str(judge_timeout),
    ]
    if run_id:
        command.extend(["--run-id", run_id])
    return command


def build_subject_command(
    *,
    python_bin: Path,
    case_id: str,
    run_id: str | None,
    round_timeout: int,
) -> list[str]:
    command = [
        str(python_bin),
        str(BENCHMARK_DIR / "evaluator" / "run_case.py"),
        "--case",
        normalize_case(case_id),
        "--skip-judge",
        "--round-timeout",
        str(round_timeout),
    ]
    if run_id:
        command.extend(["--run-id", run_id])
    return command


def build_judge_command(*, python_bin: Path, case_id: str, run_id: str, judge_timeout: int) -> list[str]:
    return [
        str(python_bin),
        str(BENCHMARK_DIR / "evaluator" / "run_case.py"),
        "--case",
        normalize_case(case_id),
        "--run-id",
        run_id,
        "--skip-subject",
        "--judge-timeout",
        str(judge_timeout),
    ]


def build_batch_command(
    *,
    python_bin: Path,
    cases: list[str],
    run_id: str | None,
    workers: int,
    repeats: int,
    round_timeout: int,
    judge_timeout: int,
    skip_judge: bool,
) -> list[str]:
    command = [
        str(python_bin),
        str(BENCHMARK_DIR / "evaluator" / "run_all.py"),
        "--workers",
        str(workers),
        "--round-timeout",
        str(round_timeout),
        "--judge-timeout",
        str(judge_timeout),
    ]
    if cases:
        command.extend(["--cases", *[normalize_case(case) for case in cases]])
    if run_id:
        command.extend(["--run-id", run_id])
    if repeats != 1:
        command.extend(["--repeats", str(repeats)])
    if skip_judge:
        command.append("--skip-judge")
    return command


def add_case_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("case", help="Case id, e.g. 001 or 1.")


def add_common_run_args(parser: argparse.ArgumentParser, *, judge: bool = True) -> None:
    parser.add_argument("--run-id", help="Optional run id.")
    parser.add_argument("--round-timeout", type=int, default=DEFAULT_TIMEOUT)
    if judge:
        parser.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true")


def run_command(command: list[str], *, env: dict[str, str], dry_run: bool) -> None:
    print("+ " + " ".join(command), flush=True)
    if dry_run:
        return
    completed = subprocess.run(command, cwd=REPO_DIR, env=env, check=False)
    raise SystemExit(completed.returncode)


def run_ui(args: argparse.Namespace) -> None:
    env = os.environ.copy()
    load_dotenv(REPO_DIR / ".env", env)
    python_bin = resolve_python(args.python)
    while True:
        clear_screen()
        print_banner()
        print_case_table()
        print()
        print(color("Actions", CYAN, bold=True))
        print("  1  Run one case: subject + judge")
        print("  2  Subject only")
        print("  3  Judge latest artifact")
        print("  4  Batch run")
        print("  5  Show latest status")
        print("  6  Show command examples")
        print("  0  Quit")
        choice = prompt("Select").strip()
        if choice in {"0", "q", "quit", "exit"}:
            print(color("bye", DIM))
            return
        if choice == "1":
            case_id = ask_case()
            command = build_run_command(
                python_bin=python_bin,
                case_id=case_id,
                run_id=None,
                round_timeout=DEFAULT_TIMEOUT,
                judge_timeout=DEFAULT_TIMEOUT,
            )
            confirm_and_run(command, env=env)
        elif choice == "2":
            case_id = ask_case()
            command = build_subject_command(
                python_bin=python_bin,
                case_id=case_id,
                run_id=None,
                round_timeout=DEFAULT_TIMEOUT,
            )
            confirm_and_run(command, env=env)
        elif choice == "3":
            case_id = ask_case()
            try:
                run_id = latest_run_id_for_case(normalize_case(case_id))
            except SystemExit as exc:
                pause(str(exc))
                continue
            command = build_judge_command(
                python_bin=python_bin,
                case_id=case_id,
                run_id=run_id,
                judge_timeout=DEFAULT_TIMEOUT,
            )
            confirm_and_run(command, env=env)
        elif choice == "4":
            raw_cases = prompt("Cases", "001 002 003").strip()
            cases = raw_cases.split() if raw_cases else []
            workers = ask_int("Workers", 1)
            repeats = ask_int("Repeats", 1)
            skip_judge = prompt("Skip judge? y/N", "").lower().startswith("y")
            command = build_batch_command(
                python_bin=python_bin,
                cases=cases,
                run_id=None,
                workers=workers,
                repeats=repeats,
                round_timeout=DEFAULT_TIMEOUT,
                judge_timeout=DEFAULT_TIMEOUT,
                skip_judge=skip_judge,
            )
            confirm_and_run(command, env=env)
        elif choice == "5":
            run_id = prompt("Run id", "latest").strip()
            case_id = prompt("Case filter", "").strip() or None
            print()
            show_status(run_id=None if run_id in {"", "latest"} else run_id, case_id=case_id)
            pause()
        elif choice == "6":
            print_examples()
            pause()
        else:
            pause("Unknown action.")


def print_banner() -> None:
    title = "Software Patent Benchmark"
    subtitle = "subject agent -> artifact -> Codex judge"
    width = max(54, len(title) + 8)
    print(color("+" + "-" * (width - 2) + "+", CYAN))
    print(color("|", CYAN) + color(title.center(width - 2), GREEN, bold=True) + color("|", CYAN))
    print(color("|", CYAN) + color(subtitle.center(width - 2), DIM) + color("|", CYAN))
    print(color("+" + "-" * (width - 2) + "+", CYAN))
    latest = latest_run_dir()
    latest_text = latest.name if latest else "none"
    print(f"{color('cwd', DIM)} {REPO_DIR}")
    print(f"{color('python', DIM)} {resolve_python(None)}")
    print(f"{color('latest run', DIM)} {latest_text}")
    print()


def print_case_table() -> None:
    rows = []
    for case_dir in sorted((BENCHMARK_DIR / "cases").glob("[0-9][0-9][0-9]")):
        metadata = read_json(case_dir / "metadata.json")
        title = str(metadata.get("title", ""))
        status = str(metadata.get("status", ""))
        difficulty = str(metadata.get("case", {}).get("difficulty", ""))
        rows.append((case_dir.name, status, difficulty, title))
    print(color("Cases", CYAN, bold=True))
    print(color(" ID   Status   Difficulty   Title", DIM))
    for case_id, status, difficulty, title in rows:
        display_title = title if len(title) <= 42 else title[:39] + "..."
        print(f" {color(case_id, GREEN)}  {status:<8} {difficulty:<12} {display_title}")


def print_examples() -> None:
    print()
    print(color("Examples", CYAN, bold=True))
    print("  benchmarks/software_patent_solution_github/bench.py run 001")
    print("  benchmarks/software_patent_solution_github/bench.py subject 001")
    print("  benchmarks/software_patent_solution_github/bench.py judge 001")
    print("  benchmarks/software_patent_solution_github/bench.py batch 001 002 003")
    print("  benchmarks/software_patent_solution_github/bench.py status")


def ask_case() -> str:
    return normalize_case(prompt("Case", "001").strip() or "001")


def ask_int(label: str, default: int) -> int:
    value = prompt(label, str(default)).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def confirm_and_run(command: list[str], *, env: dict[str, str]) -> None:
    print()
    print(color("Command", CYAN, bold=True))
    print("+ " + " ".join(command))
    if not prompt("Run now? y/N", "").lower().startswith("y"):
        pause("Cancelled.")
        return
    completed = subprocess.run(command, cwd=REPO_DIR, env=env, check=False)
    pause(f"Command exited with {completed.returncode}.")


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{color(label, YELLOW)}{suffix}: ")


def pause(message: str = "Press Enter to continue.") -> None:
    print()
    input(color(message, DIM))


def clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def color(text: str, code: str, *, bold: bool = False) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    prefix = BOLD if bold else ""
    return f"{prefix}{code}{text}{RESET}"


def load_dotenv(path: Path, env: dict[str, str]) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in env:
            env[key] = value


def normalize_case(case_id: str) -> str:
    return str(case_id).zfill(3)


def list_cases() -> None:
    for case_dir in sorted((BENCHMARK_DIR / "cases").glob("[0-9][0-9][0-9]")):
        metadata = read_json(case_dir / "metadata.json")
        title = metadata.get("title", "")
        status = metadata.get("status", "")
        print(f"{case_dir.name}\t{status}\t{title}")


def show_status(*, run_id: str | None, case_id: str | None) -> None:
    run_dir = BENCHMARK_DIR / "runs" / run_id if run_id else latest_run_dir()
    if run_dir is None:
        raise SystemExit("No runs found.")
    progress_files = sorted(run_dir.glob("**/progress.json"))
    if case_id:
        normalized = normalize_case(case_id)
        progress_files = [path for path in progress_files if f"/cases/{normalized}/" in str(path)]
    if not progress_files:
        print(f"No progress files under {run_dir}")
        return
    for path in progress_files:
        payload = read_json(path)
        rel = path.relative_to(BENCHMARK_DIR)
        phase = payload.get("phase", "-")
        message = payload.get("message", "-")
        elapsed = payload.get("elapsed_seconds", "-")
        score = payload.get("total_score")
        suffix = f" score={score}" if score is not None else ""
        print(f"{rel}: phase={phase} elapsed={elapsed}s message={message}{suffix}")


def latest_run_dir() -> Path | None:
    runs_dir = BENCHMARK_DIR / "runs"
    if not runs_dir.exists():
        return None
    candidates = [path for path in runs_dir.iterdir() if path.is_dir()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def latest_run_id_for_case(case_id: str) -> str:
    runs_dir = BENCHMARK_DIR / "runs"
    matches = sorted(
        runs_dir.glob(f"**/cases/{case_id}/evaluated_artifact.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit(f"No evaluated_artifact.md found for case {case_id}. Run subject first.")
    case_run_dir = matches[0].parent
    return str(case_run_dir.relative_to(runs_dir).parents[1])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
