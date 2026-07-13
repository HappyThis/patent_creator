from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
RUN_CASE = Path(__file__).resolve().parent / "run_case.py"
EVALUATOR_DIR = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()

if __package__:
    from .judge_runtime import JudgeRuntimeResolutionError, resolve_judge_runtime
    from .process_utils import terminate_process_group
    from .records import (
        aggregate_case_records,
        atomic_write_json,
        finish_execution,
        finish_run_record,
        new_execution,
        new_run_record,
        read_json_dict,
    )
    from .run_metadata import (
        capture_judge_requested_config,
        capture_model_config,
        git_metadata,
        normalize_judge_reasoning_effort,
    )
    from .tracks import TrackConfigError, load_track, resolve_track_case
else:
    if str(EVALUATOR_DIR) not in sys.path:
        sys.path.insert(0, str(EVALUATOR_DIR))

    from judge_runtime import JudgeRuntimeResolutionError, resolve_judge_runtime  # noqa: E402
    from process_utils import terminate_process_group  # noqa: E402
    from records import (  # noqa: E402
        aggregate_case_records,
        atomic_write_json,
        finish_execution,
        finish_run_record,
        new_execution,
        new_run_record,
        read_json_dict,
    )
    from run_metadata import (  # noqa: E402
        capture_judge_requested_config,
        capture_model_config,
        git_metadata,
        normalize_judge_reasoning_effort,
    )
    from tracks import TrackConfigError, load_track, resolve_track_case  # noqa: E402


def main() -> None:
    args = parse_args()
    track_id = str(getattr(args, "track", None) or "general_solution")
    try:
        track = load_track(track_id, benchmark_dir=BENCHMARK_DIR)
        case_ids = [str(value).zfill(3) for value in (args.cases or track.case_ids)]
        for case_id in case_ids:
            resolve_track_case(track, case_id)
    except TrackConfigError as exc:
        raise SystemExit(str(exc)) from exc
    validate_batch_inputs(args, case_ids)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    runs_dir = Path(args.runs_dir).resolve()
    run_dir = runs_dir / run_id
    run_path = run_dir / "run.json"
    jobs = [
        {
            "case_id": case_id,
            "repeat": repeat,
            "case_run_dir": run_dir / "cases" / case_id / f"r{repeat:02d}",
        }
        for repeat in range(1, args.repeats + 1)
        for case_id in case_ids
    ]
    models = capture_model_config()
    if track.subject_policy.web_search_enabled is not None:
        models["agent"]["web_search_enabled"] = track.subject_policy.web_search_enabled
    judge_requested = capture_judge_requested_config()
    apply_judge_overrides(models, judge_requested, args)
    config = {
        "track_id": track.track_id,
        "cases": case_ids,
        "repeats": args.repeats,
        "workers": args.workers,
        "round_timeout_seconds": args.round_timeout,
        "judge_timeout_seconds": args.judge_timeout,
        "skip_judge": bool(args.skip_judge),
    }
    run_record = new_run_record(
        run_id=run_id,
        run_kind="batch",
        case_ids=case_ids,
        config=config,
        models=models,
        benchmark_git=git_metadata(BENCHMARK_DIR),
    )
    diagnostics = run_record.setdefault("diagnostics", {})
    diagnostics["judge_requested"] = dict(judge_requested)
    atomic_write_json(run_path, run_record)
    judge_runtime_resolution = None
    if not args.skip_judge:
        judge_config = models["judge"]
        try:
            judge_runtime_resolution = asyncio.run(
                resolve_judge_runtime(
                    cwd=run_dir,
                    model=judge_config.get("model"),
                    provider=str(judge_config.get("provider") or "openai"),
                    reasoning_effort=str(judge_config.get("reasoning_effort") or "high"),
                    service_tier=judge_config.get("service_tier"),
                )
            )
        except JudgeRuntimeResolutionError as exc:
            diagnostics["judge_runtime_resolution"] = exc.resolution
            finish_run_record(
                run_record,
                status="failed",
                cases=[],
                aggregate=None,
                error={
                    "phase": "judge_preflight",
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            atomic_write_json(run_path, run_record)
            raise SystemExit(str(exc)) from exc
        diagnostics["judge_runtime_resolution"] = judge_runtime_resolution
        atomic_write_json(run_path, run_record)
    try:
        records = run_jobs(
            jobs,
            args=args,
            runs_dir=runs_dir,
            run_id=run_id,
            models=models,
            judge_requested=judge_requested,
            judge_runtime_resolution=judge_runtime_resolution,
        )
    except BaseException as exc:
        finish_run_record(
            run_record,
            status="failed",
            cases=[],
            aggregate=None,
            error={"phase": "batch", "type": type(exc).__name__, "message": str(exc)},
        )
        atomic_write_json(run_path, run_record)
        raise

    aggregate = aggregate_case_records(records)
    statuses = {str(item.get("status")) for item in records}
    completed_statuses = {"completed", "subject_completed"}
    if statuses and statuses <= completed_statuses:
        status = "completed"
    elif statuses & completed_statuses:
        status = "partial_failed"
    else:
        status = "failed"
    finish_run_record(
        run_record,
        status=status,
        cases=records,
        aggregate=aggregate,
        error=None,
    )
    atomic_write_json(run_path, run_record)
    print(f"run: {run_path}")


def run_jobs(
    jobs: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    runs_dir: Path,
    run_id: str,
    models: dict[str, Any],
    judge_requested: dict[str, Any],
    judge_runtime_resolution: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if args.workers <= 1:
        return [
            run_one_job(
                job,
                args=args,
                runs_dir=runs_dir,
                run_id=run_id,
                models=models,
                judge_requested=judge_requested,
                judge_runtime_resolution=judge_runtime_resolution,
            )
            for job in jobs
        ]

    records: list[dict[str, Any]] = []
    print(f"running {len(jobs)} jobs with {args.workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="bench-worker") as executor:
        future_to_job = {
            executor.submit(
                run_one_job,
                job,
                args=args,
                runs_dir=runs_dir,
                run_id=run_id,
                models=models,
                judge_requested=judge_requested,
                judge_runtime_resolution=judge_runtime_resolution,
            ): job
            for job in jobs
        }
        for future in as_completed(future_to_job):
            records.append(future.result())
    records.sort(key=lambda item: (str(item["case_id"]), int(item.get("repeat") or 0)))
    return records


def run_one_job(
    job: dict[str, Any],
    *,
    args: argparse.Namespace,
    runs_dir: Path,
    run_id: str,
    models: dict[str, Any],
    judge_requested: dict[str, Any],
    judge_runtime_resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    case_id = str(job["case_id"])
    repeat = int(job["repeat"])
    case_run_dir = Path(job["case_run_dir"])
    command = [
        sys.executable,
        str(RUN_CASE),
        "--case",
        case_id,
        "--track",
        str(getattr(args, "track", None) or "general_solution"),
        "--run-id",
        run_id,
        "--runs-dir",
        str(runs_dir),
        "--case-output-dir",
        str(case_run_dir),
        "--repeat",
        str(repeat),
        "--batch-child",
        "--round-timeout",
        str(args.round_timeout),
        "--judge-timeout",
        str(args.judge_timeout),
    ]
    if args.skip_judge:
        command.append("--skip-judge")
    command.extend(judge_override_args(args, judge_requested))
    if judge_runtime_resolution is not None:
        command.extend(
            [
                "--judge-runtime-resolution",
                json.dumps(judge_runtime_resolution, ensure_ascii=False, separators=(",", ":")),
            ]
        )
    label = f"case={case_id} repeat={repeat}/{args.repeats}"
    with PRINT_LOCK:
        print(f"[benchmark] {label} started", flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    process_timeout = args.round_timeout + (0 if args.skip_judge else args.judge_timeout) + 120
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=process_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(process)
        stdout, stderr = process.communicate()

    execution_path = case_run_dir / "execution.json"
    execution = read_json_dict(execution_path)
    if execution is None:
        execution = new_execution(
            run_id=run_id,
            case_id=case_id,
            repeat=repeat,
            agent_config=_execution_config(models.get("agent", {})),
            judge_config={
                **_execution_config(models.get("judge", {})),
                "requested": dict(judge_requested),
                "runtime_resolution": judge_runtime_resolution,
            },
        )
        execution["track_id"] = str(getattr(args, "track", None) or "general_solution")
        error_message = "Case process timed out。" if timed_out else (stderr.strip() or "Case process failed。")
        finish_execution(
            execution,
            status="process_failed",
            error={"phase": "process", "type": "process_failed", "message": error_message},
        )
        atomic_write_json(execution_path, execution)

    if process.returncode != 0 or timed_out:
        persist_process_failure(case_run_dir, stdout=stdout, stderr=stderr)

    conclusion = None
    conclusion_info = execution.get("conclusion")
    if isinstance(conclusion_info, dict) and isinstance(conclusion_info.get("path"), str):
        conclusion = read_json_dict(case_run_dir / conclusion_info["path"])
    record = {
        "case_id": case_id,
        "repeat": repeat,
        "status": execution.get("status") or "unknown",
        "execution": execution_path.relative_to(runs_dir / run_id).as_posix(),
        "conclusion": (
            (case_run_dir / conclusion_info["path"]).relative_to(runs_dir / run_id).as_posix()
            if isinstance(conclusion_info, dict) and isinstance(conclusion_info.get("path"), str)
            else None
        ),
        "total_score": conclusion.get("total_score") if isinstance(conclusion, dict) else None,
    }
    if isinstance(conclusion, dict) and "representation_score" in conclusion:
        record.update(
            {
                "solution_score": conclusion.get("solution_score"),
                "representation_score": conclusion.get("representation_score"),
                "representation": conclusion.get("representation"),
            }
        )
    with PRINT_LOCK:
        print(
            f"[benchmark] {label} finished status={record['status']} score={record['total_score']}",
            flush=True,
        )
    return record


def persist_process_failure(case_run_dir: Path, *, stdout: str, stderr: str) -> None:
    logs_dir = case_run_dir / "agent_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if stdout and not (logs_dir / "stdout.log").exists():
        (logs_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    if stderr:
        with (logs_dir / "stderr.log").open("a", encoding="utf-8") as handle:
            handle.write("\n[run_case process]\n")
            handle.write(stderr)


def apply_judge_overrides(
    models: dict[str, Any],
    requested: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    judge = dict(models.get("judge", {}))
    judge_model = getattr(args, "judge_model", None)
    judge_provider = getattr(args, "judge_provider", None)
    judge_reasoning_effort = getattr(args, "judge_reasoning_effort", None)
    if judge_model:
        requested["model"] = judge_model
        judge["model"] = judge_model
    if judge_provider:
        requested["provider"] = judge_provider
        judge["provider"] = judge_provider
    if judge_reasoning_effort:
        requested["reasoning_effort"] = judge_reasoning_effort.strip().lower()
        judge["reasoning_effort"] = normalize_judge_reasoning_effort(judge_reasoning_effort)
    models["judge"] = judge


def judge_override_args(args: argparse.Namespace, requested: dict[str, Any]) -> list[str]:
    values = (
        ("--judge-model", getattr(args, "judge_model", None), requested.get("model")),
        ("--judge-provider", getattr(args, "judge_provider", None), requested.get("provider")),
        (
            "--judge-reasoning-effort",
            getattr(args, "judge_reasoning_effort", None),
            requested.get("reasoning_effort"),
        ),
    )
    return [item for flag, provided, value in values if provided for item in (flag, str(value))]


def _execution_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in config.items()
        if key
        in {
            "provider",
            "model",
            "reasoning_effort",
            "base_url",
            "web_search_enabled",
            "service_tier",
            "sdk_version",
        }
        and value is not None
    }


def discover_case_ids() -> list[str]:
    return [path.name for path in sorted((BENCHMARK_DIR / "cases").glob("[0-9][0-9][0-9]"))]


def validate_batch_inputs(args: argparse.Namespace, case_ids: list[str]) -> None:
    if len(case_ids) != len(set(case_ids)):
        raise SystemExit("case ids must not contain duplicates")
    if args.workers < 1:
        raise SystemExit("--workers must be a positive integer")
    if args.repeats < 1:
        raise SystemExit("--repeats must be a positive integer")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run patent technical solution benchmark cases.")
    parser.add_argument("cases", nargs="*", help="Case ids. Defaults to all cases.")
    parser.add_argument("--track", default="general_solution", help="Benchmark track id.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--round-timeout", type=int, default=1800)
    parser.add_argument("--judge-timeout", type=int, default=1800)
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--judge-provider", default=None)
    parser.add_argument("--judge-reasoning-effort", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    main()
