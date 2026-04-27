from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..core import ApiError, Settings, generate_id, now_iso
from ..domain import derive_message_intent
from ..runtime.executor import ExecutorEngine
from ..schemas import ChatMessageRequest, ChatMessageResponse
from ..storage.workspace_store import WorkspaceStore
from .event_bus import SessionEventBus


@dataclass(slots=True)
class RoundState:
    session_id: str
    message_id: str
    round_id: str


class ChatService:
    def __init__(
        self,
        store: WorkspaceStore,
        executor: ExecutorEngine,
        bus: SessionEventBus,
        settings: Settings,
    ) -> None:
        self.store = store
        self.executor = executor
        self.bus = bus
        self.settings = settings
        self._project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def start_round(self, project_id: str, payload: ChatMessageRequest) -> ChatMessageResponse:
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

        asyncio.create_task(self._run_round(project_id, payload, RoundState(session_id, message_id, round_id)))
        return ChatMessageResponse(
            accepted=True,
            session_id=session_id,
            message_id=message_id,
            round_id=round_id,
        )

    async def _run_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        key = (project_id, state.session_id)
        disclosure = self.store.get_disclosure(project_id)
        intent = derive_message_intent(disclosure["sections"], payload.message, payload.active_section_id)
        changed_payload: dict[str, Any] = {
            "changed": False,
            "changed_section_ids": [],
            "changed_block_ids": [],
            "primary_section_id": intent.target_section_id,
            "primary_block_id": None,
            "change_scope": None,
            "active_section_id": intent.target_section_id,
            "active_block_id": payload.active_block_id,
        }
        try:
            await self._sleep()
            await self._emit_agent_output(
                project_id,
                state,
                f"我会先阅读你的文本内容，定位到“{intent.target_section_id}”相关章节，再把本轮变更同步到预览区。",
            )

            await self._sleep()
            read_arguments = {"action": "get_section", "section_id": intent.target_section_id, "include_children": True}
            read_result = self.executor.document_read(project_id, read_arguments)
            await self._emit_tool(
                project_id,
                state,
                tool="document_read",
                arguments=read_arguments,
                summary_started=f"开始读取 {intent.target_section_id}",
                summary_finished=f"{intent.target_section_id} 已读取",
                result=read_result,
            )
            self._raise_if_tool_failed(read_result)

            await self._sleep()
            subagent_arguments = {
                "agent_id": "section_writer",
                "goal": f"根据用户原始请求提炼章节信息并补全正文。匹配方式：{intent.matched_by}。用户原始请求：{payload.message}",
                "call_type": "rich_context_specialist",
                "target_section_id": intent.target_section_id,
                "target_block_id": payload.active_block_id,
                "user_message": payload.message,
            }
            subagent_result = await self._emit_execute_subagent(
                project_id,
                state,
                arguments=subagent_arguments,
            )
            self._raise_if_tool_failed(subagent_result)

            await self._sleep()
            operations = subagent_result["output"]["result"]["proposal"]["operations"]
            edit_arguments = {"operations": operations}
            edit_result = self.executor.document_edit(project_id, edit_arguments)
            await self._emit_tool(
                project_id,
                state,
                tool="document_edit",
                arguments=edit_arguments,
                summary_started="开始写入 disclosure.json",
                summary_finished="文档更新已完成",
                result=edit_result,
            )
            self._raise_if_tool_failed(edit_result)
            changed_payload = {
                "changed": True,
                **edit_result["output"],
                "active_section_id": edit_result["output"]["primary_section_id"],
                "active_block_id": edit_result["output"]["primary_block_id"],
            }
            await self.bus.publish(key, "document_changed", changed_payload)

            await self._sleep(self.settings.round_finish_delay)
            final_reply = (
                subagent_result["output"]["result"].get("reply")
                or subagent_result["output"]["result"].get("summary")
                or f"我已经根据你的文本内容更新了“{intent.target_section_id}”对应章节。"
            )
            await self._emit_agent_output(project_id, state, final_reply)

            committed, commit_error = await self._commit(project_id, changed_payload)
            await self._set_project_idle(project_id)
            await self.bus.publish(
                key,
                "round_finished",
                {
                    "reply": final_reply,
                    **changed_payload,
                    "committed": committed,
                    "commit_error": commit_error,
                },
            )
        except Exception as exc:
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

    async def _emit_agent_output(self, project_id: str, state: RoundState, text: str) -> None:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="agent_output",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            payload={"text": text},
        )
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
        agent_id = arguments["agent_id"]
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
                "summary": f"已启动 {agent_id}",
            },
        )

        sub_call_id = generate_id("call")
        sub_read_arguments = {
            "action": "get_section",
            "section_id": arguments.get("target_section_id") or "technical_solution",
            "include_children": True,
        }
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_call",
            scope=f"subagent:{agent_id}",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=sub_call_id,
            parent_call_id=call_id,
            payload={"tool": "document_read", "arguments": sub_read_arguments},
        )
        sub_read_result = self.executor.document_read(project_id, sub_read_arguments, scope="subagent")
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="tool_result",
            scope=f"subagent:{agent_id}",
            round_id=state.round_id,
            message_id=state.message_id,
            call_id=sub_call_id,
            parent_call_id=call_id,
            payload={"tool": "document_read", **sub_read_result},
        )

        result = await self.executor.execute_subagent(project_id, arguments, session_id=state.session_id)
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
                "summary": f"{agent_id} 已完成",
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
