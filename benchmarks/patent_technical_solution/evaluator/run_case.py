from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]
EVALUATOR_DIR = Path(__file__).resolve().parent
RUNNER_FILE = "runner.md"

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
        capture_judge_requested_config,
        capture_model_config,
        compact_dict,
        git_metadata,
        normalize_judge_reasoning_effort,
    )
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
        capture_judge_requested_config,
        capture_model_config,
        compact_dict,
        git_metadata,
        normalize_judge_reasoning_effort,
    )


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
    parser.add_argument("--run-id", default=None, help="Run id. Defaults to timestamp + case id.")
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"), help="Directory for run artifacts.")
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
    if args.skip_judge and args.skip_subject:
        raise SystemExit("--skip-judge 与 --skip-subject 不能同时使用。")

    case_id = str(args.case).zfill(3)
    source_case_dir = BENCHMARK_DIR / "cases" / case_id
    if not source_case_dir.exists():
        raise SystemExit(f"case 不存在：{case_id}")

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{case_id}"
    runs_dir = Path(args.runs_dir).resolve()
    run_dir = runs_dir / run_id
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
    judge_requested = capture_judge_requested_config()
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
            "repeat": args.repeat,
            "skip_judge": bool(args.skip_judge),
            "skip_subject": bool(args.skip_subject),
            "round_timeout_seconds": args.round_timeout,
            "judge_timeout_seconds": args.judge_timeout,
        }
    )

    if args.skip_subject:
        execution = require_reusable_execution(execution_path, prepared_environment, subject_dir)
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

    atomic_write_json(execution_path, execution)
    run_record = None
    run_record_path: Path | None = None
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
                benchmark_git=git_metadata(BENCHMARK_DIR),
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
        append_runtime_diagnostic(run_record, runtime_resolution)
        atomic_write_json(execution_path, execution)
        if run_record is not None and run_record_path is not None:
            atomic_write_json(run_record_path, run_record)
        selected_source = runtime_resolution["selected"]["source"]
        _announce(case_id, "judge_preflight", f"selected runtime: {selected_source}")

    if not args.skip_subject:
        _announce(case_id, "prepare", "preparing frozen environment")
        try:
            prepare_exploration_environment(source_case_dir, prepared_environment)
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
            benchmark_dir=BENCHMARK_DIR,
            logs_dir=judge_logs_dir,
            model=judge_config.get("model"),
            provider=str(judge_config.get("provider") or "openai"),
            reasoning_effort=str(judge_config.get("reasoning_effort") or "high"),
            service_tier=judge_config.get("service_tier"),
            codex_bin=selected_runtime.get("launch_codex_bin"),
            timeout_seconds=args.judge_timeout,
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
    settings.data_dir = data_dir
    settings.llm_timeout = max(settings.llm_timeout, float(round_timeout))
    services = AppServices(settings)
    project = services.store.create_project_with_id(f"bench_{case_id}_{generate_id('proj')}", f"Benchmark {case_id}")
    disclosure = services.store.get_disclosure(project.project_id)
    section_id = find_section_id(disclosure.get("sections", []), "技术方案")
    if section_id is None:
        raise RuntimeError("技术方案章节不存在。")

    runner_md = (BENCHMARK_DIR / RUNNER_FILE).read_text(encoding="utf-8")
    message = "\n\n".join(
        [
            runner_md,
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
    except TimeoutError as exc:
        try:
            await services.chat.cancel_round(project.project_id, response.session_id, response.round_id)
        except Exception:
            pass
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
    append_runtime_diagnostic(run_record, runtime_resolution)
    atomic_write_json(execution_path, execution)
    if run_record is not None and run_record_path is not None:
        atomic_write_json(run_record_path, run_record)
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


def require_reusable_execution(execution_path: Path, prepared_environment: Path, subject_dir: Path) -> dict[str, Any]:
    execution = read_json_dict(execution_path)
    if execution is None or execution.get("schema_version") != EXECUTION_SCHEMA_VERSION:
        raise SystemExit(f"没有可复用的 v2 execution：{execution_path}")
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
    if run_record is not None:
        aggregate = aggregate_case_records([case_record])
        run_status = "completed" if execution["status"] in {"completed", "subject_completed"} else "failed"
        finish_run_record(
            run_record,
            status=run_status,
            cases=[case_record],
            aggregate=aggregate,
            error=execution.get("error") if run_status == "failed" else None,
        )
        atomic_write_json(run_dir / "run.json", run_record)
    return {
        "status": execution["status"],
        "run_id": execution["run_id"],
        "case_id": execution["case_id"],
        "repeat": execution.get("repeat"),
        "execution": case_record["execution"],
        "conclusion": case_record["conclusion"],
        "total_score": case_record["total_score"],
    }


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
