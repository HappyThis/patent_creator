from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import run_case as run_case_module
from .codex_judge import JudgeRunResult
from .judge_runtime import JudgeRuntimeResolutionError
from .records import (
    aggregate_case_records,
    atomic_write_json,
    finish_execution,
    finish_run_record,
    new_execution,
    new_run_record,
)
from .run_case import audit_subject_network_use, aggregate_agent_usage, validate_subject_workspace


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


def test_representation_network_audit_rejects_disabled_tool_calls(tmp_path: Path) -> None:
    session = tmp_path / "data" / "projects" / "project" / "sessions" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_result",
                        "payload": {"output": {"content": "documentation mentions curl"}},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "payload": {"tool": "file_read", "arguments": {"path": "source.txt"}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert audit_subject_network_use(tmp_path)["external_network_calls"] == 0

    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "tool_call",
                    "payload": {
                        "tool": "exec_command",
                        "arguments": {"command": "rg source project_snapshot"},
                    },
                }
            )
            + "\n"
        )
    with pytest.raises(RuntimeError, match="disabled external-access tool"):
        audit_subject_network_use(tmp_path)


def test_representation_network_audit_rejects_native_web_search_events(tmp_path: Path) -> None:
    session = tmp_path / "data" / "projects" / "project" / "sessions" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "llm_audit",
                "payload": {
                    "category": "web_search",
                    "source": "openai_responses",
                    "item": {"type": "web_search_call"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="web_search:openai_responses"):
        audit_subject_network_use(tmp_path)


def test_temporary_subject_access_policy_restores_environment_and_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backend_dir = run_case_module.REPO_DIR / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from app.agents.workers import MAIN_AGENT_TOOLS

    prepared_environment = tmp_path / "prepared_environment"
    prepared_environment.mkdir()
    previous_tools = list(MAIN_AGENT_TOOLS)
    monkeypatch.setenv(run_case_module._EXTERNAL_READ_ROOTS_ENV, "previous-root")
    monkeypatch.setenv(run_case_module._EXEC_COMMAND_DISABLED_ENV, "off")

    async def exercise_policy() -> None:
        with pytest.raises(RuntimeError, match="policy test"):
            async with run_case_module._temporary_subject_access_policy(
                prepared_environment,
                enabled=True,
            ):
                assert os.environ[run_case_module._EXTERNAL_READ_ROOTS_ENV] == str(
                    prepared_environment.resolve()
                )
                assert os.environ[run_case_module._EXEC_COMMAND_DISABLED_ENV] == "1"
                assert all(
                    tool.get("function", {}).get("name") != "exec_command"
                    for tool in MAIN_AGENT_TOOLS
                )
                raise RuntimeError("policy test")

    asyncio.run(exercise_policy())

    assert os.environ[run_case_module._EXTERNAL_READ_ROOTS_ENV] == "previous-root"
    assert os.environ[run_case_module._EXEC_COMMAND_DISABLED_ENV] == "off"
    assert MAIN_AGENT_TOOLS == previous_tools


def test_temporary_subject_access_policy_serializes_same_process_callers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared_a = tmp_path / "prepared-a"
    prepared_b = tmp_path / "prepared-b"
    prepared_a.mkdir()
    prepared_b.mkdir()
    monkeypatch.delenv(run_case_module._EXTERNAL_READ_ROOTS_ENV, raising=False)
    monkeypatch.delenv(run_case_module._EXEC_COMMAND_DISABLED_ENV, raising=False)
    active = 0
    maximum_active = 0
    observed_roots: list[str] = []

    async def worker(path: Path) -> None:
        nonlocal active, maximum_active
        async with run_case_module._temporary_subject_access_policy(path, enabled=True):
            active += 1
            maximum_active = max(maximum_active, active)
            observed_roots.append(os.environ[run_case_module._EXTERNAL_READ_ROOTS_ENV])
            await asyncio.sleep(0.01)
            assert os.environ[run_case_module._EXTERNAL_READ_ROOTS_ENV] == str(path.resolve())
            active -= 1

    async def run_workers() -> None:
        await asyncio.gather(worker(prepared_a), worker(prepared_b))

    asyncio.run(run_workers())

    assert maximum_active == 1
    assert observed_roots == [str(prepared_a.resolve()), str(prepared_b.resolve())]
    assert run_case_module._EXTERNAL_READ_ROOTS_ENV not in os.environ
    assert run_case_module._EXEC_COMMAND_DISABLED_ENV not in os.environ


def test_unrestricted_policy_waits_for_restricted_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    monkeypatch.delenv(run_case_module._EXTERNAL_READ_ROOTS_ENV, raising=False)
    restricted_entered = asyncio.Event()
    release_restricted = asyncio.Event()
    unrestricted_entered = False

    async def restricted_worker() -> None:
        async with run_case_module._temporary_subject_access_policy(prepared, enabled=True):
            restricted_entered.set()
            await release_restricted.wait()

    async def unrestricted_worker() -> None:
        nonlocal unrestricted_entered
        await restricted_entered.wait()
        async with run_case_module._temporary_subject_access_policy(prepared, enabled=False):
            unrestricted_entered = True
            assert run_case_module._EXTERNAL_READ_ROOTS_ENV not in os.environ

    async def exercise() -> None:
        restricted_task = asyncio.create_task(restricted_worker())
        unrestricted_task = asyncio.create_task(unrestricted_worker())
        await restricted_entered.wait()
        await asyncio.sleep(0.02)
        assert not unrestricted_entered
        release_restricted.set()
        await asyncio.gather(restricted_task, unrestricted_task)

    asyncio.run(exercise())
    assert unrestricted_entered


def test_manual_rejudge_case_lock_rejects_same_identity(tmp_path: Path) -> None:
    case_run_dir = tmp_path / "run" / "cases" / "001" / "r01"

    with run_case_module.manual_rejudge_case_lock(case_run_dir):
        with pytest.raises(SystemExit, match="已有重判正在运行"):
            with run_case_module.manual_rejudge_case_lock(case_run_dir):
                pass

    with run_case_module.manual_rejudge_case_lock(case_run_dir):
        pass


def test_reusable_execution_identity_must_match_rejudge_target(tmp_path: Path) -> None:
    case_run_dir = tmp_path / "cases" / "001" / "r01"
    prepared_environment = case_run_dir / "prepared_environment"
    prepared_environment.mkdir(parents=True)
    disclosure_path = case_run_dir / "subject" / "data" / "projects" / "project" / "disclosure.json"
    disclosure_path.parent.mkdir(parents=True)
    disclosure_path.write_text("{}\n", encoding="utf-8")
    execution = new_execution(
        run_id="actual-run",
        case_id="001",
        repeat=1,
        agent_config={"model": "agent-model"},
        judge_config={"model": "judge-model"},
    )
    atomic_write_json(case_run_dir / "execution.json", execution)

    with pytest.raises(SystemExit, match="身份与重判目标不一致"):
        run_case_module.require_reusable_execution(
            case_run_dir / "execution.json",
            prepared_environment,
            case_run_dir / "subject",
            expected_run_id="different-run",
            expected_case_id="001",
            expected_repeat=1,
        )

    execution["repeat"] = True
    atomic_write_json(case_run_dir / "execution.json", execution)
    with pytest.raises(SystemExit, match="execution.repeat 非法"):
        run_case_module.require_reusable_execution(
            case_run_dir / "execution.json",
            prepared_environment,
            case_run_dir / "subject",
            expected_run_id="actual-run",
            expected_case_id="001",
            expected_repeat=1,
        )


def test_cancel_subject_round_waits_for_chat_cleanup() -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeChat:
        async def cancel_round(self, project_id: str, session_id: str, round_id: str) -> None:
            await asyncio.sleep(0)
            calls.append((project_id, session_id, round_id))

    services = SimpleNamespace(chat=FakeChat())
    response = SimpleNamespace(session_id="session", round_id="round")

    asyncio.run(run_case_module.cancel_subject_round(services, "project", response))

    assert calls == [("project", "session", "round")]


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
    patch_general_track(monkeypatch, benchmark_dir)
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
    patch_general_track(monkeypatch, benchmark_dir)
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


def test_manual_batch_rejudge_replaces_parent_case_and_recomputes_aggregate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_dir = tmp_path / "benchmark"
    source_case_dir = benchmark_dir / "cases" / "001"
    source_case_dir.mkdir(parents=True)
    runs_dir = benchmark_dir / "runs"
    run_id = "representation-batch"
    run_dir = runs_dir / run_id
    case_run_dir = run_dir / "cases" / "001" / "r01"
    (case_run_dir / "prepared_environment").mkdir(parents=True)
    disclosure_path = case_run_dir / "subject" / "data" / "projects" / "project" / "disclosure.json"
    disclosure_path.parent.mkdir(parents=True)
    disclosure_path.write_text("{}\n", encoding="utf-8")

    execution = new_execution(
        run_id=run_id,
        case_id="001",
        repeat=1,
        agent_config={"model": "agent-model"},
        judge_config={"model": "old-judge", "reasoning_effort": "high"},
    )
    execution["track_id"] = "representation_semantics"
    finish_execution(
        execution,
        status="judge_failed",
        error={"phase": "judge", "type": "RuntimeError", "message": "old failure"},
    )
    atomic_write_json(case_run_dir / "execution.json", execution)

    failed_record = {
        "case_id": "001",
        "repeat": 1,
        "status": "judge_failed",
        "execution": "cases/001/r01/execution.json",
        "conclusion": None,
        "total_score": None,
    }
    preserved_record = representation_case_record(
        case_id="002",
        repeat=1,
        solution_score=100,
        representation_score=100,
    )
    parent = new_run_record(
        run_id=run_id,
        run_kind="batch",
        case_ids=["001", "002"],
        config={"track_id": "representation_semantics", "repeats": 1, "workers": 10},
        models=model_config(),
        benchmark_git={"commit": "test", "dirty": False},
    )
    initial_records = [failed_record, preserved_record]
    finish_run_record(
        parent,
        status="partial_failed",
        cases=initial_records,
        aggregate=aggregate_case_records(initial_records),
    )
    atomic_write_json(run_dir / "run.json", parent)

    resolution = selected_resolution()
    captured: dict[str, object] = {}

    async def fake_run_codex_judge(**kwargs):
        captured.update(kwargs)
        return JudgeRunResult(
            conclusion={
                "status": "scored",
                "solution_score": 80,
                "representation_score": 80,
                "total_score": 80,
                "representation": {
                    "figure": {
                        "policy": "recommended",
                        "used": True,
                        "score": 80,
                        "verdict": "partially_correct",
                        "assessment": "minor issue",
                    },
                    "formula": {
                        "policy": "optional",
                        "used": False,
                        "score": 100,
                        "verdict": "not_used",
                        "assessment": "not used",
                    },
                },
                "evaluation_report": "ok",
            },
            thread_id="thread-2",
            turn_id="turn-2",
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
    patch_representation_track(monkeypatch, benchmark_dir)
    monkeypatch.setattr(run_case_module, "capture_model_config", model_config)
    monkeypatch.setattr(run_case_module, "capture_judge_requested_config", requested_config)
    monkeypatch.setattr(run_case_module, "resolve_judge_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(run_case_module, "run_codex_judge", fake_run_codex_judge)

    args = case_args(runs_dir, run_id=run_id)
    args.track = "representation_semantics"
    args.skip_subject = True
    args.batch_child = True
    args.repeat = 1
    args.case_output_dir = str(case_run_dir)
    args.judge_model = "replacement-judge"
    args.judge_runtime_resolution = json.dumps(resolution)
    result = asyncio.run(run_case_module.run_case(args))

    assert result["status"] == "completed"
    assert captured["judge_profile"] == "representation_semantics"
    assert captured["model"] == "replacement-judge"
    updated_parent = read_json(run_dir / "run.json")
    assert updated_parent["status"] == "completed"
    assert len(updated_parent["cases"]) == 2
    assert [(item["case_id"], item["repeat"]) for item in updated_parent["cases"]] == [
        ("001", 1),
        ("002", 1),
    ]
    assert updated_parent["cases"][0]["status"] == "completed"
    assert updated_parent["cases"][0]["representation_score"] == 80
    assert updated_parent["cases"][1] == preserved_record
    assert updated_parent["aggregate"]["runs"] == 2
    assert updated_parent["aggregate"]["scored_runs"] == 2
    assert updated_parent["aggregate"]["average_score"] == 90
    assert updated_parent["aggregate"]["representation"]["representation_average_score"] == 90
    assert updated_parent["aggregate"]["representation"]["modalities"]["formula"][
        "recommended_not_used_runs"
    ] == 0
    assert updated_parent["models"]["judge"]["model"] == "judge-model"
    assert updated_parent["models"]["judge_overrides"]["001:r01"]["model"] == (
        "replacement-judge"
    )
    assert updated_parent["diagnostics"]["judge_requested_by_case"]["001:r01"]["model"] == (
        "replacement-judge"
    )


def test_manual_batch_rejudge_rejects_parent_while_batch_is_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_dir = tmp_path / "benchmark"
    (benchmark_dir / "cases" / "001").mkdir(parents=True)
    runs_dir = benchmark_dir / "runs"
    run_id = "running-batch"
    run_dir = runs_dir / run_id
    case_run_dir = run_dir / "cases" / "001" / "r01"
    parent = new_run_record(
        run_id=run_id,
        run_kind="batch",
        case_ids=["001"],
        config={"track_id": "representation_semantics", "repeats": 1, "workers": 1},
        models=model_config(),
        benchmark_git={"commit": "test", "dirty": False},
    )
    atomic_write_json(run_dir / "run.json", parent)

    monkeypatch.setattr(run_case_module, "BENCHMARK_DIR", benchmark_dir)
    patch_representation_track(monkeypatch, benchmark_dir)
    monkeypatch.setattr(run_case_module, "capture_model_config", model_config)
    monkeypatch.setattr(run_case_module, "capture_judge_requested_config", requested_config)

    args = case_args(runs_dir, run_id=run_id)
    args.track = "representation_semantics"
    args.skip_subject = True
    args.batch_child = True
    args.repeat = 1
    args.case_output_dir = str(case_run_dir)

    with pytest.raises(SystemExit, match="父 batch 仍在运行"):
        asyncio.run(run_case_module.run_case(args))


def test_batch_status_requires_every_expected_case_and_repeat() -> None:
    case = representation_case_record(
        case_id="001",
        repeat=1,
        solution_score=100,
        representation_score=100,
    )
    expected = {("001", 1), ("002", 1)}

    assert run_case_module.batch_run_status([case], expected_identities=expected) == "partial_failed"
    assert run_case_module.batch_run_status(
        [case, dict(case)],
        expected_identities=expected,
    ) == "partial_failed"
    assert run_case_module.batch_run_status(
        [
            case,
            representation_case_record(
                case_id="002",
                repeat=1,
                solution_score=100,
                representation_score=100,
            ),
        ],
        expected_identities=expected,
    ) == "completed"


def test_batch_parent_updates_merge_against_latest_run_record(tmp_path: Path) -> None:
    run_id = "concurrent-rejudge"
    run_dir = tmp_path / "runs" / run_id
    run_record_path = run_dir / "run.json"
    failed_records = [
        {
            "case_id": case_id,
            "repeat": 1,
            "status": "judge_failed",
            "execution": f"cases/{case_id}/r01/execution.json",
            "conclusion": None,
            "total_score": None,
        }
        for case_id in ("001", "002")
    ]
    parent = new_run_record(
        run_id=run_id,
        run_kind="batch",
        case_ids=["001", "002"],
        config={"track_id": "representation_semantics", "repeats": 1, "workers": 2},
        models=model_config(),
        benchmark_git={"commit": "test", "dirty": False},
    )
    finish_run_record(
        parent,
        status="failed",
        cases=failed_records,
        aggregate=aggregate_case_records(failed_records),
    )
    atomic_write_json(run_record_path, parent)

    run_case_module.persist_batch_judge_override(
        run_record_path,
        case_id="001",
        repeat=1,
        judge_config={"model": "judge-a"},
        judge_requested={"model": "judge-a"},
    )
    stale_after_first_override = read_json(run_record_path)
    run_case_module.persist_batch_judge_override(
        run_record_path,
        case_id="002",
        repeat=1,
        judge_config={"model": "judge-b"},
        judge_requested={"model": "judge-b"},
    )
    run_case_module.persist_runtime_diagnostic(
        stale_after_first_override,
        run_record_path,
        {"selected": {"source": "runtime-a"}},
    )
    run_case_module.persist_runtime_diagnostic(
        stale_after_first_override,
        run_record_path,
        {"selected": {"source": "runtime-b"}},
    )

    stale_a = read_json(run_record_path)
    stale_b = read_json(run_record_path)
    for case_id, stale, score in (("001", stale_a, 80), ("002", stale_b, 90)):
        execution = new_execution(
            run_id=run_id,
            case_id=case_id,
            repeat=1,
            agent_config={"model": "agent"},
            judge_config={"model": f"judge-{case_id}"},
        )
        finish_execution(
            execution,
            status="completed",
            conclusion_path="judge/conclusion/result.json",
        )
        run_case_module.finalize_case(
            execution=execution,
            conclusion={"status": "scored", "total_score": score},
            run_record=stale,
            run_dir=run_dir,
            case_run_dir=run_dir / "cases" / case_id / "r01",
        )

    updated = read_json(run_record_path)
    assert updated["status"] == "completed"
    assert [(item["case_id"], item["total_score"]) for item in updated["cases"]] == [
        ("001", 80),
        ("002", 90),
    ]
    assert set(updated["models"]["judge_overrides"]) == {"001:r01", "002:r01"}
    assert set(updated["diagnostics"]["judge_requested_by_case"]) == {
        "001:r01",
        "002:r01",
    }
    assert [
        item["selected"]["source"]
        for item in updated["diagnostics"]["judge_runtime_resolutions"]
    ] == ["runtime-a", "runtime-b"]


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


def patch_general_track(monkeypatch, benchmark_dir: Path) -> None:
    track = SimpleNamespace(
        track_id="general_solution",
        judge_profile="general",
        track_dir=benchmark_dir / "tracks" / "general_solution",
        track_judge_path=None,
        subject_policy=SimpleNamespace(
            web_search_enabled=None,
            expose_snapshot_provenance=True,
            preserve_snapshot_git=True,
        ),
    )
    monkeypatch.setattr(run_case_module, "load_track", lambda *_args, **_kwargs: track)
    monkeypatch.setattr(
        run_case_module,
        "resolve_track_case",
        lambda _track, case_id: SimpleNamespace(
            case_id=str(case_id).zfill(3),
            source_case_dir=benchmark_dir / "cases" / str(case_id).zfill(3),
            track_rubric_path=None,
            figure_policy=None,
            formula_policy=None,
        ),
    )


def patch_representation_track(monkeypatch, benchmark_dir: Path) -> None:
    track = SimpleNamespace(
        track_id="representation_semantics",
        judge_profile="representation_semantics",
        track_dir=benchmark_dir / "tracks" / "representation_semantics",
        track_judge_path=benchmark_dir / "tracks" / "representation_semantics" / "judge.md",
        subject_policy=SimpleNamespace(
            web_search_enabled=False,
            expose_snapshot_provenance=False,
            preserve_snapshot_git=False,
        ),
    )
    monkeypatch.setattr(run_case_module, "load_track", lambda *_args, **_kwargs: track)
    monkeypatch.setattr(
        run_case_module,
        "resolve_track_case",
        lambda _track, case_id: SimpleNamespace(
            case_id=str(case_id).zfill(3),
            source_case_dir=benchmark_dir / "cases" / str(case_id).zfill(3),
            track_rubric_path=benchmark_dir / "tracks" / "representation_semantics" / "rubric.md",
            figure_policy="recommended",
            formula_policy="optional",
        ),
    )


def representation_case_record(
    *,
    case_id: str,
    repeat: int,
    solution_score: int,
    representation_score: int,
) -> dict:
    return {
        "case_id": case_id,
        "repeat": repeat,
        "status": "completed",
        "execution": f"cases/{case_id}/r{repeat:02d}/execution.json",
        "conclusion": f"cases/{case_id}/r{repeat:02d}/judge/conclusion/result.json",
        "solution_score": solution_score,
        "representation_score": representation_score,
        "total_score": 0.7 * solution_score + 0.3 * representation_score,
        "representation": {
            "figure": {
                "policy": "recommended",
                "used": True,
                "score": representation_score,
                "verdict": "correct",
                "assessment": "figure",
            },
            "formula": {
                "policy": "optional",
                "used": False,
                "score": 100,
                "verdict": "not_used",
                "assessment": "formula",
            },
        },
    }


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
