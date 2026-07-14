from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]
EVALUATOR_DIR = Path(__file__).resolve().parent
RUNNER_FILE = "runner.md"
_EXTERNAL_READ_ROOTS_ENV = "PATENT_CREATOR_AGENT_EXTERNAL_READ_ROOTS"
_EXEC_COMMAND_DISABLED_ENV = "PATENT_CREATOR_AGENT_EXEC_COMMAND_DISABLED"
_SUBJECT_ACCESS_POLICY_LOCK = threading.Lock()
_BATCH_RUN_LOCKS_GUARD = threading.Lock()
_BATCH_RUN_LOCKS: dict[Path, threading.RLock] = {}
_ACTIVE_REJUDGE_LOCKS_GUARD = threading.Lock()
_ACTIVE_REJUDGE_LOCKS: set[Path] = set()

if __package__:
    from .codex_judge import CodexJudgeTimeout, JudgeRunResult, run_codex_judge
    from .judge_runtime import JudgeRuntimeResolutionError, resolve_judge_runtime
    from .prepare_env import prepare_exploration_environment
    from .records import (
        EXECUTION_SCHEMA_VERSION,
        aggregate_case_records,
        atomic_write_json,
        finish_execution,
        finish_judge_attempt,
        finish_phase,
        finish_run_record,
        new_execution,
        new_run_record,
        preserve_legacy_judge_attempt,
        read_json_dict,
        start_judge_attempt,
        start_phase,
    )
    from .run_metadata import (
        apply_default_judge_config,
        capture_judge_requested_config,
        capture_model_config,
        compact_dict,
        git_metadata,
        normalize_judge_reasoning_effort,
    )
    from .tracks import TrackConfigError, load_track, resolve_track_case
else:
    if str(EVALUATOR_DIR) not in sys.path:
        sys.path.insert(0, str(EVALUATOR_DIR))

    from codex_judge import CodexJudgeTimeout, JudgeRunResult, run_codex_judge  # noqa: E402
    from judge_runtime import JudgeRuntimeResolutionError, resolve_judge_runtime  # noqa: E402
    from prepare_env import prepare_exploration_environment  # noqa: E402
    from records import (  # noqa: E402
        EXECUTION_SCHEMA_VERSION,
        aggregate_case_records,
        atomic_write_json,
        finish_execution,
        finish_judge_attempt,
        finish_phase,
        finish_run_record,
        new_execution,
        new_run_record,
        preserve_legacy_judge_attempt,
        read_json_dict,
        start_judge_attempt,
        start_phase,
    )
    from run_metadata import (  # noqa: E402
        apply_default_judge_config,
        capture_judge_requested_config,
        capture_model_config,
        compact_dict,
        git_metadata,
        normalize_judge_reasoning_effort,
    )
    from tracks import TrackConfigError, load_track, resolve_track_case  # noqa: E402


@dataclass(slots=True)
class SubjectRunResult:
    status: str
    project_id: str
    session_id: str
    round_id: str
    usage: dict[str, int] | None
    error: str | None = None


def main() -> None:
    args = parse_args()
    result = asyncio.run(run_case(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "judge_preflight_failed":
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one patent technical solution benchmark case.")
    parser.add_argument("--case", required=True, help="Case id, e.g. 001.")
    parser.add_argument("--benchmark-dir", default=str(BENCHMARK_DIR))
    parser.add_argument("--track", default=None, help="Benchmark id or legacy track id.")
    parser.add_argument("--run-id", default=None, help="Run id. Defaults to timestamp + case id.")
    parser.add_argument("--runs-dir", default=None, help="Directory for run artifacts.")
    parser.add_argument("--skip-judge", action="store_true", help="Run the subject agent without judging it.")
    parser.add_argument("--skip-subject", action="store_true", help="Judge the existing subject workspace.")
    parser.add_argument("--round-timeout", type=int, default=1800, help="Subject-agent timeout in seconds.")
    parser.add_argument("--judge-timeout", type=int, default=1800, help="Codex judge timeout in seconds.")
    parser.add_argument("--judge-model", default=None, help="Override BENCHMARK_JUDGE_MODEL/Codex config.")
    parser.add_argument("--judge-provider", default=None, help="Override BENCHMARK_JUDGE_PROVIDER/Codex config.")
    parser.add_argument("--judge-reasoning-effort", default=None, help="Override judge reasoning effort.")
    parser.add_argument("--judge-runtime-resolution", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--repeat", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--case-output-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--batch-child", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


async def run_case(args: argparse.Namespace) -> dict[str, Any]:
    manual_batch_rejudge = bool(
        getattr(args, "batch_child", False) and getattr(args, "skip_subject", False)
    )
    run_id = getattr(args, "run_id", None)
    if manual_batch_rejudge and run_id:
        case_id = str(args.case).zfill(3)
        case_run_dir = (
            Path(args.case_output_dir).resolve()
            if getattr(args, "case_output_dir", None)
            else Path(args.runs_dir).resolve() / str(run_id) / "cases" / case_id
        )
        with manual_rejudge_case_lock(case_run_dir):
            return await _run_case(args)
    return await _run_case(args)


async def _run_case(args: argparse.Namespace) -> dict[str, Any]:
    if args.skip_judge and args.skip_subject:
        raise SystemExit("--skip-judge 与 --skip-subject 不能同时使用。")

    case_id = str(args.case).zfill(3)
    benchmark_dir = Path(getattr(args, "benchmark_dir", None) or BENCHMARK_DIR).resolve()
    track_id_value = getattr(args, "track", None)
    track_id = str(track_id_value) if track_id_value else None
    try:
        track = load_track(track_id, benchmark_dir=benchmark_dir)
        track_case = resolve_track_case(track, case_id)
    except TrackConfigError as exc:
        raise SystemExit(str(exc)) from exc
    effective_benchmark_dir = getattr(track, "benchmark_dir", benchmark_dir)
    source_case_dir = track_case.source_case_dir

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{case_id}"
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else effective_benchmark_dir / "runs"
    run_dir = runs_dir / run_id
    if not args.batch_child and not args.skip_subject and run_dir.exists():
        raise SystemExit(f"Run id already exists: {run_id}")
    case_run_dir = (
        Path(args.case_output_dir).resolve()
        if args.case_output_dir
        else run_dir / "cases" / case_id
    )
    execution_path = case_run_dir / "execution.json"
    prepared_environment = case_run_dir / "prepared_environment"
    subject_dir = case_run_dir / "subject"
    agent_logs_dir = case_run_dir / "agent_logs"
    judge_logs_root = case_run_dir / "judge" / "codex_logs"
    conclusion_path = case_run_dir / "judge" / "conclusion" / "result.json"
    conclusion_rel = conclusion_path.relative_to(case_run_dir).as_posix()

    models = capture_model_config()
    if track.subject_policy.web_search_enabled is not None:
        models["agent"]["web_search_enabled"] = track.subject_policy.web_search_enabled
    judge_requested = capture_judge_requested_config()
    apply_default_judge_config(models, judge_requested, getattr(track, "default_judge", None))
    judge_config = dict(models["judge"])
    if args.judge_model:
        judge_requested["model"] = args.judge_model
        judge_config["model"] = args.judge_model
    if args.judge_provider:
        judge_requested["provider"] = args.judge_provider
        judge_config["provider"] = args.judge_provider
    if args.judge_reasoning_effort:
        judge_requested["reasoning_effort"] = args.judge_reasoning_effort.strip().lower()
        judge_config["reasoning_effort"] = normalize_judge_reasoning_effort(args.judge_reasoning_effort)
    models["judge"] = judge_config
    run_config = compact_dict(
        {
            "case_id": case_id,
            "benchmark_id": track.track_id,
            "track_id": track.track_id,
            "repeat": args.repeat,
            "skip_judge": bool(args.skip_judge),
            "skip_subject": bool(args.skip_subject),
            "round_timeout_seconds": args.round_timeout,
            "judge_timeout_seconds": args.judge_timeout,
        }
    )
    manual_batch_rejudge = bool(args.batch_child and args.skip_subject)
    run_record: dict[str, Any] | None = None
    run_record_path: Path | None = None
    if manual_batch_rejudge:
        run_record_path = run_dir / "run.json"
        with batch_run_record_lock(run_dir):
            run_record = read_json_dict(run_record_path)
        if run_record is None or run_record.get("run_kind") != "batch":
            raise SystemExit(f"没有可更新的父 batch run.json：{run_record_path}")
        if run_record.get("status") == "running":
            raise SystemExit("父 batch 仍在运行，不能同时启动手动重判。")
        if str(run_record.get("run_id") or "") != run_id:
            raise SystemExit(f"父 batch run_id 与重判目标不一致：{run_record_path}")
        parent_config = run_record.get("config")
        parent_track_id = (
            str(parent_config.get("track_id") or "general_solution")
            if isinstance(parent_config, dict)
            else "general_solution"
        )
        if parent_track_id != track.track_id:
            raise SystemExit(
                f"父 batch 属于 track {parent_track_id}，不能按 {track.track_id} 重判。"
            )
        expected_identities = expected_batch_identities(run_record)
        target_identity = (case_id, args.repeat)
        if target_identity not in expected_identities:
            raise SystemExit(f"重判目标不属于父 batch：{target_identity!r}")

    if args.skip_subject:
        execution = require_reusable_execution(
            execution_path,
            prepared_environment,
            subject_dir,
            expected_run_id=run_id,
            expected_case_id=case_id,
            expected_repeat=args.repeat,
        )
        execution_track_id = str(execution.get("track_id") or "general_solution")
        if execution_track_id != track.track_id:
            raise SystemExit(
                f"现有 execution 属于 track {execution_track_id}，不能按 {track.track_id} 重判。"
            )
        reset_judge_phase(execution, judge_config, requested=judge_requested)
    else:
        if execution_path.exists():
            raise SystemExit(f"Case 运行目录已存在：{case_run_dir}。请更换 run id。")
        case_run_dir.mkdir(parents=True, exist_ok=True)
        execution = new_execution(
            run_id=run_id,
            case_id=case_id,
            repeat=args.repeat,
            agent_config=execution_agent_config(models["agent"]),
            judge_config=execution_judge_config(judge_config, requested=judge_requested),
        )
        execution["track_id"] = track.track_id
        execution["benchmark_id"] = track.track_id
        if track.judge_profile == "representation_semantics":
            execution["agent"]["access_policy"] = {
                "web_search_enabled": False,
                "exec_command_enabled": False,
                "external_read_scope": "prepared_environment_only",
            }

    atomic_write_json(execution_path, execution)
    if manual_batch_rejudge and run_record is not None and run_record_path is not None:
        run_record = persist_batch_judge_override(
            run_record_path,
            case_id=case_id,
            repeat=args.repeat,
            judge_config=judge_config,
            judge_requested=judge_requested,
        )
    if not args.batch_child:
        run_record_path = run_dir / "run.json"
        run_record = read_json_dict(run_record_path) if args.skip_subject else None
        if run_record is None:
            run_record = new_run_record(
                run_id=run_id,
                run_kind="single_case",
                case_ids=[case_id],
                config=run_config,
                models=models,
                benchmark_git=git_metadata(effective_benchmark_dir),
            )
        elif args.skip_subject:
            run_record.setdefault("models", {})["judge"] = judge_config
            run_record.setdefault("config", {})["skip_judge"] = False
            run_record["config"]["judge_timeout_seconds"] = args.judge_timeout
        diagnostics = run_record.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
            run_record["diagnostics"] = diagnostics
        diagnostics["judge_requested"] = dict(judge_requested)
        atomic_write_json(run_record_path, run_record)

    runtime_resolution: dict[str, Any] | None = None
    effective_judge_config: dict[str, Any] | None = None
    if not args.skip_judge:
        _announce(case_id, "judge_preflight", "resolving a compatible Codex runtime")
        try:
            runtime_resolution = (
                parse_runtime_resolution(args.judge_runtime_resolution)
                if args.judge_runtime_resolution
                else await resolve_judge_runtime(
                    cwd=case_run_dir,
                    model=judge_config.get("model"),
                    provider=str(judge_config.get("provider") or "openai"),
                    reasoning_effort=str(judge_config.get("reasoning_effort") or "high"),
                    service_tier=judge_config.get("service_tier"),
                )
            )
            effective_judge_config = resolved_judge_config(judge_config, runtime_resolution)
        except JudgeRuntimeResolutionError as exc:
            runtime_resolution = exc.resolution
            return finish_judge_preflight_failure(
                execution=execution,
                execution_path=execution_path,
                requested=judge_requested,
                runtime_resolution=runtime_resolution,
                exc=exc,
                run_record=run_record,
                run_record_path=run_record_path,
                run_dir=run_dir,
                case_run_dir=case_run_dir,
            )
        except BaseException as exc:
            runtime_resolution = unexpected_runtime_resolution(exc)
            return finish_judge_preflight_failure(
                execution=execution,
                execution_path=execution_path,
                requested=judge_requested,
                runtime_resolution=runtime_resolution,
                exc=exc,
                run_record=run_record,
                run_record_path=run_record_path,
                run_dir=run_dir,
                case_run_dir=case_run_dir,
            )

        execution["judge"]["runtime_resolution"] = runtime_resolution
        execution["judge"]["effective"] = effective_judge_config
        if args.skip_subject:
            conclusion_path.unlink(missing_ok=True)
        run_record = persist_runtime_diagnostic(
            run_record,
            run_record_path,
            runtime_resolution,
        )
        atomic_write_json(execution_path, execution)
        selected_source = runtime_resolution["selected"]["source"]
        _announce(case_id, "judge_preflight", f"selected runtime: {selected_source}")

    if not args.skip_subject:
        _announce(case_id, "prepare", "preparing frozen environment")
        try:
            prepare_exploration_environment(
                source_case_dir,
                prepared_environment,
                expose_snapshot_provenance=track.subject_policy.expose_snapshot_provenance,
                preserve_snapshot_git=track.subject_policy.preserve_snapshot_git,
            )
        except BaseException as exc:
            finish_execution(
                execution,
                status="preparation_failed",
                error=error_record("prepare", exc),
            )
            atomic_write_json(execution_path, execution)
            return finalize_case(
                execution=execution,
                conclusion=None,
                run_record=run_record,
                run_dir=run_dir,
                case_run_dir=case_run_dir,
            )

        start_phase(execution, "agent")
        atomic_write_json(execution_path, execution)
        _announce(case_id, "agent", "subject agent started")
        try:
            with capture_agent_logs(agent_logs_dir):
                subject_result = await run_subject_agent(
                    case_id=case_id,
                    prepared_environment=prepared_environment,
                    request_md=(source_case_dir / "request.md").read_text(encoding="utf-8"),
                    subject_dir=subject_dir,
                    round_timeout=args.round_timeout,
                    web_search_enabled=track.subject_policy.web_search_enabled,
                    restrict_external_access=track.judge_profile == "representation_semantics",
                    runner_path=getattr(track, "runner_path", effective_benchmark_dir / RUNNER_FILE),
                    runner_addendum_path=getattr(
                        track,
                        "runner_addendum_path",
                        track.track_dir / RUNNER_FILE
                        if (track.track_dir / RUNNER_FILE).is_file()
                        else None,
                    ),
                )
            finish_phase(
                execution,
                "agent",
                status=subject_result.status,
                fields={
                    "project_id": subject_result.project_id,
                    "session_id": subject_result.session_id,
                    "round_id": subject_result.round_id,
                    "usage": subject_result.usage,
                },
            )
            atomic_write_json(execution_path, execution)
        except BaseException as exc:
            append_traceback(agent_logs_dir / "stderr.log")
            finish_phase(execution, "agent", status="failed")
            finish_execution(execution, status="agent_failed", error=error_record("agent", exc))
            atomic_write_json(execution_path, execution)
            return finalize_case(
                execution=execution,
                conclusion=None,
                run_record=run_record,
                run_dir=run_dir,
                case_run_dir=case_run_dir,
            )

        if subject_result.status != "completed":
            final_status = "agent_timed_out" if subject_result.status == "timed_out" else "agent_failed"
            finish_execution(
                execution,
                status=final_status,
                error={"phase": "agent", "type": subject_result.status, "message": subject_result.error},
            )
            atomic_write_json(execution_path, execution)
            return finalize_case(
                execution=execution,
                conclusion=None,
                run_record=run_record,
                run_dir=run_dir,
                case_run_dir=case_run_dir,
            )

        try:
            validate_subject_workspace(subject_dir)
            if track.judge_profile == "representation_semantics":
                execution["agent"]["network_audit"] = audit_subject_network_use(subject_dir)
                atomic_write_json(execution_path, execution)
        except BaseException as exc:
            finish_execution(execution, status="agent_failed", error=error_record("agent", exc))
            atomic_write_json(execution_path, execution)
            return finalize_case(
                execution=execution,
                conclusion=None,
                run_record=run_record,
                run_dir=run_dir,
                case_run_dir=case_run_dir,
            )

    if args.skip_judge:
        finish_execution(execution, status="subject_completed")
        atomic_write_json(execution_path, execution)
        return finalize_case(
            execution=execution,
            conclusion=None,
            run_record=run_record,
            run_dir=run_dir,
            case_run_dir=case_run_dir,
        )

    if runtime_resolution is None or effective_judge_config is None:
        raise RuntimeError("Judge runtime resolution is missing after preflight.")
    selected_runtime = runtime_resolution.get("selected")
    if not isinstance(selected_runtime, dict):
        raise RuntimeError("Judge runtime resolution has no selected runtime.")
    attempt_number = len(execution["judge"].get("attempts", [])) + 1
    judge_logs_rel = f"judge/codex_logs/attempt-{attempt_number:03d}"
    judge_logs_dir = case_run_dir / judge_logs_rel

    start_phase(execution, "judge")
    start_judge_attempt(
        execution,
        logs_path=judge_logs_rel,
        requested=dict(judge_requested),
        effective=effective_judge_config,
        runtime_resolution=runtime_resolution,
    )
    atomic_write_json(execution_path, execution)
    _announce(case_id, "judge", "Codex judge started")
    try:
        judge_result = await run_codex_judge(
            case_id=case_id,
            case_run_dir=case_run_dir,
            source_case_dir=source_case_dir,
            benchmark_dir=effective_benchmark_dir,
            logs_dir=judge_logs_dir,
            model=judge_config.get("model"),
            provider=str(judge_config.get("provider") or "openai"),
            reasoning_effort=str(judge_config.get("reasoning_effort") or "high"),
            service_tier=judge_config.get("service_tier"),
            codex_bin=selected_runtime.get("launch_codex_bin"),
            timeout_seconds=args.judge_timeout,
            track_id=track.track_id,
            judge_profile=track.judge_profile,
            track_judge_path=track.track_judge_path,
            track_rubric_path=track_case.track_rubric_path,
            representation_policies=(
                {
                    "figure": str(track_case.figure_policy),
                    "formula": str(track_case.formula_policy),
                }
                if track.judge_profile == "representation_semantics"
                else None
            ),
        )
    except CodexJudgeTimeout as exc:
        error = error_record("judge", exc)
        finish_phase(execution, "judge", status="timed_out")
        finish_judge_attempt(execution, status="timed_out", error=error)
        finish_execution(execution, status="judge_timed_out", error=error)
        atomic_write_json(execution_path, execution)
        return finalize_case(
            execution=execution,
            conclusion=None,
            run_record=run_record,
            run_dir=run_dir,
            case_run_dir=case_run_dir,
        )
    except BaseException as exc:
        error = error_record("judge", exc)
        finish_phase(execution, "judge", status="failed")
        finish_judge_attempt(execution, status="failed", error=error)
        finish_execution(execution, status="judge_failed", error=error)
        atomic_write_json(execution_path, execution)
        return finalize_case(
            execution=execution,
            conclusion=None,
            run_record=run_record,
            run_dir=run_dir,
            case_run_dir=case_run_dir,
        )

    atomic_write_json(conclusion_path, judge_result.conclusion)
    effective_judge_config.update(
        compact_dict(
            {
                "provider": judge_result.provider,
                "model": judge_result.model,
                "reasoning_effort": judge_result.reasoning_effort,
                "sdk_version": judge_result.sdk_version,
            }
        )
    )
    runtime_config = effective_judge_config.get("runtime")
    if isinstance(runtime_config, dict) and judge_result.runtime_version:
        runtime_config["judge_appserver_version"] = judge_result.runtime_version
    finish_phase(
        execution,
        "judge",
        status="completed",
        fields=judge_execution_fields(judge_result),
    )
    finish_judge_attempt(execution, status="completed")
    finish_execution(execution, status="completed", conclusion_path=conclusion_rel)
    atomic_write_json(execution_path, execution)
    _announce(case_id, "result", f"case scored: {judge_result.conclusion['total_score']}")
    return finalize_case(
        execution=execution,
        conclusion=judge_result.conclusion,
        run_record=run_record,
        run_dir=run_dir,
        case_run_dir=case_run_dir,
    )


async def run_subject_agent(
    *,
    case_id: str,
    prepared_environment: Path,
    request_md: str,
    subject_dir: Path,
    round_timeout: int,
    web_search_enabled: bool | None = None,
    restrict_external_access: bool = False,
    runner_path: Path | None = None,
    runner_addendum_path: Path | None = None,
) -> SubjectRunResult:
    backend_dir = REPO_DIR / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    async with _temporary_subject_access_policy(
        prepared_environment,
        enabled=restrict_external_access,
    ):
        return await _run_subject_agent_impl(
            case_id=case_id,
            prepared_environment=prepared_environment,
            request_md=request_md,
            subject_dir=subject_dir,
            round_timeout=round_timeout,
            web_search_enabled=web_search_enabled,
            runner_path=runner_path,
            runner_addendum_path=runner_addendum_path,
        )


@contextlib.asynccontextmanager
async def _temporary_subject_access_policy(
    prepared_environment: Path,
    *,
    enabled: bool,
) -> AsyncIterator[None]:
    await _acquire_subject_access_policy_lock()
    try:
        if not enabled:
            yield
            return

        backend_dir = REPO_DIR / "backend"
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        from app.agents.workers import MAIN_AGENT_TOOLS

        previous_external_roots = os.environ.get(_EXTERNAL_READ_ROOTS_ENV)
        previous_exec_disabled = os.environ.get(_EXEC_COMMAND_DISABLED_ENV)
        previous_tools = list(MAIN_AGENT_TOOLS)
        try:
            os.environ[_EXTERNAL_READ_ROOTS_ENV] = str(prepared_environment.resolve())
            os.environ[_EXEC_COMMAND_DISABLED_ENV] = "1"
            MAIN_AGENT_TOOLS[:] = [
                tool
                for tool in MAIN_AGENT_TOOLS
                if tool.get("function", {}).get("name") != "exec_command"
            ]
            yield
        finally:
            _restore_environment_value(_EXTERNAL_READ_ROOTS_ENV, previous_external_roots)
            _restore_environment_value(_EXEC_COMMAND_DISABLED_ENV, previous_exec_disabled)
            MAIN_AGENT_TOOLS[:] = previous_tools
    finally:
        _SUBJECT_ACCESS_POLICY_LOCK.release()


async def _acquire_subject_access_policy_lock() -> None:
    while not _SUBJECT_ACCESS_POLICY_LOCK.acquire(blocking=False):
        await asyncio.sleep(0.01)


def _restore_environment_value(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


async def _run_subject_agent_impl(
    *,
    case_id: str,
    prepared_environment: Path,
    request_md: str,
    subject_dir: Path,
    round_timeout: int,
    web_search_enabled: bool | None,
    runner_path: Path | None,
    runner_addendum_path: Path | None,
) -> SubjectRunResult:
    backend_dir = REPO_DIR / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from app.core import Settings, generate_id
    from app.schemas import ChatMessageRequest
    from app.services import AppServices

    data_dir = subject_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings.from_env()
    if web_search_enabled is not None:
        settings.openai_web_search_enabled = web_search_enabled
    settings.data_dir = data_dir
    settings.llm_timeout = max(settings.llm_timeout, float(round_timeout))
    services = AppServices(settings)
    project = services.store.create_project_with_id(f"bench_{case_id}_{generate_id('proj')}", f"Benchmark {case_id}")
    disclosure = services.store.get_disclosure(project.project_id)
    section_id = find_section_id(disclosure.get("sections", []), "技术方案")
    if section_id is None:
        raise RuntimeError("技术方案章节不存在。")

    runner_md = (runner_path or BENCHMARK_DIR / RUNNER_FILE).read_text(encoding="utf-8")
    runner_parts = [runner_md]
    if runner_addendum_path is not None:
        runner_parts.append(runner_addendum_path.read_text(encoding="utf-8"))
    message = "\n\n".join(
        [
            *runner_parts,
            f"探索环境路径：{prepared_environment.resolve()}",
            request_md,
        ]
    )
    response = await services.chat.start_round(
        project.project_id,
        ChatMessageRequest(session_id=None, message=message, active_section_id=section_id),
    )
    try:
        await wait_for_round(services, project.project_id, timeout_seconds=round_timeout)
    except asyncio.CancelledError:
        await cancel_subject_round(services, project.project_id, response)
        raise
    except TimeoutError as exc:
        await cancel_subject_round(services, project.project_id, response)
        events = services.store.read_session_events(project.project_id, response.session_id)
        return SubjectRunResult(
            status="timed_out",
            project_id=project.project_id,
            session_id=response.session_id,
            round_id=response.round_id,
            usage=aggregate_agent_usage(events),
            error=str(exc),
        )

    events = services.store.read_session_events(project.project_id, response.session_id)
    failed_event = next(
        (
            event
            for event in reversed(events)
            if event.type == "agent_output"
            and event.round_id == response.round_id
            and event.payload.get("status") == "failed"
        ),
        None,
    )
    if failed_event is not None:
        message = str(failed_event.payload.get("message") or failed_event.payload.get("detail") or "Agent round failed。")
        status = "failed"
    else:
        message = None
        status = "completed"
    return SubjectRunResult(
        status=status,
        project_id=project.project_id,
        session_id=response.session_id,
        round_id=response.round_id,
        usage=aggregate_agent_usage(events),
        error=message,
    )


async def cancel_subject_round(services: Any, project_id: str, response: Any) -> None:
    try:
        await services.chat.cancel_round(project_id, response.session_id, response.round_id)
    except Exception:
        pass


async def wait_for_round(services: Any, project_id: str, *, timeout_seconds: int) -> None:
    started = time.monotonic()
    while services.store.get_project(project_id).is_busy:
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(f"主 Agent 超时：{timeout_seconds} 秒。")
        await asyncio.sleep(0.5)


def find_section_id(sections: list[dict[str, Any]], title: str) -> str | None:
    for section in sections:
        section_title = section.get("title")
        text = section_title.get("text") if isinstance(section_title, dict) else None
        if text == title:
            value = section.get("id")
            return str(value) if value else None
        found = find_section_id(section.get("sections", []), title)
        if found:
            return found
    return None


def validate_subject_workspace(subject_dir: Path) -> Path:
    project_dirs = sorted(path for path in (subject_dir / "data" / "projects").glob("*") if path.is_dir())
    if len(project_dirs) != 1:
        raise RuntimeError(f"Subject 应包含且只包含一个项目，实际为 {len(project_dirs)} 个。")
    disclosure_path = project_dirs[0] / "disclosure.json"
    disclosure = read_json_dict(disclosure_path)
    if disclosure is None:
        raise RuntimeError(f"Subject disclosure 不存在或不是合法 JSON：{disclosure_path}")
    return disclosure_path


def audit_subject_network_use(subject_dir: Path) -> dict[str, Any]:
    """Reject cooperative-benchmark violations recorded in subject events."""

    events_scanned = 0
    violations: list[str] = []
    for session_path in sorted(subject_dir.glob("data/projects/*/sessions/*.jsonl")):
        for line_number, raw_line in enumerate(
            session_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            events_scanned += 1
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "llm_audit" and str(payload.get("category") or "").lower() == "web_search":
                source = str(payload.get("source") or "unknown")
                violations.append(f"{session_path.name}:{line_number}:web_search:{source}")
                continue
            if event_type != "tool_call":
                continue
            tool_name = str(payload.get("tool") or "").lower()
            if "web_search" in tool_name or tool_name in {"search_web", "browser_search"}:
                violations.append(f"{session_path.name}:{line_number}:{tool_name}")
                continue
            if tool_name == "exec_command":
                violations.append(f"{session_path.name}:{line_number}:exec_command")
    if violations:
        raise RuntimeError(
            "representation subject attempted a disabled external-access tool: "
            + ", ".join(violations)
        )
    return {
        "status": "passed",
        "events_scanned": events_scanned,
        "external_network_calls": 0,
    }


def aggregate_agent_usage(events: list[Any]) -> dict[str, int] | None:
    totals: dict[str, int] = {}
    aliases = {
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
    }
    for event in events:
        if getattr(event, "type", None) != "agent_message":
            continue
        payload = getattr(event, "payload", {})
        message = payload.get("message") if isinstance(payload, dict) else None
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            normalized = aliases.get(key, key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[normalized] = totals.get(normalized, 0) + value
    return totals or None


def execution_agent_config(config: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "provider": config.get("provider"),
            "model": config.get("model"),
            "reasoning_effort": config.get("reasoning_effort"),
            "base_url": config.get("base_url"),
            "web_search_enabled": config.get("web_search_enabled"),
        }
    )


def execution_judge_config(
    config: dict[str, Any],
    *,
    requested: dict[str, Any] | None = None,
    runtime_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = compact_dict(
        {
            "provider": config.get("provider"),
            "model": config.get("model"),
            "reasoning_effort": config.get("reasoning_effort"),
            "service_tier": config.get("service_tier"),
            "sdk_version": config.get("sdk_version"),
        }
    )
    value["requested"] = dict(requested or value)
    value["effective"] = None
    value["runtime_resolution"] = runtime_resolution
    value["attempts"] = []
    return value


def parse_runtime_resolution(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Batch Judge runtime resolution is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Batch Judge runtime resolution must be a JSON object.")
    if not isinstance(value.get("policy"), list) or not isinstance(value.get("attempts"), list):
        raise ValueError("Batch Judge runtime resolution is missing policy or attempts.")
    selected = value.get("selected")
    if not isinstance(selected, dict) or not isinstance(selected.get("source"), str):
        raise ValueError("Batch Judge runtime resolution has no selected runtime.")
    launch_codex_bin = selected.get("launch_codex_bin")
    if launch_codex_bin is not None and not isinstance(launch_codex_bin, str):
        raise ValueError("Selected Judge runtime has an invalid launch path.")
    return value


def resolved_judge_config(
    config: dict[str, Any],
    runtime_resolution: dict[str, Any],
) -> dict[str, Any]:
    selected = runtime_resolution.get("selected")
    if not isinstance(selected, dict):
        raise ValueError("Judge runtime resolution has no selected runtime.")
    runtime = compact_dict(
        {
            "source": selected.get("source"),
            "path": selected.get("path"),
            "launch_mode": selected.get("launch_mode"),
            "binary_version": selected.get("binary_version"),
            "preflight_appserver_version": selected.get("appserver_version"),
            "sdk_version": selected.get("sdk_version"),
        }
    )
    return compact_dict(
        {
            "provider": config.get("provider"),
            "model": config.get("model"),
            "reasoning_effort": config.get("reasoning_effort"),
            "service_tier": config.get("service_tier"),
            "sdk_version": selected.get("sdk_version") or config.get("sdk_version"),
            "runtime": runtime,
        }
    )


def append_runtime_diagnostic(
    run_record: dict[str, Any] | None,
    runtime_resolution: dict[str, Any],
) -> None:
    if run_record is None:
        return
    diagnostics = run_record.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
        run_record["diagnostics"] = diagnostics
    resolutions = diagnostics.setdefault("judge_runtime_resolutions", [])
    if not isinstance(resolutions, list):
        resolutions = []
        diagnostics["judge_runtime_resolutions"] = resolutions
    resolutions.append(runtime_resolution)


def persist_runtime_diagnostic(
    run_record: dict[str, Any] | None,
    run_record_path: Path | None,
    runtime_resolution: dict[str, Any],
) -> dict[str, Any] | None:
    if run_record is None:
        return None
    if run_record.get("run_kind") == "batch":
        if run_record_path is None:
            raise ValueError("batch run record path is required")
        with batch_run_record_lock(run_record_path.parent):
            current_run_record = require_batch_run_record(run_record_path)
            append_runtime_diagnostic(current_run_record, runtime_resolution)
            atomic_write_json(run_record_path, current_run_record)
            return current_run_record

    append_runtime_diagnostic(run_record, runtime_resolution)
    if run_record_path is not None:
        atomic_write_json(run_record_path, run_record)
    return run_record


def unexpected_runtime_resolution(exc: BaseException) -> dict[str, Any]:
    return {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": None,
        "attempts": [
            {
                "source": "batch_payload",
                "status": "rejected",
                "stage": "validation",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        ],
    }


def finish_judge_preflight_failure(
    *,
    execution: dict[str, Any],
    execution_path: Path,
    requested: dict[str, Any],
    runtime_resolution: dict[str, Any],
    exc: BaseException,
    run_record: dict[str, Any] | None,
    run_record_path: Path | None,
    run_dir: Path,
    case_run_dir: Path,
) -> dict[str, Any]:
    error = error_record("judge_preflight", exc)
    start_phase(execution, "judge")
    start_judge_attempt(
        execution,
        logs_path=None,
        requested=dict(requested),
        effective=None,
        runtime_resolution=runtime_resolution,
    )
    finish_phase(execution, "judge", status="preflight_failed")
    finish_judge_attempt(execution, status="preflight_failed", error=error)
    finish_execution(execution, status="judge_preflight_failed", error=error)
    run_record = persist_runtime_diagnostic(
        run_record,
        run_record_path,
        runtime_resolution,
    )
    atomic_write_json(execution_path, execution)
    _announce(str(execution["case_id"]), "judge_preflight", "no compatible Codex runtime")
    return finalize_case(
        execution=execution,
        conclusion=None,
        run_record=run_record,
        run_dir=run_dir,
        case_run_dir=case_run_dir,
    )


def judge_execution_fields(result: JudgeRunResult) -> dict[str, Any]:
    return {
        "provider": result.provider,
        "model": result.model,
        "reasoning_effort": result.reasoning_effort,
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "sdk_version": result.sdk_version,
        "runtime_version": result.runtime_version,
        "turn_started_at": result.turn_started_at,
        "turn_finished_at": result.turn_finished_at,
        "turn_duration_ms": result.turn_duration_ms,
        "usage": result.usage,
    }


def require_reusable_execution(
    execution_path: Path,
    prepared_environment: Path,
    subject_dir: Path,
    *,
    expected_run_id: str,
    expected_case_id: str,
    expected_repeat: int | None,
) -> dict[str, Any]:
    if expected_repeat is not None and (
        not isinstance(expected_repeat, int)
        or isinstance(expected_repeat, bool)
        or expected_repeat < 1
    ):
        raise SystemExit(f"重判目标 repeat 非法：{expected_repeat!r}")
    execution = read_json_dict(execution_path)
    if execution is None or execution.get("schema_version") != EXECUTION_SCHEMA_VERSION:
        raise SystemExit(f"没有可复用的 v2 execution：{execution_path}")
    actual_repeat = execution.get("repeat")
    if actual_repeat is not None and (
        not isinstance(actual_repeat, int)
        or isinstance(actual_repeat, bool)
        or actual_repeat < 1
    ):
        raise SystemExit(f"现有 execution.repeat 非法：{actual_repeat!r}")
    expected_identity = (expected_run_id, str(expected_case_id).zfill(3), expected_repeat)
    actual_identity = (
        str(execution.get("run_id") or ""),
        str(execution.get("case_id") or "").zfill(3),
        actual_repeat,
    )
    if actual_identity != expected_identity:
        raise SystemExit(
            "现有 execution 身份与重判目标不一致："
            f"expected={expected_identity!r}, actual={actual_identity!r}。"
        )
    if not prepared_environment.exists() or not subject_dir.exists():
        raise SystemExit("现有运行缺少 prepared_environment 或 subject，不能单独执行 Judge。")
    validate_subject_workspace(subject_dir)
    return execution


def reset_judge_phase(
    execution: dict[str, Any],
    judge_config: dict[str, Any],
    *,
    requested: dict[str, Any],
) -> None:
    attempts = list(preserve_legacy_judge_attempt(execution))
    execution["status"] = "preparing"
    execution["finished_at"] = None
    execution["duration_ms"] = None
    execution["error"] = None
    execution["conclusion"] = None
    execution["judge"] = {
        **execution_judge_config(judge_config, requested=requested),
        "attempts": attempts,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "thread_id": None,
        "turn_id": None,
        "sdk_version": judge_config.get("sdk_version"),
        "runtime_version": None,
        "usage": None,
    }


def finalize_case(
    *,
    execution: dict[str, Any],
    conclusion: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
    run_dir: Path,
    case_run_dir: Path,
) -> dict[str, Any]:
    case_record = {
        "case_id": execution["case_id"],
        "repeat": execution.get("repeat"),
        "status": execution["status"],
        "execution": (case_run_dir / "execution.json").relative_to(run_dir).as_posix(),
        "conclusion": (
            (case_run_dir / execution["conclusion"]["path"]).relative_to(run_dir).as_posix()
            if isinstance(execution.get("conclusion"), dict)
            else None
        ),
        "total_score": conclusion.get("total_score") if isinstance(conclusion, dict) else None,
    }
    if isinstance(conclusion, dict) and "representation_score" in conclusion:
        case_record.update(
            {
                "solution_score": conclusion.get("solution_score"),
                "representation_score": conclusion.get("representation_score"),
                "representation": conclusion.get("representation"),
            }
        )
    if run_record is not None:
        if run_record.get("run_kind") == "batch":
            run_record_path = run_dir / "run.json"
            with batch_run_record_lock(run_dir):
                current_run_record = require_batch_run_record(run_record_path)
                finish_batch_case_record(current_run_record, case_record)
                atomic_write_json(run_record_path, current_run_record)
                run_record = current_run_record
        else:
            cases = [case_record]
            aggregate = aggregate_case_records(cases)
            run_status = (
                "completed"
                if execution["status"] in {"completed", "subject_completed"}
                else "failed"
            )
            run_error = execution.get("error") if run_status == "failed" else None
            finish_run_record(
                run_record,
                status=run_status,
                cases=cases,
                aggregate=aggregate,
                error=run_error,
            )
            atomic_write_json(run_dir / "run.json", run_record)
    result = {
        "status": execution["status"],
        "run_id": execution["run_id"],
        "case_id": execution["case_id"],
        "repeat": execution.get("repeat"),
        "execution": case_record["execution"],
        "conclusion": case_record["conclusion"],
        "total_score": case_record["total_score"],
    }
    for key in ("solution_score", "representation_score", "representation"):
        if key in case_record:
            result[key] = case_record[key]
    return result


def finish_batch_case_record(
    run_record: dict[str, Any],
    case_record: dict[str, Any],
) -> None:
    cases = replace_batch_case_record(run_record.get("cases"), case_record)
    aggregate = aggregate_case_records(cases)
    expected_identities = expected_batch_identities(run_record)
    run_status = batch_run_status(cases, expected_identities=expected_identities)
    actual_identities = batch_case_identities(cases)
    if not batch_identities_complete(cases, expected_identities):
        missing = sorted(expected_identities - actual_identities)
        extra = sorted(actual_identities - expected_identities)
        run_error = {
            "phase": "batch",
            "type": "incomplete_batch",
            "message": (
                "batch identities mismatch: "
                f"expected_count={len(expected_identities)}, actual_count={len(cases)}, "
                f"missing={missing!r}, extra={extra!r}"
            ),
        }
    else:
        run_error = None
    finish_run_record(
        run_record,
        status=run_status,
        cases=cases,
        aggregate=aggregate,
        error=run_error,
    )


def replace_batch_case_record(
    existing: Any,
    replacement: dict[str, Any],
) -> list[dict[str, Any]]:
    records = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    identity = (str(replacement.get("case_id") or ""), replacement.get("repeat"))
    updated: list[dict[str, Any]] = []
    replaced = False
    for record in records:
        record_identity = (str(record.get("case_id") or ""), record.get("repeat"))
        if record_identity == identity:
            if not replaced:
                updated.append(replacement)
                replaced = True
            continue
        updated.append(record)
    if not replaced:
        updated.append(replacement)
    updated.sort(key=lambda item: (str(item.get("case_id") or ""), int(item.get("repeat") or 0)))
    return updated


def batch_run_status(
    cases: list[dict[str, Any]],
    *,
    expected_identities: set[tuple[str, int]] | None = None,
) -> str:
    statuses = {str(item.get("status") or "unknown") for item in cases}
    completed_statuses = {"completed", "subject_completed"}
    identities_complete = expected_identities is None or batch_identities_complete(
        cases,
        expected_identities,
    )
    if identities_complete and statuses and statuses <= completed_statuses:
        return "completed"
    if statuses & completed_statuses:
        return "partial_failed"
    return "failed"


def batch_case_identities(cases: list[dict[str, Any]]) -> set[tuple[str, int]]:
    identities: set[tuple[str, int]] = set()
    for item in cases:
        raw_case_id = str(item.get("case_id") or "").strip()
        repeat = item.get("repeat")
        if raw_case_id and isinstance(repeat, int) and not isinstance(repeat, bool) and repeat > 0:
            identities.add((raw_case_id.zfill(3), repeat))
    return identities


def batch_identities_complete(
    cases: list[dict[str, Any]],
    expected_identities: set[tuple[str, int]],
) -> bool:
    return len(cases) == len(expected_identities) and batch_case_identities(cases) == expected_identities


def expected_batch_identities(run_record: dict[str, Any]) -> set[tuple[str, int]]:
    raw_case_ids = run_record.get("case_ids")
    config = run_record.get("config")
    repeats = config.get("repeats") if isinstance(config, dict) else None
    if (
        not isinstance(raw_case_ids, list)
        or not raw_case_ids
        or not isinstance(repeats, int)
        or isinstance(repeats, bool)
        or repeats < 1
    ):
        raise ValueError("batch run.json 缺少合法的 case_ids 或 config.repeats。")
    normalized_case_ids = [
        str(case_id).strip().zfill(3)
        for case_id in raw_case_ids
        if str(case_id).strip()
    ]
    if len(normalized_case_ids) != len(raw_case_ids) or len(set(normalized_case_ids)) != len(
        normalized_case_ids
    ):
        raise ValueError("batch run.json case_ids 不能为空或重复。")
    case_ids = set(normalized_case_ids)
    return {(case_id, repeat) for case_id in case_ids for repeat in range(1, repeats + 1)}


def require_batch_run_record(run_record_path: Path) -> dict[str, Any]:
    run_record = read_json_dict(run_record_path)
    if run_record is None or run_record.get("run_kind") != "batch":
        raise RuntimeError(f"父 batch run.json 不存在或无效：{run_record_path}")
    return run_record


@contextlib.contextmanager
def batch_run_record_lock(run_dir: Path) -> Iterator[None]:
    resolved_run_dir = run_dir.resolve()
    with _BATCH_RUN_LOCKS_GUARD:
        thread_lock = _BATCH_RUN_LOCKS.setdefault(resolved_run_dir, threading.RLock())
    with thread_lock:
        lock_path = resolved_run_dir / ".run.json.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            _lock_file(lock_file)
            try:
                yield
            finally:
                _unlock_file(lock_file)


@contextlib.contextmanager
def manual_rejudge_case_lock(case_run_dir: Path) -> Iterator[None]:
    lock_path = case_run_dir.resolve() / ".rejudge.lock"
    with _ACTIVE_REJUDGE_LOCKS_GUARD:
        if lock_path in _ACTIVE_REJUDGE_LOCKS:
            raise SystemExit(f"该 case/repeat 已有重判正在运行：{case_run_dir}")
        _ACTIVE_REJUDGE_LOCKS.add(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            if not _try_lock_file(lock_file):
                raise SystemExit(f"该 case/repeat 已有重判正在运行：{case_run_dir}")
            try:
                yield
            finally:
                _unlock_file(lock_file)
    finally:
        with _ACTIVE_REJUDGE_LOCKS_GUARD:
            _ACTIVE_REJUDGE_LOCKS.discard(lock_path)


def _try_lock_file(handle: Any) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_batch_judge_override(
    run_record: dict[str, Any],
    *,
    case_id: str,
    repeat: int | None,
    judge_config: dict[str, Any],
    judge_requested: dict[str, Any],
) -> None:
    if repeat is None:
        raise ValueError("batch rejudge requires a repeat identity")
    identity = f"{str(case_id).zfill(3)}:r{repeat:02d}"
    models = run_record.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("batch run.json models must be an object")
    overrides = models.setdefault("judge_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("batch run.json models.judge_overrides must be an object")
    overrides[identity] = dict(judge_config)

    diagnostics = run_record.setdefault("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise ValueError("batch run.json diagnostics must be an object")
    requested_by_case = diagnostics.setdefault("judge_requested_by_case", {})
    if not isinstance(requested_by_case, dict):
        raise ValueError("batch run.json diagnostics.judge_requested_by_case must be an object")
    requested_by_case[identity] = dict(judge_requested)


def persist_batch_judge_override(
    run_record_path: Path,
    *,
    case_id: str,
    repeat: int | None,
    judge_config: dict[str, Any],
    judge_requested: dict[str, Any],
) -> dict[str, Any]:
    with batch_run_record_lock(run_record_path.parent):
        run_record = require_batch_run_record(run_record_path)
        record_batch_judge_override(
            run_record,
            case_id=case_id,
            repeat=repeat,
            judge_config=judge_config,
            judge_requested=judge_requested,
        )
        atomic_write_json(run_record_path, run_record)
        return run_record


def error_record(phase: str, exc: BaseException) -> dict[str, Any]:
    return {
        "phase": phase,
        "type": type(exc).__name__,
        "message": str(exc),
    }


def _announce(case_id: str, phase: str, message: str) -> None:
    print(f"[benchmark] case={case_id} phase={phase} message={message}", flush=True)


@contextlib.contextmanager
def capture_agent_logs(logs_dir: Path):
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "stdout.log").open("w", encoding="utf-8") as stdout_log, (
        logs_dir / "stderr.log"
    ).open("w", encoding="utf-8") as stderr_log:
        with contextlib.redirect_stdout(_TeeStream(sys.stdout, stdout_log)), contextlib.redirect_stderr(
            _TeeStream(sys.stderr, stderr_log)
        ):
            yield


class _TeeStream:
    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self.primary = primary
        self.secondary = secondary

    def write(self, text: str) -> int:
        self.primary.write(text)
        self.secondary.write(text)
        return len(text)

    def flush(self) -> None:
        self.primary.flush()
        self.secondary.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)


def append_traceback(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(traceback.format_exc())


if __name__ == "__main__":
    main()
