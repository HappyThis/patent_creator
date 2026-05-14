from __future__ import annotations

from typing import Any

from ..runtime.executor import ExecutorEngine
from ..storage.workspace_store import WorkspaceStore
from .chat_protocol import RoundState
from .event_bus import SessionEventBus


class ChatEventEmitter:
    def __init__(self, store: WorkspaceStore, bus: SessionEventBus, executor: ExecutorEngine) -> None:
        self.store = store
        self.bus = bus
        self.executor = executor

    async def agent_message(
        self,
        project_id: str,
        state: RoundState,
        *,
        message: dict[str, Any],
        model: str,
        provider: str,
        thinking: str,
    ) -> None:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="agent_message",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            payload={
                "message": message,
                "model": model,
                "provider": provider,
                "thinking": thinking,
            },
        )

    async def agent_output(self, project_id: str, state: RoundState, text: str) -> None:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="agent_output",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            payload={"text": text},
        )

    async def failed_tool_result(
        self,
        project_id: str,
        state: RoundState,
        *,
        tool: str,
        call_id: str,
        result: dict[str, Any],
    ) -> None:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_call",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=call_id,
            payload={"tool": tool, "arguments": {}},
        )
        await self.bus.publish(
            (project_id, state.session_id),
            "tool_call_started",
            {
                "call_id": call_id,
                "parent_call_id": None,
                "scope": "main",
                "tool": tool,
                "summary": f"开始执行 {tool}",
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_result",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=call_id,
            payload={"tool": tool, **result},
        )
        await self.bus.publish(
            (project_id, state.session_id),
            "tool_call_finished",
            {
                "call_id": call_id,
                "parent_call_id": None,
                "scope": "main",
                "tool": tool,
                "summary": "执行失败",
                "result": result,
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )

    async def tool(
        self,
        project_id: str,
        state: RoundState,
        *,
        tool: str,
        arguments: dict[str, Any],
        summary_started: str,
        summary_finished: str,
        result: dict[str, Any],
        call_id: str,
    ) -> str:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_call",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=call_id,
            payload={"tool": tool, "arguments": arguments},
        )
        await self.bus.publish(
            (project_id, state.session_id),
            "tool_call_started",
            {
                "call_id": call_id,
                "parent_call_id": None,
                "scope": "main",
                "tool": tool,
                "summary": summary_started,
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_result",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=call_id,
            payload={"tool": tool, **result},
        )
        await self.bus.publish(
            (project_id, state.session_id),
            "tool_call_finished",
            {
                "call_id": call_id,
                "parent_call_id": None,
                "scope": "main",
                "tool": tool,
                "summary": summary_finished,
                "result": result,
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )
        return call_id

    async def execute_subagent(
        self,
        project_id: str,
        state: RoundState,
        *,
        arguments: dict[str, Any],
        call_id: str,
        caller_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        agent_id = str(arguments.get("agent_id") or "")
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_call",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=call_id,
            payload={"tool": "execute_subagent", "arguments": arguments},
        )
        await self.bus.publish(
            (project_id, state.session_id),
            "tool_call_started",
            {
                "call_id": call_id,
                "parent_call_id": None,
                "scope": "main",
                "tool": "execute_subagent",
                "summary": f"已启动 {agent_id}" if agent_id else "已启动子 agent",
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )

        result = await self.executor.execute_subagent(
            project_id,
            arguments,
            session_id=state.session_id,
            round_id=state.round_id,
            message_id=state.message_id,
            parent_call_id=call_id,
            caller_messages=caller_messages,
            on_tool_event=lambda event_name, event_payload: self.bus.publish(
                (project_id, state.session_id),
                event_name,
                {
                    **event_payload,
                    "round_id": state.round_id,
                    "message_id": state.message_id,
                },
            ),
        )
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_result",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=call_id,
            payload={"tool": "execute_subagent", **result},
        )
        await self.bus.publish(
            (project_id, state.session_id),
            "tool_call_finished",
            {
                "call_id": call_id,
                "parent_call_id": None,
                "scope": "main",
                "tool": "execute_subagent",
                "summary": f"{agent_id} 已完成" if agent_id else "子 agent 已完成",
                "result": result,
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )
        return result
