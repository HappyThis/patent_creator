from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from ..agents.prompts import build_main_agent_system_prompt
from ..agents.runtime.model_profiles import resolve_model_profile
from ..agents.runtime.openai_compat import OpenAICompatibleClient
from ..agents.workers import MAIN_AGENT_TOOLS, MainAgentToolCall, decide_main_agent_step
from ..core import ApiError, Settings, generate_id, now_iso
from ..domain.document_tool_results import tool_failed
from ..runtime.context import ContextManager
from ..runtime.executor import ExecutorEngine, ToolRuntimeContext
from ..tools import DOCUMENT_WRITE_TOOL_NAMES, get_tool_declaration
from ..schemas import ChatMessageRequest, ChatMessageResponse, ProjectRecord
from ..storage.workspace_store import WorkspaceStore
from .chat_events import ChatEventEmitter
from .chat_protocol import DEFAULT_CHANGED_PAYLOAD, RoundState, assistant_message_text, build_commit_message
from .event_bus import SessionEventBus

logger = logging.getLogger("patent_creator.chat")
LockedResult = TypeVar("LockedResult")


@dataclass
class _ProjectLockState:
    lock: asyncio.Lock
    leases: int = 0


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
        self.events = ChatEventEmitter(store, bus, executor)
        self._project_locks: dict[str, _ProjectLockState] = {}
        self._project_locks_guard = asyncio.Lock()
        self._running_tasks: dict[tuple[str, str, str], asyncio.Task[None]] = {}

    async def prepare_round(
        self,
        project_id: str,
        payload: ChatMessageRequest,
    ) -> tuple[ChatMessageResponse, RoundState]:
        def prepare() -> tuple[ChatMessageResponse, RoundState]:
            project = self.store.get_project(project_id)
            if project.is_busy:
                raise ApiError(409, "project_busy", "当前已有 session 正在执行，请等待本轮完成后再发送消息。")

            if payload.session_id and not self.store.session_exists(project_id, payload.session_id):
                raise ApiError(404, "session_not_found", f"session_id 不存在：{payload.session_id}")

            session_id = payload.session_id or generate_id("sess")
            first_user_text = payload.message
            if payload.session_id:
                first_user_text = self.store.first_user_text(project_id, payload.session_id) or payload.message
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
                first_user_text=first_user_text,
            )
            return response, RoundState(session_id, message_id, round_id)

        return await self._run_with_project_lock(project_id, prepare)

    async def start_round(self, project_id: str, payload: ChatMessageRequest) -> ChatMessageResponse:
        response, state = await self.prepare_round(project_id, payload)
        self.launch_round(project_id, payload, state)
        return response

    async def rename_project(self, project_id: str, project_name: str) -> ProjectRecord:
        return await self._run_with_project_lock(project_id, lambda: self.store.rename_project(project_id, project_name))

    async def delete_project(self, project_id: str) -> str | None:
        return await self._run_with_project_lock(project_id, lambda: self.store.delete_project(project_id))

    def launch_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        key = (project_id, state.session_id, state.round_id)
        task = asyncio.create_task(self._run_round(project_id, payload, state))
        self._running_tasks[key] = task

        def discard_finished_task(done_task: asyncio.Task[None]) -> None:
            if self._running_tasks.get(key) is done_task:
                self._running_tasks.pop(key, None)

        task.add_done_callback(discard_finished_task)

    async def cancel_round(self, project_id: str, session_id: str, round_id: str) -> dict[str, Any]:
        def find_running_task() -> asyncio.Task[None] | None:
            project = self.store.get_project(project_id)
            if (
                not project.is_busy
                or project.running_session_id != session_id
                or project.running_round_id != round_id
            ):
                raise ApiError(409, "round_not_running", "当前没有匹配的运行中任务。")

            task = self._running_tasks.get((project_id, session_id, round_id))
            if task is not None:
                task.cancel()
            return task

        task = await self._run_with_project_lock(project_id, find_running_task)
        if task is not None:
            await task

        return await self._mark_round_cancelled(project_id, session_id, round_id)

    async def _run_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        key = (project_id, state.session_id)
        system_prompt = build_main_agent_system_prompt()

        async def on_context_event(event_name: str, event_payload: dict[str, Any]) -> None:
            await self.bus.publish(
                key,
                event_name,
                {
                    **event_payload,
                    "round_id": state.round_id,
                    "message_id": state.message_id,
                },
            )

        changed_payload: dict[str, Any] = dict(DEFAULT_CHANGED_PAYLOAD)
        changed_payload["active_section_id"] = payload.active_section_id
        changed_payload["active_block_id"] = payload.active_block_id
        final_reply: str | None = None
        messages: list[dict[str, Any]] = []
        logger.info(
            "round started project=%s session=%s round=%s message_len=%d",
            project_id,
            state.session_id,
            state.round_id,
            len(payload.message or ""),
        )

        try:
            await self._sleep()
            step_index = 0
            model_profile = resolve_model_profile(self.settings)

            while True:
                step_index += 1
                logger.info(
                    "round step=%d project=%s session=%s",
                    step_index,
                    project_id,
                    state.session_id,
                )
                messages = await self.context_manager.prepare_main_agent_messages(
                    project_id,
                    state.session_id,
                    user_message=payload.message,
                    current_message_id=state.message_id,
                    round_id=state.round_id,
                    system_prompt=system_prompt,
                    llm_client=self.llm_client,
                    on_context_event=on_context_event,
                )

                async def on_text_delta(delta: str) -> None:
                    if not delta:
                        return
                    await self.bus.publish(
                        key,
                        "assistant_delta",
                        {
                            "text": delta,
                            "scope": "main",
                            "round_id": state.round_id,
                            "message_id": state.message_id,
                        },
                    )

                action = await decide_main_agent_step(
                    self.llm_client,
                    system_prompt=system_prompt,
                    messages=messages,
                    on_text_delta=on_text_delta,
                    trace_context={
                        "scope": "main",
                        "project_id": project_id,
                        "session_id": state.session_id,
                        "round_id": state.round_id,
                        "message_id": state.message_id,
                        "step_index": step_index,
                    },
                )

                if action.type == "respond":
                    final_reply = action.text or ""
                    await self.events.agent_message(
                        project_id,
                        state,
                        message=action.assistant_message,
                        model=self.settings.openai_model,
                        provider=model_profile.provider,
                        thinking=model_profile.thinking,
                    )
                    logger.info(
                        "round step=%d action=respond text_len=%d",
                        step_index,
                        len(final_reply),
                    )
                    messages.append(model_profile.prepare_messages_for_request([action.assistant_message])[0])
                    await self.events.agent_output(project_id, state, final_reply)
                    break

                # tool_calls 分支：DeepSeek 要求 assistant(tool_calls) 后紧跟每个 tool_call_id 的 tool 结果。
                tool_calls = action.tool_calls or []
                logger.info("round step=%d action=tool_calls count=%d", step_index, len(tool_calls))
                await self.events.agent_message(
                    project_id,
                    state,
                    message=action.assistant_message,
                    model=self.settings.openai_model,
                    provider=model_profile.provider,
                    thinking=model_profile.thinking,
                )
                messages.append(model_profile.prepare_messages_for_request([action.assistant_message])[0])
                tool_preamble = assistant_message_text(action.assistant_message)
                if tool_preamble:
                    await self.events.agent_output(project_id, state, tool_preamble)

                for tool_call in tool_calls:
                    result = await self._execute_tool_call(
                        project_id,
                        state,
                        tool_call,
                        caller_messages=messages,
                        system_prompt=system_prompt,
                    )

                    if tool_call.tool in DOCUMENT_WRITE_TOOL_NAMES and result.get("status") == "success":
                        output = result["output"]
                        changed_payload = {
                            "changed": True,
                            **output,
                            "active_section_id": output.get("primary_section_id"),
                            "active_block_id": output.get("primary_block_id"),
                        }
                        await self.bus.publish(
                            key,
                            "document_changed",
                            {
                                **changed_payload,
                                "round_id": state.round_id,
                                "message_id": state.message_id,
                            },
                        )

                    if (
                        tool_call.tool == "figure_kit"
                        and result.get("status") == "success"
                        and tool_call.arguments.get("action") in {"create", "update", "delete"}
                    ):
                        changed_payload = {
                            **changed_payload,
                            "changed": True,
                            "change_scope": "figure_asset_updated",
                            "active_section_id": changed_payload.get("active_section_id"),
                            "active_block_id": changed_payload.get("active_block_id"),
                        }
                        await self.bus.publish(
                            key,
                            "document_changed",
                            {
                                **changed_payload,
                                "round_id": state.round_id,
                                "message_id": state.message_id,
                            },
                        )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.tool_call_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                await self._sleep()

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
                    "round_id": state.round_id,
                    "message_id": state.message_id,
                },
            )
        except asyncio.CancelledError:
            logger.info(
                "round cancelled project=%s session=%s round=%s",
                project_id,
                state.session_id,
                state.round_id,
            )
            await self._mark_round_cancelled(project_id, state.session_id, state.round_id)
        except Exception as exc:
            logger.exception(
                "round failed project=%s session=%s round=%s",
                project_id,
                state.session_id,
                state.round_id,
            )
            failure_code = exc.code if isinstance(exc, ApiError) else "round_runtime_error"
            failure_message = exc.message if isinstance(exc, ApiError) else str(exc)
            self.store.append_session_event(
                project_id,
                state.session_id,
                event_type="agent_output",
                scope="main",
                round_id=state.round_id,
                message_id=state.message_id,
                payload={
                    "text": "本轮未完成，请重试或补充信息。",
                    "status": "failed",
                    "code": failure_code,
                    "message": failure_message,
                },
            )
            await self._set_project_idle(project_id)
            await self.bus.publish(
                key,
                "round_failed",
                {
                    "code": failure_code,
                    "message": failure_message,
                    "reply": "本轮未完成，请重试或补充信息。",
                    "round_id": state.round_id,
                    "message_id": state.message_id,
                },
            )

    async def _execute_tool_call(
        self,
        project_id: str,
        state: RoundState,
        tool_call: MainAgentToolCall,
        *,
        caller_messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> dict[str, Any]:
        logger.info(
            "round tool_call id=%s tool=%s arguments=%s",
            tool_call.tool_call_id,
            tool_call.tool,
            tool_call.arguments,
        )
        if tool_call.arguments_error:
            result = self._invalid_tool_arguments_json_result(tool_call.arguments_error)
            await self.events.failed_tool_result(
                project_id,
                state,
                tool=tool_call.tool,
                call_id=tool_call.tool_call_id,
                result=result,
            )
            logger.info(
                "round tool_call id=%s tool=%s status=%s",
                tool_call.tool_call_id,
                tool_call.tool,
                result.get("status"),
            )
            return result
        try:
            declaration = get_tool_declaration(tool_call.tool)
        except KeyError:
            result = tool_failed("unsupported_tool", f"主 agent 不支持的工具：{tool_call.tool}")
            await self.events.failed_tool_result(
                project_id,
                state,
                tool=tool_call.tool,
                call_id=tool_call.tool_call_id,
                result=result,
            )
            logger.info(
                "round tool_call id=%s tool=%s status=%s",
                tool_call.tool_call_id,
                tool_call.tool,
                result.get("status"),
            )
            return result

        await self.events.tool_started(
            project_id,
            state,
            tool=tool_call.tool,
            arguments=tool_call.arguments,
            summary=declaration.started_summary(tool_call.arguments),
            call_id=tool_call.tool_call_id,
        )
        result = await self.executor.execute_tool(
            project_id,
            tool_call.tool,
            tool_call.arguments,
            runtime_context=ToolRuntimeContext(
                session_id=state.session_id,
                round_id=state.round_id,
                message_id=state.message_id,
                caller_messages=caller_messages,
                system_prompt=system_prompt,
                tools=MAIN_AGENT_TOOLS,
                llm_client=self.llm_client,
                settings=self.settings,
            ),
        )
        await self.events.tool_finished(
            project_id,
            state,
            tool=tool_call.tool,
            result=result,
            summary=declaration.finished_summary(tool_call.arguments, result),
            call_id=tool_call.tool_call_id,
        )
        logger.info(
            "round tool_call id=%s tool=%s status=%s",
            tool_call.tool_call_id,
            tool_call.tool,
            result.get("status"),
        )
        return result

    @staticmethod
    def _invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
        return tool_failed("invalid_tool_arguments_json", message)

    async def _commit(self, project_id: str, changed_payload: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
        return await asyncio.to_thread(
            self.store.commit_workspace,
            project_id,
            build_commit_message(changed_payload),
        )

    async def _set_project_idle(self, project_id: str) -> None:
        def mark_idle() -> None:
            project = self.store.get_project(project_id)
            project.running_session_id = None
            project.running_round_id = None
            project.is_busy = False
            project.updated_at = now_iso()
            self.store.save_project(project)

        await self._run_with_project_lock(project_id, mark_idle)

    async def _mark_round_cancelled(self, project_id: str, session_id: str, round_id: str) -> dict[str, Any]:
        def mark_cancelled() -> tuple[str, bool]:
            project = self.store.get_project(project_id)
            if project.running_session_id == session_id and project.running_round_id == round_id:
                project.running_session_id = None
                project.running_round_id = None
                project.is_busy = False
                project.updated_at = now_iso()
                self.store.save_project(project)

            message_id = generate_id("msg")
            already_marked = False
            if self.store.session_exists(project_id, session_id):
                round_message_id, already_marked = self.store.round_message_status(
                    project_id,
                    session_id,
                    round_id,
                    "round_cancelled",
                )
                message_id = round_message_id or message_id
                if not already_marked:
                    self.store.append_session_event(
                        project_id,
                        session_id,
                        event_type="agent_output",
                        scope="main",
                        round_id=round_id,
                        message_id=message_id,
                        payload={
                            "text": "本轮任务已取消。",
                            "status": "cancelled",
                            "code": "round_cancelled",
                        },
                    )

            return message_id, already_marked

        message_id, already_marked = await self._run_with_project_lock(project_id, mark_cancelled)
        payload = {
            "cancelled": True,
            "project_id": project_id,
            "session_id": session_id,
            "round_id": round_id,
            "message_id": message_id,
            "reply": "本轮任务已取消。",
        }
        if not already_marked:
            await self.bus.publish((project_id, session_id), "round_cancelled", payload)
        return payload

    async def _sleep(self, duration: float | None = None) -> None:
        await asyncio.sleep(self.settings.round_step_delay if duration is None else duration)

    async def _run_with_project_lock(
        self,
        project_id: str,
        operation: Callable[[], LockedResult],
    ) -> LockedResult:
        lock_state = await self._get_project_lock(project_id)
        try:
            async with lock_state.lock:
                return operation()
        finally:
            await self._release_project_lock(project_id, lock_state)

    async def _get_project_lock(self, project_id: str) -> _ProjectLockState:
        async with self._project_locks_guard:
            lock_state = self._project_locks.get(project_id)
            if lock_state is None:
                lock_state = _ProjectLockState(lock=asyncio.Lock())
                self._project_locks[project_id] = lock_state
            lock_state.leases += 1
            return lock_state

    async def _release_project_lock(self, project_id: str, lock_state: _ProjectLockState) -> None:
        async with self._project_locks_guard:
            if self._project_locks.get(project_id) is not lock_state:
                return
            lock_state.leases = max(0, lock_state.leases - 1)
            if lock_state.leases > 0:
                return
            self._project_locks.pop(project_id, None)
