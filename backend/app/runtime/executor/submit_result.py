from __future__ import annotations

from typing import Any

from ...agents.workers.consistency_reviewer import build_consistency_reviewer_result
from ...agents.workers.material_analyst import build_material_analyst_result
from ...agents.workers.section_writer import build_section_writer_result
from ...agents.workers.solution_refiner import build_solution_refiner_result
from ...core import ApiError
from ...domain.document_tools import tool_failed, tool_success

ALLOWED_PROPOSAL_TYPES: dict[str, set[str]] = {
    "section_writer": {"document_edit_proposal"},
    "material_analyst": {"analysis_result"},
    "solution_refiner": {"analysis_result", "document_edit_proposal"},
    "consistency_reviewer": {"review_report"},
}


def invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
    return tool_failed("invalid_tool_arguments_json", message)


def submit_subagent_result(agent_id: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        result = build_subagent_result(agent_id, payload, context)
    except ApiError as exc:
        return tool_failed(exc.code, exc.message)
    return tool_success({"result": result})


def build_subagent_result(agent_id: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    validate_submit_result_envelope(agent_id, payload)
    if agent_id == "section_writer":
        task = context["task"]
        return build_section_writer_result(payload, task["target_section_id"], task.get("target_block_id"))
    if agent_id == "material_analyst":
        return build_material_analyst_result(payload)
    if agent_id == "solution_refiner":
        return build_solution_refiner_result(payload)
    if agent_id == "consistency_reviewer":
        return build_consistency_reviewer_result(payload)
    raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")


def validate_submit_result_envelope(agent_id: str, payload: dict[str, Any]) -> None:
    string_fields = ("summary", "reply", "rationale")
    for field in string_fields:
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ApiError(502, "subagent_invalid_submit_result", f"submit_result.{field} 必须是非空字符串。")

    proposal_type = payload.get("proposal_type")
    allowed_types = ALLOWED_PROPOSAL_TYPES.get(agent_id)
    if not allowed_types:
        raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")
    if proposal_type not in {"analysis_result", "document_edit_proposal", "review_report"}:
        raise ApiError(
            502,
            "subagent_invalid_submit_result",
            "submit_result.proposal_type 必须是 analysis_result、document_edit_proposal 或 review_report。",
        )
    if proposal_type not in allowed_types:
        allowed = "、".join(sorted(allowed_types))
        raise ApiError(
            502,
            "subagent_invalid_submit_result",
            f"{agent_id} 只能提交 proposal_type：{allowed}。",
        )
    if not isinstance(payload.get("proposal"), dict):
        raise ApiError(502, "subagent_invalid_submit_result", "submit_result.proposal 必须是对象。")
    for field in ("questions", "warnings"):
        value = payload.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ApiError(502, "subagent_invalid_submit_result", f"submit_result.{field} 必须是字符串数组。")


def subagent_tool_summary(agent_id: str, tool: str, result: dict[str, Any]) -> str:
    if tool == "submit_result":
        if result.get("status") == "success":
            return f"{agent_id} 提交结果"
        return f"{agent_id} 提交结果失败"
    if result.get("status") == "failed":
        return f"{agent_id} 执行 {tool} 失败"
    return f"{agent_id} 完成 {tool}"
