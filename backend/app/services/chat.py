from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..agents.prompts import build_main_agent_system_prompt
from ..agents.runtime.openai_compat import OpenAICompatibleClient
from ..agents.workers import MainAgentAction, decide_main_agent_step
from ..core import ApiError, Settings, generate_id, now_iso
from ..runtime.context import ContextManager
from ..runtime.executor import ExecutorEngine
from ..schemas import ChatMessageRequest, ChatMessageResponse
from ..storage.workspace_store import WorkspaceStore
from .event_bus import SessionEventBus

logger = logging.getLogger("patent_creator.chat")


@dataclass(slots=True)
class RoundState:
    session_id: str
    message_id: str
    round_id: str


DEFAULT_CHANGED_PAYLOAD: dict[str, Any] = {
    "changed": False,
    "changed_section_ids": [],
    "changed_block_ids": [],
    "primary_section_id": None,
    "primary_block_id": None,
    "change_scope": None,
    "active_section_id": None,
    "active_block_id": None,
}


class ChatService:
    def __init__(
        self,
        store: WorkspaceStore,
        context_manager: ContextManager,
        executor: ExecutorEngine,
        bus: SessionEventBus,
        settings: Settings,
        llm_client: OpenAICompatibleClient,
    ) -> None:
        self.store = store
        self.context_manager = context_manager
        self.executor = executor
        self.bus = bus
        self.settings = settings
        self.llm_client = llm_client
        self._project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def prepare_round(
        self,
        project_id: str,
        payload: ChatMessageRequest,
    ) -> tuple[ChatMessageResponse, RoundState]:
        async with self._project_locks[project_id]:
            project = self.store.get_project(project_id)
            if project.is_busy:
                raise ApiError(409, "project_busy", "当前已有 session 正在执行，请等待本轮完成后再发送消息。")

            if payload.session_id and not self.store.session_exists(project_id, payload.session_id):
                raise ApiError(404, "session_not_found", f"session_id 不存在：{payload.session_id}")

            session_id = payload.session_id or generate_id("sess")
            message_id = generate_id("msg")
            round_id = generate_id("round")

            project.active_session_id = session_id
            project.running_session_id = session_id
            project.running_round_id = round_id
            project.is_busy = True
            project.updated_at = now_iso()
            self.store.save_project(project)
            self.store.append_session_event(
                project_id,
                session_id,
                event_type="user_input",
                scope="main",
                round_id=round_id,
                message_id=message_id,
                payload={
                    "text": payload.message,
                    "active_section_id": payload.active_section_id,
                    "active_block_id": payload.active_block_id,
                },
            )

        response = ChatMessageResponse(
            accepted=True,
            session_id=session_id,
            message_id=message_id,
            round_id=round_id,
        )
        return response, RoundState(session_id, message_id, round_id)

    async def start_round(self, project_id: str, payload: ChatMessageRequest) -> ChatMessageResponse:
        response, state = await self.prepare_round(project_id, payload)
        self.launch_round(project_id, payload, state)
        return response

    def launch_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        asyncio.create_task(self._run_round(project_id, payload, state))

    async def _run_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        key = (project_id, state.session_id)
        system_prompt = build_main_agent_system_prompt()
        messages = self.context_manager.build_main_agent_messages(
            project_id,
            state.session_id,
            user_message=payload.message,
            active_section_id=payload.active_section_id,
            active_block_id=payload.active_block_id,
        )
        changed_payload: dict[str, Any] = dict(DEFAULT_CHANGED_PAYLOAD)
        changed_payload["active_section_id"] = payload.active_section_id
        changed_payload["active_block_id"] = payload.active_block_id
        final_reply: str | None = None
        logger.info(
            "round started project=%s session=%s round=%s message_len=%d",
            project_id,
            state.session_id,
            state.round_id,
            len(payload.message or ""),
        )

        try:
            await self._sleep()
            max_steps = max(1, self.settings.main_agent_max_steps)

            for step_index in range(max_steps):
                logger.info(
                    "round step=%d/%d project=%s session=%s",
                    step_index + 1,
                    max_steps,
                    project_id,
                    state.session_id,
                )
                streamed_reply_parts: list[str] = []

                async def on_text_delta(delta: str) -> None:
                    if not delta:
                        return
                    streamed_reply_parts.append(delta)
                    await self.bus.publish(key, "assistant_delta", {"text": delta, "scope": "main"})

                action = await decide_main_agent_step(
                    self.llm_client,
                    system_prompt=system_prompt,
                    messages=messages,
                    on_text_delta=on_text_delta,
                )

                if action.type == "respond":
                    final_reply = action.text or ""
                    logger.info(
                        "round step=%d action=respond text_len=%d",
                        step_index + 1,
                        len(final_reply),
                    )
                    messages.append({"role": "assistant", "content": final_reply})
                    await self._emit_agent_output(
                        project_id,
                        state,
                        final_reply,
                        publish=not bool(streamed_reply_parts),
                    )
                    break

                # tool_call 分支
                tool_call_id = action.tool_call_id or generate_id("llm_call")
                tool_name = action.tool or ""
                arguments = action.arguments or {}
                logger.info(
                    "round step=%d action=tool_call tool=%s arguments=%s",
                    step_index + 1,
                    tool_name,
                    arguments,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )

                result = await self._dispatch_tool(project_id, state, tool_name, arguments)
                logger.info(
                    "round step=%d tool=%s status=%s",
                    step_index + 1,
                    tool_name,
                    result.get("status"),
                )
                self._raise_if_tool_failed(result)

                if tool_name == "document_edit":
                    output = result["output"]
                    changed_payload = {
                        "changed": True,
                        **output,
                        "active_section_id": output.get("primary_section_id"),
                        "active_block_id": output.get("primary_block_id"),
                    }
                    await self.bus.publish(key, "document_changed", changed_payload)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                await self._sleep()
            else:
                final_reply = "本轮步数已达上限，请继续补充信息或重试。"
                logger.warning(
                    "round max_steps_reached project=%s session=%s max_steps=%d",
                    project_id,
                    state.session_id,
                    max_steps,
                )
                await self._emit_agent_output(project_id, state, final_reply)

            await self._sleep(self.settings.round_finish_delay)
            committed, commit_error = await self._commit(project_id, changed_payload)
            await self._set_project_idle(project_id)
            logger.info(
                "round finished project=%s session=%s changed=%s committed=%s",
                project_id,
                state.session_id,
                changed_payload.get("changed"),
                committed,
            )
            await self.bus.publish(
                key,
                "round_finished",
                {
                    "reply": final_reply or "",
                    **changed_payload,
                    "committed": committed,
                    "commit_error": commit_error,
                },
            )
        except Exception as exc:
            logger.exception(
                "round failed project=%s session=%s round=%s",
                project_id,
                state.session_id,
                state.round_id,
            )
            await self._set_project_idle(project_id)
            await self.bus.publish(
                key,
                "round_failed",
                {
                    "code": "round_runtime_error",
                    "message": str(exc),
                    "reply": "本轮未完成，请重试或补充信息。",
                },
            )

    async def _dispatch_tool(
        self,
        project_id: str,
        state: RoundState,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "document_read":
            result = self.executor.document_read(project_id, arguments)
            section_id = arguments.get("section_id") or arguments.get("block_id") or ""
            await self._emit_tool(
                project_id,
                state,
                tool="document_read",
                arguments=arguments,
                summary_started=f"开始读取 {section_id}" if section_id else "开始读取章节",
                summary_finished=f"{section_id} 已读取" if section_id else "章节已读取",
                result=result,
            )
            return result

        if tool_name == "document_edit":
            result = self.executor.document_edit(project_id, arguments)
            await self._emit_tool(
                project_id,
                state,
                tool="document_edit",
                arguments=arguments,
                summary_started="开始写入 disclosure.json",
                summary_finished="文档更新已完成",
                result=result,
            )
            return result

        if tool_name == "execute_subagent":
            return await self._emit_execute_subagent(project_id, state, arguments=arguments)

        raise ApiError(400, "unsupported_main_tool", f"主 agent 不支持的工具：{tool_name}")

    async def _emit_agent_output(self, project_id: str, state: RoundState, text: str, publish: bool = True) -> None:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="agent_output",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            payload={"text": text},
        )
        if publish:
            await self.bus.publish((project_id, state.session_id), "agent_output", {"text": text})

    async def _emit_tool(
        self,
        project_id: str,
        state: RoundState,
        *,
        tool: str,
        arguments: dict[str, Any],
        summary_started: str,
        summary_finished: str,
        result: dict[str, Any],
    ) -> str:
        call_id = generate_id("call")
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
            },
        )
        return call_id

    async def _emit_execute_subagent(
        self,
        project_id: str,
        state: RoundState,
        *,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        call_id = generate_id("call")
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
            },
        )

        result = await self.executor.execute_subagent(
            project_id,
            arguments,
            session_id=state.session_id,
            round_id=state.round_id,
            message_id=state.message_id,
            parent_call_id=call_id,
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
            },
        )
        return result

    async def _commit(self, project_id: str, changed_payload: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
        return await asyncio.to_thread(
            self.store.commit_workspace,
            project_id,
            build_commit_message(changed_payload),
        )

    async def _set_project_idle(self, project_id: str) -> None:
        async with self._project_locks[project_id]:
            project = self.store.get_project(project_id)
            project.running_session_id = None
            project.running_round_id = None
            project.is_busy = False
            project.updated_at = now_iso()
            self.store.save_project(project)

    async def _sleep(self, duration: float | None = None) -> None:
        await asyncio.sleep(self.settings.round_step_delay if duration is None else duration)

    @staticmethod
    def _raise_if_tool_failed(result: dict[str, Any]) -> None:
        if result["status"] == "failed":
            output = result["output"]
            raise ApiError(500, output.get("code", "tool_failed"), output.get("message", "工具调用失败。"))


def format_sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_commit_message(changed_payload: dict[str, Any]) -> str:
    sections = changed_payload.get("changed_section_ids", [])[:10]
    blocks = changed_payload.get("changed_block_ids", [])[:10]
    lines = ["update disclosure", "", f"Time: {now_iso()}", "", "Changed sections:"]
    lines.extend(f"- {section_id}" for section_id in sections)
    lines.append("")
    lines.append("Changed blocks:")
    lines.extend(f"- {block_id}" for block_id in blocks)
    return "\n".join(lines)
