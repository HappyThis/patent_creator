from __future__ import annotations

from typing import Any

from ...core import ApiError


def build_section_writer_result(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("proposal_type") != "document_edit_proposal":
        raise ApiError(502, "subagent_invalid_submit_result", "section_writer 必须提交 document_edit_proposal。")
    proposal_payload = payload.get("proposal")
    if not isinstance(proposal_payload, dict):
        raise ApiError(502, "subagent_invalid_submit_result", "section_writer.proposal 必须是对象。")

    operations = proposal_payload.get("operations")
    if not isinstance(operations, list) or not operations or not all(isinstance(item, dict) for item in operations):
        raise ApiError(502, "subagent_invalid_submit_result", "section_writer.proposal.operations 必须是非空对象数组。")

    summary = str(payload.get("summary") or "已生成候选正文。").strip()
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
            "target_section_id": proposal_payload.get("target_section_id"),
            "target_block_id": proposal_payload.get("target_block_id"),
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
