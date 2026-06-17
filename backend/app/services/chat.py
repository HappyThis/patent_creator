from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from ..agents.prompts import build_main_agent_system_prompt
from ..agents.runtime.model_profiles import resolve_model_profile
from ..agents.runtime.openai_compat import OpenAICompatibleClient
from ..agents.workers import MAIN_AGENT_TOOLS, MainAgentAction, MainAgentToolCall, decide_main_agent_step
from ..core import ApiError, Settings, generate_id, now_iso
from ..domain.document_tool_results import tool_failed
from ..runtime.context import ContextManager
from ..runtime.executor import ExecutorEngine, ToolRuntimeContext
from ..tools import DOCUMENT_WRITE_TOOL_NAMES, get_tool_declaration
from ..schemas import ChatMessageRequest, ChatMessageResponse
from ..storage.workspace_store import WorkspaceStore
from .chat_events import ChatEventEmitter
from .chat_protocol import DEFAULT_CHANGED_PAYLOAD, RoundState, assistant_message_text, build_commit_message
from .event_bus import SessionEventBus

logger = logging.getLogger("patent_creator.chat")


def _message_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = message.get("tool_calls")
    return [item for item in raw_tool_calls if isinstance(item, dict)] if isinstance(raw_tool_calls, list) else []


def _is_innovation_kernel_access_call(tool_call: dict[str, Any]) -> bool:
    function = tool_call.get("function")
    if not isinstance(function, dict) or function.get("name") != "innovation_kernel_kit":
        return False
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return False
    elif isinstance(arguments, dict):
        parsed_arguments = arguments
    else:
        return False
    return parsed_arguments.get("action") in {"read", "write"}


def _tool_result_has_kernel_markdown(content: Any) -> bool:
    if isinstance(content, str):
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            return False
    elif isinstance(content, dict):
        result = content
    else:
        return False
    if result.get("status") != "success":
        return False
    output = result.get("output")
    return isinstance(output, dict) and bool(str(output.get("kernel_markdown") or "").strip())


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
        self._project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._running_tasks: dict[tuple[str, str, str], asyncio.Task[None]] = {}

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
            first_user_text = payload.message
            if payload.session_id:
                existing_events = self.store.read_session_events(project_id, payload.session_id)
                first_user_event = next((event for event in existing_events if event.type == "user_input"), None)
                if first_user_event:
                    first_user_text = str(first_user_event.payload.get("text") or payload.message)
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

    async def start_round(self, project_id: str, payload: ChatMessageRequest) -> ChatMessageResponse:
        response, state = await self.prepare_round(project_id, payload)
        self.launch_round(project_id, payload, state)
        return response

    def launch_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        key = (project_id, state.session_id, state.round_id)
        task = asyncio.create_task(self._run_round(project_id, payload, state))
        self._running_tasks[key] = task

        def discard_finished_task(done_task: asyncio.Task[None]) -> None:
            if self._running_tasks.get(key) is done_task:
                self._running_tasks.pop(key, None)

        task.add_done_callback(discard_finished_task)

    async def cancel_round(self, project_id: str, session_id: str, round_id: str) -> dict[str, Any]:
        task: asyncio.Task[None] | None
        async with self._project_locks[project_id]:
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
                    active_section_id=payload.active_section_id,
                    active_block_id=payload.active_block_id,
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

                    if (
                        tool_call.tool == "innovation_kernel_kit"
                        and result.get("status") == "success"
                        and tool_call.arguments.get("action") == "write"
                    ):
                        await self.bus.publish(
                            key,
                            "innovation_kernel_changed",
                            {
                                **result["output"],
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
        if tool_call.tool in DOCUMENT_WRITE_TOOL_NAMES:
            if not self._session_has_innovation_kernel(project_id, state.session_id):
                result = self._innovation_kernel_required_result()
                await self.events.failed_tool_result(
                    project_id,
                    state,
                    tool=tool_call.tool,
                    call_id=tool_call.tool_call_id,
                    result=result,
                )
                logger.info(
                    "round tool_call id=%s tool=%s status=%s reason=innovation_kernel_required",
                    tool_call.tool_call_id,
                    tool_call.tool,
                    result.get("status"),
                )
                return result
            if not self._caller_messages_have_innovation_kernel_access(caller_messages):
                result = self._innovation_kernel_read_required_result()
                await self.events.failed_tool_result(
                    project_id,
                    state,
                    tool=tool_call.tool,
                    call_id=tool_call.tool_call_id,
                    result=result,
                )
                logger.info(
                    "round tool_call id=%s tool=%s status=%s reason=innovation_kernel_read_required",
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
                parent_call_id=tool_call.tool_call_id,
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

    @staticmethod
    def _innovation_kernel_required_result() -> dict[str, Any]:
        return tool_failed(
            "innovation_kernel_required",
            (
                "当前 session 尚无可依赖的创新内核，不能写入或改写交底书正文。"
                "请先基于当前上下文整理完整创新内核，并调用 innovation_kernel_kit.write 写入。"
                "后续修改交底书必须忠于创新内核，保持创新内核与交底书正文一致。"
            ),
        )

    @staticmethod
    def _innovation_kernel_read_required_result() -> dict[str, Any]:
        return tool_failed(
            "innovation_kernel_read_required",
            (
                "修改交底书前必须先读取当前完整创新内核。"
                "请先调用 innovation_kernel_kit.read，评估本次修改是否必要，以及是否与创新内核一致；"
                "若用户需求会改变发明核心，应先更新创新内核，再修改交底书。"
            ),
        )

    def _session_has_innovation_kernel(self, project_id: str, session_id: str) -> bool:
        kernel = self.store.get_innovation_kernel(project_id, session_id)
        return bool(kernel and kernel.kernel_markdown.strip())

    @staticmethod
    def _caller_messages_have_innovation_kernel_access(messages: list[dict[str, Any]]) -> bool:
        kernel_call_ids: set[str] = set()
        for message in messages:
            if message.get("role") == "assistant":
                for tool_call in _message_tool_calls(message):
                    call_id = str(tool_call.get("id") or "")
                    if call_id and _is_innovation_kernel_access_call(tool_call):
                        kernel_call_ids.add(call_id)
                continue
            if message.get("role") != "tool":
                continue
            tool_call_id = str(message.get("tool_call_id") or "")
            if tool_call_id in kernel_call_ids and _tool_result_has_kernel_markdown(message.get("content")):
                return True
        return False

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

    async def _mark_round_cancelled(self, project_id: str, session_id: str, round_id: str) -> dict[str, Any]:
        async with self._project_locks[project_id]:
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
                events = self.store.read_session_events(project_id, session_id)
                round_events = [event for event in events if event.round_id == round_id]
                user_event = next((event for event in round_events if event.type == "user_input"), None)
                if user_event:
                    message_id = user_event.message_id
                already_marked = any(
                    event.type == "agent_output"
                    and event.round_id == round_id
                    and event.payload.get("code") == "round_cancelled"
                    for event in round_events
                )
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
