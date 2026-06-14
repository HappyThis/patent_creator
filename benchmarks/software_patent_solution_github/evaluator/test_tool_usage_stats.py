from __future__ import annotations

import json
import sys
from pathlib import Path

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from tool_usage_stats import collect_run_tool_stats  # noqa: E402


def test_collect_run_tool_stats_counts_calls_failures_and_markers(tmp_path: Path) -> None:
    case_dir = tmp_path / "cases" / "001"
    subject_dir = case_dir / "subject"
    subject_dir.mkdir(parents=True)
    _write_jsonl(
        subject_dir / "session_events.jsonl",
        [
            {
                "type": "agent_message",
                "payload": {
                    "model": "deepseek-v4-pro",
                    "provider": "deepseek",
                    "thinking": "enabled",
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "file_read",
                                    "arguments": '{"path": "/tmp/prepared_repo/src/a.ts"}',
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                    },
                },
            },
            {
                "type": "tool_call",
                "payload": {"tool": "file_read", "arguments": {"path": "/tmp/prepared_repo/src/a.ts"}},
            },
            {
                "type": "tool_result",
                "payload": {
                    "tool": "file_read",
                    "status": "success",
                    "output": {
                        "content": "1 | hello",
                        "truncated": True,
                        "next_start_line": 2,
                    },
                },
            },
            {
                "type": "tool_result",
                "payload": {
                    "tool": "disclosure_edit",
                    "status": "failed",
                    "output": {"code": "invalid_tool_arguments_json", "message": "bad json"},
                },
            },
            {"type": "context_summary", "payload": {"compressed_markdown": "## 当前任务"}},
        ],
    )
    (case_dir / "result.json").write_text(
        json.dumps({"status": "artifact_extracted", "subject_status": "completed"}),
        encoding="utf-8",
    )
    (case_dir / "diagnostics.json").write_text(
        json.dumps({"rounds_run": 1}),
        encoding="utf-8",
    )

    stats = collect_run_tool_stats(tmp_path)

    assert stats["case_count"] == 1
    case_stats = stats["cases"]["001"]
    assert case_stats["assistant_tool_calls"] == {"file_read": 1}
    assert case_stats["tool_call_events"] == {"file_read": 1}
    assert case_stats["tool_failures"] == {"disclosure_edit:invalid_tool_arguments_json": 1}
    assert case_stats["prepared_repo_arg_refs"] == {"file_read": 2}
    assert case_stats["processed_markers"]["truncated"] == 1
    assert case_stats["context_events"] == {"context_summary": 1}
    assert case_stats["max_total_tokens"] == 12
    assert case_stats["result"]["status"] == "artifact_extracted"


def test_collect_run_tool_stats_supports_batch_repeat_layout(tmp_path: Path) -> None:
    first_case = tmp_path / "r01-001" / "cases" / "001"
    second_case = tmp_path / "r02-001" / "cases" / "001"
    for case_dir, tool_name in ((first_case, "file_glob"), (second_case, "file_read")):
        subject_dir = case_dir / "subject"
        subject_dir.mkdir(parents=True)
        _write_jsonl(
            subject_dir / "session_events.jsonl",
            [
                {
                    "type": "agent_message",
                    "payload": {
                        "message": {
                            "tool_calls": [{"function": {"name": tool_name, "arguments": "{}"}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        }
                    },
                }
            ],
        )

    stats = collect_run_tool_stats(tmp_path)

    assert stats["case_count"] == 2
    assert set(stats["cases"]) == {"r01-001/001", "r02-001/001"}
    assert stats["aggregate"]["assistant_tool_calls"] == {"file_glob": 1, "file_read": 1}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
