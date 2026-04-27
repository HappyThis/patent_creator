from __future__ import annotations

from typing import Any

from ..prompts import build_solution_refiner_system_prompt, build_solution_refiner_user_prompt
from ..types import SubagentDeclaration
from .section_writer import SupportsGenerateJson


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


async def run_solution_refiner(
    declaration: SubagentDeclaration,
    llm_client: SupportsGenerateJson,
    context: dict[str, Any],
) -> dict[str, Any]:
    payload = await llm_client.generate_json(
        system_prompt=build_solution_refiner_system_prompt(declaration),
        user_prompt=build_solution_refiner_user_prompt(context),
        temperature=0.0,
    )
    return build_solution_refiner_result(payload)


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

    return {
        "status": "success",
        "summary": summary,
        "reply": reply,
        "proposal": {
            "type": "analysis_result",
            "rationale": rationale,
            "solution_outline": solution_outline,
            "modules": modules,
            "key_constraints": key_constraints,
            "innovations": innovations,
            "open_questions": open_questions,
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
