from __future__ import annotations

from typing import Any

def build_solution_refiner_context(
    *,
    goal: str,
    user_message: str,
    outline: list[dict[str, Any]],
    target_section: dict[str, Any] | None,
    recent_user_inputs: list[str],
) -> dict[str, Any]:
    return {
        "task": {
            "goal": goal,
            "user_message": user_message,
        },
        "outline": outline,
        "target_section": target_section,
        "recent_user_inputs": recent_user_inputs,
    }


def build_solution_refiner_result(payload: dict[str, Any]) -> dict[str, Any]:
    modules = _object_list(payload.get("modules"), required_keys=("name", "responsibility"))
    key_constraints = _string_list(payload.get("key_constraints"))
    innovations = _string_list(payload.get("innovations"))
    open_questions = _string_list(payload.get("open_questions"))

    summary = str(payload.get("summary") or "已整理技术方案骨架。").strip()
    reply = str(payload.get("reply") or summary).strip()
    rationale = str(payload.get("rationale") or "根据当前事实收敛方案走向。").strip()
    solution_outline = str(payload.get("solution_outline") or "").strip()
    questions = _string_list(payload.get("questions"))
    warnings = _string_list(payload.get("warnings"))
    operations = payload.get("operations")

    if payload.get("proposal_type") == "document_edit_proposal" and isinstance(operations, list) and operations:
        proposal = {
            "type": "document_edit_proposal",
            "target_section_id": payload.get("target_section_id"),
            "target_block_id": payload.get("target_block_id"),
            "intent": str(payload.get("intent") or "refine_solution"),
            "confidence": float(payload.get("confidence") or 0.72),
            "rationale": rationale,
            "operations": [item for item in operations if isinstance(item, dict)],
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
