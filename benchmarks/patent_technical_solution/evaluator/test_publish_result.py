from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from publish_result import RESULT_SCHEMA_VERSION, publish_run, sanitize_result_id
from records import RUN_SCHEMA_VERSION


def test_publish_v2_run_keeps_only_manifest_and_original_conclusions(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    results_dir = tmp_path / "results"
    run_dir = runs_dir / "run-1"
    case_dir = run_dir / "cases" / "001" / "r01"
    conclusion = {
        "status": "scored",
        "total_score": 91,
        "evaluation_report": "## 总评\n\n核心机制完整。",
    }
    write_json(case_dir / "judge" / "conclusion" / "result.json", conclusion)
    write_json(
        case_dir / "execution.json",
        {
            "schema_version": "patent-technical-solution-execution-v2",
            "run_id": "run-1",
            "case_id": "001",
            "repeat": 1,
            "status": "completed",
            "conclusion": {"path": "judge/conclusion/result.json"},
        },
    )
    (case_dir / "subject").mkdir(parents=True)
    (case_dir / "agent_logs").mkdir()
    write_json(
        run_dir / "run.json",
        {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": "run-1",
            "status": "completed",
            "started_at": "2026-07-11T00:00:00Z",
            "finished_at": "2026-07-11T00:01:00Z",
            "duration_ms": 60000,
            "config": {"repeats": 1},
            "models": {"agent": {"model": "agent"}, "judge": {"model": "judge"}},
            "aggregate": {"average_score": 91},
            "cases": [
                {
                    "case_id": "001",
                    "repeat": 1,
                    "status": "completed",
                    "execution": "cases/001/r01/execution.json",
                    "conclusion": "cases/001/r01/judge/conclusion/result.json",
                    "total_score": 91,
                }
            ],
        },
    )

    result_dir, manifest = publish_run(
        source_run_id="run-1",
        runs_dir=runs_dir,
        results_dir=results_dir,
        result_id="published run",
        metadata={"notes": "v2"},
    )

    assert result_dir == results_dir / "published-run"
    assert manifest["schema_version"] == RESULT_SCHEMA_VERSION
    assert manifest["scored_runs"] == 1
    assert manifest["cases"][0]["total_score"] == 91
    assert read_json(result_dir / "conclusions" / "001" / "r01.json") == conclusion
    assert sorted(path.name for path in result_dir.iterdir()) == ["conclusions", "manifest.json"]
    assert not (result_dir / "artifacts").exists()
    assert not (result_dir / "evaluation_summary.json").exists()
    assert "subject/" in manifest["not_duplicated"]
    assert json.loads((results_dir / "index.jsonl").read_text(encoding="utf-8"))["result_id"] == "published-run"


def test_publish_rejects_v1_runs(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "old-run"
    write_json(run_dir / "run.json", {"schema_version": 1, "cases": []})

    with pytest.raises(SystemExit, match="v2 run"):
        publish_run(
            source_run_id="old-run",
            runs_dir=tmp_path / "runs",
            results_dir=tmp_path / "results",
        )


def test_sanitize_result_id_removes_path_like_characters() -> None:
    assert sanitize_result_id(" 2026/07/11 comprehensive run ") == "2026-07-11-comprehensive-run"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
