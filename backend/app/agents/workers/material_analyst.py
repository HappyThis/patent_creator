from __future__ import annotations

from typing import Any

from ...core import ApiError


def build_material_analyst_result(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("proposal_type") != "analysis_result":
        raise ApiError(502, "subagent_invalid_submit_result", "material_analyst 必须提交 analysis_result。")
    proposal_payload = payload.get("proposal")
    if not isinstance(proposal_payload, dict):
        raise ApiError(502, "subagent_invalid_submit_result", "material_analyst.proposal 必须是对象。")

    facts = _object_list(proposal_payload.get("facts"), required_keys=("kind", "text"))
    candidate_terms = _string_list(proposal_payload.get("candidate_terms"))
    recommended_actions = _object_list(proposal_payload.get("recommended_next_actions"), required_keys=("action",))

    summary = str(payload.get("summary") or "已完成材料分析。").strip()
    reply = str(payload.get("reply") or summary).strip()
    rationale = str(payload.get("rationale") or "依据当前材料提炼技术事实。").strip()
    questions = _string_list(payload.get("questions"))
    warnings = _string_list(payload.get("warnings"))

    return {
        "status": "success",
        "summary": summary,
        "reply": reply,
        "proposal": {
            "type": "analysis_result",
            "rationale": rationale,
            "facts": facts,
            "candidate_terms": candidate_terms,
            "recommended_next_actions": recommended_actions,
        },
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
