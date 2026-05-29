from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    args = parse_args()
    run_dir = resolve_run_dir(args.run_id, args.runs_dir)
    stats = collect_run_tool_stats(run_dir)
    if args.format == "markdown":
        print(render_markdown(stats))
    else:
        print(json.dumps(stats, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize tool usage from benchmark session_events.jsonl files.")
    parser.add_argument("run_id", help="Run id under runs/, or an absolute/relative run directory.")
    parser.add_argument("--runs-dir", default=str(BENCHMARK_DIR / "runs"), help="Directory containing benchmark runs.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    return parser.parse_args()


def resolve_run_dir(run_id: str, runs_dir: str) -> Path:
    candidate = Path(run_id).expanduser()
    if candidate.exists():
        return candidate.resolve()
    resolved = (Path(runs_dir).expanduser() / run_id).resolve()
    if not resolved.exists():
        raise SystemExit(f"run 不存在：{resolved}")
    return resolved


def collect_run_tool_stats(run_dir: Path) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    aggregate = _empty_case_stats()
    for events_path in _iter_session_event_paths(run_dir):
        case_dir = events_path.parents[1]
        case_key = _case_key(run_dir, case_dir)
        case_stats = collect_case_tool_stats(events_path)
        result_path = case_dir / "result.json"
        diagnostics_path = case_dir / "diagnostics.json"
        case_stats["result"] = _read_json_if_exists(result_path)
        case_stats["diagnostics"] = _read_json_if_exists(diagnostics_path)
        cases[case_key] = case_stats
        _merge_case_stats(aggregate, case_stats)
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "case_count": len(cases),
        "aggregate": _finalize_stats(aggregate),
        "cases": {case_id: _finalize_stats(stats) for case_id, stats in cases.items()},
    }


def _iter_session_event_paths(run_dir: Path) -> list[Path]:
    paths = {
        *run_dir.glob("cases/*/subject/session_events.jsonl"),
        *run_dir.glob("*/cases/*/subject/session_events.jsonl"),
    }
    return sorted(paths)


def _case_key(run_dir: Path, case_dir: Path) -> str:
    relative_parts = case_dir.relative_to(run_dir).parts
    if len(relative_parts) >= 3 and relative_parts[-2] == "cases":
        run_label = "/".join(relative_parts[:-2])
        return f"{run_label}/{relative_parts[-1]}"
    return relative_parts[-1]


def collect_case_tool_stats(events_path: Path) -> dict[str, Any]:
    stats = _empty_case_stats()
    stats["events_path"] = str(events_path)
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        stats["event_count"] += 1

        if event_type == "agent_message":
            _collect_agent_message_stats(stats, payload)
        elif event_type == "tool_call":
            _collect_tool_call_event_stats(stats, payload)
        elif event_type == "tool_result":
            _collect_tool_result_stats(stats, payload)
        elif event_type.startswith("context_"):
            stats["context_events"][event_type] += 1
    return stats


def _empty_case_stats() -> dict[str, Any]:
    return {
        "event_count": 0,
        "agent_message_count": 0,
        "assistant_tool_calls": Counter(),
        "tool_call_events": Counter(),
        "tool_result_events": Counter(),
        "tool_failures": Counter(),
        "prepared_repo_arg_refs": Counter(),
        "processed_markers": Counter(),
        "context_events": Counter(),
        "max_tool_result_json_chars": 0,
        "max_tool_result_tool": None,
        "max_prompt_tokens": 0,
        "max_completion_tokens": 0,
        "max_total_tokens": 0,
        "model_calls": Counter(),
        "result": {},
        "diagnostics": {},
    }


def _collect_agent_message_stats(stats: dict[str, Any], payload: dict[str, Any]) -> None:
    stats["agent_message_count"] += 1
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") if isinstance(tool_call, dict) else {}
        name = str(function.get("name") or "<unknown>")
        stats["assistant_tool_calls"][name] += 1
        arguments = function.get("arguments")
        if isinstance(arguments, str) and "prepared_repo" in arguments:
            stats["prepared_repo_arg_refs"][name] += 1

    usage = message.get("usage") if isinstance(message.get("usage"), dict) else payload.get("usage")
    if isinstance(usage, dict):
        stats["max_prompt_tokens"] = max(stats["max_prompt_tokens"], int(usage.get("prompt_tokens") or 0))
        stats["max_completion_tokens"] = max(stats["max_completion_tokens"], int(usage.get("completion_tokens") or 0))
        stats["max_total_tokens"] = max(stats["max_total_tokens"], int(usage.get("total_tokens") or 0))

    model = str(payload.get("model") or "")
    provider = str(payload.get("provider") or "")
    thinking = str(payload.get("thinking") or "")
    if model or provider:
        stats["model_calls"][f"{provider}/{model}/{thinking}".strip("/")] += 1


def _collect_tool_call_event_stats(stats: dict[str, Any], payload: dict[str, Any]) -> None:
    tool = str(payload.get("tool") or "<unknown>")
    stats["tool_call_events"][tool] += 1
    if "prepared_repo" in json.dumps(payload.get("arguments"), ensure_ascii=False, default=str):
        stats["prepared_repo_arg_refs"][tool] += 1


def _collect_tool_result_stats(stats: dict[str, Any], payload: dict[str, Any]) -> None:
    tool = str(payload.get("tool") or "<unknown>")
    stats["tool_result_events"][tool] += 1
    output = payload.get("output")
    output_json = json.dumps(output, ensure_ascii=False, default=str)
    output_chars = len(output_json)
    if output_chars > stats["max_tool_result_json_chars"]:
        stats["max_tool_result_json_chars"] = output_chars
        stats["max_tool_result_tool"] = tool
    if isinstance(output, dict):
        code = output.get("code")
        if isinstance(code, str) and code:
            stats["tool_failures"][f"{tool}:{code}"] += 1
        _collect_processed_markers(stats["processed_markers"], output)


def _collect_processed_markers(counter: Counter[str], value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_truncated") and item is True:
                counter[key] += 1
            if key == "truncated" and item is True:
                counter[key] += 1
            if key.endswith("_path") and isinstance(item, str) and item:
                counter[key] += 1
            if key == "preview_policy":
                counter[key] += 1
            _collect_processed_markers(counter, item)
    elif isinstance(value, list):
        for item in value:
            _collect_processed_markers(counter, item)


def _merge_case_stats(total: dict[str, Any], case_stats: dict[str, Any]) -> None:
    for key in ("event_count", "agent_message_count"):
        total[key] += int(case_stats.get(key) or 0)
    for key in (
        "assistant_tool_calls",
        "tool_call_events",
        "tool_result_events",
        "tool_failures",
        "prepared_repo_arg_refs",
        "processed_markers",
        "context_events",
        "model_calls",
    ):
        total[key].update(case_stats.get(key) or {})
    for key in ("max_prompt_tokens", "max_completion_tokens", "max_total_tokens"):
        total[key] = max(total[key], int(case_stats.get(key) or 0))
    if int(case_stats.get("max_tool_result_json_chars") or 0) > total["max_tool_result_json_chars"]:
        total["max_tool_result_json_chars"] = int(case_stats.get("max_tool_result_json_chars") or 0)
        total["max_tool_result_tool"] = case_stats.get("max_tool_result_tool")


def _finalize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(stats)
    for key, value in list(finalized.items()):
        if isinstance(value, Counter):
            finalized[key] = dict(value.most_common())
    return finalized


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_error": "invalid_json", "path": str(path)}


def render_markdown(stats: dict[str, Any]) -> str:
    aggregate = stats["aggregate"]
    lines = [
        f"# Tool Usage Stats: {stats['run_id']}",
        "",
        f"- cases: {stats['case_count']}",
        f"- agent messages: {aggregate['agent_message_count']}",
        f"- max total tokens: {aggregate['max_total_tokens']}",
        f"- max tool result chars: {aggregate['max_tool_result_json_chars']} ({aggregate['max_tool_result_tool']})",
        "",
        "## Aggregate Tool Calls",
        "",
    ]
    for tool, count in aggregate["assistant_tool_calls"].items():
        lines.append(f"- {tool}: {count}")
    lines.extend(["", "## Failures", ""])
    if aggregate["tool_failures"]:
        for failure, count in aggregate["tool_failures"].items():
            lines.append(f"- {failure}: {count}")
    else:
        lines.append("- none")
    lines.extend(["", "## Cases", ""])
    for case_id, case_stats in stats["cases"].items():
        result = case_stats.get("result") or {}
        diagnostics = case_stats.get("diagnostics") or {}
        lines.append(
            "- {case}: status={status}, subject={subject}, rounds={rounds}, agent_messages={messages}, "
            "max_total_tokens={tokens}, max_tool_chars={tool_chars}".format(
                case=case_id,
                status=result.get("status", "-"),
                subject=result.get("subject_status", "-"),
                rounds=diagnostics.get("rounds_run", "-"),
                messages=case_stats["agent_message_count"],
                tokens=case_stats["max_total_tokens"],
                tool_chars=case_stats["max_tool_result_json_chars"],
            )
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
