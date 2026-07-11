from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from run_case import aggregate_agent_usage, validate_subject_workspace


def test_validate_subject_workspace_checks_structure_without_scoring_content(tmp_path: Path) -> None:
    disclosure_path = tmp_path / "data" / "projects" / "proj_1" / "disclosure.json"
    disclosure_path.parent.mkdir(parents=True)
    disclosure_path.write_text("{}\n", encoding="utf-8")

    assert validate_subject_workspace(tmp_path) == disclosure_path


def test_validate_subject_workspace_rejects_multiple_projects(tmp_path: Path) -> None:
    for project_id in ("proj_1", "proj_2"):
        path = tmp_path / "data" / "projects" / project_id / "disclosure.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="只包含一个项目"):
        validate_subject_workspace(tmp_path)


def test_aggregate_agent_usage_reads_canonical_session_events() -> None:
    events = [
        SimpleNamespace(
            type="agent_message",
            payload={"message": {"usage": {"prompt_tokens": 10, "completion_tokens": 4}}},
        ),
        SimpleNamespace(
            type="agent_message",
            payload={"message": {"usage": {"input_tokens": 3, "output_tokens": 2}}},
        ),
        SimpleNamespace(type="tool_result", payload={}),
    ]

    assert aggregate_agent_usage(events) == {"input_tokens": 13, "output_tokens": 6}

