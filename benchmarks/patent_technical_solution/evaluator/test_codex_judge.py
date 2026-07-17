from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from .codex_judge import (
    JUDGE_SCHEMA,
    REPRESENTATION_JUDGE_SCHEMA,
    _build_codex_config,
    _collect_turn,
    build_judge_prompt,
    judge_schema,
    validate_conclusion,
)


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


def test_representation_conclusion_enforces_independent_optional_and_recommended_rules() -> None:
    value = validate_conclusion(
        json.dumps(
            {
                "status": "scored",
                "solution_score": 90,
                "representation": {
                    "figure": {
                        "used": False,
                        "score": 100,
                        "verdict": "not_used",
                        "assessment": "应画但未画。",
                    },
                    "formula": {
                        "used": False,
                        "score": 0,
                        "verdict": "not_used",
                        "assessment": "本题无需公式。",
                    },
                },
                "evaluation_report": "## 总评\n\n技术方案成立，但缺少推荐配图。",
            },
            ensure_ascii=False,
        ),
        track_id="representation_semantics",
        representation_policies={"figure": "recommended", "formula": "optional"},
    )

    assert value["total_score"] == 84
    assert value["representation_score"] == 70
    assert value["representation"]["figure"]["policy"] == "recommended"
    assert value["representation"]["formula"]["policy"] == "optional"
    assert value["representation"]["figure"]["score"] == 40
    assert value["representation"]["formula"]["score"] == 100


def test_representation_conclusion_normalizes_unpenalized_wrong_or_missing_usage() -> None:
    payload = {
        "status": "scored",
        "solution_score": 90,
        "representation": {
            "figure": {
                "used": False,
                "score": 100,
                "verdict": "not_used",
                "assessment": "missing",
            },
            "formula": {
                "used": False,
                "score": 100,
                "verdict": "not_used",
                "assessment": "not needed",
            },
        },
        "evaluation_report": "report",
    }

    normalized = validate_conclusion(
        json.dumps(payload),
        track_id="representation_semantics",
        representation_policies={"figure": "recommended", "formula": "optional"},
    )
    assert normalized["representation"]["figure"]["score"] == 40
    assert normalized["representation"]["formula"]["score"] == 100

    payload["representation"]["figure"] = {
        "used": True,
        "score": 100,
        "verdict": "incorrect",
        "assessment": "wrong",
    }
    normalized = validate_conclusion(
        json.dumps(payload),
        track_id="representation_semantics",
        representation_policies={"figure": "recommended", "formula": "optional"},
    )
    assert normalized["representation"]["figure"]["score"] == 39
    assert normalized["total_score"] == 83.85


def test_representation_conclusion_clamps_partial_score_to_published_band() -> None:
    payload = {
        "status": "scored",
        "solution_score": 80,
        "representation": {
            "figure": {
                "used": True,
                "score": 99,
                "verdict": "partially_correct",
                "assessment": "有一处实际关系表达不清。",
            },
            "formula": {
                "used": True,
                "score": 1,
                "verdict": "partially_correct",
                "assessment": "公式存在局部歧义。",
            },
        },
        "evaluation_report": "report",
    }

    value = validate_conclusion(
        json.dumps(payload),
        track_id="representation_semantics",
        representation_policies={"figure": "optional", "formula": "recommended"},
    )

    assert value["representation"]["figure"]["score"] == 79
    assert value["representation"]["formula"]["score"] == 40


def test_optional_used_correct_is_always_full_credit() -> None:
    payload = {
        "status": "scored",
        "solution_score": 80,
        "representation": {
            "figure": {
                "used": True,
                "score": 3,
                "verdict": "correct",
                "assessment": "图较简单但所画关系正确。",
            },
            "formula": {
                "used": False,
                "score": 0,
                "verdict": "not_used",
                "assessment": "未使用公式。",
            },
        },
        "evaluation_report": "report",
    }

    value = validate_conclusion(
        json.dumps(payload),
        track_id="representation_semantics",
        representation_policies={"figure": "optional", "formula": "optional"},
    )

    assert value["representation"]["figure"]["score"] == 100
    assert value["representation"]["formula"]["score"] == 100
    assert value["representation_score"] == 100


def test_recommended_correct_figure_keeps_quality_score_between_80_and_100() -> None:
    payload = {
        "status": "scored",
        "solution_score": 80,
        "representation": {
            "figure": {
                "used": True,
                "score": 86,
                "verdict": "correct",
                "assessment": "技术关系完整，但局部连线和留白仍可改善。",
            },
            "formula": {
                "used": True,
                "score": 73,
                "verdict": "correct",
                "assessment": "公式关系正确。",
            },
        },
        "evaluation_report": "report",
    }

    value = validate_conclusion(
        json.dumps(payload),
        track_id="representation_semantics",
        representation_policies={"figure": "recommended", "formula": "recommended"},
    )

    assert value["representation"]["figure"]["score"] == 86
    assert value["representation"]["formula"]["score"] == 100


def test_judge_profile_controls_schema_for_custom_track_ids() -> None:
    assert judge_schema("custom_general", judge_profile="general") is JUDGE_SCHEMA
    assert (
        judge_schema("custom_representation", judge_profile="representation_semantics")
        is REPRESENTATION_JUDGE_SCHEMA
    )


def test_representation_prompt_loads_hidden_case_specific_rules(tmp_path: Path) -> None:
    benchmark_dir = tmp_path / "benchmark"
    source_case_dir = benchmark_dir / "cases" / "003"
    case_run_dir = benchmark_dir / "runs" / "run-1" / "cases" / "003"
    track_dir = benchmark_dir / "tracks" / "representation_semantics"

    prompt = build_judge_prompt(
        case_id="003",
        case_run_dir=case_run_dir,
        source_case_dir=source_case_dir,
        benchmark_dir=benchmark_dir,
        track_id="representation_semantics",
        track_judge_path=track_dir / "judge.md",
        track_rubric_path=track_dir / "cases" / "003" / "rubric.md",
        representation_policies={"figure": "recommended", "formula": "optional"},
    )

    assert "表达专项评价规则" in prompt
    assert "figure=`recommended`" in prompt
    assert "formula=`optional`" in prompt
    assert str((track_dir / "cases" / "003" / "rubric.md").resolve()) in prompt


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


def test_codex_config_overrides_global_reasoning_effort(tmp_path: Path) -> None:
    sdk = SimpleNamespace(CodexConfig=lambda **kwargs: kwargs)

    config = _build_codex_config(
        sdk,
        case_run_dir=tmp_path,
        codex_bin="codex",
        reasoning_effort="xhigh",
    )

    assert config == {
        "cwd": str(tmp_path),
        "codex_bin": "codex",
        "config_overrides": ('model_reasoning_effort="xhigh"',),
    }


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
