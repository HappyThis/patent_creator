from __future__ import annotations

from typing import Any

from ...core import ApiError
from ..prompts import build_material_analyst_system_prompt, build_material_analyst_user_prompt
from ..types import SubagentDeclaration
from .section_writer import SupportsGenerateJson


def build_material_analyst_context(
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


async def run_material_analyst(
    declaration: SubagentDeclaration,
    llm_client: SupportsGenerateJson,
    context: dict[str, Any],
) -> dict[str, Any]:
    payload = await llm_client.generate_json(
        system_prompt=build_material_analyst_system_prompt(declaration),
        user_prompt=build_material_analyst_user_prompt(context),
        temperature=0.0,
    )
    return build_material_analyst_result(payload)


def build_material_analyst_result(payload: dict[str, Any]) -> dict[str, Any]:
    facts = _object_list(payload.get("facts"), required_keys=("kind", "text"))
    candidate_terms = _string_list(payload.get("candidate_terms"))
    recommended_actions = _object_list(payload.get("recommended_next_actions"), required_keys=("action",))

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
