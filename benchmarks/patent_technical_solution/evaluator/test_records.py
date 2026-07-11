from __future__ import annotations

import sys
from pathlib import Path

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from records import (
    EXECUTION_SCHEMA_VERSION,
    aggregate_case_records,
    atomic_write_json,
    finish_execution,
    finish_phase,
    new_execution,
    read_json_dict,
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

