from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
EVALUATOR_DIR = Path(__file__).resolve().parent

if __package__:
    from .records import RUN_SCHEMA_VERSION, atomic_write_json, read_json_dict
    from .run_metadata import compact_dict, git_metadata
else:
    if str(EVALUATOR_DIR) not in sys.path:
        sys.path.insert(0, str(EVALUATOR_DIR))

    from records import RUN_SCHEMA_VERSION, atomic_write_json, read_json_dict  # noqa: E402
    from run_metadata import compact_dict, git_metadata  # noqa: E402


RESULT_SCHEMA_VERSION = "patent-technical-solution-result-v2"


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    repeat: int | None
    status: str
    execution_path: Path
    conclusion_path: Path | None
    total_score: int | float | None


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
    print(f"已发布 {manifest['runs']} 次执行，其中 {manifest['scored_runs']} 次有评分。")
    print("本脚本不会执行 git add、commit 或 push。")


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
    run_record = read_json_dict(source_run_dir / "run.json")
    if run_record is None or run_record.get("schema_version") != RUN_SCHEMA_VERSION:
        raise SystemExit(f"没有可发布的 v2 run：{source_run_dir}")

    normalized_result_id = sanitize_result_id(result_id or source_run_id)
    result_dir = results_dir / normalized_result_id
    if result_dir.exists():
        if not overwrite:
            raise SystemExit(f"结果目录已存在：{result_dir}。如需覆盖，请显式使用 --overwrite。")
        shutil.rmtree(result_dir)

    case_results = discover_case_results(source_run_dir=source_run_dir, run_record=run_record)
    if not case_results:
        raise SystemExit(f"run 中没有 Case 执行记录：{source_run_dir / 'run.json'}")

    result_dir.mkdir(parents=True)
    published_cases = [
        publish_case_result(case_result=item, result_dir=result_dir, source_run_dir=source_run_dir)
        for item in case_results
    ]
    manifest = build_manifest(
        result_id=normalized_result_id,
        source_run_id=source_run_id,
        source_run_dir=source_run_dir,
        runs_dir=runs_dir,
        run_record=run_record,
        published_cases=published_cases,
        metadata=metadata or {},
    )
    atomic_write_json(result_dir / "manifest.json", manifest)
    upsert_index(results_dir / "index.jsonl", manifest)
    return result_dir, manifest


def discover_case_results(*, source_run_dir: Path, run_record: dict[str, Any]) -> list[CaseResult]:
    raw_cases = run_record.get("cases")
    if not isinstance(raw_cases, list):
        raise SystemExit(f"run.json 的 cases 格式不正确：{source_run_dir / 'run.json'}")

    results: list[CaseResult] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise SystemExit("run.json 中存在非对象 Case 记录。")
        execution_rel = raw_case.get("execution")
        if not isinstance(execution_rel, str) or not execution_rel:
            raise SystemExit("run.json 中的 Case 缺少 execution 路径。")
        execution_path = resolve_inside(source_run_dir, execution_rel)
        execution = read_json_dict(execution_path)
        if execution is None:
            raise SystemExit(f"execution.json 不存在或格式无效：{execution_path}")

        conclusion_path = None
        conclusion = None
        conclusion_info = execution.get("conclusion")
        if isinstance(conclusion_info, dict) and isinstance(conclusion_info.get("path"), str):
            conclusion_path = resolve_inside(execution_path.parent, conclusion_info["path"])
            conclusion = read_json_dict(conclusion_path)
            if conclusion is None:
                raise SystemExit(f"评价结论不存在或格式无效：{conclusion_path}")

        total_score = conclusion.get("total_score") if conclusion else None
        results.append(
            CaseResult(
                case_id=str(execution.get("case_id") or raw_case.get("case_id") or "").zfill(3),
                repeat=normalize_repeat(execution.get("repeat")),
                status=str(execution.get("status") or raw_case.get("status") or "unknown"),
                execution_path=execution_path,
                conclusion_path=conclusion_path,
                total_score=total_score if isinstance(total_score, (int, float)) else None,
            )
        )
    return results


def publish_case_result(
    *,
    case_result: CaseResult,
    result_dir: Path,
    source_run_dir: Path,
) -> dict[str, Any]:
    conclusion_rel = None
    conclusion_sha256 = None
    if case_result.conclusion_path is not None:
        repeat_label = f"r{case_result.repeat:02d}" if case_result.repeat is not None else "result"
        target = result_dir / "conclusions" / case_result.case_id / f"{repeat_label}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        data = case_result.conclusion_path.read_bytes()
        target.write_bytes(data)
        conclusion_rel = target.relative_to(result_dir).as_posix()
        conclusion_sha256 = hashlib.sha256(data).hexdigest()

    return {
        "case_id": case_result.case_id,
        "repeat": case_result.repeat,
        "status": case_result.status,
        "total_score": case_result.total_score,
        "source_execution": case_result.execution_path.relative_to(source_run_dir).as_posix(),
        "conclusion": conclusion_rel,
        "conclusion_sha256": conclusion_sha256,
    }


def build_manifest(
    *,
    result_id: str,
    source_run_id: str,
    source_run_dir: Path,
    runs_dir: Path,
    run_record: dict[str, Any],
    published_cases: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    scored = [item for item in published_cases if item.get("total_score") is not None]
    included_files = ["manifest.json"]
    included_files.extend(
        str(item["conclusion"])
        for item in published_cases
        if isinstance(item.get("conclusion"), str)
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "result_id": result_id,
        "source_run_id": source_run_id,
        "source_run_dir": relative_posix(source_run_dir, runs_dir.parent),
        "published_at": _now_iso(),
        "benchmark_git": git_metadata(BENCHMARK_DIR),
        "metadata": compact_dict(metadata),
        "source_run": {
            "status": run_record.get("status"),
            "started_at": run_record.get("started_at"),
            "finished_at": run_record.get("finished_at"),
            "duration_ms": run_record.get("duration_ms"),
            "config": run_record.get("config"),
            "models": run_record.get("models"),
            "aggregate": run_record.get("aggregate"),
        },
        "case_ids": sorted({str(item["case_id"]) for item in published_cases}),
        "runs": len(published_cases),
        "scored_runs": len(scored),
        "cases": published_cases,
        "included_files": included_files,
        "not_duplicated": [
            "prepared_environment/",
            "subject/",
            "agent_logs/",
            "judge/codex_logs/",
            "execution.json",
        ],
    }


def upsert_index(index_path: Path, manifest: dict[str, Any]) -> None:
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
            "schema_version": RESULT_SCHEMA_VERSION,
            "result_id": manifest["result_id"],
            "source_run_id": manifest["source_run_id"],
            "published_at": manifest["published_at"],
            "case_ids": manifest["case_ids"],
            "runs": manifest["runs"],
            "scored_runs": manifest["scored_runs"],
            "metadata": manifest["metadata"],
        }
    )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in entries),
        encoding="utf-8",
    )


def resolve_inside(base: Path, relative_path: str) -> Path:
    candidate = (base / relative_path).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise SystemExit(f"运行记录包含越界路径：{relative_path}") from exc
    return candidate


def normalize_repeat(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SystemExit("repeat 必须是正整数或 null。")
    try:
        repeat = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit("repeat 必须是正整数或 null。") from exc
    if repeat < 1:
        raise SystemExit("repeat 必须是正整数或 null。")
    return repeat


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


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a v2 benchmark result snapshot.")
    parser.add_argument("--run-id", required=True, help="Source run id under runs/.")
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"))
    parser.add_argument("--results-dir", default=str(BENCHMARK_DIR / "results"))
    parser.add_argument("--name", default=None, help="Result snapshot id. Defaults to --run-id.")
    parser.add_argument("--subject-model", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
