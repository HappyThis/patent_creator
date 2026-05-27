from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from run_all import aggregate_results, parse_case_result, read_case_result, stream_pipe, write_text_if_present
from run_metadata import capture_model_config


def test_stream_pipe_prefixes_terminal_output_without_mutating_chunks() -> None:
    chunks: list[str] = []
    target = StringIO()

    stream_pipe(StringIO("alpha\nbeta\n"), chunks, target, "[worker=bench-worker_0 run=r01-001] stdout")

    assert "".join(chunks) == "alpha\nbeta\n"
    assert target.getvalue() == (
        "[worker=bench-worker_0 run=r01-001] stdout alpha\n"
        "[worker=bench-worker_0 run=r01-001] stdout beta\n"
    )


def test_write_text_if_present_only_writes_non_empty_text(tmp_path: Path) -> None:
    empty_path = tmp_path / "empty.txt"
    written_path = tmp_path / "nested" / "stdout.txt"

    assert write_text_if_present(empty_path, "") is None
    assert not empty_path.exists()

    assert write_text_if_present(written_path, "hello\n") == written_path
    assert written_path.read_text(encoding="utf-8") == "hello\n"


def test_read_case_result_reads_result_json(tmp_path: Path) -> None:
    case_run_dir = tmp_path / "cases" / "001"
    case_run_dir.mkdir(parents=True)
    payload = {"status": "scored", "judge": {"total_score": 82}}
    (case_run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    assert read_case_result(case_run_dir) == payload


def test_parse_case_result_ignores_prefixed_progress_and_finds_final_json() -> None:
    stdout = "\n".join(
        [
            "[benchmark] case=001 phase=prepare message=ok",
            "[benchmark] case=001 phase=result message=done",
            json.dumps({"status": "scored", "judge": {"total_score": 76}}, ensure_ascii=False),
        ]
    )

    assert parse_case_result(stdout) == {"status": "scored", "judge": {"total_score": 76}}


def test_aggregate_marks_judge_failed_as_unscored_infrastructure_failure() -> None:
    aggregate = aggregate_results(
        [
            {
                "case_id": "008",
                "repeat": 1,
                "result": {
                    "status": "judge_failed",
                    "subject_status": "completed",
                    "diagnostics": {"artifact_extracted": True},
                },
            }
        ],
        case_ids=["008"],
    )

    assert aggregate[0]["artifact_success_rate"] == 1
    assert aggregate[0]["artifact_success_runs"] == 1
    assert aggregate[0]["scored_runs"] == 0
    assert aggregate[0]["status_counts"] == {"judge_failed": 1}
    assert "recommendation" not in aggregate[0]
    assert "recommendation_reason" not in aggregate[0]


def test_capture_model_config_records_model_settings_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_COMPAT_PROVIDER", "deepseek")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-secret-value")
    monkeypatch.setenv("OPENAI_COMPAT_THINKING", "enabled")
    monkeypatch.setenv("PATENT_CREATOR_CONTEXT_MAX_TOKENS", "128000")

    model_config = capture_model_config()
    text = json.dumps(model_config, ensure_ascii=False)

    assert model_config["subject"]["provider"] == "deepseek"
    assert model_config["subject"]["model"] == "deepseek-v4-pro"
    assert model_config["subject"]["api_key_configured"] is True
    assert "sk-secret-value" not in text


def test_aggregate_marks_skip_judge_artifact_as_unscored_smoke_result() -> None:
    aggregate = aggregate_results(
        [
            {
                "case_id": "001",
                "repeat": 1,
                "result": {
                    "status": "artifact_extracted",
                    "subject_status": "completed",
                    "diagnostics": {"artifact_extracted": True},
                },
            }
        ],
        case_ids=["001"],
    )

    assert aggregate[0]["artifact_success_rate"] == 1
    assert aggregate[0]["artifact_success_runs"] == 1
    assert aggregate[0]["scored_runs"] == 0
    assert aggregate[0]["status_counts"] == {"artifact_extracted": 1}
    assert "recommendation" not in aggregate[0]
    assert "recommendation_reason" not in aggregate[0]
