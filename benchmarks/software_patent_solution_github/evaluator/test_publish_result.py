from __future__ import annotations

import json
import sys
from pathlib import Path

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from publish_result import publish_run, sanitize_result_id


def test_publish_batch_run_writes_sanitized_result_snapshot(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    results_dir = tmp_path / "results"
    case_dir = runs_dir / "batch-1" / "r01-001" / "cases" / "001"
    case_dir.mkdir(parents=True)
    (case_dir / "evaluated_artifact.md").write_text("## 技术方案\n\n有效方案内容。\n", encoding="utf-8")
    result = scored_result(case_id="001", run_id="batch-1/r01-001", score=88, artifact_path=case_dir / "evaluated_artifact.md")
    write_json(case_dir / "result.json", result)
    write_json(
        runs_dir / "batch-1" / "run_summary.json",
        [
            {
                "case_id": "001",
                "repeat": 1,
                "run_id": "batch-1/r01-001",
                "result": result,
            }
        ],
    )

    result_dir, manifest = publish_run(
        source_run_id="batch-1",
        runs_dir=runs_dir,
        results_dir=results_dir,
        result_id="published batch",
        metadata={"subject_model": "mimo-v2.5-pro", "judge_model": "codex"},
    )

    assert result_dir == results_dir / "published-batch"
    assert manifest["metadata"] == {"subject_model": "mimo-v2.5-pro", "judge_model": "codex"}
    assert (result_dir / "manifest.json").exists()
    assert (result_dir / "evaluation_summary.json").exists()
    assert (result_dir / "evaluation_report.md").exists()
    assert (result_dir / "artifacts" / "001" / "r01" / "technical_solution.md").exists()
    assert (result_dir / "judge_results" / "001" / "r01" / "judge.json").exists()
    assert not (result_dir / "subject").exists()

    case_records_text = (result_dir / "case_results.jsonl").read_text(encoding="utf-8")
    assert str(tmp_path) not in case_records_text
    case_record = json.loads(case_records_text)
    assert case_record["artifact_path"] == "artifacts/001/r01/technical_solution.md"
    assert case_record["judge_result_path"] == "judge_results/001/r01/judge.json"
    assert case_record["total_score"] == 88

    index_lines = (results_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(index_lines) == 1
    assert json.loads(index_lines[0])["result_id"] == "published-batch"


def test_publish_single_case_run_generates_summary_without_run_summary(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    results_dir = tmp_path / "results"
    case_dir = runs_dir / "single-001" / "cases" / "001"
    case_dir.mkdir(parents=True)
    (case_dir / "evaluated_artifact.md").write_text("## 技术方案\n\n单 case 方案。\n", encoding="utf-8")
    write_json(
        case_dir / "result.json",
        scored_result(case_id="001", run_id="single-001", score=76, artifact_path=case_dir / "evaluated_artifact.md"),
    )

    result_dir, manifest = publish_run(source_run_id="single-001", runs_dir=runs_dir, results_dir=results_dir)

    assert result_dir == results_dir / "single-001"
    assert manifest["runs"] == 1
    summary = json.loads((result_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
    assert summary[0]["case_id"] == "001"
    assert summary[0]["average_score"] == 76


def test_publish_refuses_existing_result_without_overwrite(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    results_dir = tmp_path / "results"
    case_dir = runs_dir / "single-001" / "cases" / "001"
    case_dir.mkdir(parents=True)
    write_json(case_dir / "result.json", {"status": "skipped_no_solution_artifact", "case_id": "001"})
    (results_dir / "single-001").mkdir(parents=True)

    try:
        publish_run(source_run_id="single-001", runs_dir=runs_dir, results_dir=results_dir)
    except SystemExit as exc:
        assert "结果目录已存在" in str(exc)
    else:
        raise AssertionError("publish_run should refuse to overwrite by default")


def test_sanitize_result_id_removes_path_like_characters() -> None:
    assert sanitize_result_id(" 2026/05/22 mimo run ") == "2026-05-22-mimo-run"


def scored_result(*, case_id: str, run_id: str, score: int, artifact_path: Path) -> dict[str, object]:
    return {
        "status": "scored",
        "case_id": case_id,
        "run_id": run_id,
        "prepared_repo": str(artifact_path.parent / "prepared_repo"),
        "evaluated_artifact": str(artifact_path),
        "subject_status": "completed",
        "diagnostics": {"artifact_extracted": True, "judge_failed": False},
        "judge": {
            "total_score": score,
            "dimension_scores": {"technical_mechanism": score},
            "weaknesses": ["不足项"],
            "missing_key_mechanisms": ["缺失机制"],
        },
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
