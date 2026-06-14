from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from run_case import build_diagnostics, resolve_reused_subject_state, round_failed


def event(event_type: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, payload=payload, round_id="round_1")


def test_build_diagnostics_ignores_recoverable_tool_failures() -> None:
    diagnostics = build_diagnostics(
        [
            event(
                "tool_result",
                {
                    "tool": "disclosure_edit",
                    "status": "failed",
                    "output": {"code": "duplicate_section_id", "message": "section_id 已存在。"},
                },
            ),
            event(
                "tool_result",
                {
                    "tool": "disclosure_edit",
                    "status": "failed",
                    "output": {"code": "invalid_operation", "message": "参数错误。"},
                },
            ),
            event(
                "tool_result",
                {
                    "tool": "disclosure_read_section",
                    "status": "failed",
                    "output": {"code": "section_not_found", "message": "章节不存在。"},
                },
            ),
        ],
        subject_status="completed_after_refinement",
        rounds_run=2,
        artifact_extracted=True,
    )

    assert diagnostics["refinement_attempts"] == 1
    assert diagnostics["artifact_extracted"] is True
    assert diagnostics["round_failed"] is False
    assert "tool_failure_count" not in diagnostics
    assert "tool_failure_codes" not in diagnostics
    assert "disclosure_edit_failure_count" not in diagnostics


def test_build_diagnostics_counts_round_failures() -> None:
    events = [
        event(
            "agent_output",
            {
                "status": "failed",
                "code": "llm_http_error",
                "message": "模型调用失败。",
            },
        )
    ]

    diagnostics = build_diagnostics(
        events,
        subject_status="round_failed",
        rounds_run=1,
        artifact_extracted=False,
    )

    assert round_failed(events, "round_1") is True
    assert diagnostics["round_failed"] is True


def test_resolve_reused_subject_state_keeps_original_subject_status() -> None:
    existing = {
        "subject_status": "completed",
        "rounds_run": 1,
        "artifact_extracted": True,
        "tool_failure_count": 3,
        "tool_failure_codes": {"subagent_plain_response": 1},
    }

    status, diagnostics = resolve_reused_subject_state(effective_solution_markdown(), existing)

    assert status == "completed"
    assert status != "reused"
    assert diagnostics["artifact_extracted"] is True
    assert "tool_failure_count" not in diagnostics
    assert "tool_failure_codes" not in diagnostics


def test_resolve_reused_subject_state_without_diagnostics_uses_artifact_quality() -> None:
    status, diagnostics = resolve_reused_subject_state(effective_solution_markdown(), None)

    assert status == "completed"
    assert diagnostics["subject_status"] == "completed"
    assert diagnostics["artifact_extracted"] is True

    empty_status, empty_diagnostics = resolve_reused_subject_state("## 技术方案\n\n太短。", None)

    assert empty_status == "skipped_no_solution_artifact"
    assert empty_diagnostics["subject_status"] == "skipped_no_solution_artifact"
    assert empty_diagnostics["artifact_extracted"] is False


def test_resolve_reused_subject_state_recovers_artifact_after_round_failure() -> None:
    existing = {
        "subject_status": "round_failed",
        "rounds_run": 2,
        "artifact_extracted": True,
        "round_failed": True,
    }

    status, diagnostics = resolve_reused_subject_state(effective_solution_markdown(), existing)

    assert status == "completed"
    assert diagnostics["subject_status"] == "completed"
    assert diagnostics["artifact_extracted"] is True
    assert diagnostics["round_failed"] is True


def effective_solution_markdown() -> str:
    return "## 技术方案\n\n" + (
        "系统通过模块、流程、接口、状态和数据处理机制生成技术方案。"
        "该机制包含多个步骤，用于保证技术方案内容可实施。"
        * 8
    )
