from __future__ import annotations

from typing import Any

from ...core import ApiError


def build_section_writer_context(
    *,
    target_section_id: str,
    target_block_id: str | None,
    goal: str,
    user_message: str,
    outline: list[dict[str, Any]],
    section: dict[str, Any] | None,
    recent_user_inputs: list[str],
) -> dict[str, Any]:
    return {
        "task": {
            "goal": goal,
            "user_message": user_message,
            "target_section_id": target_section_id,
            "target_block_id": target_block_id,
        },
        "document_constraints": {
            "allowed_ops": [
                "update_meta",
                "replace_section_blocks",
                "append_block",
                "replace_block",
                "append_child_section",
                "replace_section",
            ],
            "preferred_write_strategy": (
                "短小局部修改使用 replace_section_blocks/append_block；"
                "当内容涉及整体架构、处理流程、模块、步骤、关键规则、实施例或拓展方案时，"
                "优先使用 replace_section 生成 children，避免复杂内容平铺成 blocks。"
            ),
            "final_text_policy": (
                "交底书正文必须是最终态文本，不描述对话过程、修改过程或方案迭代过程；"
                "替换旧方案时直接写最终方案，不写“之前/现在/改为/本次修改”等过程性表述。"
            ),
        },
        "outline": outline,
        "current_section": section,
        "recent_user_inputs": recent_user_inputs,
    }


def build_section_writer_result(
    payload: dict[str, Any],
    target_section_id: str,
    target_block_id: str | None,
) -> dict[str, Any]:
    if payload.get("proposal_type") != "document_edit_proposal":
        raise ApiError(502, "subagent_invalid_submit_result", "section_writer 必须提交 document_edit_proposal。")
    proposal_payload = payload.get("proposal")
    if not isinstance(proposal_payload, dict):
        raise ApiError(502, "subagent_invalid_submit_result", "section_writer.proposal 必须是对象。")

    operations = proposal_payload.get("operations")
    if not isinstance(operations, list) or not operations or not all(isinstance(item, dict) for item in operations):
        raise ApiError(502, "subagent_invalid_submit_result", "section_writer.proposal.operations 必须是非空对象数组。")

    summary = str(payload.get("summary") or f"已为 {target_section_id} 生成候选正文。").strip()
    reply = str(payload.get("reply") or summary).strip()
    rationale = str(payload.get("rationale") or "已根据当前输入和目标章节生成候选修改。").strip()
    questions = _string_list(payload.get("questions"))
    warnings = _string_list(payload.get("warnings"))

    return {
        "status": "success",
        "summary": summary,
        "reply": reply,
        "proposal": {
            "type": "document_edit_proposal",
            "target_section_id": target_section_id,
            "target_block_id": target_block_id,
            "intent": str(proposal_payload.get("intent") or "replace_section_content"),
            "confidence": float(proposal_payload.get("confidence") or 0.75),
            "rationale": rationale,
            "operations": operations,
        },
        "questions": questions,
        "warnings": warnings,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
