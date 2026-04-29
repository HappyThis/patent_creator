from __future__ import annotations

from app.agents.workers import (
    build_consistency_reviewer_result,
    build_consistency_reviewer_context,
    build_material_analyst_result,
    build_material_analyst_context,
    build_solution_refiner_result,
    build_solution_refiner_context,
)


def test_material_analyst_context_and_envelope() -> None:
    context = build_material_analyst_context(
        goal="整理当前聊天材料",
        user_message="我们在低算力设备上做图像识别",
        outline=[],
        target_section=None,
        recent_user_inputs=[],
    )
    assert context["task"]["goal"] == "整理当前聊天材料"
    result = build_material_analyst_result(
        {
            "summary": "已整理事实。",
            "reply": "已整理事实。",
            "rationale": "基于用户聊天提炼。",
            "facts": [
                {"kind": "technical_problem", "text": "低算力下推理延迟高。"},
                {"kind": "missing", "text": ""},
            ],
            "candidate_terms": ["低算力", "", "特征提取"],
            "recommended_next_actions": [
                {"action": "write_section", "section_id": "technical_problem"},
                {"action": ""},
            ],
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


def test_solution_refiner_context_and_envelope() -> None:
    context = build_solution_refiner_context(
        goal="收敛方案",
        user_message="请整理技术方案",
        outline=[],
        target_section=None,
        recent_user_inputs=[],
    )
    assert context["task"]["user_message"] == "请整理技术方案"
    result = build_solution_refiner_result(
        {
            "summary": "方案骨架。",
            "reply": "方案骨架。",
            "rationale": "据事实收敛。",
            "solution_outline": "分三段处理：采集、推理、反馈。",
            "modules": [
                {"name": "采集模块", "responsibility": "摄像头数据接入"},
                {"name": "", "responsibility": "缺名"},
            ],
            "key_constraints": ["端侧算力有限"],
            "innovations": ["轻量化特征网络"],
            "open_questions": ["是否需要离线模型？"],
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


def test_consistency_reviewer_context_and_envelope() -> None:
    context = build_consistency_reviewer_context(
        goal="审查一致性",
        user_message="请检查",
        outline=[],
        target_section=None,
        target_section_id="technical_effects",
        recent_user_inputs=[],
    )
    assert context["task"]["target_section_id"] == "technical_effects"
    result = build_consistency_reviewer_result(
        {
            "summary": "已审查。",
            "reply": "已审查。",
            "rationale": "对比章节。",
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
