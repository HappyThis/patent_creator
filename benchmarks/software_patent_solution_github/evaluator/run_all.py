from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
RUN_CASE = Path(__file__).resolve().parent / "run_case.py"


def main() -> None:
    args = parse_args()
    benchmark = json.loads((BENCHMARK_DIR / "benchmark.json").read_text(encoding="utf-8"))
    case_ids = args.cases or benchmark.get("case_ids", [])
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
    results = run_jobs(jobs, args=args, runs_dir=runs_dir)

    batch_dir.mkdir(parents=True, exist_ok=True)
    summary_path = batch_dir / "run_summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    aggregate = aggregate_results(results, case_ids=normalized_case_ids)
    aggregate_path = batch_dir / "case_selection_summary.json"
    aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = render_report(batch_id=batch_id, aggregate=aggregate, repeats=args.repeats)
    report_path = batch_dir / "case_selection_report.md"
    report_path.write_text(report, encoding="utf-8")
    (BENCHMARK_DIR / "latest_run_report.md").write_text(report, encoding="utf-8")
    print(f"\nsummary: {summary_path}")
    print(f"report: {report_path}")


def run_jobs(jobs: list[dict[str, Any]], *, args: argparse.Namespace, runs_dir: Path) -> list[dict[str, Any]]:
    if args.workers <= 1:
        return [run_one_job(job, args=args, runs_dir=runs_dir) for job in jobs]

    results: list[dict[str, Any]] = []
    print(f"running {len(jobs)} jobs with {args.workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_job = {
            executor.submit(run_one_job, job, args=args, runs_dir=runs_dir): job
            for job in jobs
        }
        for future in as_completed(future_to_job):
            results.append(future.result())

    results.sort(key=lambda item: (int(item["repeat"]), item["case_id"]))
    return results


def run_one_job(job: dict[str, Any], *, args: argparse.Namespace, runs_dir: Path) -> dict[str, Any]:
    case_id = str(job["case_id"])
    repeat = int(job["repeat"])
    run_id = str(job["run_id"])
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
    ]
    if args.skip_judge:
        command.append("--skip-judge")
    child_env = os.environ.copy()
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    print(f"\n=== case {case_id} repeat {repeat}/{args.repeats} ===", flush=True)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        check=False,
    )
    parsed_result = parse_case_result(completed.stdout)
    result = {
        "case_id": case_id,
        "repeat": repeat,
        "run_id": run_id,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "result": parsed_result,
        "diagnostics": parsed_result.get("diagnostics") if isinstance(parsed_result, dict) else None,
    }
    print(completed.stdout)
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
    return result


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
        successes = [
            item
            for item in parsed
            if item.get("status") == "scored"
            and item.get("subject_status") in {"completed", "completed_after_refinement"}
            and item.get("diagnostics", {}).get("artifact_extracted") is True
        ]
        failure_statuses = sorted({str(item.get("status")) for item in parsed if item.get("status") != "scored"})
        row = {
            "case_id": case_id,
            "runs": len(case_results),
            "parsed_runs": len(parsed),
            "scored_runs": len(scored),
            "success_rate": len(successes) / len(case_results) if case_results else 0,
            "scores": scores,
            "average_score": mean(scores) if scores else None,
            "min_score": min(scores) if scores else None,
            "max_score": max(scores) if scores else None,
            "score_stddev": pstdev(scores) if len(scores) > 1 else 0 if scores else None,
            "failure_statuses": failure_statuses,
            "top_weaknesses": top_text_items(scored, "weaknesses"),
            "top_missing_key_mechanisms": top_text_items(scored, "missing_key_mechanisms"),
            "recommendation": "",
            "recommendation_reason": "",
        }
        recommendation, reason = recommend_case(row)
        row["recommendation"] = recommendation
        row["recommendation_reason"] = reason
        rows.append(row)
    return rows


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


def recommend_case(row: dict[str, Any]) -> tuple[str, str]:
    success_rate = float(row["success_rate"])
    avg = row["average_score"]
    stddev = row["score_stddev"]
    if success_rate < 0.67:
        return "淘汰或暂缓", "成功率过低，不适合作为第一批黄金 case。"
    if avg is None:
        return "淘汰或暂缓", "没有有效评分结果。"
    if stddev is not None and not math.isnan(float(stddev)) and float(stddev) >= 15:
        return "保留但需复核", "分数波动较大，需要检查题目、参考答案或运行链路稳定性。"
    if avg >= 85:
        return "黄金 case 候选", "成功率高且平均分较高，可优先人工复核题目质量和参考答案。"
    if avg >= 70:
        return "保留但需修改", "能稳定评分，但平均分偏中等，需要人工判断是系统短板还是 case 设计问题。"
    return "淘汰或暂缓", "平均分偏低，优先检查题目是否过难、过细或参考答案/评分标准是否不匹配。"


def render_report(*, batch_id: str, aggregate: list[dict[str, Any]], repeats: int) -> str:
    lines = [
        "# 软件专利技术方案 benchmark case 筛选报告",
        "",
        f"- 批次：`{batch_id}`",
        f"- 重复次数：`{repeats}`",
        "",
        "## 汇总表",
        "",
        "| Case | 成功率 | 平均分 | 最低分 | 最高分 | 标准差 | 建议 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in aggregate:
        lines.append(
            "| {case_id} | {success_rate:.0%} | {avg} | {min_score} | {max_score} | {stddev} | {recommendation} |".format(
                case_id=row["case_id"],
                success_rate=float(row["success_rate"]),
                avg=format_optional_number(row["average_score"]),
                min_score=format_optional_number(row["min_score"]),
                max_score=format_optional_number(row["max_score"]),
                stddev=format_optional_number(row["score_stddev"]),
                recommendation=row["recommendation"],
            )
        )
    lines.extend(["", "## 逐项判断", ""])
    for row in aggregate:
        lines.extend(
            [
                f"### Case {row['case_id']}",
                "",
                f"- 建议：{row['recommendation']}",
                f"- 理由：{row['recommendation_reason']}",
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
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_optional_number(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all software patent solution benchmark cases.")
    parser.add_argument("--cases", nargs="*", help="Optional case ids. Defaults to benchmark.json case_ids.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--round-timeout", type=int, default=1800)
    parser.add_argument("--judge-timeout", type=int, default=1800)
    parser.add_argument("--workers", type=int, default=1, help="Number of cases to run concurrently.")
    parser.add_argument("--skip-judge", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


if __name__ == "__main__":
    main()
