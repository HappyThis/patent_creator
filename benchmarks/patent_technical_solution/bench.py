#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCHMARK_DIR.parents[1]
DEFAULT_PYTHON = REPO_DIR / "backend" / ".venv" / "bin" / "python"
DRAWIO_PREFLIGHT = REPO_DIR / "scripts" / "drawio_render_preflight.py"
DEFAULT_DRAWIO_URL = "http://127.0.0.1:8081/"
DRAWIO_PREFLIGHT_COMMANDS = frozenset({"run", "subject", "batch"})
DEFAULT_TIMEOUT = 900


def main() -> None:
    args = parse_args()
    if args.command == "list":
        list_cases()
        return
    if args.command == "status":
        show_status(run_id=args.run_id, case_id=args.case)
        return

    env = os.environ.copy()
    load_dotenv(REPO_DIR / ".env", env)
    python_bin = resolve_python(args.python)
    if command_requires_drawio_preflight(args.command):
        run_drawio_preflight(python_bin=python_bin, env=env, dry_run=args.dry_run)

    if args.command == "run":
        command = run_case_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=args.run_id,
            round_timeout=args.round_timeout,
            judge_timeout=args.judge_timeout,
            extra_args=judge_override_args(args),
        )
    elif args.command == "subject":
        command = run_case_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=args.run_id,
            round_timeout=args.round_timeout,
            judge_timeout=args.round_timeout,
            extra_args=["--skip-judge"],
        )
    elif args.command == "judge":
        run_id = args.run_id or latest_run_id_for_case(normalize_case(args.case))
        command = run_case_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=run_id,
            round_timeout=args.judge_timeout,
            judge_timeout=args.judge_timeout,
            extra_args=["--skip-subject", *judge_override_args(args)],
        )
    elif args.command == "batch":
        command = [
            str(python_bin),
            str(BENCHMARK_DIR / "evaluator" / "run_all.py"),
            "--workers",
            str(args.workers),
            "--repeats",
            str(args.repeats),
            "--round-timeout",
            str(args.round_timeout),
            "--judge-timeout",
            str(args.judge_timeout),
        ]
        if args.run_id:
            command.extend(["--run-id", args.run_id])
        if args.skip_judge:
            command.append("--skip-judge")
        command.extend(judge_override_args(args))
        if args.cases:
            command.extend([normalize_case(case_id) for case_id in args.cases])
    else:
        raise SystemExit(f"unknown command: {args.command}")

    run_command(command, env=env, dry_run=args.dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the patent technical solution benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--python", default=None, help="Python executable. Defaults to backend/.venv/bin/python.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List benchmark cases.")

    status = subparsers.add_parser("status", help="Show execution state for a run or case.")
    status.add_argument("run_id", nargs="?", help="Run id. Defaults to latest run directory.")
    status.add_argument("--case", help="Optional case id.")

    run = subparsers.add_parser("run", help="Run one case with subject + judge.")
    add_case_arg(run)
    add_common_run_args(run)

    subject = subparsers.add_parser("subject", help="Run one case subject only.")
    add_case_arg(subject)
    add_common_run_args(subject, judge=False)

    judge = subparsers.add_parser("judge", help="Judge an existing subject workspace.")
    add_case_arg(judge)
    judge.add_argument("--run-id", help="Existing run id. Defaults to latest single run containing this case.")
    judge.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
    add_judge_args(judge)
    judge.add_argument("--dry-run", action="store_true")

    batch = subparsers.add_parser("batch", help="Run multiple cases.")
    batch.add_argument("cases", nargs="*", help="Case ids. Defaults to all cases under cases/.")
    batch.add_argument("--run-id", help="Optional batch run id.")
    batch.add_argument("--workers", type=int, default=1)
    batch.add_argument("--repeats", type=int, default=1)
    batch.add_argument("--round-timeout", type=int, default=DEFAULT_TIMEOUT)
    batch.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
    batch.add_argument("--skip-judge", action="store_true")
    add_judge_args(batch)
    batch.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def add_case_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("case", help="Case id, e.g. 001.")


def add_common_run_args(parser: argparse.ArgumentParser, *, judge: bool = True) -> None:
    parser.add_argument("--run-id", help="Optional run id.")
    parser.add_argument("--round-timeout", type=int, default=DEFAULT_TIMEOUT)
    if judge:
        parser.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
        add_judge_args(parser)
    parser.add_argument("--dry-run", action="store_true")


def add_judge_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--judge-model", default=None, help="Override BENCHMARK_JUDGE_MODEL/Codex config.")
    parser.add_argument("--judge-provider", default=None, help="Override BENCHMARK_JUDGE_PROVIDER/Codex config.")
    parser.add_argument("--judge-reasoning-effort", default=None, help="Override judge reasoning effort.")


def judge_override_args(args: argparse.Namespace) -> list[str]:
    values = (
        ("--judge-model", getattr(args, "judge_model", None)),
        ("--judge-provider", getattr(args, "judge_provider", None)),
        ("--judge-reasoning-effort", getattr(args, "judge_reasoning_effort", None)),
    )
    return [item for flag, value in values if value for item in (flag, str(value))]


def resolve_python(value: str | None) -> Path:
    python_bin = Path(value).expanduser() if value else DEFAULT_PYTHON
    return python_bin if python_bin.exists() else Path(sys.executable)


def run_case_command(
    *,
    python_bin: Path,
    case_id: str,
    run_id: str | None,
    round_timeout: int,
    judge_timeout: int,
    extra_args: list[str] | None = None,
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
    if extra_args:
        command.extend(extra_args)
    return command


def run_command(command: list[str], *, env: dict[str, str], dry_run: bool) -> None:
    print("+ " + " ".join(command))
    if dry_run:
        return
    raise SystemExit(subprocess.run(command, cwd=REPO_DIR, env=env, check=False).returncode)


def run_drawio_preflight(*, python_bin: Path, env: dict[str, str], dry_run: bool) -> None:
    command = [
        str(python_bin),
        str(DRAWIO_PREFLIGHT),
        "--drawio-url",
        env.get("PATENT_CREATOR_DRAWIO_EMBED_URL", DEFAULT_DRAWIO_URL),
    ]
    print("+ " + " ".join(command))
    if dry_run:
        return
    completed = subprocess.run(command, cwd=REPO_DIR, env=env, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode or 1)


def command_requires_drawio_preflight(command: str) -> bool:
    return command in DRAWIO_PREFLIGHT_COMMANDS


def normalize_case(case_id: str) -> str:
    return str(case_id).zfill(3)


def list_cases() -> None:
    for case_dir in case_dirs():
        print(f"{case_dir.name}\t{case_title(case_dir)}")


def case_dirs() -> list[Path]:
    return sorted((BENCHMARK_DIR / "cases").glob("[0-9][0-9][0-9]"))


def case_title(case_dir: Path) -> str:
    request_path = case_dir / "request.md"
    if not request_path.exists():
        return ""
    for line in request_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return ""


def show_status(*, run_id: str | None, case_id: str | None) -> None:
    run_dir = BENCHMARK_DIR / "runs" / run_id if run_id else latest_run_dir()
    if run_dir is None:
        raise SystemExit("No runs found.")
    execution_files = sorted(run_dir.glob("**/execution.json"))
    if case_id:
        normalized = normalize_case(case_id)
        execution_files = [path for path in execution_files if normalized in path.parts]
    if not execution_files:
        print(f"No execution.json files under {run_dir}")
        return
    for path in execution_files:
        payload = read_json(path)
        rel = path.relative_to(BENCHMARK_DIR)
        conclusion = payload.get("conclusion")
        score = None
        if isinstance(conclusion, dict) and isinstance(conclusion.get("path"), str):
            result_path = path.parent / conclusion["path"]
            if result_path.exists():
                score = read_json(result_path).get("total_score")
        suffix = f" score={score}" if score is not None else ""
        print(
            f"{rel}: status={payload.get('status', '-')} "
            f"duration_ms={payload.get('duration_ms', '-')} "
            f"agent={payload.get('agent', {}).get('model', '-')} "
            f"judge={payload.get('judge', {}).get('model', '-')}{suffix}"
        )


def latest_run_dir() -> Path | None:
    runs_dir = BENCHMARK_DIR / "runs"
    if not runs_dir.exists():
        return None
    candidates = [path for path in runs_dir.iterdir() if path.is_dir() and (path / "run.json").exists()]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def latest_run_id_for_case(case_id: str) -> str:
    runs_dir = BENCHMARK_DIR / "runs"
    executions = sorted(
        runs_dir.glob(f"*/cases/{case_id}/execution.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for execution in executions:
        payload = read_json(execution)
        if payload.get("schema_version") == "patent-technical-solution-execution-v2":
            return execution.parents[2].relative_to(runs_dir).as_posix()
    raise SystemExit(f"No reusable v2 subject found for case {case_id}. Run subject first.")


def read_json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


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


if __name__ == "__main__":
    main()
