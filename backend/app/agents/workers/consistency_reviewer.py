from __future__ import annotations

from typing import Any

from ..prompts import build_consistency_reviewer_system_prompt, build_consistency_reviewer_user_prompt
from ..types import SubagentDeclaration
from .section_writer import SupportsGenerateJson


_ALLOWED_SEVERITIES = {"low", "medium", "high"}


def build_consistency_reviewer_context(
    *,
    goal: str,
    user_message: str,
    outline: list[dict[str, Any]],
    target_section: dict[str, Any] | None,
    target_section_id: str | None,
    recent_user_inputs: list[str],
) -> dict[str, Any]:
    return {
        "task": {
            "goal": goal,
            "user_message": user_message,
            "target_section_id": target_section_id,
        },
        "outline": outline,
        "target_section": target_section,
        "recent_user_inputs": recent_user_inputs,
    }


async def run_consistency_reviewer(
    declaration: SubagentDeclaration,
    llm_client: SupportsGenerateJson,
    context: dict[str, Any],
) -> dict[str, Any]:
    payload = await llm_client.generate_json(
        system_prompt=build_consistency_reviewer_system_prompt(declaration),
        user_prompt=build_consistency_reviewer_user_prompt(context),
        temperature=0.0,
    )
    return build_consistency_reviewer_result(payload)


def build_consistency_reviewer_result(payload: dict[str, Any]) -> dict[str, Any]:
    issues = _normalize_issues(payload.get("issues"))

    summary = str(payload.get("summary") or "已完成一致性审查。").strip()
    reply = str(payload.get("reply") or summary).strip()
    rationale = str(payload.get("rationale") or "基于目标章节与关联章节进行对比审查。").strip()
    questions = _string_list(payload.get("questions"))
    warnings = _string_list(payload.get("warnings"))

    return {
        "status": "success",
        "summary": summary,
        "reply": reply,
        "proposal": {
            "type": "review_report",
            "rationale": rationale,
            "issues": issues,
        },
        "questions": questions,
        "warnings": warnings,
    }


def _normalize_issues(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        severity = str(item.get("severity") or "medium").strip().lower()
        if severity not in _ALLOWED_SEVERITIES:
            severity = "medium"
        section_id = item.get("section_id")
        block_id = item.get("block_id")
        result.append(
            {
                "severity": severity,
                "section_id": str(section_id).strip() if section_id else None,
                "block_id": str(block_id).strip() if block_id else None,
                "message": message,
                "suggested_fix": str(item.get("suggested_fix") or "").strip(),
            }
        )
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
