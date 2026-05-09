from __future__ import annotations

from typing import Any

from ...core import ApiError


def build_solution_refiner_result(payload: dict[str, Any]) -> dict[str, Any]:
    proposal_type = payload.get("proposal_type")
    if proposal_type not in {"analysis_result", "document_edit_proposal"}:
        raise ApiError(
            502,
            "subagent_invalid_submit_result",
            "solution_refiner 必须提交 analysis_result 或 document_edit_proposal。",
        )
    proposal_payload = payload.get("proposal")
    if not isinstance(proposal_payload, dict):
        raise ApiError(502, "subagent_invalid_submit_result", "solution_refiner.proposal 必须是对象。")

    modules = _object_list(proposal_payload.get("modules"), required_keys=("name", "responsibility"))
    key_constraints = _string_list(proposal_payload.get("key_constraints"))
    innovations = _string_list(proposal_payload.get("innovations"))
    open_questions = _string_list(proposal_payload.get("open_questions"))

    summary = str(payload.get("summary") or "已整理技术方案骨架。").strip()
    reply = str(payload.get("reply") or summary).strip()
    rationale = str(payload.get("rationale") or "根据当前事实收敛方案走向。").strip()
    solution_outline = str(proposal_payload.get("solution_outline") or "").strip()
    questions = _string_list(payload.get("questions"))
    warnings = _string_list(payload.get("warnings"))
    operations = proposal_payload.get("operations")

    if proposal_type == "document_edit_proposal":
        if not isinstance(operations, list) or not operations or not all(isinstance(item, dict) for item in operations):
            raise ApiError(
                502,
                "subagent_invalid_submit_result",
                "solution_refiner.proposal.operations 必须是非空对象数组。",
            )
        proposal = {
            "type": "document_edit_proposal",
            "target_section_id": proposal_payload.get("target_section_id"),
            "target_block_id": proposal_payload.get("target_block_id"),
            "intent": str(proposal_payload.get("intent") or "refine_solution"),
            "confidence": float(proposal_payload.get("confidence") or 0.72),
            "rationale": rationale,
            "operations": operations,
        }
    else:
        proposal = {
            "type": "analysis_result",
            "rationale": rationale,
            "solution_outline": solution_outline,
            "modules": modules,
            "key_constraints": key_constraints,
            "innovations": innovations,
            "open_questions": open_questions,
        }

    return {
        "status": "success",
        "summary": summary,
        "reply": reply,
        "proposal": proposal,
        "questions": questions,
        "warnings": warnings,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _object_list(value: Any, *, required_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if any(not str(item.get(key, "")).strip() for key in required_keys):
            continue
        result.append(item)
    return result
