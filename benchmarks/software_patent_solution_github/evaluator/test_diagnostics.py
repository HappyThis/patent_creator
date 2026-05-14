from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from run_case import build_diagnostics, forbidden_document_edit_sections, round_failed


def event(event_type: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, payload=payload, round_id="round_1")


def test_build_diagnostics_counts_tool_failures() -> None:
    diagnostics = build_diagnostics(
        [
            event(
                "tool_result",
                {
                    "tool": "document_edit",
                    "status": "failed",
                    "output": {"code": "duplicate_section_id", "message": "section_id 已存在。"},
                },
            ),
            event(
                "tool_result",
                {
                    "tool": "document_edit",
                    "status": "failed",
                    "output": {"code": "invalid_operation", "message": "参数错误。"},
                },
            ),
            event(
                "tool_result",
                {
                    "tool": "document_read",
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
    assert diagnostics["tool_failure_count"] == 3
    assert diagnostics["document_edit_failure_count"] == 2
    assert diagnostics["duplicate_section_id_count"] == 1
    assert diagnostics["invalid_operation_count"] == 1
    assert diagnostics["tool_failure_codes"] == {
        "duplicate_section_id": 1,
        "invalid_operation": 1,
        "section_not_found": 1,
    }
    assert diagnostics["round_failed"] is False
    assert diagnostics["round_failure_count"] == 0


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
    assert diagnostics["round_failure_count"] == 1
    assert diagnostics["round_failure_codes"] == {"llm_http_error": 1}


def test_forbidden_document_edit_sections_uses_append_child_parent_section() -> None:
    assert (
        forbidden_document_edit_sections(
            {
                "operations": [
                    {
                        "op": "append_child_section",
                        "parent_section_id": "sec_000007",
                        "section": {"type": "custom", "title": "子章节", "blocks": [], "children": []},
                    }
                ]
            },
            "sec_000007",
        )
        == set()
    )

    assert forbidden_document_edit_sections(
        {
            "operations": [
                {
                    "op": "append_child_section",
                    "section_id": "sec_000007",
                    "section": {"type": "custom", "title": "子章节", "blocks": [], "children": []},
                }
            ]
        },
        "sec_000007",
    ) == {"<missing_parent_section_id>"}
