#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
# Direct script execution already puts this directory on ``sys.path``.  Tests
# and other callers that load this file with ``spec_from_file_location`` do
# not, so bootstrap the sibling evaluator package explicitly.
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from evaluator.tracks import TrackConfigError, load_track  # noqa: E402

REPO_DIR = BENCHMARK_DIR.parents[1]
DEFAULT_PYTHON = REPO_DIR / "backend" / ".venv" / "bin" / "python"
DRAWIO_PREFLIGHT = REPO_DIR / "scripts" / "drawio_render_preflight.py"
DEFAULT_DRAWIO_URL = "http://127.0.0.1:8081/"
DRAWIO_PREFLIGHT_COMMANDS = frozenset({"run", "subject", "batch"})
DEFAULT_TIMEOUT = 900


def main() -> None:
    args = parse_args()
    if args.command == "list":
        list_cases(args.track)
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
            track_id=args.track,
            extra_args=judge_override_args(args),
        )
    elif args.command == "subject":
        command = run_case_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=args.run_id,
            round_timeout=args.round_timeout,
            judge_timeout=args.round_timeout,
            track_id=args.track,
            extra_args=["--skip-judge"],
        )
    elif args.command == "judge":
        run_id = args.run_id
        if run_id is None:
            run_id = (
                "LATEST_RUN_ID"
                if args.dry_run
                else latest_run_id_for_case(normalize_case(args.case), track_id=args.track)
            )
        command = judge_case_command(
            python_bin=python_bin,
            case_id=args.case,
            run_id=run_id,
            judge_timeout=args.judge_timeout,
            track_id=args.track,
            repeat=args.repeat,
            extra_args=judge_override_args(args),
            dry_run=args.dry_run,
        )
    elif args.command == "batch":
        command = [
            str(python_bin),
            str(BENCHMARK_DIR / "evaluator" / "run_all.py"),
            "--workers",
            str(args.workers),
            "--track",
            args.track,
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

    list_parser = subparsers.add_parser("list", help="List benchmark cases.")
    add_track_arg(list_parser)

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
    add_track_arg(judge)
    judge.add_argument("--run-id", help="Existing run id. Defaults to latest single run containing this case.")
    judge.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="Batch repeat number to judge. Auto-selected when the batch contains only one repeat.",
    )
    judge.add_argument("--judge-timeout", type=int, default=DEFAULT_TIMEOUT)
    add_judge_args(judge)
    judge.add_argument("--dry-run", action="store_true")

    batch = subparsers.add_parser("batch", help="Run multiple cases.")
    batch.add_argument("cases", nargs="*", help="Case ids. Defaults to all cases under cases/.")
    add_track_arg(batch)
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


def add_track_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--track", default="general_solution", help="Benchmark track id.")


def add_common_run_args(parser: argparse.ArgumentParser, *, judge: bool = True) -> None:
    add_track_arg(parser)
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
    track_id: str = "general_solution",
    extra_args: list[str] | None = None,
) -> list[str]:
    command = [
        str(python_bin),
        str(BENCHMARK_DIR / "evaluator" / "run_case.py"),
        "--case",
        normalize_case(case_id),
        "--track",
        track_id,
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


def judge_case_command(
    *,
    python_bin: Path,
    case_id: str,
    run_id: str,
    judge_timeout: int,
    track_id: str = "general_solution",
    repeat: int | None = None,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
) -> list[str]:
    if dry_run:
        if repeat is not None and repeat < 1:
            raise SystemExit("--repeat must be a positive integer.")
        resolved_repeat = repeat
        case_output_dir = (
            BENCHMARK_DIR
            / "runs"
            / run_id
            / "cases"
            / normalize_case(case_id)
            / f"r{repeat:02d}"
            if repeat is not None
            else None
        )
    else:
        case_output_dir, resolved_repeat = resolve_rejudge_target(
            run_id=run_id,
            case_id=case_id,
            repeat=repeat,
        )
    child_args = ["--skip-subject"]
    if case_output_dir is not None:
        child_args.extend(
            [
                "--case-output-dir",
                str(case_output_dir),
                "--repeat",
                str(resolved_repeat),
                "--batch-child",
            ]
        )
    if extra_args:
        child_args.extend(extra_args)
    return run_case_command(
        python_bin=python_bin,
        case_id=case_id,
        run_id=run_id,
        round_timeout=judge_timeout,
        judge_timeout=judge_timeout,
        track_id=track_id,
        extra_args=child_args,
    )


def resolve_rejudge_target(
    *,
    run_id: str,
    case_id: str,
    repeat: int | None,
) -> tuple[Path | None, int | None]:
    if repeat is not None and repeat < 1:
        raise SystemExit("--repeat must be a positive integer.")

    normalized_case = normalize_case(case_id)
    case_root = BENCHMARK_DIR / "runs" / run_id / "cases" / normalized_case
    single_execution = case_root / "execution.json"
    if single_execution.is_file():
        if repeat is not None:
            raise SystemExit(
                f"Run {run_id} stores case {normalized_case} as a single-case run; "
                "do not pass --repeat."
            )
        return None, None

    repeat_targets = batch_repeat_targets(case_root)
    if repeat is not None:
        target = repeat_targets.get(repeat)
        if target is not None:
            return target, repeat
        available = ", ".join(str(value) for value in repeat_targets) or "none"
        raise SystemExit(
            f"No execution.json for case {normalized_case} repeat {repeat} in run {run_id}. "
            f"Available repeats: {available}."
        )

    if len(repeat_targets) == 1:
        resolved_repeat, target = next(iter(repeat_targets.items()))
        return target, resolved_repeat
    if len(repeat_targets) > 1:
        available = ", ".join(str(value) for value in repeat_targets)
        raise SystemExit(
            f"Run {run_id} contains multiple repeats for case {normalized_case} "
            f"({available}); pass --repeat N."
        )
    raise SystemExit(f"No execution.json found for case {normalized_case} in run {run_id}.")


def batch_repeat_targets(case_root: Path) -> dict[int, Path]:
    targets: dict[int, Path] = {}
    for execution_path in sorted(case_root.glob("r*/execution.json")):
        repeat_dir = execution_path.parent
        suffix = repeat_dir.name[1:]
        if not suffix.isdigit() or int(suffix) < 1:
            continue
        repeat = int(suffix)
        if repeat in targets:
            raise SystemExit(
                f"Ambiguous batch directories for repeat {repeat}: "
                f"{targets[repeat]} and {repeat_dir}."
            )
        targets[repeat] = repeat_dir
    return dict(sorted(targets.items()))


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


def list_cases(track_id: str = "general_solution") -> None:
    for case_dir in case_dirs(track_id):
        print(f"{case_dir.name}\t{case_title(case_dir)}")


def case_dirs(track_id: str = "general_solution") -> list[Path]:
    try:
        track = load_track(track_id, benchmark_dir=BENCHMARK_DIR)
    except TrackConfigError as exc:
        raise SystemExit(str(exc)) from exc
    return [BENCHMARK_DIR / "cases" / case_id for case_id in track.case_ids]


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


def latest_run_id_for_case(case_id: str, *, track_id: str = "general_solution") -> str:
    runs_dir = BENCHMARK_DIR / "runs"
    executions = sorted(
        runs_dir.glob(f"*/cases/{case_id}/execution.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for execution in executions:
        payload = read_json(execution)
        execution_track = str(payload.get("track_id") or "general_solution")
        if (
            payload.get("schema_version") == "patent-technical-solution-execution-v2"
            and execution_track == track_id
        ):
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
