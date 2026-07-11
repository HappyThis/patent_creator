from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

from codex_judge import _collect_turn, build_judge_prompt, validate_conclusion


def test_validate_conclusion_accepts_only_the_public_three_field_contract() -> None:
    value = validate_conclusion(
        json.dumps(
            {
                "status": "scored",
                "total_score": 86,
                "evaluation_report": "## 总评\n\n方案机制完整。",
            },
            ensure_ascii=False,
        )
    )

    assert value["total_score"] == 86
    assert set(value) == {"status", "total_score", "evaluation_report"}

    with pytest.raises(ValueError, match="字段不符合"):
        validate_conclusion(
            json.dumps(
                {
                    "status": "scored",
                    "total_score": 86,
                    "evaluation_report": "ok",
                    "dimension_scores": {},
                }
            )
        )


def test_build_judge_prompt_points_codex_to_original_workspace(tmp_path: Path) -> None:
    benchmark_dir = tmp_path / "benchmark"
    source_case_dir = benchmark_dir / "cases" / "001"
    case_run_dir = benchmark_dir / "runs" / "run-1" / "cases" / "001"

    prompt = build_judge_prompt(
        case_id="001",
        case_run_dir=case_run_dir,
        source_case_dir=source_case_dir,
        benchmark_dir=benchmark_dir,
    )

    assert str(case_run_dir.resolve()) in prompt
    assert "subject/data/projects/*/disclosure.json" in prompt
    assert "prepared_environment/" in prompt
    assert "assets/figures/" in prompt
    assert "evaluated_artifact" not in prompt
    assert "不要评价图片的视觉美观" in prompt


def test_collect_turn_persists_events_and_extracts_final_answer(tmp_path: Path) -> None:
    turn = SimpleNamespace(
        id="turn-1",
        status="completed",
        error=None,
        started_at=100,
        completed_at=103,
        duration_ms=3000,
    )
    events = [
        SimpleNamespace(
            method="item/completed",
            payload=SimpleNamespace(
                turn_id="turn-1",
                item={"type": "agentMessage", "phase": "commentary", "text": "working"},
            ),
        ),
        SimpleNamespace(
            method="thread/tokenUsage/updated",
            payload=SimpleNamespace(turn_id="turn-1", token_usage={"total": {"input_tokens": 8}}),
        ),
        SimpleNamespace(
            method="item/completed",
            payload=SimpleNamespace(
                turn_id="turn-1",
                item={
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"status":"scored","total_score":88,"evaluation_report":"ok"}',
                },
            ),
        ),
        SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=turn)),
    ]

    async def stream():
        for event in events:
            yield event

    events_path = tmp_path / "events.jsonl"
    collected = asyncio.run(_collect_turn(stream(), turn_id="turn-1", events_path=events_path))

    assert collected.final_response.startswith('{"status"')
    assert collected.duration_ms == 3000
    assert collected.usage == {"total": {"input_tokens": 8}}
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 4

