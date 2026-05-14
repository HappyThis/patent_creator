from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]
EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from artifact import extract_technical_solution, has_effective_solution, write_artifact  # noqa: E402
from codex_judge import run_codex_judge  # noqa: E402
from prepare_env import prepare_project_checkout  # noqa: E402


def main() -> None:
    args = parse_args()
    result = asyncio.run(run_case(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one software patent solution benchmark case.")
    parser.add_argument("--case", required=True, help="Case id, e.g. 001.")
    parser.add_argument("--run-id", default=None, help="Run id. Defaults to timestamp + case id.")
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"), help="Directory for run artifacts.")
    parser.add_argument("--skip-judge", action="store_true", help="Only run the subject agent and extract artifact.")
    parser.add_argument("--skip-subject", action="store_true", help="Reuse existing evaluated_artifact.md and run judge.")
    parser.add_argument("--codex-bin", default="codex", help="Codex executable used for judging.")
    parser.add_argument("--round-timeout", type=int, default=1800, help="Timeout per main-agent round in seconds.")
    parser.add_argument("--judge-timeout", type=int, default=1800, help="Timeout for Codex judge in seconds.")
    parser.add_argument("--max-refinement-rounds", type=int, default=None, help="Override benchmark refinement count.")
    return parser.parse_args()


async def run_case(args: argparse.Namespace) -> dict[str, Any]:
    case_id = str(args.case).zfill(3)
    case_dir = BENCHMARK_DIR / "cases" / case_id
    if not case_dir.exists():
        raise SystemExit(f"case 不存在：{case_id}")

    benchmark_config = json.loads((BENCHMARK_DIR / "benchmark.json").read_text(encoding="utf-8"))
    max_refinements = (
        args.max_refinement_rounds
        if args.max_refinement_rounds is not None
        else int(benchmark_config.get("runner_policy", {}).get("max_refinement_rounds_when_artifact_missing", 2))
    )
    refinement_instruction = str(
        benchmark_config.get("runner_policy", {}).get(
            "refinement_instruction",
            "请继续充实交底书中的“技术方案”章节，只落实技术方案内容，不要生成完整交底书。",
        )
    )

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{case_id}"
    run_dir = Path(args.runs_dir).resolve() / run_id
    case_run_dir = run_dir / "cases" / case_id
    prepared_repo = case_run_dir / "prepared_repo"
    subject_dir = case_run_dir / "subject"
    judge_dir = case_run_dir / "judge"
    artifact_path = case_run_dir / "evaluated_artifact.md"
    manifest_path = case_run_dir / "input_manifest.json"

    case_run_dir.mkdir(parents=True, exist_ok=True)
    request_md = (case_dir / "request.md").read_text(encoding="utf-8")

    if not args.skip_subject:
        prepare_project_checkout(case_dir, prepared_repo)
        technical_solution_md, subject_status, diagnostics = await run_subject_agent(
            case_id=case_id,
            prepared_repo=prepared_repo,
            request_md=request_md,
            subject_dir=subject_dir,
            max_refinements=max_refinements,
            refinement_instruction=refinement_instruction,
            round_timeout=args.round_timeout,
        )
        write_artifact(artifact_path, technical_solution_md)
    else:
        prepare_project_checkout(case_dir, prepared_repo)
        technical_solution_md = artifact_path.read_text(encoding="utf-8")
        subject_status = "reused"
        diagnostics = read_existing_diagnostics(case_run_dir) or build_diagnostics(
            [],
            subject_status=subject_status,
            rounds_run=0,
            artifact_extracted=has_effective_solution(technical_solution_md),
        )

    manifest = {
        "case_id": case_id,
        "run_id": run_id,
        "prepared_repo": str(prepared_repo),
        "agent_visible_text_inputs": [
            "runner.md",
            "request.md",
            "project environment absolute path line",
        ],
        "evaluated_artifact": str(artifact_path),
        "artifact_source": "disclosure.technical_solution",
        "subject_status": subject_status,
        "max_refinement_rounds": max_refinements,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_json(case_run_dir / "diagnostics.json", diagnostics)

    if subject_status in {"skipped_no_solution_artifact", "round_failed"}:
        result = {"status": subject_status, **manifest, "diagnostics": diagnostics}
        write_result(case_run_dir, result)
        return result

    if args.skip_judge:
        result_status = "artifact_extracted" if has_effective_solution(technical_solution_md) else subject_status
        result = {"status": result_status, **manifest, "diagnostics": diagnostics}
        write_result(case_run_dir, result)
        return result

    try:
        judge_result = run_codex_judge(
            prepared_repo=prepared_repo,
            request_md=request_md,
            evaluated_artifact_md=technical_solution_md,
            judge_md=(BENCHMARK_DIR / "judge.md").read_text(encoding="utf-8"),
            rubric_md=(case_dir / "rubric.md").read_text(encoding="utf-8"),
            reference_solution_md=(case_dir / "reference_solution.md").read_text(encoding="utf-8"),
            output_dir=judge_dir,
            codex_bin=args.codex_bin,
            timeout_seconds=args.judge_timeout,
        )
    except Exception as exc:
        diagnostics["judge_failed"] = True
        write_json(case_run_dir / "diagnostics.json", diagnostics)
        result = {"status": "judge_failed", **manifest, "diagnostics": diagnostics, "judge_error": str(exc)}
        write_result(case_run_dir, result)
        return result

    diagnostics["judge_failed"] = False
    write_json(case_run_dir / "diagnostics.json", diagnostics)
    result = {"status": "scored", **manifest, "diagnostics": diagnostics, "judge": judge_result}
    write_result(case_run_dir, result)
    return result


async def run_subject_agent(
    *,
    case_id: str,
    prepared_repo: Path,
    request_md: str,
    subject_dir: Path,
    max_refinements: int,
    refinement_instruction: str,
    round_timeout: int,
) -> tuple[str, str, dict[str, Any]]:
    backend_dir = REPO_DIR / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from app.core import Settings, generate_id
    from app.domain import find_section_by_type
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
    restrict_document_edit_to_technical_solution(services)
    project = services.store.create_project_with_id(f"bench_{case_id}_{generate_id('proj')}", f"Benchmark {case_id}")
    technical_solution = find_section_by_type(services.store.get_disclosure(project.project_id)["sections"], "technical_solution")
    if not technical_solution:
        raise RuntimeError("技术方案章节不存在。")
    technical_solution_section_id = technical_solution["id"]

    session_id: str | None = None
    runner_md = (BENCHMARK_DIR / "runner.md").read_text(encoding="utf-8")
    initial_message = "\n\n".join(
        [
            runner_md,
            f"项目环境路径：{prepared_repo.resolve()}",
            request_md,
        ]
    )

    messages = [initial_message, *([refinement_instruction] * max_refinements)]
    technical_solution_md = ""
    status = "skipped_no_solution_artifact"
    rounds_run = 0

    for index, message in enumerate(messages):
        rounds_run = index + 1
        response = await services.chat.start_round(
            project.project_id,
            ChatMessageRequest(session_id=session_id, message=message, active_section_id=technical_solution_section_id),
        )
        session_id = response.session_id
        try:
            await wait_for_round(services, project.project_id, timeout_seconds=round_timeout)
        except BaseException:
            mark_project_idle(services, project.project_id)
            raise
        disclosure = services.store.get_disclosure(project.project_id)
        technical_solution_md = extract_technical_solution(disclosure)
        write_artifact(subject_dir / f"technical_solution_after_round_{index + 1}.md", technical_solution_md)
        dump_session_events(services, project.project_id, session_id, subject_dir / "session_events.jsonl")
        events = services.store.read_session_events(project.project_id, session_id)
        if round_failed(events, response.round_id):
            status = "round_failed"
            break
        if has_effective_solution(technical_solution_md):
            status = "completed" if index == 0 else "completed_after_refinement"
            break

    (subject_dir / "disclosure.json").write_text(
        json.dumps(services.store.get_disclosure(project.project_id), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    events = services.store.read_session_events(project.project_id, session_id) if session_id else []
    diagnostics = build_diagnostics(
        events,
        subject_status=status,
        rounds_run=rounds_run,
        artifact_extracted=has_effective_solution(technical_solution_md),
    )
    return technical_solution_md, status, diagnostics


async def wait_for_round(services: Any, project_id: str, *, timeout_seconds: int) -> None:
    started = time.monotonic()
    while True:
        project = services.store.get_project(project_id)
        if not project.is_busy:
            return
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(f"主 agent round 超时：{timeout_seconds} 秒。")
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


def restrict_document_edit_to_technical_solution(services: Any) -> None:
    from app.domain import find_section_by_type

    original_document_edit = services.executor.document_edit

    def guarded_document_edit(project_id: str, arguments: dict[str, Any], scope: str = "main_agent") -> dict[str, Any]:
        disclosure = services.store.get_disclosure(project_id)
        technical_solution = find_section_by_type(disclosure["sections"], "technical_solution")
        technical_solution_section_id = technical_solution["id"] if technical_solution else ""
        forbidden = forbidden_document_edit_sections(arguments, technical_solution_section_id)
        if forbidden:
            return {
                "status": "failed",
                "output": {
                    "code": "benchmark_forbidden_section_edit",
                    "message": (
                        "本 benchmark 只允许编辑 technical_solution 章节；"
                        f"禁止编辑：{', '.join(sorted(forbidden))}。"
                    ),
                },
            }
        return original_document_edit(project_id, arguments, scope)

    services.executor.document_edit = guarded_document_edit


def forbidden_document_edit_sections(arguments: dict[str, Any], technical_solution_section_id: str) -> set[str]:
    operations = arguments.get("operations")
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except json.JSONDecodeError:
            return {"<invalid_operations>"}
    if not isinstance(operations, list):
        return {"<invalid_operations>"}
    forbidden: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            forbidden.add("<invalid_operation>")
            continue
        op = str(operation.get("op") or "")
        section_id = operation.get("section_id")
        parent_section_id = operation.get("parent_section_id")
        if op == "update_meta":
            forbidden.add("meta")
            continue
        if op == "append_child_section":
            if parent_section_id != technical_solution_section_id:
                forbidden.add(str(parent_section_id or "<missing_parent_section_id>"))
            continue
        if isinstance(section_id, str) and section_id != technical_solution_section_id:
            forbidden.add(section_id)
            continue
        if op in {"replace_section", "replace_section_blocks", "append_block"}:
            if section_id != technical_solution_section_id:
                forbidden.add(str(section_id or "<missing_section_id>"))
        elif op == "replace_block":
            forbidden.add("replace_block_without_section_guard")
    return forbidden


def build_diagnostics(
    events: list[Any],
    *,
    subject_status: str,
    rounds_run: int,
    artifact_extracted: bool,
    judge_failed: bool = False,
) -> dict[str, Any]:
    failure_codes: Counter[str] = Counter()
    document_edit_failure_codes: Counter[str] = Counter()
    round_failure_codes: Counter[str] = Counter()
    tool_failure_count = 0
    document_edit_failure_count = 0

    for event in events:
        event_type = event_value(event, "type")
        payload = event_payload(event)
        if event_type == "agent_output" and payload.get("status") == "failed":
            round_failure_codes[failure_code(payload)] += 1
            continue
        if event_type != "tool_result":
            continue
        if payload.get("status") != "failed":
            continue
        tool_failure_count += 1
        code = failure_code(payload)
        failure_codes[code] += 1
        if payload.get("tool") == "document_edit":
            document_edit_failure_count += 1
            document_edit_failure_codes[code] += 1

    return {
        "subject_status": subject_status,
        "rounds_run": rounds_run,
        "refinement_attempts": max(0, rounds_run - 1),
        "artifact_extracted": artifact_extracted,
        "skipped_no_solution_artifact": subject_status == "skipped_no_solution_artifact",
        "round_failed": subject_status == "round_failed" or bool(round_failure_codes),
        "round_failure_count": sum(round_failure_codes.values()),
        "round_failure_codes": dict(sorted(round_failure_codes.items())),
        "judge_failed": judge_failed,
        "tool_failure_count": tool_failure_count,
        "tool_failure_codes": dict(sorted(failure_codes.items())),
        "document_edit_failure_count": document_edit_failure_count,
        "document_edit_failure_codes": dict(sorted(document_edit_failure_codes.items())),
        "duplicate_section_id_count": failure_codes.get("duplicate_section_id", 0),
        "invalid_operation_count": failure_codes.get("invalid_operation", 0),
        "benchmark_forbidden_section_edit_count": failure_codes.get("benchmark_forbidden_section_edit", 0),
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


def failure_code(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, dict) and isinstance(output.get("code"), str) and output["code"]:
        return output["code"]
    code = payload.get("code")
    return code if isinstance(code, str) and code else "unknown_tool_failure"


def read_existing_diagnostics(case_run_dir: Path) -> dict[str, Any] | None:
    path = case_run_dir / "diagnostics.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_result(case_run_dir: Path, result: dict[str, Any]) -> None:
    write_json(case_run_dir / "result.json", result)


def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
