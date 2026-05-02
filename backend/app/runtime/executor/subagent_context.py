from __future__ import annotations

from typing import Any, Awaitable, Callable

from ...agents.workers import (
    build_consistency_reviewer_context,
    build_material_analyst_context,
    build_section_writer_context,
    build_solution_refiner_context,
)
from ...core import ApiError
from ..context import ContextManager

SectionReader = Callable[..., Awaitable[dict[str, Any] | None]]
RecentEventsReader = Callable[[str, str | None], list[dict[str, Any]]]


class SubagentContextBuilder:
    def __init__(
        self,
        context_manager: ContextManager,
        section_reader: SectionReader,
        recent_events_reader: RecentEventsReader,
    ) -> None:
        self.context_manager = context_manager
        self.section_reader = section_reader
        self.recent_events_reader = recent_events_reader

    async def build(
        self,
        *,
        project_id: str,
        agent_id: str,
        call_type: str,
        arguments: dict[str, Any],
        session_id: str | None,
        round_id: str | None,
        message_id: str | None,
        parent_call_id: str | None,
        on_tool_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        goal = str(arguments.get("goal") or "")
        user_message = str(arguments.get("user_message") or "")
        target_section_id = arguments.get("target_section_id")
        target_block_id = arguments.get("target_block_id")

        outline: list[dict[str, Any]] = []
        recent_inputs: list[str] = []
        section_snapshot: dict[str, Any] | None = None

        if call_type in {"rich_context_specialist", "forked_context"}:
            outline = self.context_manager.build_outline_snapshot(project_id)
            recent_inputs = self.context_manager.recent_user_inputs(project_id, session_id)
            if target_section_id:
                section_snapshot = await self.section_reader(
                    project_id=project_id,
                    section_id=str(target_section_id),
                    agent_id=agent_id,
                    session_id=session_id,
                    round_id=round_id,
                    message_id=message_id,
                    parent_call_id=parent_call_id,
                    on_tool_event=on_tool_event,
                )

        if agent_id == "section_writer":
            context = build_section_writer_context(
                target_section_id=str(target_section_id),
                target_block_id=target_block_id,
                goal=goal,
                user_message=user_message,
                outline=outline,
                section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
        elif agent_id == "material_analyst":
            context = build_material_analyst_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
        elif agent_id == "solution_refiner":
            context = build_solution_refiner_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                recent_user_inputs=recent_inputs,
            )
        elif agent_id == "consistency_reviewer":
            context = build_consistency_reviewer_context(
                goal=goal,
                user_message=user_message,
                outline=outline,
                target_section=section_snapshot,
                target_section_id=str(target_section_id) if target_section_id else None,
                recent_user_inputs=recent_inputs,
            )
        else:
            raise ApiError(400, "unsupported_agent", f"未实现的子 agent：{agent_id}")

        context["call_type"] = call_type
        context["available_tools"] = ["document_read", "exec_command", "submit_result"]
        if call_type == "forked_context":
            context["caller_context"] = {
                "recent_session_events": self.recent_events_reader(project_id, session_id),
            }
        return context
