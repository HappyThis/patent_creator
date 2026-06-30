from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]
EVALUATOR_DIR = Path(__file__).resolve().parent
DEFAULT_MAX_REFINEMENT_ROUNDS = 2
DEFAULT_REFINEMENT_INSTRUCTION = (
    "请继续充实交底书中的“技术方案”章节。只允许编辑“技术方案”章节；"
    "正文应聚焦解决方法、必要技术特征、协同机理和技术效果，不要写成验证计划或实施任务清单。"
)
MODES = ("solution", "figure", "combined")
RUNNER_FILES = {
    "solution": "runner.md",
    "figure": "figure_runner.md",
    "combined": "combined_runner.md",
}
JUDGE_FILES = {
    "solution": "judge.md",
    "figure": "figure_judge.md",
    "combined": "combined_judge.md",
}
REFINEMENT_INSTRUCTIONS = {
    "solution": DEFAULT_REFINEMENT_INSTRUCTION,
    "figure": (
        "请继续完善本 case 的附图表达。仍需基于探索环境中的技术事实；"
        "如果尚未生成附图，请使用 figure_kit 生成能帮助理解技术机制的图；"
        "如果已生成附图，请优先读取并更新已有图，而不是新增无实质信息的图。"
    ),
    "combined": (
        "请继续完善“技术方案”章节及其附图表达。正文应保持技术方案深度，"
        "附图应帮助理解关键结构、流程、状态、边界或数据流，并确保正文与附图一致。"
    ),
}
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from artifact import extract_technical_solution, find_technical_solution_section, has_effective_solution, write_artifact  # noqa: E402
from codex_judge import run_codex_judge  # noqa: E402
from prepare_env import prepare_exploration_environment  # noqa: E402
from run_metadata import build_run_manifest  # noqa: E402


def main() -> None:
    args = parse_args()
    result = asyncio.run(run_case(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one patent technical solution benchmark case.")
    parser.add_argument("--case", required=True, help="Case id, e.g. 001.")
    parser.add_argument("--mode", choices=MODES, default="solution", help="Benchmark mode.")
    parser.add_argument("--run-id", default=None, help="Run id. Defaults to timestamp + case id.")
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"), help="Directory for run artifacts.")
    parser.add_argument("--skip-judge", action="store_true", help="Only run the subject agent and extract artifact.")
    parser.add_argument("--skip-subject", action="store_true", help="Reuse existing evaluated_artifact.md and run judge.")
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_BIN", "codex"),
        help="Codex executable used for judging. Defaults to CODEX_BIN or codex.",
    )
    parser.add_argument("--round-timeout", type=int, default=1800, help="Timeout per main-agent round in seconds.")
    parser.add_argument("--judge-timeout", type=int, default=1800, help="Timeout for Codex judge in seconds.")
    parser.add_argument("--max-refinement-rounds", type=int, default=None, help="Override benchmark refinement count.")
    return parser.parse_args()


async def run_case(args: argparse.Namespace) -> dict[str, Any]:
    case_id = str(args.case).zfill(3)
    mode = str(args.mode)
    case_dir = BENCHMARK_DIR / "cases" / case_id
    if not case_dir.exists():
        raise SystemExit(f"case 不存在：{case_id}")

    max_refinements = (
        args.max_refinement_rounds
        if args.max_refinement_rounds is not None
        else DEFAULT_MAX_REFINEMENT_ROUNDS
    )
    refinement_instruction = REFINEMENT_INSTRUCTIONS[mode]

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{case_id}"
    run_dir = Path(args.runs_dir).resolve() / run_id
    case_run_dir = run_dir / "cases" / case_id
    prepared_environment = case_run_dir / "prepared_environment"
    subject_dir = case_run_dir / "subject"
    judge_dir = case_run_dir / "judge"
    artifact_path = case_run_dir / "evaluated_artifact.md"
    manifest_path = case_run_dir / "input_manifest.json"

    case_run_dir.mkdir(parents=True, exist_ok=True)
    run_config = {
        "case_id": case_id,
        "mode": mode,
        "skip_judge": bool(args.skip_judge),
        "skip_subject": bool(args.skip_subject),
        "round_timeout_seconds": args.round_timeout,
        "judge_timeout_seconds": args.judge_timeout,
        "max_refinement_rounds": max_refinements,
        "codex_bin": args.codex_bin,
    }
    run_manifest = build_run_manifest(
        run_id=run_id,
        run_kind="single_case",
        run_config=run_config,
        case_ids=[case_id],
    )
    write_json(run_dir / "run_manifest.json", run_manifest)
    progress = case_progress_writer(case_run_dir=case_run_dir, case_id=case_id, run_id=run_id)
    progress("prepare", "case run directory initialized")
    request_md = (case_dir / "request.md").read_text(encoding="utf-8")
    subject_reused = bool(args.skip_subject)

    if not args.skip_subject:
        progress("prepare", "preparing exploration environment", prepared_environment=str(prepared_environment))
        prepare_exploration_environment(case_dir, prepared_environment)
        progress("prepare", "exploration environment prepared", prepared_environment=str(prepared_environment))
        progress("subject", "subject agent started", max_refinement_rounds=max_refinements)
        technical_solution_md, subject_status, diagnostics = await run_subject_agent(
            mode=mode,
            case_id=case_id,
            prepared_environment=prepared_environment,
            request_md=request_md,
            subject_dir=subject_dir,
            max_refinements=max_refinements,
            refinement_instruction=refinement_instruction,
            round_timeout=args.round_timeout,
            progress=progress,
        )
        write_artifact(artifact_path, technical_solution_md)
        figure_manifest = write_figure_manifest(case_run_dir)
        diagnostics["figure_manifest"] = figure_manifest
        progress(
            "artifact",
            "evaluated artifact written",
            artifact_path=str(artifact_path),
            subject_status=subject_status,
            artifact_extracted=has_effective_solution(technical_solution_md),
            mode_output_extracted=diagnostics.get("mode_output_extracted"),
            figure_artifact_count=diagnostics.get("figure_artifact_count"),
        )
    else:
        progress(
            "prepare",
            "preparing exploration environment for reused subject",
            prepared_environment=str(prepared_environment),
        )
        prepare_exploration_environment(case_dir, prepared_environment)
        technical_solution_md = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else ""
        subject_status, diagnostics = resolve_reused_subject_state(
            mode,
            technical_solution_md,
            subject_dir,
            read_existing_diagnostics(case_run_dir),
        )
        figure_manifest = write_figure_manifest(case_run_dir)
        diagnostics["figure_manifest"] = figure_manifest
        progress(
            "subject",
            "reused existing evaluated artifact",
            subject_status=subject_status,
            artifact_path=str(artifact_path),
        )

    manifest = {
        "case_id": case_id,
        "run_id": run_id,
        "prepared_environment": str(prepared_environment),
        "agent_visible_text_inputs": [
            RUNNER_FILES[mode],
            "exploration environment absolute path line",
            "request.md",
        ],
        "evaluated_artifact": str(artifact_path),
        "artifact_source": "disclosure.sections[title=技术方案]",
        "subject_status": subject_status,
        "subject_reused": subject_reused,
        "max_refinement_rounds": max_refinements,
        "model_config": run_manifest["model_config"],
        "run_config": run_config,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_json(case_run_dir / "diagnostics.json", diagnostics)

    if subject_status == "round_failed" or not diagnostics.get("mode_output_extracted"):
        result = {"status": subject_status, **manifest, "diagnostics": diagnostics}
        write_result(case_run_dir, result)
        progress("result", "case ended before judge", status=subject_status, result_path=str(case_run_dir / "result.json"))
        return result

    if args.skip_judge:
        result_status = "artifact_extracted" if diagnostics.get("mode_output_extracted") else subject_status
        result = {"status": result_status, **manifest, "diagnostics": diagnostics}
        write_result(case_run_dir, result)
        progress("result", "case ended with judge skipped", status=result_status, result_path=str(case_run_dir / "result.json"))
        return result

    try:
        progress("judge", "judge started", timeout_seconds=args.judge_timeout)
        judge_working_dir = prepared_environment if mode == "solution" else case_run_dir
        judge_result = run_codex_judge(
            mode=mode,
            working_dir=judge_working_dir,
            case_run_dir=case_run_dir if mode != "solution" else None,
            request_md=request_md,
            evaluated_artifact_md=technical_solution_md,
            judge_md=(BENCHMARK_DIR / JUDGE_FILES[mode]).read_text(encoding="utf-8"),
            rubric_md=(case_dir / "rubric.md").read_text(encoding="utf-8"),
            reference_solution_md=(case_dir / "reference_solution.md").read_text(encoding="utf-8"),
            output_dir=judge_dir,
            codex_bin=args.codex_bin,
            timeout_seconds=args.judge_timeout,
            progress=progress,
        )
    except Exception as exc:
        diagnostics["judge_failed"] = True
        write_json(case_run_dir / "diagnostics.json", diagnostics)
        result = {"status": "judge_failed", **manifest, "diagnostics": diagnostics, "judge_error": str(exc)}
        write_result(case_run_dir, result)
        progress("judge", "judge failed", status="judge_failed", error=str(exc), result_path=str(case_run_dir / "result.json"))
        return result

    diagnostics["judge_failed"] = False
    write_json(case_run_dir / "diagnostics.json", diagnostics)
    result = {"status": "scored", **manifest, "diagnostics": diagnostics, "judge": judge_result}
    write_result(case_run_dir, result)
    progress(
        "result",
        "case scored",
        status="scored",
        total_score=judge_result.get("total_score"),
        result_path=str(case_run_dir / "result.json"),
    )
    return result


async def run_subject_agent(
    *,
    mode: str,
    case_id: str,
    prepared_environment: Path,
    request_md: str,
    subject_dir: Path,
    max_refinements: int,
    refinement_instruction: str,
    round_timeout: int,
    progress: Callable[..., None],
) -> tuple[str, str, dict[str, Any]]:
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
    technical_solution = find_technical_solution_section(services.store.get_disclosure(project.project_id)["sections"])
    if not technical_solution:
        raise RuntimeError("技术方案章节不存在。")
    technical_solution_section_id = technical_solution["id"]
    progress("subject", "benchmark project created", project_id=project.project_id)

    session_id: str | None = None
    runner_md = (BENCHMARK_DIR / RUNNER_FILES[mode]).read_text(encoding="utf-8")
    initial_message = "\n\n".join(
        [
            runner_md,
            f"探索环境路径：{prepared_environment.resolve()}",
            request_md,
        ]
    )

    messages = [initial_message, *([refinement_instruction] * max_refinements)]
    technical_solution_md = ""
    status = skipped_status(mode)
    rounds_run = 0

    for index, message in enumerate(messages):
        rounds_run = index + 1
        progress("subject_round", "round request submitted", round=rounds_run, timeout_seconds=round_timeout)
        response = await services.chat.start_round(
            project.project_id,
            ChatMessageRequest(session_id=session_id, message=message, active_section_id=technical_solution_section_id),
        )
        session_id = response.session_id
        progress("subject_round", "waiting for round completion", round=rounds_run, session_id=session_id, round_id=response.round_id)
        try:
            await wait_for_round(
                services,
                project.project_id,
                timeout_seconds=round_timeout,
                progress=progress,
                round_index=rounds_run,
            )
        except TimeoutError as exc:
            mark_project_idle(services, project.project_id)
            disclosure = services.store.get_disclosure(project.project_id)
            technical_solution_md = extract_technical_solution(disclosure)
            artifact_after_round = subject_dir / f"technical_solution_after_round_{index + 1}.md"
            write_artifact(artifact_after_round, technical_solution_md)
            dump_session_events(services, project.project_id, session_id, subject_dir / "session_events.jsonl")
            technical_solution_extracted = has_effective_solution(technical_solution_md)
            figure_artifact_count = count_figure_artifacts(subject_dir)
            mode_output_extracted = has_mode_output(mode, technical_solution_md, subject_dir)
            progress(
                "subject_round",
                "round timed out",
                round=rounds_run,
                timeout_seconds=round_timeout,
                artifact_extracted=technical_solution_extracted,
                mode_output_extracted=mode_output_extracted,
                figure_artifact_count=figure_artifact_count,
                artifact_path=str(artifact_after_round),
                error=str(exc),
            )
            status = "round_failed"
            break
        except BaseException:
            mark_project_idle(services, project.project_id)
            raise
        disclosure = services.store.get_disclosure(project.project_id)
        technical_solution_md = extract_technical_solution(disclosure)
        write_artifact(subject_dir / f"technical_solution_after_round_{index + 1}.md", technical_solution_md)
        dump_session_events(services, project.project_id, session_id, subject_dir / "session_events.jsonl")
        events = services.store.read_session_events(project.project_id, session_id)
        technical_solution_extracted = has_effective_solution(technical_solution_md)
        figure_artifact_count = count_figure_artifacts(subject_dir)
        mode_output_extracted = has_mode_output(mode, technical_solution_md, subject_dir)
        progress(
            "subject_round",
            "round completed",
            round=rounds_run,
            artifact_extracted=technical_solution_extracted,
            mode_output_extracted=mode_output_extracted,
            figure_artifact_count=figure_artifact_count,
            artifact_path=str(subject_dir / f"technical_solution_after_round_{index + 1}.md"),
        )
        if mode_output_extracted:
            status = "completed" if index == 0 else "completed_after_refinement"
            break
        if round_failed(events, response.round_id):
            status = "round_failed"
            break

    (subject_dir / "disclosure.json").write_text(
        json.dumps(services.store.get_disclosure(project.project_id), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    events = services.store.read_session_events(project.project_id, session_id) if session_id else []
    diagnostics = build_diagnostics(
        events,
        mode=mode,
        subject_status=status,
        rounds_run=rounds_run,
        artifact_extracted=has_effective_solution(technical_solution_md),
        mode_output_extracted=has_mode_output(mode, technical_solution_md, subject_dir),
        figure_artifact_count=count_figure_artifacts(subject_dir),
    )
    return technical_solution_md, status, diagnostics


async def wait_for_round(
    services: Any,
    project_id: str,
    *,
    timeout_seconds: int,
    progress: Callable[..., None],
    round_index: int,
) -> None:
    started = time.monotonic()
    last_progress = started
    while True:
        project = services.store.get_project(project_id)
        if not project.is_busy:
            return
        now = time.monotonic()
        elapsed = now - started
        if elapsed > timeout_seconds:
            raise TimeoutError(f"主 agent round 超时：{timeout_seconds} 秒。")
        if now - last_progress >= 10:
            progress(
                "subject_round",
                "round still running",
                round=round_index,
                elapsed_seconds=int(elapsed),
                timeout_seconds=timeout_seconds,
            )
            last_progress = now
        await asyncio.sleep(0.5)


def dump_session_events(services: Any, project_id: str, session_id: str, path: Path) -> None:
    events = services.store.read_session_events(project_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(ensure_ascii=False) + "\n")


def round_failed(events: list[Any], round_id: str) -> bool:
    return any(
        event_value(event, "type") == "agent_output"
        and event_value(event, "round_id") == round_id
        and event_payload(event).get("status") == "failed"
        for event in events
    )


def mark_project_idle(services: Any, project_id: str) -> None:
    project = services.store.get_project(project_id)
    project.is_busy = False
    project.running_session_id = None
    project.running_round_id = None
    services.store.save_project(project)


def skipped_status(mode: str) -> str:
    if mode == "solution":
        return "skipped_no_solution_artifact"
    return "skipped_no_mode_artifact"


def has_mode_output(mode: str, technical_solution_md: str, subject_dir: Path) -> bool:
    has_solution = has_effective_solution(technical_solution_md)
    has_figures = count_figure_artifacts(subject_dir) > 0
    if mode == "solution":
        return has_solution
    if mode == "figure":
        return has_figures
    if mode == "combined":
        return has_solution and has_figures
    raise ValueError(f"unknown benchmark mode: {mode}")


def count_figure_artifacts(subject_dir: Path) -> int:
    return len(collect_figure_artifacts(subject_dir))


def collect_figure_artifacts(subject_dir: Path) -> list[dict[str, Any]]:
    projects_dir = subject_dir / "data" / "projects"
    if not projects_dir.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    for figure_json in sorted(projects_dir.glob("*/assets/figures/fig_*/figure.json")):
        figure_dir = figure_json.parent
        diagram_html = figure_dir / "diagram.html"
        render_png = figure_dir / "render.png"
        if not diagram_html.exists() or not render_png.exists():
            continue
        project_dir = figure_json.parents[3]
        item: dict[str, Any] = {
            "project_id": project_dir.name,
            "figure_id": figure_dir.name,
            "figure_json": str(figure_json.relative_to(subject_dir)),
            "diagram_html": str(diagram_html.relative_to(subject_dir)),
            "render_png": str(render_png.relative_to(subject_dir)),
        }
        try:
            metadata = json.loads(figure_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            item["title"] = metadata.get("title") or ""
            item["caption"] = f"{metadata.get('label') or ''} {metadata.get('title') or ''}".strip()
        artifacts.append(item)
    return artifacts


def write_figure_manifest(case_run_dir: Path) -> dict[str, Any]:
    subject_dir = case_run_dir / "subject"
    figures = collect_figure_artifacts(subject_dir)
    manifest = {
        "schema_version": 1,
        "figure_count": len(figures),
        "figures": figures,
        "search_root": "subject/data/projects/*/assets/figures/",
    }
    write_json(case_run_dir / "figure_manifest.json", manifest)
    return manifest


def build_diagnostics(
    events: list[Any],
    *,
    mode: str,
    subject_status: str,
    rounds_run: int,
    artifact_extracted: bool,
    mode_output_extracted: bool,
    figure_artifact_count: int,
    judge_failed: bool = False,
) -> dict[str, Any]:
    round_failed_flag = any(
        event_value(event, "type") == "agent_output"
        and event_payload(event).get("status") == "failed"
        for event in events
    )
    return {
        "subject_status": subject_status,
        "mode": mode,
        "rounds_run": rounds_run,
        "refinement_attempts": max(0, rounds_run - 1),
        "artifact_extracted": artifact_extracted,
        "mode_output_extracted": mode_output_extracted,
        "figure_artifact_count": figure_artifact_count,
        "skipped_no_solution_artifact": subject_status == "skipped_no_solution_artifact",
        "skipped_no_mode_artifact": subject_status == "skipped_no_mode_artifact",
        "round_failed": subject_status == "round_failed" or round_failed_flag,
        "judge_failed": judge_failed,
    }


def event_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        return payload
    if isinstance(event, dict) and isinstance(event.get("payload"), dict):
        return event["payload"]
    return {}


def event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def read_existing_diagnostics(case_run_dir: Path) -> dict[str, Any] | None:
    path = case_run_dir / "diagnostics.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_reused_subject_state(
    mode: str,
    technical_solution_md: str,
    subject_dir: Path,
    existing_diagnostics: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    artifact_extracted = has_effective_solution(technical_solution_md)
    mode_output_extracted = has_mode_output(mode, technical_solution_md, subject_dir)
    fallback_status = "completed" if mode_output_extracted else skipped_status(mode)
    if existing_diagnostics:
        diagnostics = normalize_content_diagnostics(
            existing_diagnostics,
            mode=mode,
            subject_dir=subject_dir,
            technical_solution_md=technical_solution_md,
            fallback_status=fallback_status,
        )
        return str(diagnostics["subject_status"]), diagnostics
    return fallback_status, build_diagnostics(
        [],
        mode=mode,
        subject_status=fallback_status,
        rounds_run=0,
        artifact_extracted=artifact_extracted,
        mode_output_extracted=mode_output_extracted,
        figure_artifact_count=count_figure_artifacts(subject_dir),
    )


def normalize_content_diagnostics(
    diagnostics: dict[str, Any],
    *,
    mode: str,
    subject_dir: Path,
    technical_solution_md: str,
    fallback_status: str,
) -> dict[str, Any]:
    subject_status = str(diagnostics.get("subject_status") or fallback_status)
    rounds_run = int(diagnostics.get("rounds_run") or 0)
    artifact_extracted = has_effective_solution(technical_solution_md)
    mode_output_extracted = has_mode_output(mode, technical_solution_md, subject_dir)
    if mode_output_extracted and subject_status == "round_failed":
        subject_status = fallback_status
    return {
        "subject_status": subject_status,
        "mode": mode,
        "rounds_run": rounds_run,
        "refinement_attempts": max(0, rounds_run - 1),
        "artifact_extracted": artifact_extracted,
        "mode_output_extracted": mode_output_extracted,
        "figure_artifact_count": count_figure_artifacts(subject_dir),
        "skipped_no_solution_artifact": subject_status == "skipped_no_solution_artifact",
        "skipped_no_mode_artifact": subject_status == "skipped_no_mode_artifact",
        "round_failed": subject_status == "round_failed" or bool(diagnostics.get("round_failed")),
        "judge_failed": bool(diagnostics.get("judge_failed")),
    }


def write_result(case_run_dir: Path, result: dict[str, Any]) -> None:
    write_json(case_run_dir / "result.json", result)


def case_progress_writer(*, case_run_dir: Path, case_id: str, run_id: str) -> Callable[..., None]:
    progress_path = case_run_dir / "progress.json"
    events_path = case_run_dir / "progress.jsonl"
    started_at = time.monotonic()

    def write_progress(phase: str, message: str, **fields: Any) -> None:
        elapsed_seconds = round(time.monotonic() - started_at, 1)
        payload = {
            "case_id": case_id,
            "run_id": run_id,
            "phase": phase,
            "message": message,
            "elapsed_seconds": elapsed_seconds,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **fields,
        }
        progress_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        suffix = f" {details}" if details else ""
        print(f"[benchmark] case={case_id} phase={phase} elapsed={elapsed_seconds:.1f}s message={message}{suffix}", flush=True)

    return write_progress


def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
