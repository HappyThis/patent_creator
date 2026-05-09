from __future__ import annotations

from app.agents.workers import (
    build_consistency_reviewer_result,
    build_material_analyst_result,
    build_solution_refiner_result,
    build_section_writer_result,
)


def test_material_analyst_envelope() -> None:
    result = build_material_analyst_result(
        {
            "summary": "已整理事实。",
            "reply": "已整理事实。",
            "rationale": "基于用户聊天提炼。",
            "proposal_type": "analysis_result",
            "proposal": {
                "facts": [
                    {"kind": "technical_problem", "text": "低算力下推理延迟高。"},
                    {"kind": "missing", "text": ""},
                ],
                "candidate_terms": ["低算力", "", "特征提取"],
                "recommended_next_actions": [
                    {"action": "write_section", "section_id": "technical_problem"},
                    {"action": ""},
                ],
            },
            "questions": ["需要明确硬件型号吗？"],
            "warnings": [],
        }
    )
    assert result["status"] == "success"
    assert result["proposal"]["type"] == "analysis_result"
    # 字段过滤：kind 为 missing 但 text 空的被剔除
    assert len(result["proposal"]["facts"]) == 1
    assert result["proposal"]["candidate_terms"] == ["低算力", "特征提取"]
    # action 为空串的被剔除
    assert len(result["proposal"]["recommended_next_actions"]) == 1
    assert result["questions"] == ["需要明确硬件型号吗？"]


def test_solution_refiner_envelope() -> None:
    result = build_solution_refiner_result(
        {
            "summary": "方案骨架。",
            "reply": "方案骨架。",
            "rationale": "据事实收敛。",
            "proposal_type": "analysis_result",
            "proposal": {
                "solution_outline": "分三段处理：采集、推理、反馈。",
                "modules": [
                    {"name": "采集模块", "responsibility": "摄像头数据接入"},
                    {"name": "", "responsibility": "缺名"},
                ],
                "key_constraints": ["端侧算力有限"],
                "innovations": ["轻量化特征网络"],
                "open_questions": ["是否需要离线模型？"],
            },
            "questions": [],
            "warnings": ["部分性能数据未验证"],
        }
    )
    assert result["status"] == "success"
    proposal = result["proposal"]
    assert proposal["type"] == "analysis_result"
    assert proposal["solution_outline"].startswith("分三段处理")
    assert len(proposal["modules"]) == 1
    assert proposal["modules"][0]["name"] == "采集模块"
    assert proposal["innovations"] == ["轻量化特征网络"]
    assert proposal["open_questions"] == ["是否需要离线模型？"]


def test_consistency_reviewer_envelope() -> None:
    result = build_consistency_reviewer_result(
        {
            "summary": "已审查。",
            "reply": "已审查。",
            "rationale": "对比章节。",
            "proposal_type": "review_report",
            "proposal": {
                "issues": [
                    {
                        "severity": "high",
                        "section_id": "technical_effects",
                        "block_id": None,
                        "message": "效果未呼应问题中的实时性。",
                        "suggested_fix": "补充低延迟描述。",
                    },
                    {"severity": "unknown", "section_id": "", "block_id": "", "message": "术语不统一。", "suggested_fix": ""},
                    {"severity": "low", "section_id": None, "block_id": None, "message": "", "suggested_fix": ""},
                ],
            },
            "questions": [],
            "warnings": [],
        }
    )
    assert result["status"] == "success"
    proposal = result["proposal"]
    assert proposal["type"] == "review_report"
    # 无效 severity 归一到 medium，空 message 被剔除
    assert len(proposal["issues"]) == 2
    assert proposal["issues"][0]["severity"] == "high"
    assert proposal["issues"][1]["severity"] == "medium"
    assert proposal["issues"][1]["section_id"] is None


def test_section_writer_envelope_uses_proposal_target_not_task_context() -> None:
    result = build_section_writer_result(
        {
            "summary": "已生成。",
            "reply": "已生成。",
            "rationale": "根据任务生成。",
            "proposal_type": "document_edit_proposal",
            "proposal": {
                "target_section_id": "technical_solution",
                "target_block_id": None,
                "intent": "replace_section_blocks",
                "confidence": 0.8,
                "operations": [
                    {
                        "op": "replace_section_blocks",
                        "section_id": "technical_solution",
                        "blocks": [{"type": "paragraph", "text": "候选正文。"}],
                    }
                ],
            },
            "questions": [],
            "warnings": [],
        }
    )

    assert result["status"] == "success"
    assert result["proposal"]["target_section_id"] == "technical_solution"
    assert result["proposal"]["operations"][0]["section_id"] == "technical_solution"
