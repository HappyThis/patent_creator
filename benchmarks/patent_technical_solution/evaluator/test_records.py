from __future__ import annotations

from pathlib import Path

from .records import (
    EXECUTION_SCHEMA_VERSION,
    aggregate_case_records,
    atomic_write_json,
    finish_execution,
    finish_judge_attempt,
    finish_phase,
    new_execution,
    preserve_legacy_judge_attempt,
    read_json_dict,
    start_judge_attempt,
    start_phase,
)


def test_execution_records_phase_timing_status_and_conclusion(tmp_path: Path) -> None:
    execution = new_execution(
        run_id="run-1",
        case_id="001",
        repeat=2,
        agent_config={"provider": "test", "model": "agent-model"},
        judge_config={"provider": "openai", "model": "judge-model"},
    )

    start_phase(execution, "agent")
    finish_phase(
        execution,
        "agent",
        status="completed",
        fields={"session_id": "session-1", "usage": {"input_tokens": 10}},
    )
    start_phase(execution, "judge")
    finish_phase(execution, "judge", status="completed", fields={"thread_id": "thread-1"})
    finish_execution(
        execution,
        status="completed",
        conclusion_path="judge/conclusion/result.json",
    )
    path = tmp_path / "execution.json"
    atomic_write_json(path, execution)

    saved = read_json_dict(path)
    assert saved is not None
    assert saved["schema_version"] == EXECUTION_SCHEMA_VERSION
    assert saved["status"] == "completed"
    assert saved["agent"]["model"] == "agent-model"
    assert saved["agent"]["session_id"] == "session-1"
    assert saved["agent"]["usage"] == {"input_tokens": 10}
    assert saved["judge"]["thread_id"] == "thread-1"
    assert saved["conclusion"] == {"path": "judge/conclusion/result.json"}
    assert isinstance(saved["duration_ms"], int)


def test_aggregate_case_records_keeps_only_total_score_statistics() -> None:
    aggregate = aggregate_case_records(
        [
            {"case_id": "001", "repeat": 1, "status": "completed", "total_score": 80},
            {"case_id": "001", "repeat": 2, "status": "judge_failed", "total_score": None},
            {"case_id": "002", "repeat": 1, "status": "completed", "total_score": 90},
        ]
    )

    assert aggregate["runs"] == 3
    assert aggregate["scored_runs"] == 2
    assert aggregate["average_score"] == 85
    assert aggregate["status_counts"] == {"completed": 2, "judge_failed": 1}
    assert aggregate["cases"][0]["case_id"] == "001"
    assert aggregate["cases"][0]["average_score"] == 80
    assert "dimension_scores" not in aggregate


def test_judge_attempts_append_across_retries() -> None:
    execution = new_execution(
        run_id="run-1",
        case_id="001",
        repeat=None,
        agent_config={"model": "agent"},
        judge_config={"model": "judge", "requested": {"model": "judge"}},
    )
    resolution = {
        "policy": ["codex_app", "sdk_pinned", "path_cli"],
        "selected": {"source": "codex_app", "path": "/app/codex"},
        "attempts": [],
    }

    start_phase(execution, "judge")
    start_judge_attempt(
        execution,
        logs_path="judge/codex_logs/attempt-001",
        requested={"model": "judge", "reasoning_effort": "ultra"},
        effective={"model": "judge", "reasoning_effort": "xhigh"},
        runtime_resolution=resolution,
    )
    finish_phase(execution, "judge", status="failed")
    finish_judge_attempt(
        execution,
        status="failed",
        error={"phase": "judge", "type": "RuntimeError", "message": "failed"},
    )

    start_phase(execution, "judge")
    start_judge_attempt(
        execution,
        logs_path="judge/codex_logs/attempt-002",
        requested={"model": "judge", "reasoning_effort": "ultra"},
        effective={"model": "judge", "reasoning_effort": "xhigh"},
        runtime_resolution=resolution,
    )
    finish_phase(execution, "judge", status="completed", fields={"thread_id": "thread-2"})
    finish_judge_attempt(execution, status="completed")

    attempts = execution["judge"]["attempts"]
    assert [attempt["status"] for attempt in attempts] == ["failed", "completed"]
    assert attempts[0]["error"]["message"] == "failed"
    assert attempts[1]["thread_id"] == "thread-2"
    assert attempts[1]["logs_path"].endswith("attempt-002")


def test_legacy_judge_summary_becomes_first_attempt() -> None:
    execution = new_execution(
        run_id="run-1",
        case_id="001",
        repeat=None,
        agent_config={"model": "agent"},
        judge_config={"model": "judge"},
    )
    execution["judge"].pop("attempts")
    execution["judge"].update(
        {
            "status": "failed",
            "started_at": "2026-01-01T00:00:00.000Z",
            "finished_at": "2026-01-01T00:00:01.000Z",
            "duration_ms": 1000,
        }
    )
    execution["status"] = "judge_failed"
    execution["error"] = {"phase": "judge", "type": "RuntimeError", "message": "old failure"}

    attempts = preserve_legacy_judge_attempt(execution)

    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["logs_path"] == "judge/codex_logs"
    assert attempts[0]["error"]["message"] == "old failure"

