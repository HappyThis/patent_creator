from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import run_case as run_case_module
from .codex_judge import JudgeRunResult
from .judge_runtime import JudgeRuntimeResolutionError
from .records import atomic_write_json, finish_execution, new_execution
from .run_case import aggregate_agent_usage, validate_subject_workspace


def test_validate_subject_workspace_checks_structure_without_scoring_content(tmp_path: Path) -> None:
    disclosure_path = tmp_path / "data" / "projects" / "proj_1" / "disclosure.json"
    disclosure_path.parent.mkdir(parents=True)
    disclosure_path.write_text("{}\n", encoding="utf-8")

    assert validate_subject_workspace(tmp_path) == disclosure_path


def test_validate_subject_workspace_rejects_multiple_projects(tmp_path: Path) -> None:
    for project_id in ("proj_1", "proj_2"):
        path = tmp_path / "data" / "projects" / project_id / "disclosure.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="只包含一个项目"):
        validate_subject_workspace(tmp_path)


def test_aggregate_agent_usage_reads_canonical_session_events() -> None:
    events = [
        SimpleNamespace(
            type="agent_message",
            payload={"message": {"usage": {"prompt_tokens": 10, "completion_tokens": 4}}},
        ),
        SimpleNamespace(
            type="agent_message",
            payload={"message": {"usage": {"input_tokens": 3, "output_tokens": 2}}},
        ),
        SimpleNamespace(type="tool_result", payload={}),
    ]

    assert aggregate_agent_usage(events) == {"input_tokens": 13, "output_tokens": 6}


def test_main_exits_nonzero_for_judge_preflight_failure(monkeypatch) -> None:
    async def fake_run_case(_args):
        return {"status": "judge_preflight_failed"}

    monkeypatch.setattr(run_case_module, "parse_args", lambda: SimpleNamespace())
    monkeypatch.setattr(run_case_module, "run_case", fake_run_case)

    with pytest.raises(SystemExit) as exc_info:
        run_case_module.main()

    assert exc_info.value.code == 1


def test_runtime_preflight_failure_is_recorded_before_agent_starts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_dir = tmp_path / "benchmark"
    (benchmark_dir / "cases" / "001").mkdir(parents=True)
    runs_dir = benchmark_dir / "runs"
    resolution = {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": None,
        "attempts": [
            {
                "source": source,
                "status": "rejected",
                "stage": "model_auth_probe",
                "error": {"type": "RuntimeError", "message": f"{source} rejected"},
            }
            for source in ("codex_app", "sdk_pinned", "path_cli")
        ],
    }
    agent_started = False

    async def fail_runtime(**_kwargs):
        raise JudgeRuntimeResolutionError(resolution)

    def fail_if_prepared(*_args, **_kwargs):
        nonlocal agent_started
        agent_started = True
        raise AssertionError("Agent preparation must not start after Judge preflight failure")

    monkeypatch.setattr(run_case_module, "BENCHMARK_DIR", benchmark_dir)
    monkeypatch.setattr(run_case_module, "capture_model_config", model_config)
    monkeypatch.setattr(run_case_module, "capture_judge_requested_config", requested_config)
    monkeypatch.setattr(run_case_module, "git_metadata", lambda _cwd: {"commit": "test", "dirty": False})
    monkeypatch.setattr(run_case_module, "resolve_judge_runtime", fail_runtime)
    monkeypatch.setattr(run_case_module, "prepare_exploration_environment", fail_if_prepared)

    result = asyncio.run(
        run_case_module.run_case(case_args(runs_dir, run_id="preflight-failed"))
    )

    assert result["status"] == "judge_preflight_failed"
    assert not agent_started
    execution = read_json(
        runs_dir / "preflight-failed" / "cases" / "001" / "execution.json"
    )
    assert execution["agent"]["status"] == "pending"
    assert execution["judge"]["status"] == "preflight_failed"
    assert execution["judge"]["attempts"][0]["status"] == "preflight_failed"
    assert execution["judge"]["attempts"][0]["runtime_resolution"] == resolution
    run_record = read_json(runs_dir / "preflight-failed" / "run.json")
    assert run_record["error"]["phase"] == "judge_preflight"
    assert run_record["diagnostics"]["judge_requested"]["model"] == "judge-model"
    assert run_record["diagnostics"]["judge_runtime_resolutions"] == [resolution]


def test_injected_batch_resolution_is_reused_and_judge_attempt_is_persisted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_dir = tmp_path / "benchmark"
    source_case_dir = benchmark_dir / "cases" / "001"
    source_case_dir.mkdir(parents=True)
    runs_dir = benchmark_dir / "runs"
    run_id = "rejudge"
    case_run_dir = runs_dir / run_id / "cases" / "001"
    (case_run_dir / "prepared_environment").mkdir(parents=True)
    disclosure_path = case_run_dir / "subject" / "data" / "projects" / "project" / "disclosure.json"
    disclosure_path.parent.mkdir(parents=True)
    disclosure_path.write_text("{}\n", encoding="utf-8")
    execution = new_execution(
        run_id=run_id,
        case_id="001",
        repeat=None,
        agent_config={"model": "agent-model"},
        judge_config={"model": "old-judge", "reasoning_effort": "high"},
    )
    finish_execution(execution, status="subject_completed")
    atomic_write_json(case_run_dir / "execution.json", execution)

    resolution = selected_resolution()
    captured: dict[str, object] = {}

    async def fail_if_resolved(**_kwargs):
        raise AssertionError("batch child must reuse the parent runtime resolution")

    async def fake_run_codex_judge(**kwargs):
        captured.update(kwargs)
        return JudgeRunResult(
            conclusion={"status": "scored", "total_score": 93, "evaluation_report": "ok"},
            thread_id="thread-1",
            turn_id="turn-1",
            model="judge-model",
            provider="openai",
            reasoning_effort="xhigh",
            sdk_version="0.1.0b3",
            runtime_version="0.144.0-alpha.4",
            turn_started_at="2026-07-12T00:00:00Z",
            turn_finished_at="2026-07-12T00:00:01Z",
            turn_duration_ms=1000,
            usage={"input_tokens": 10, "output_tokens": 2},
        )

    monkeypatch.setattr(run_case_module, "BENCHMARK_DIR", benchmark_dir)
    monkeypatch.setattr(run_case_module, "capture_model_config", model_config)
    monkeypatch.setattr(run_case_module, "capture_judge_requested_config", requested_config)
    monkeypatch.setattr(run_case_module, "git_metadata", lambda _cwd: {"commit": "test", "dirty": False})
    monkeypatch.setattr(run_case_module, "resolve_judge_runtime", fail_if_resolved)
    monkeypatch.setattr(run_case_module, "run_codex_judge", fake_run_codex_judge)

    args = case_args(runs_dir, run_id=run_id)
    args.skip_subject = True
    args.judge_reasoning_effort = "ultra"
    args.judge_runtime_resolution = json.dumps(resolution)
    result = asyncio.run(run_case_module.run_case(args))

    assert result["status"] == "completed"
    assert captured["codex_bin"] == "C:/CodexApp/codex.exe"
    assert Path(captured["logs_dir"]).relative_to(case_run_dir).as_posix() == (
        "judge/codex_logs/attempt-001"
    )
    execution = read_json(case_run_dir / "execution.json")
    assert execution["judge"]["requested"]["reasoning_effort"] == "ultra"
    assert execution["judge"]["effective"]["reasoning_effort"] == "xhigh"
    assert execution["judge"]["effective"]["runtime"]["source"] == "codex_app"
    assert execution["judge"]["effective"]["runtime"]["judge_appserver_version"] == (
        "0.144.0-alpha.4"
    )
    attempt = execution["judge"]["attempts"][0]
    assert attempt["status"] == "completed"
    assert attempt["logs_path"] == "judge/codex_logs/attempt-001"
    assert attempt["runtime_resolution"] == resolution


def case_args(runs_dir: Path, *, run_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        case="001",
        run_id=run_id,
        runs_dir=str(runs_dir),
        skip_judge=False,
        skip_subject=False,
        round_timeout=30,
        judge_timeout=40,
        judge_model=None,
        judge_provider=None,
        judge_reasoning_effort=None,
        judge_runtime_resolution=None,
        repeat=None,
        case_output_dir=None,
        batch_child=False,
    )


def model_config() -> dict:
    return {
        "agent": {"model": "agent-model"},
        "judge": {
            "provider": "openai",
            "model": "judge-model",
            "reasoning_effort": "xhigh",
            "sdk_version": "0.1.0b3",
            "source": "test",
        },
    }


def requested_config() -> dict:
    return {
        "provider": "openai",
        "model": "judge-model",
        "reasoning_effort": "ultra",
    }


def selected_resolution() -> dict:
    return {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": {
            "source": "codex_app",
            "path": "C:/CodexApp/codex.exe",
            "launch_mode": "explicit",
            "launch_codex_bin": "C:/CodexApp/codex.exe",
            "binary_version": "codex-cli 0.144.0-alpha.4",
            "appserver_version": "0.144.0-alpha.4",
            "sdk_version": "0.1.0b3",
        },
        "attempts": [{"source": "codex_app", "status": "selected"}],
    }


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

