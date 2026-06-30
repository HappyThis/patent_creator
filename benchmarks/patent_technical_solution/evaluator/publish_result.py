from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from run_all import aggregate_results, render_report  # noqa: E402
from run_metadata import capture_model_config, compact_dict, git_metadata  # noqa: E402


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    repeat: int
    run_id: str
    case_run_dir: Path
    result: dict[str, Any] | None


def main() -> None:
    args = parse_args()
    result_dir, manifest = publish_run(
        source_run_id=args.run_id,
        runs_dir=Path(args.runs_dir),
        results_dir=Path(args.results_dir),
        result_id=args.name,
        metadata={
            "subject_model": args.subject_model,
            "judge_model": args.judge_model,
            "provider": args.provider,
            "notes": args.notes,
        },
        overwrite=args.overwrite,
    )
    print(f"结果已整理：{result_dir}")
    print("")
    print("请人工检查：")
    for item in manifest["included_files"]:
        print(f"- {item}")
    print("")
    print("确认有效后再由开发者决定是否提交。本脚本不会执行 git add 或 git commit。")


def publish_run(
    *,
    source_run_id: str,
    runs_dir: Path,
    results_dir: Path,
    result_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> tuple[Path, dict[str, Any]]:
    runs_dir = runs_dir.resolve()
    results_dir = results_dir.resolve()
    source_run_dir = runs_dir / source_run_id
    if not source_run_dir.exists():
        raise SystemExit(f"run 不存在：{source_run_dir}")

    normalized_result_id = sanitize_result_id(result_id or source_run_id)
    result_dir = results_dir / normalized_result_id
    if result_dir.exists():
        if not overwrite:
            raise SystemExit(f"结果目录已存在：{result_dir}。如需覆盖，请显式使用 --overwrite。")
        shutil.rmtree(result_dir)

    case_runs = discover_case_runs(source_run_dir=source_run_dir, runs_dir=runs_dir)
    if not case_runs:
        raise SystemExit(f"未发现可发布的 case 运行结果：{source_run_dir}")

    result_dir.mkdir(parents=True)
    case_records = write_case_outputs(case_runs=case_runs, result_dir=result_dir, runs_dir=runs_dir)
    summary = aggregate_results(
        [{"case_id": item.case_id, "repeat": item.repeat, "result": item.result} for item in case_runs],
        case_ids=sorted({item.case_id for item in case_runs}),
    )
    model_config = resolve_model_config(source_run_dir=source_run_dir, case_runs=case_runs)
    run_config = resolve_run_config(source_run_dir=source_run_dir, case_runs=case_runs)
    write_json(result_dir / "evaluation_summary.json", summary)
    repeats = max((item.repeat for item in case_runs), default=1)
    (result_dir / "evaluation_report.md").write_text(
        render_report(
            batch_id=normalized_result_id,
            aggregate=summary,
            repeats=repeats,
            model_config=model_config,
            run_config=run_config,
        ),
        encoding="utf-8",
    )
    write_jsonl(result_dir / "case_results.jsonl", case_records)

    manifest = build_manifest(
        result_id=normalized_result_id,
        source_run_id=source_run_id,
        source_run_dir=source_run_dir,
        runs_dir=runs_dir,
        case_runs=case_runs,
        case_records=case_records,
        metadata=metadata or {},
        model_config=model_config,
        run_config=run_config,
    )
    write_json(result_dir / "manifest.json", manifest)
    upsert_index(results_dir / "index.jsonl", manifest)
    return result_dir, manifest


def discover_case_runs(*, source_run_dir: Path, runs_dir: Path) -> list[CaseRun]:
    run_summary_path = source_run_dir / "run_summary.json"
    if run_summary_path.exists():
        return discover_batch_case_runs(run_summary_path=run_summary_path, runs_dir=runs_dir)
    cases_dir = source_run_dir / "cases"
    if not cases_dir.exists():
        return []
    case_runs: list[CaseRun] = []
    for case_dir in sorted(path for path in cases_dir.iterdir() if path.is_dir()):
        case_id = case_dir.name.zfill(3)
        case_runs.append(
            CaseRun(
                case_id=case_id,
                repeat=1,
                run_id=source_run_dir.name,
                case_run_dir=case_dir,
                result=read_json_dict(case_dir / "result.json"),
            )
        )
    return case_runs


def discover_batch_case_runs(*, run_summary_path: Path, runs_dir: Path) -> list[CaseRun]:
    summary = json.loads(run_summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, list):
        raise SystemExit(f"run_summary.json 格式不正确：{run_summary_path}")
    case_runs: list[CaseRun] = []
    for item in summary:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "").zfill(3)
        run_id = str(item.get("run_id") or "")
        if not case_id or not run_id:
            continue
        case_run_dir = runs_dir / run_id / "cases" / case_id
        result = read_json_dict(case_run_dir / "result.json")
        if result is None and isinstance(item.get("result"), dict):
            result = item["result"]
        case_runs.append(
            CaseRun(
                case_id=case_id,
                repeat=int(item.get("repeat") or 1),
                run_id=run_id,
                case_run_dir=case_run_dir,
                result=result,
            )
        )
    return case_runs


def write_case_outputs(*, case_runs: list[CaseRun], result_dir: Path, runs_dir: Path) -> list[dict[str, Any]]:
    records = []
    for case_run in case_runs:
        repeat_label = f"r{case_run.repeat:02d}"
        artifact_rel, artifact_hash, artifact_bytes = copy_artifact(
            source=case_run.case_run_dir / "evaluated_artifact.md",
            target=result_dir / "artifacts" / case_run.case_id / repeat_label / "technical_solution.md",
            result_dir=result_dir,
        )
        judge_rel = write_judge_result(
            result=case_run.result,
            target=result_dir / "judge_results" / case_run.case_id / repeat_label / "judge.json",
            result_dir=result_dir,
        )
        records.append(
            build_case_record(
                case_run=case_run,
                artifact_rel=artifact_rel,
                artifact_hash=artifact_hash,
                artifact_bytes=artifact_bytes,
                judge_rel=judge_rel,
                runs_dir=runs_dir,
            )
        )
    return records


def copy_artifact(*, source: Path, target: Path, result_dir: Path) -> tuple[str | None, str | None, int | None]:
    if not source.exists():
        return None, None, None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    data = target.read_bytes()
    return relative_posix(target, result_dir), hashlib.sha256(data).hexdigest(), len(data)


def write_judge_result(*, result: dict[str, Any] | None, target: Path, result_dir: Path) -> str | None:
    judge = result.get("judge") if isinstance(result, dict) else None
    if not isinstance(judge, dict):
        return None
    write_json(target, judge)
    return relative_posix(target, result_dir)


def build_case_record(
    *,
    case_run: CaseRun,
    artifact_rel: str | None,
    artifact_hash: str | None,
    artifact_bytes: int | None,
    judge_rel: str | None,
    runs_dir: Path,
) -> dict[str, Any]:
    result = case_run.result or {}
    judge = result.get("judge") if isinstance(result.get("judge"), dict) else {}
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    return {
        "case_id": case_run.case_id,
        "repeat": case_run.repeat,
        "mode": result.get("run_config", {}).get("mode") or diagnostics.get("mode") or "solution",
        "source_run_id": case_run.run_id,
        "source_run_case_dir": relative_posix(case_run.case_run_dir, runs_dir.parent),
        "status": result.get("status") or "unparsed",
        "subject_status": result.get("subject_status"),
        "artifact_extracted": diagnostics.get("artifact_extracted"),
        "mode_output_extracted": diagnostics.get("mode_output_extracted", diagnostics.get("artifact_extracted")),
        "figure_artifact_count": diagnostics.get("figure_artifact_count"),
        "artifact_path": artifact_rel,
        "artifact_sha256": artifact_hash,
        "artifact_bytes": artifact_bytes,
        "judge_result_path": judge_rel,
        "total_score": judge.get("total_score"),
        "dimension_scores": judge.get("dimension_scores"),
        "solution_score": judge.get("solution_score"),
        "figure_score": judge.get("figure_score"),
        "integration_score": judge.get("integration_score"),
        "visual_quality_scores": judge.get("visual_quality_scores"),
        "shape_issues": judge.get("shape_issues"),
        "layout_issues": judge.get("layout_issues"),
        "connector_issues": judge.get("connector_issues"),
        "text_issues": judge.get("text_issues"),
        "score_caps_applied": judge.get("score_caps_applied"),
        "diagnostics": diagnostics,
        "judge_error": result.get("judge_error"),
    }


def build_manifest(
    *,
    result_id: str,
    source_run_id: str,
    source_run_dir: Path,
    runs_dir: Path,
    case_runs: list[CaseRun],
    case_records: list[dict[str, Any]],
    metadata: dict[str, Any],
    model_config: dict[str, Any],
    run_config: dict[str, Any],
) -> dict[str, Any]:
    scored_runs = [record for record in case_records if record.get("status") == "scored"]
    artifact_runs = [record for record in case_records if record.get("artifact_extracted") is True]
    mode_artifact_runs = [record for record in case_records if record.get("mode_output_extracted") is True]
    return {
        "schema_version": 1,
        "result_id": result_id,
        "source_run_id": source_run_id,
        "source_run_dir": relative_posix(source_run_dir, runs_dir.parent),
        "published_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "benchmark_git": git_metadata(BENCHMARK_DIR),
        "metadata": compact_dict(metadata),
        "model_config": model_config,
        "run_config": run_config,
        "case_ids": sorted({item.case_id for item in case_runs}),
        "runs": len(case_runs),
        "scored_runs": len(scored_runs),
        "artifact_success_runs": len(mode_artifact_runs or artifact_runs),
        "included_files": [
            "manifest.json",
            "evaluation_summary.json",
            "evaluation_report.md",
            "case_results.jsonl",
            "artifacts/",
            "judge_results/",
        ],
        "excluded_from_publish": [
            "prepared_environment/project_snapshot/",
            "subject/session_events.jsonl",
            "judge/codex_judge_events.jsonl",
            "run_case_stdout.txt",
            "run_case_stderr.txt",
            "subject/disclosure.json",
            "absolute local paths",
            "provider API keys",
        ],
    }


def upsert_index(index_path: Path, manifest: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict) and value.get("result_id") != manifest["result_id"]:
                entries.append(value)
    entries.append(
        {
            "result_id": manifest["result_id"],
            "source_run_id": manifest["source_run_id"],
            "published_at": manifest["published_at"],
            "case_ids": manifest["case_ids"],
            "runs": manifest["runs"],
            "scored_runs": manifest["scored_runs"],
            "artifact_success_runs": manifest["artifact_success_runs"],
            "metadata": manifest["metadata"],
            "model_config": manifest["model_config"],
            "run_config": manifest["run_config"],
        }
    )
    write_jsonl(index_path, entries)


def resolve_model_config(*, source_run_dir: Path, case_runs: list[CaseRun]) -> dict[str, Any]:
    source_manifest = read_json_dict(source_run_dir / "run_manifest.json")
    if isinstance(source_manifest, dict) and isinstance(source_manifest.get("model_config"), dict):
        return {"source": "source_run_manifest", **source_manifest["model_config"]}

    for case_run in case_runs:
        input_manifest = read_json_dict(case_run.case_run_dir / "input_manifest.json")
        if isinstance(input_manifest, dict) and isinstance(input_manifest.get("model_config"), dict):
            return {"source": "case_input_manifest", **input_manifest["model_config"]}

    return {"source": "current_environment_fallback", **capture_model_config()}


def resolve_run_config(*, source_run_dir: Path, case_runs: list[CaseRun]) -> dict[str, Any]:
    source_manifest = read_json_dict(source_run_dir / "run_manifest.json")
    if isinstance(source_manifest, dict) and isinstance(source_manifest.get("run_config"), dict):
        return {"source": "source_run_manifest", **source_manifest["run_config"]}

    for case_run in case_runs:
        input_manifest = read_json_dict(case_run.case_run_dir / "input_manifest.json")
        if isinstance(input_manifest, dict) and isinstance(input_manifest.get("run_config"), dict):
            return {"source": "case_input_manifest", **input_manifest["run_config"]}

    return {"source": "publish_result_inferred"}


def read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def sanitize_result_id(value: str) -> str:
    sanitized = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "-" for ch in value.strip())
    sanitized = sanitized.strip(".-_")
    if not sanitized:
        raise SystemExit("result id 不能为空。")
    return sanitized


def relative_posix(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a sanitized benchmark run result snapshot.")
    parser.add_argument("--run-id", required=True, help="Source run id under runs/.")
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"), help="Directory containing raw run artifacts.")
    parser.add_argument("--results-dir", default=str(BENCHMARK_DIR / "results"), help="Directory for committed result snapshots.")
    parser.add_argument("--name", default=None, help="Result snapshot id. Defaults to --run-id.")
    parser.add_argument("--subject-model", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing result snapshot with the same name.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
