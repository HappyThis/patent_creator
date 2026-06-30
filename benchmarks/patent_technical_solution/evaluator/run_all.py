from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
RUN_CASE = Path(__file__).resolve().parent / "run_case.py"
EVALUATOR_DIR = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

try:
    from .process_utils import terminate_process_group  # type: ignore[import-not-found]
    from .run_metadata import build_run_manifest  # type: ignore[import-not-found]
except ImportError:
    from process_utils import terminate_process_group  # type: ignore[no-redef]
    from run_metadata import build_run_manifest  # type: ignore[no-redef]


def main() -> None:
    args = parse_args()
    case_ids = args.cases or discover_case_ids()
    batch_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    runs_dir = Path(args.runs_dir).resolve()
    batch_dir = runs_dir / batch_id
    normalized_case_ids = [str(case_id).zfill(3) for case_id in case_ids]
    jobs = [
        {
            "case_id": case_id,
            "repeat": repeat,
            "run_id": f"{batch_id}/r{repeat:02d}-{case_id}",
        }
        for repeat in range(1, args.repeats + 1)
        for case_id in normalized_case_ids
    ]
    batch_dir.mkdir(parents=True, exist_ok=True)
    run_manifest = build_run_manifest(
        run_id=batch_id,
        run_kind="batch",
        run_config={
            "cases": normalized_case_ids,
            "repeats": args.repeats,
            "workers": args.workers,
            "mode": args.mode,
            "round_timeout_seconds": args.round_timeout,
            "judge_timeout_seconds": args.judge_timeout,
            "skip_judge": bool(args.skip_judge),
        },
        case_ids=normalized_case_ids,
    )
    write_json(batch_dir / "run_manifest.json", run_manifest)
    results = run_jobs(jobs, args=args, runs_dir=runs_dir)

    summary_path = batch_dir / "run_summary.json"
    write_json(summary_path, results)
    aggregate = aggregate_results(results, case_ids=normalized_case_ids)
    aggregate_path = batch_dir / "evaluation_summary.json"
    write_json(aggregate_path, aggregate)
    report = render_report(
        batch_id=batch_id,
        aggregate=aggregate,
        repeats=args.repeats,
        model_config=run_manifest["model_config"],
        run_config=run_manifest["run_config"],
    )
    report_path = batch_dir / "evaluation_report.md"
    report_path.write_text(report, encoding="utf-8")
    (BENCHMARK_DIR / "latest_run_report.md").write_text(report, encoding="utf-8")
    print(f"\nsummary: {summary_path}")
    print(f"report: {report_path}")


def run_jobs(jobs: list[dict[str, Any]], *, args: argparse.Namespace, runs_dir: Path) -> list[dict[str, Any]]:
    if args.workers <= 1:
        return [run_one_job(job, args=args, runs_dir=runs_dir) for job in jobs]

    results: list[dict[str, Any]] = []
    print(f"running {len(jobs)} jobs with {args.workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="bench-worker") as executor:
        future_to_job = {
            executor.submit(run_one_job, job, args=args, runs_dir=runs_dir): job
            for job in jobs
        }
        for future in as_completed(future_to_job):
            results.append(future.result())

    results.sort(key=lambda item: (int(item["repeat"]), item["case_id"]))
    return results


def discover_case_ids() -> list[str]:
    return [path.name for path in sorted((BENCHMARK_DIR / "cases").glob("[0-9][0-9][0-9]"))]


def run_one_job(job: dict[str, Any], *, args: argparse.Namespace, runs_dir: Path) -> dict[str, Any]:
    case_id = str(job["case_id"])
    repeat = int(job["repeat"])
    run_id = str(job["run_id"])
    run_label = Path(run_id).name
    worker_label = threading.current_thread().name
    log_prefix = f"[worker={worker_label} run={run_label} case={case_id} repeat={repeat}/{args.repeats}]"
    command = [
        sys.executable,
        str(RUN_CASE),
        "--case",
        case_id,
        "--run-id",
        run_id,
        "--runs-dir",
        str(runs_dir),
        "--round-timeout",
        str(args.round_timeout),
        "--judge-timeout",
        str(args.judge_timeout),
        "--mode",
        str(args.mode),
    ]
    if args.skip_judge:
        command.append("--skip-judge")
    child_env = os.environ.copy()
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    with PRINT_LOCK:
        print(f"\n=== {log_prefix} started ===", flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        start_new_session=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=stream_pipe,
        args=(process.stdout, stdout_chunks, sys.stdout, f"{log_prefix} stdout"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stream_pipe,
        args=(process.stderr, stderr_chunks, sys.stderr, f"{log_prefix} stderr"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait()
    except BaseException:
        terminate_process_group(process)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        raise
    stdout_thread.join()
    stderr_thread.join()
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    case_run_dir = runs_dir / run_id / "cases" / case_id
    stdout_path = write_text_if_present(case_run_dir / "run_case_stdout.txt", stdout)
    stderr_path = write_text_if_present(case_run_dir / "run_case_stderr.txt", stderr)
    parsed_result = read_case_result(case_run_dir) or parse_case_result(stdout)
    with PRINT_LOCK:
        print(f"=== {log_prefix} finished returncode={returncode} ===", flush=True)
    result = {
        "case_id": case_id,
        "repeat": repeat,
        "run_id": run_id,
        "log_prefix": log_prefix,
        "returncode": returncode,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stderr_path": str(stderr_path) if stderr_path else None,
        "result": parsed_result,
        "diagnostics": parsed_result.get("diagnostics") if isinstance(parsed_result, dict) else None,
    }
    return result


def read_case_result(case_run_dir: Path) -> dict[str, Any] | None:
    result_path = case_run_dir / "result.json"
    if not result_path.exists():
        return None
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def write_text_if_present(path: Path, text: str) -> Path | None:
    if not text:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stream_pipe(pipe: Any, chunks: list[str], target: Any, prefix: str) -> None:
    if pipe is None:
        return
    for line in pipe:
        chunks.append(line)
        with PRINT_LOCK:
            print(f"{prefix} {line}", end="", file=target, flush=True)


def parse_case_result(stdout: str | None) -> dict[str, Any] | None:
    if not stdout:
        return None
    text = stdout.strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    index = text.rfind("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index = text.rfind("{", 0, index)
            continue
        if text[index + end :].strip() == "" and isinstance(value, dict):
            return value
        index = text.rfind("{", 0, index)
    return None


def aggregate_results(results: list[dict[str, Any]], *, case_ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for case_id in case_ids:
        case_results = [item for item in results if item["case_id"] == case_id]
        parsed = [item.get("result") for item in case_results if isinstance(item.get("result"), dict)]
        scored = [item for item in parsed if item.get("status") == "scored" and isinstance(item.get("judge"), dict)]
        scores = [float(item["judge"]["total_score"]) for item in scored if item.get("judge", {}).get("total_score") is not None]
        artifact_successes = [
            item
            for item in parsed
            if mode_output_extracted(item)
            and item.get("subject_status") in {"completed", "completed_after_refinement"}
        ]
        status_counts = count_statuses(parsed)
        failure_statuses = sorted({str(item.get("status")) for item in parsed if item.get("status") != "scored"})
        artifact_success_rate = len(artifact_successes) / len(case_results) if case_results else 0
        row = {
            "case_id": case_id,
            "runs": len(case_results),
            "parsed_runs": len(parsed),
            "scored_runs": len(scored),
            "artifact_success_rate": artifact_success_rate,
            "artifact_success_runs": len(artifact_successes),
            "status_counts": status_counts,
            "scores": scores,
            "average_score": mean(scores) if scores else None,
            "min_score": min(scores) if scores else None,
            "max_score": max(scores) if scores else None,
            "score_stddev": pstdev(scores) if len(scores) > 1 else 0 if scores else None,
            "failure_statuses": failure_statuses,
            "top_weaknesses": top_text_items(scored, "weaknesses"),
            "top_missing_key_mechanisms": top_text_items(scored, "missing_key_mechanisms"),
            "top_missing_visual_mechanisms": top_text_items(scored, "missing_visual_mechanisms"),
            "top_figure_issues": top_text_items(scored, "figure_issues"),
            "top_shape_issues": top_text_items(scored, "shape_issues"),
            "top_layout_issues": top_text_items(scored, "layout_issues"),
            "top_connector_issues": top_text_items(scored, "connector_issues"),
            "top_text_issues": top_text_items(scored, "text_issues"),
            "top_score_caps_applied": top_text_items(scored, "score_caps_applied"),
            "solution_scores": numeric_judge_values(scored, "solution_score"),
            "figure_scores": numeric_judge_values(scored, "figure_score"),
            "integration_scores": numeric_judge_values(scored, "integration_score"),
            "shape_scores": numeric_nested_judge_values(scored, "visual_quality_scores", "shape_score"),
            "layout_scores": numeric_nested_judge_values(scored, "visual_quality_scores", "layout_score"),
            "connector_scores": numeric_nested_judge_values(scored, "visual_quality_scores", "connector_score"),
            "text_scores": numeric_nested_judge_values(scored, "visual_quality_scores", "text_score"),
        }
        rows.append(row)
    return rows


def mode_output_extracted(result: dict[str, Any]) -> bool:
    diagnostics = result.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    if "mode_output_extracted" in diagnostics:
        return diagnostics.get("mode_output_extracted") is True
    return diagnostics.get("artifact_extracted") is True


def count_statuses(parsed: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in parsed:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def top_text_items(scored: list[dict[str, Any]], key: str, *, limit: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for item in scored:
        values = item.get("judge", {}).get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value).strip()
            if text:
                counts[text] = counts.get(text, 0) + 1
    return [text for text, _count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]


def numeric_judge_values(scored: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in scored:
        value = item.get("judge", {}).get(key)
        if isinstance(value, int | float):
            values.append(float(value))
    return values


def numeric_nested_judge_values(scored: list[dict[str, Any]], parent_key: str, key: str) -> list[float]:
    values: list[float] = []
    for item in scored:
        parent = item.get("judge", {}).get(parent_key)
        if not isinstance(parent, dict):
            continue
        value = parent.get(key)
        if isinstance(value, int | float):
            values.append(float(value))
    return values


def render_report(
    *,
    batch_id: str,
    aggregate: list[dict[str, Any]],
    repeats: int,
    model_config: dict[str, Any] | None = None,
    run_config: dict[str, Any] | None = None,
) -> str:
    lines = [
        "# 专利交底书技术方案 benchmark 批量评估报告",
        "",
        f"- 批次：`{batch_id}`",
        f"- 重复次数：`{repeats}`",
    ]
    subject = model_config.get("subject", {}) if isinstance(model_config, dict) else {}
    context = model_config.get("context_compression", {}) if isinstance(model_config, dict) else {}
    runtime = model_config.get("runtime", {}) if isinstance(model_config, dict) else {}
    if subject:
        lines.extend(
            [
                f"- Subject API：`{subject.get('api', 'responses')}`",
                f"- Subject 模型：`{subject.get('model', '-')}`",
                f"- Base URL：`{subject.get('base_url', '-')}`",
                f"- Reasoning effort：`{subject.get('reasoning_effort', '-')}`；max_output_tokens：`{subject.get('max_output_tokens', '-')}`",
                f"- Web search：`{subject.get('web_search_enabled', '-')}`；context_size：`{subject.get('web_search_context_size', '-')}`",
            ]
        )
    if context:
        lines.append(
            "- 压缩配置：max_tokens=`{max_tokens}`，threshold_ratio=`{ratio}`，token_char_coefficient=`{coef}`".format(
                max_tokens=context.get("max_tokens", "-"),
                ratio=context.get("compress_threshold_ratio", "-"),
                coef=context.get("token_char_coefficient", "-"),
            )
        )
    if runtime:
        lines.append(
            "- Runtime：llm_timeout=`{timeout}`，llm_max_retries=`{retries}`，compression_timeout=`{compression_timeout}`".format(
                timeout=runtime.get("llm_timeout", "-"),
                retries=runtime.get("llm_max_retries", "-"),
                compression_timeout=context.get("compression_timeout", "-") if context else "-",
            )
        )
    if run_config:
        lines.append(
            "- 运行参数：mode=`{mode}`，workers=`{workers}`，round_timeout=`{round_timeout}`，judge_timeout=`{judge_timeout}`，skip_judge=`{skip_judge}`".format(
                mode=run_config.get("mode", "solution"),
                workers=run_config.get("workers", "-"),
                round_timeout=run_config.get("round_timeout_seconds", "-"),
                judge_timeout=run_config.get("judge_timeout_seconds", "-"),
                skip_judge=run_config.get("skip_judge", "-"),
            )
        )
    lines.extend(
        [
            "",
            "## 汇总表",
            "",
            "| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in aggregate:
        lines.append(
            "| {case_id} | {runs} | {artifact_success_runs} ({artifact_success_rate:.0%}) | {scored_runs} | {avg} | {min_score} | {max_score} | {stddev} | {status_counts} |".format(
                case_id=row["case_id"],
                runs=row["runs"],
                artifact_success_runs=row["artifact_success_runs"],
                artifact_success_rate=float(row["artifact_success_rate"]),
                scored_runs=row["scored_runs"],
                avg=format_optional_number(row["average_score"]),
                min_score=format_optional_number(row["min_score"]),
                max_score=format_optional_number(row["max_score"]),
                stddev=format_optional_number(row["score_stddev"]),
                status_counts=format_status_counts(row["status_counts"]),
            )
        )
    lines.extend(["", "## 逐项结果", ""])
    for row in aggregate:
        lines.extend(
            [
                f"### Case {row['case_id']}",
                "",
                f"- 运行次数：{row['runs']}",
                f"- 产物成功：{row['artifact_success_runs']} ({float(row['artifact_success_rate']):.0%})",
                f"- 已评分次数：{row['scored_runs']}",
                f"- 状态分布：{format_status_counts(row['status_counts'])}",
                f"- 分数：{', '.join(format_optional_number(score) for score in row['scores']) or '无'}",
            ]
        )
        if row["failure_statuses"]:
            lines.append(f"- 失败状态：{', '.join(row['failure_statuses'])}")
        if row["top_weaknesses"]:
            lines.append("- 主要扣分点：")
            for item in row["top_weaknesses"][:3]:
                lines.append(f"  - {item}")
        if row["top_missing_key_mechanisms"]:
            lines.append("- 主要缺失机制：")
            for item in row["top_missing_key_mechanisms"][:3]:
                lines.append(f"  - {item}")
        if row["top_missing_visual_mechanisms"]:
            lines.append("- 主要缺失视觉机制：")
            for item in row["top_missing_visual_mechanisms"][:3]:
                lines.append(f"  - {item}")
        if row["top_figure_issues"]:
            lines.append("- 主要附图问题：")
            for item in row["top_figure_issues"][:3]:
                lines.append(f"  - {item}")
        if row["top_shape_issues"]:
            lines.append("- 主要形状问题：")
            for item in row["top_shape_issues"][:3]:
                lines.append(f"  - {item}")
        if row["top_layout_issues"]:
            lines.append("- 主要布局问题：")
            for item in row["top_layout_issues"][:3]:
                lines.append(f"  - {item}")
        if row["top_connector_issues"]:
            lines.append("- 主要连接/箭头问题：")
            for item in row["top_connector_issues"][:3]:
                lines.append(f"  - {item}")
        if row["top_text_issues"]:
            lines.append("- 主要文字问题：")
            for item in row["top_text_issues"][:3]:
                lines.append(f"  - {item}")
        if row["top_score_caps_applied"]:
            lines.append("- 适用封顶规则：")
            for item in row["top_score_caps_applied"][:3]:
                lines.append(f"  - {item}")
        subscore_lines = []
        if row["solution_scores"]:
            subscore_lines.append(f"solution={format_optional_number(mean(row['solution_scores']))}")
        if row["figure_scores"]:
            subscore_lines.append(f"figure={format_optional_number(mean(row['figure_scores']))}")
        if row["integration_scores"]:
            subscore_lines.append(f"integration={format_optional_number(mean(row['integration_scores']))}")
        if subscore_lines:
            lines.append(f"- 子分平均：{', '.join(subscore_lines)}")
        visual_score_lines = []
        if row["shape_scores"]:
            visual_score_lines.append(f"shape={format_optional_number(mean(row['shape_scores']))}")
        if row["layout_scores"]:
            visual_score_lines.append(f"layout={format_optional_number(mean(row['layout_scores']))}")
        if row["connector_scores"]:
            visual_score_lines.append(f"connector={format_optional_number(mean(row['connector_scores']))}")
        if row["text_scores"]:
            visual_score_lines.append(f"text={format_optional_number(mean(row['text_scores']))}")
        if visual_score_lines:
            lines.append(f"- 视觉子分平均：{', '.join(visual_score_lines)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_optional_number(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def format_status_counts(status_counts: dict[str, int]) -> str:
    if not status_counts:
        return "-"
    return ", ".join(f"{status}:{count}" for status, count in sorted(status_counts.items()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all patent technical solution benchmark cases.")
    parser.add_argument("--cases", nargs="*", help="Optional case ids. Defaults to all cases under cases/.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--round-timeout", type=int, default=1800)
    parser.add_argument("--judge-timeout", type=int, default=1800)
    parser.add_argument("--mode", choices=("solution", "figure", "combined"), default="solution")
    parser.add_argument("--workers", type=int, default=1, help="Number of cases to run concurrently.")
    parser.add_argument("--skip-judge", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


if __name__ == "__main__":
    main()
