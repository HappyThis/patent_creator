from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


RUN_SCHEMA_VERSION = "patent-technical-solution-run-v2"
EXECUTION_SCHEMA_VERSION = "patent-technical-solution-execution-v2"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def duration_ms(started_at: str | None, finished_at: str | None = None) -> int | None:
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finish = datetime.fromisoformat((finished_at or now_iso()).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((finish - start).total_seconds() * 1000))


def new_execution(
    *,
    run_id: str,
    case_id: str,
    repeat: int | None,
    agent_config: dict[str, Any],
    judge_config: dict[str, Any],
) -> dict[str, Any]:
    started_at = now_iso()
    judge = {
        **judge_config,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "thread_id": None,
        "turn_id": None,
        "sdk_version": judge_config.get("sdk_version"),
        "runtime_version": None,
        "usage": None,
    }
    judge.setdefault("requested", None)
    judge.setdefault("effective", None)
    judge.setdefault("runtime_resolution", None)
    judge.setdefault("attempts", [])
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "repeat": repeat,
        "status": "preparing",
        "started_at": started_at,
        "updated_at": started_at,
        "finished_at": None,
        "duration_ms": None,
        "agent": {
            **agent_config,
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "project_id": None,
            "session_id": None,
            "round_id": None,
            "usage": None,
        },
        "judge": judge,
        "conclusion": None,
        "error": None,
    }


def preserve_legacy_judge_attempt(execution: dict[str, Any]) -> list[dict[str, Any]]:
    judge = execution.get("judge")
    if not isinstance(judge, dict):
        raise ValueError("execution.judge must be an object")
    attempts = judge.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        judge["attempts"] = attempts
    if attempts or not judge.get("started_at") or judge.get("status") in {None, "pending", "running"}:
        return attempts

    requested = judge.get("requested")
    if not isinstance(requested, dict):
        requested = _judge_config_snapshot(judge)
    effective = judge.get("effective")
    if not isinstance(effective, dict):
        effective = _judge_config_snapshot(judge, include_runtime=True)
    attempts.append(
        {
            "attempt": 1,
            "status": judge.get("status") or "unknown",
            "started_at": judge.get("started_at"),
            "finished_at": judge.get("finished_at"),
            "duration_ms": judge.get("duration_ms"),
            "logs_path": "judge/codex_logs",
            "requested": requested,
            "effective": effective,
            "runtime_resolution": judge.get("runtime_resolution"),
            "thread_id": judge.get("thread_id"),
            "turn_id": judge.get("turn_id"),
            "usage": judge.get("usage"),
            "error": execution.get("error") if execution.get("status") != "completed" else None,
        }
    )
    return attempts


def start_judge_attempt(
    execution: dict[str, Any],
    *,
    logs_path: str | None,
    requested: dict[str, Any],
    effective: dict[str, Any] | None,
    runtime_resolution: dict[str, Any],
) -> dict[str, Any]:
    judge = execution.get("judge")
    if not isinstance(judge, dict):
        raise ValueError("execution.judge must be an object")
    attempts = preserve_legacy_judge_attempt(execution)
    attempt = {
        "attempt": len(attempts) + 1,
        "status": "running",
        "started_at": judge.get("started_at") or now_iso(),
        "finished_at": None,
        "duration_ms": None,
        "logs_path": logs_path,
        "requested": requested,
        "effective": effective,
        "runtime_resolution": runtime_resolution,
        "thread_id": None,
        "turn_id": None,
        "usage": None,
        "error": None,
    }
    attempts.append(attempt)
    judge["requested"] = requested
    judge["effective"] = effective
    judge["runtime_resolution"] = runtime_resolution
    return attempt


def finish_judge_attempt(
    execution: dict[str, Any],
    *,
    status: str,
    error: dict[str, Any] | None = None,
) -> None:
    judge = execution.get("judge")
    if not isinstance(judge, dict):
        raise ValueError("execution.judge must be an object")
    attempts = judge.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("judge attempt has not been started")
    attempt = attempts[-1]
    if not isinstance(attempt, dict) or attempt.get("status") != "running":
        raise ValueError("latest judge attempt is not running")
    attempt.update(
        {
            "status": status,
            "finished_at": judge.get("finished_at") or now_iso(),
            "duration_ms": judge.get("duration_ms"),
            "effective": judge.get("effective"),
            "thread_id": judge.get("thread_id"),
            "turn_id": judge.get("turn_id"),
            "usage": judge.get("usage"),
            "error": error,
        }
    )


def _judge_config_snapshot(value: dict[str, Any], *, include_runtime: bool = False) -> dict[str, Any]:
    keys = {"provider", "model", "reasoning_effort", "service_tier", "sdk_version"}
    if include_runtime:
        keys.update({"codex_bin", "runtime_version"})
    return {key: value[key] for key in keys if value.get(key) is not None}


def start_phase(execution: dict[str, Any], phase: str) -> None:
    timestamp = now_iso()
    execution["status"] = f"{phase}_running"
    execution["updated_at"] = timestamp
    phase_record = execution[phase]
    phase_record["status"] = "running"
    phase_record["started_at"] = timestamp
    phase_record["finished_at"] = None
    phase_record["duration_ms"] = None


def finish_phase(
    execution: dict[str, Any],
    phase: str,
    *,
    status: str,
    fields: dict[str, Any] | None = None,
) -> None:
    timestamp = now_iso()
    phase_record = execution[phase]
    phase_record["status"] = status
    phase_record["finished_at"] = timestamp
    phase_record["duration_ms"] = duration_ms(phase_record.get("started_at"), timestamp)
    if fields:
        phase_record.update(fields)
    execution["updated_at"] = timestamp


def finish_execution(
    execution: dict[str, Any],
    *,
    status: str,
    error: dict[str, Any] | None = None,
    conclusion_path: str | None = None,
) -> None:
    timestamp = now_iso()
    execution["status"] = status
    execution["updated_at"] = timestamp
    execution["finished_at"] = timestamp
    execution["duration_ms"] = duration_ms(execution.get("started_at"), timestamp)
    execution["error"] = error
    execution["conclusion"] = {"path": conclusion_path} if conclusion_path else None


def new_run_record(
    *,
    run_id: str,
    run_kind: str,
    case_ids: list[str],
    config: dict[str, Any],
    models: dict[str, Any],
    benchmark_git: dict[str, Any],
) -> dict[str, Any]:
    started_at = now_iso()
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": run_kind,
        "status": "running",
        "started_at": started_at,
        "updated_at": started_at,
        "finished_at": None,
        "duration_ms": None,
        "benchmark_git": benchmark_git,
        "config": config,
        "models": models,
        "case_ids": case_ids,
        "cases": [],
        "aggregate": None,
        "error": None,
    }


def finish_run_record(
    record: dict[str, Any],
    *,
    status: str,
    cases: list[dict[str, Any]],
    aggregate: dict[str, Any] | None,
    error: dict[str, Any] | None = None,
) -> None:
    timestamp = now_iso()
    record["status"] = status
    record["updated_at"] = timestamp
    record["finished_at"] = timestamp
    record["duration_ms"] = duration_ms(record.get("started_at"), timestamp)
    record["cases"] = cases
    record["aggregate"] = aggregate
    record["error"] = error


def aggregate_case_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in records if isinstance(item.get("total_score"), (int, float))]
    scores = [float(item["total_score"]) for item in scored]
    case_rows: list[dict[str, Any]] = []
    case_ids = sorted({str(item.get("case_id") or "") for item in records if item.get("case_id")})
    for case_id in case_ids:
        case_records = [item for item in records if str(item.get("case_id")) == case_id]
        case_scores = [
            float(item["total_score"])
            for item in case_records
            if isinstance(item.get("total_score"), (int, float))
        ]
        case_rows.append(
            {
                "case_id": case_id,
                "runs": len(case_records),
                "scored_runs": len(case_scores),
                "average_score": mean(case_scores) if case_scores else None,
                "min_score": min(case_scores) if case_scores else None,
                "max_score": max(case_scores) if case_scores else None,
                "score_stddev": pstdev(case_scores) if len(case_scores) > 1 else 0 if case_scores else None,
                "status_counts": _status_counts(case_records),
            }
        )
    aggregate = {
        "runs": len(records),
        "scored_runs": len(scores),
        "average_score": mean(scores) if scores else None,
        "min_score": min(scores) if scores else None,
        "max_score": max(scores) if scores else None,
        "score_stddev": pstdev(scores) if len(scores) > 1 else 0 if scores else None,
        "status_counts": _status_counts(records),
        "cases": case_rows,
    }
    representation = _representation_aggregate(records)
    if representation is not None:
        aggregate["representation"] = representation
        by_case = {item["case_id"]: item for item in representation["cases"]}
        for row in case_rows:
            details = by_case.get(row["case_id"])
            if details is not None:
                row["representation"] = {
                    key: value for key, value in details.items() if key != "case_id"
                }
    return aggregate


def _representation_aggregate(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    represented = [
        item
        for item in records
        if isinstance(item.get("representation_score"), (int, float))
        and isinstance(item.get("representation"), dict)
    ]
    if not represented:
        return None

    case_ids = sorted({str(item.get("case_id")) for item in records if item.get("case_id")})
    return {
        "eligible_runs": len(records),
        "scored_runs": len(represented),
        "solution_average_score": _average_numeric(represented, "solution_score"),
        "representation_average_score": _average_numeric(represented, "representation_score"),
        "modalities": {
            name: _modality_aggregate(represented, name) for name in ("figure", "formula")
        },
        "cases": [
            {
                "case_id": case_id,
                "eligible_runs": len(eligible_case_records),
                "scored_runs": len(case_records),
                "solution_average_score": _average_numeric(case_records, "solution_score"),
                "representation_average_score": _average_numeric(
                    case_records, "representation_score"
                ),
                "modalities": {
                    name: _modality_aggregate(case_records, name)
                    for name in ("figure", "formula")
                },
            }
            for case_id in case_ids
            for eligible_case_records in [
                [item for item in records if str(item.get("case_id")) == case_id]
            ]
            for case_records in [
                [item for item in represented if str(item.get("case_id")) == case_id]
            ]
        ],
    }


def _average_numeric(records: list[dict[str, Any]], key: str) -> float | None:
    values = [
        float(item[key])
        for item in records
        if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool)
    ]
    return mean(values) if values else None


def _modality_aggregate(records: list[dict[str, Any]], name: str) -> dict[str, Any]:
    channels = []
    for item in records:
        representation = item.get("representation")
        channel = representation.get(name) if isinstance(representation, dict) else None
        if isinstance(channel, dict):
            channels.append(channel)
    scores = [
        float(channel["score"])
        for channel in channels
        if isinstance(channel.get("score"), (int, float))
        and not isinstance(channel.get("score"), bool)
    ]
    used = [channel for channel in channels if channel.get("used") is True]
    used_scores = [
        float(channel["score"])
        for channel in used
        if isinstance(channel.get("score"), (int, float))
        and not isinstance(channel.get("score"), bool)
    ]
    recommended = [channel for channel in channels if channel.get("policy") == "recommended"]
    return {
        "runs": len(channels),
        "average_score": mean(scores) if scores else None,
        "used_runs": len(used),
        "use_rate": len(used) / len(channels) if channels else None,
        "used_average_score": mean(used_scores) if used_scores else None,
        "recommended_runs": len(recommended),
        "recommended_not_used_runs": sum(
            channel.get("verdict") == "not_used" for channel in recommended
        ),
        "used_partial_runs": sum(
            channel.get("verdict") == "partially_correct" for channel in used
        ),
        "used_incorrect_runs": sum(channel.get("verdict") == "incorrect" for channel in used),
    }


def _status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in records:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
