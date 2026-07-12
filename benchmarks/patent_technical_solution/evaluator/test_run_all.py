from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import run_all
from .judge_runtime import JudgeRuntimeResolutionError
from .run_all import apply_judge_overrides, judge_override_args


def test_batch_judge_overrides_update_models_and_child_args() -> None:
    args = SimpleNamespace(
        judge_model="judge-model",
        judge_provider="judge-provider",
        judge_reasoning_effort="ultra",
    )
    models = {
        "agent": {"model": "agent-model"},
        "judge": {
            "model": "default-model",
            "provider": "default-provider",
            "reasoning_effort": "high",
        },
    }
    requested = {
        "model": "default-model",
        "provider": "default-provider",
        "reasoning_effort": "high",
    }

    apply_judge_overrides(models, requested, args)

    assert models["judge"] == {
        "model": "judge-model",
        "provider": "judge-provider",
        "reasoning_effort": "xhigh",
    }
    assert requested == {
        "model": "judge-model",
        "provider": "judge-provider",
        "reasoning_effort": "ultra",
    }
    assert judge_override_args(args, requested) == [
        "--judge-model",
        "judge-model",
        "--judge-provider",
        "judge-provider",
        "--judge-reasoning-effort",
        "ultra",
    ]


def test_batch_without_judge_overrides_does_not_add_child_args() -> None:
    args = SimpleNamespace(
        judge_model=None,
        judge_provider=None,
        judge_reasoning_effort=None,
    )
    models = {"judge": {"model": "default-model", "reasoning_effort": "high"}}
    requested = {"model": "default-model", "reasoning_effort": "high"}

    apply_judge_overrides(models, requested, args)

    assert models["judge"] == {"model": "default-model", "reasoning_effort": "high"}
    assert requested == {"model": "default-model", "reasoning_effort": "high"}
    assert judge_override_args(args, requested) == []


def test_batch_child_receives_raw_requested_config_and_resolved_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runs_dir = tmp_path / "runs"
    run_id = "batch-run"
    case_run_dir = runs_dir / run_id / "cases" / "001" / "r01"
    case_run_dir.mkdir(parents=True)
    (case_run_dir / "execution.json").write_text(
        json.dumps({"status": "subject_completed", "conclusion": None}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        repeats=1,
        round_timeout=30,
        judge_timeout=40,
        skip_judge=False,
        judge_model="judge-model",
        judge_provider="judge-provider",
        judge_reasoning_effort="ultra",
    )
    models = {"agent": {}, "judge": {"reasoning_effort": "high"}}
    requested = {"reasoning_effort": "high"}
    apply_judge_overrides(models, requested, args)
    resolution = {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": {"source": "codex_app", "launch_codex_bin": "/app/codex"},
        "attempts": [{"source": "codex_app", "status": "selected"}],
    }
    captured: dict[str, list[str]] = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            assert timeout == 190
            return "", ""

    def fake_popen(command: list[str], **_kwargs) -> FakeProcess:
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(run_all.subprocess, "Popen", fake_popen)

    run_all.run_one_job(
        {"case_id": "001", "repeat": 1, "case_run_dir": case_run_dir},
        args=args,
        runs_dir=runs_dir,
        run_id=run_id,
        models=models,
        judge_requested=requested,
        judge_runtime_resolution=resolution,
    )

    command = captured["command"]
    assert command[command.index("--judge-model") + 1] == "judge-model"
    assert command[command.index("--judge-provider") + 1] == "judge-provider"
    assert command[command.index("--judge-reasoning-effort") + 1] == "ultra"
    encoded_resolution = command[command.index("--judge-runtime-resolution") + 1]
    assert json.loads(encoded_resolution) == resolution
    assert not any("codex-bin" in value for value in command)


def test_batch_resolves_runtime_once_before_reusing_it_for_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = batch_args(tmp_path, cases=["001", "002"])
    resolution = {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": {"source": "codex_app", "launch_codex_bin": "/app/codex"},
        "attempts": [{"source": "codex_app", "status": "selected"}],
    }
    resolve_calls: list[dict] = []
    jobs_calls: list[dict] = []

    async def fake_resolve_judge_runtime(**kwargs):
        resolve_calls.append(kwargs)
        return resolution

    def fake_run_jobs(_jobs, **kwargs):
        jobs_calls.append(kwargs)
        assert kwargs["judge_runtime_resolution"] is resolution
        assert kwargs["judge_requested"]["reasoning_effort"] == "ultra"
        return [
            {
                "case_id": case_id,
                "repeat": 1,
                "status": "completed",
                "execution": f"cases/{case_id}/r01/execution.json",
                "conclusion": f"cases/{case_id}/r01/judge/conclusion/result.json",
                "total_score": 90,
            }
            for case_id in ("001", "002")
        ]

    monkeypatch.setattr(run_all, "parse_args", lambda: args)
    monkeypatch.setattr(run_all, "capture_model_config", model_config)
    monkeypatch.setattr(run_all, "capture_judge_requested_config", requested_config)
    monkeypatch.setattr(run_all, "git_metadata", lambda _cwd: {"commit": "test", "dirty": False})
    monkeypatch.setattr(run_all, "resolve_judge_runtime", fake_resolve_judge_runtime)
    monkeypatch.setattr(run_all, "run_jobs", fake_run_jobs)

    run_all.main()

    assert len(resolve_calls) == 1
    assert len(jobs_calls) == 1
    assert resolve_calls[0]["reasoning_effort"] == "xhigh"
    run_record = json.loads((Path(args.runs_dir) / args.run_id / "run.json").read_text(encoding="utf-8"))
    assert run_record["status"] == "completed"
    assert run_record["models"]["judge"]["reasoning_effort"] == "xhigh"
    assert run_record["diagnostics"]["judge_requested"]["reasoning_effort"] == "ultra"
    assert run_record["diagnostics"]["judge_runtime_resolution"] == resolution


def test_batch_runtime_preflight_failure_persists_diagnostics_without_starting_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = batch_args(tmp_path, cases=["001"])
    resolution = {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": None,
        "attempts": [
            {
                "source": "codex_app",
                "status": "rejected",
                "error": {"type": "RuntimeError", "message": "unsupported model"},
            }
        ],
    }
    jobs_started = 0

    async def fake_resolve_judge_runtime(**_kwargs):
        raise JudgeRuntimeResolutionError(resolution)

    def fail_if_jobs_start(*_args, **_kwargs):
        nonlocal jobs_started
        jobs_started += 1
        raise AssertionError("batch jobs must not start after Judge preflight failure")

    monkeypatch.setattr(run_all, "parse_args", lambda: args)
    monkeypatch.setattr(run_all, "capture_model_config", model_config)
    monkeypatch.setattr(run_all, "capture_judge_requested_config", requested_config)
    monkeypatch.setattr(run_all, "git_metadata", lambda _cwd: {"commit": "test", "dirty": False})
    monkeypatch.setattr(run_all, "resolve_judge_runtime", fake_resolve_judge_runtime)
    monkeypatch.setattr(run_all, "run_jobs", fail_if_jobs_start)

    with pytest.raises(SystemExit, match="No usable Codex runtime"):
        run_all.main()

    assert jobs_started == 0
    run_record = json.loads((Path(args.runs_dir) / args.run_id / "run.json").read_text(encoding="utf-8"))
    assert run_record["status"] == "failed"
    assert run_record["error"]["phase"] == "judge_preflight"
    assert run_record["diagnostics"]["judge_runtime_resolution"] == resolution


def test_skip_judge_batch_does_not_resolve_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = batch_args(tmp_path, cases=["001"])
    args.skip_judge = True
    jobs_started = 0

    async def fail_if_resolved(**_kwargs):
        raise AssertionError("subject-only batch must not resolve a Judge runtime")

    def fake_run_jobs(_jobs, **kwargs):
        nonlocal jobs_started
        jobs_started += 1
        assert kwargs["judge_runtime_resolution"] is None
        return [
            {
                "case_id": "001",
                "repeat": 1,
                "status": "subject_completed",
                "execution": "cases/001/r01/execution.json",
                "conclusion": None,
                "total_score": None,
            }
        ]

    monkeypatch.setattr(run_all, "parse_args", lambda: args)
    monkeypatch.setattr(run_all, "capture_model_config", model_config)
    monkeypatch.setattr(run_all, "capture_judge_requested_config", requested_config)
    monkeypatch.setattr(run_all, "git_metadata", lambda _cwd: {"commit": "test", "dirty": False})
    monkeypatch.setattr(run_all, "resolve_judge_runtime", fail_if_resolved)
    monkeypatch.setattr(run_all, "run_jobs", fake_run_jobs)

    run_all.main()

    assert jobs_started == 1
    run_record = json.loads((Path(args.runs_dir) / args.run_id / "run.json").read_text(encoding="utf-8"))
    assert run_record["status"] == "completed"
    assert "judge_runtime_resolution" not in run_record["diagnostics"]


def batch_args(tmp_path: Path, *, cases: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        cases=cases,
        run_id="batch-run",
        runs_dir=str(tmp_path / "runs"),
        workers=2,
        repeats=1,
        round_timeout=30,
        judge_timeout=40,
        skip_judge=False,
        judge_model="judge-model",
        judge_provider="judge-provider",
        judge_reasoning_effort="ultra",
    )


def model_config() -> dict:
    return {
        "agent": {"model": "agent-model"},
        "judge": {
            "model": "default-model",
            "provider": "default-provider",
            "reasoning_effort": "high",
            "service_tier": "default",
        },
    }


def requested_config() -> dict:
    return {
        "model": "default-model",
        "provider": "default-provider",
        "reasoning_effort": "high",
        "service_tier": "default",
    }
