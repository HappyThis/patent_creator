from __future__ import annotations

import asyncio
import difflib
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypeVar

from ..agents.prompts import build_main_agent_system_prompt
from ..agents.runtime.message_preparation import prepare_messages_for_model_request
from ..agents.workers import MAIN_AGENT_TOOLS, MainAgentToolCall, SupportsGenerateWithTools, decide_main_agent_step
from ..core import ApiError, Settings, generate_id, now_iso
from ..domain.document_tool_results import tool_failed
from ..runtime.context import ContextManager
from ..runtime.executor import ExecutorEngine, ToolRuntimeContext
from ..tools import DOCUMENT_WRITE_TOOL_NAMES, get_tool_declaration
from ..schemas import ChatMessageRequest, ChatMessageResponse, ProjectRecord, SessionEventType
from ..storage.workspace_store import WorkspaceStore
from .chat_events import ChatEventEmitter
from .chat_protocol import DEFAULT_CHANGED_PAYLOAD, RoundState, assistant_message_text, build_commit_message
from .event_bus import SessionEventBus
from .technical_solution_enhancement import (
    TechnicalSolutionChangeAssessmentResult,
    TechnicalSolutionChangeAssessor,
    TechnicalSolutionEnhancementSummaryResult,
    TechnicalSolutionEnhancementSummarizer,
    TechnicalSolutionImprovementAdviceResult,
    TechnicalSolutionImprovementAdvisor,
    TechnicalSolutionStructuredOutputValidationError,
    SupportsTechnicalSolutionGeneration,
    enhancement_feedback_user_message,
    technical_solution_markdown,
)

logger = logging.getLogger("patent_creator.chat")
LockedResult = TypeVar("LockedResult")
SESSION_TITLE_MAX_CHARS = 24
SESSION_TITLE_TIMEOUT = 12.0


def _tool_result_message(tool_call: MainAgentToolCall, result: dict[str, Any], state: RoundState) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.tool_call_id,
        "content": json.dumps(result, ensure_ascii=False),
        "tool_name": tool_call.tool,
        "round_id": state.round_id,
        "message_id": state.message_id,
    }


class SupportsChatLLM(SupportsGenerateWithTools, SupportsTechnicalSolutionGeneration, Protocol):
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        ...


@dataclass
class _ProjectLockState:
    lock: asyncio.Lock
    leases: int = 0


@dataclass(slots=True)
class _PendingTechnicalSolutionEnhancement:
    initial_after: str
    initial_diff: str
    assessment: TechnicalSolutionChangeAssessmentResult
    review_markdown: str
    before_enhancement: str


class ChatService:
    def __init__(
        self,
        store: WorkspaceStore,
        context_manager: ContextManager,
        executor: ExecutorEngine,
        bus: SessionEventBus,
        settings: Settings,
        llm_client: SupportsChatLLM,
    ) -> None:
        self.store = store
        self.context_manager = context_manager
        self.executor = executor
        self.bus = bus
        self.settings = settings
        self.llm_client = llm_client
        self.events = ChatEventEmitter(store, bus, executor)
        self.technical_solution_change_assessor = TechnicalSolutionChangeAssessor(llm_client, settings)
        self.technical_solution_improvement_advisor = TechnicalSolutionImprovementAdvisor(llm_client, settings)
        self.technical_solution_enhancement_summarizer = TechnicalSolutionEnhancementSummarizer(llm_client, settings)
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

    async def delete_session(self, project_id: str, session_id: str) -> str | None:
        return await self._run_with_project_lock(project_id, lambda: self.store.delete_session(project_id, session_id))

    def launch_round(self, project_id: str, payload: ChatMessageRequest, state: RoundState) -> None:
        if not payload.session_id:
            self._launch_session_title_task(project_id, payload.message, state)

        key = (project_id, state.session_id, state.round_id)
        task = asyncio.create_task(self._run_round(project_id, payload, state))
        self._running_tasks[key] = task

        def discard_finished_task(done_task: asyncio.Task[None]) -> None:
            if self._running_tasks.get(key) is done_task:
                self._running_tasks.pop(key, None)

        task.add_done_callback(discard_finished_task)

    def _launch_session_title_task(self, project_id: str, user_message: str, state: RoundState) -> None:
        task = asyncio.create_task(self._generate_session_title(project_id, user_message, state))

        def log_title_task_error(done_task: asyncio.Task[None]) -> None:
            try:
                done_task.result()
            except Exception:
                logger.exception(
                    "session title task failed project=%s session=%s round=%s",
                    project_id,
                    state.session_id,
                    state.round_id,
                )

        task.add_done_callback(log_title_task_error)

    async def _generate_session_title(self, project_id: str, user_message: str, state: RoundState) -> None:
        title = await self._summarize_session_title(user_message)
        if not title:
            return

        def append_title() -> bool:
            if not self.store.session_exists(project_id, state.session_id):
                return False
            self.store.append_session_event(
                project_id,
                state.session_id,
                event_type="session_title",
                scope="main",
                round_id=state.round_id,
                message_id=state.message_id,
                payload={"title": title},
            )
            return True

        appended = await self._run_with_project_lock(project_id, append_title)
        if not appended:
            return
        await self.bus.publish(
            (project_id, state.session_id),
            "session_title_updated",
            {
                "session_id": state.session_id,
                "round_id": state.round_id,
                "message_id": state.message_id,
                "title": title,
            },
        )

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
        llm_retry_seen = False
        llm_retry_finalized = False
        last_llm_retry_payload: dict[str, Any] | None = None

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

        async def publish_context_usage(reason: str, *, step_index: int | None = None) -> None:
            usage = self.context_manager.context_usage(project_id, state.session_id)
            if usage is None:
                return
            payload: dict[str, Any] = {
                **usage.model_dump(),
                "scope": "main",
                "session_id": state.session_id,
                "round_id": state.round_id,
                "message_id": state.message_id,
                "reason": reason,
            }
            if step_index is not None:
                payload["step_index"] = step_index
            await self.bus.publish(key, "context_usage_updated", payload)

        async def publish_llm_retry_status(event_payload: dict[str, Any]) -> None:
            stored_payload = {**event_payload, "scope": "main"}
            await self._record_and_publish_main_event(
                project_id,
                state,
                event_type="llm_retry_status",
                payload=stored_payload,
                event_name="llm_retry_status",
            )

        async def on_llm_retry_event(event_payload: dict[str, Any]) -> None:
            nonlocal llm_retry_seen, last_llm_retry_payload
            llm_retry_seen = True
            last_llm_retry_payload = dict(event_payload)
            await publish_llm_retry_status(event_payload)

        async def finalize_llm_retry_status(status: str, error_message: str | None = None) -> None:
            nonlocal llm_retry_finalized, last_llm_retry_payload
            if not llm_retry_seen or llm_retry_finalized or last_llm_retry_payload is None:
                return
            llm_retry_finalized = True
            final_payload = {
                **last_llm_retry_payload,
                "status": status,
                "retry_after_seconds": 0,
                "retry_at_ms": None,
            }
            if error_message:
                final_payload["error_message"] = error_message
            last_llm_retry_payload = final_payload
            await publish_llm_retry_status(final_payload)

        changed_payload: dict[str, Any] = dict(DEFAULT_CHANGED_PAYLOAD)
        changed_payload["active_section_id"] = payload.active_section_id
        changed_payload["active_block_id"] = payload.active_block_id
        final_reply: str | None = None
        final_reply_status: str | None = None
        final_reply_detail: str | None = None
        messages: list[dict[str, Any]] = []
        technical_solution_before = self._current_technical_solution_markdown(project_id)
        enhancement_attempted = False
        pending_enhancement: _PendingTechnicalSolutionEnhancement | None = None
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
                    on_retry_event=on_llm_retry_event,
                )
                await publish_context_usage("before_main_agent_call", step_index=step_index)

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

                async def on_audit_event(audit_event: dict[str, Any]) -> None:
                    if audit_event.get("category") != "web_search":
                        return
                    await self.bus.publish(
                        key,
                        "web_search_progress",
                        {
                            **audit_event,
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
                    on_audit_event=on_audit_event,
                    on_retry_event=on_llm_retry_event,
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
                    assistant_message = action.assistant_message
                    if assistant_message is None:
                        raise ApiError(502, "main_agent_invalid_action", "主 agent respond 缺少 assistant_message。")
                    final_reply = action.text or ""
                    if action.interrupted:
                        final_reply_status = "interrupted"
                        final_reply_detail = "输出中断，已保留当前内容。"
                    await self.events.audit_events(
                        project_id,
                        state,
                        events=action.audit_events or [],
                    )
                    await self.events.agent_message(
                        project_id,
                        state,
                        message=assistant_message,
                        model=self.settings.openai_model,
                    )
                    logger.info(
                        "round step=%d action=respond text_len=%d",
                        step_index,
                        len(final_reply),
                    )
                    messages.append(prepare_messages_for_model_request([assistant_message])[0])
                    await self.events.agent_output(
                        project_id,
                        state,
                        final_reply,
                        status=final_reply_status,
                        detail=final_reply_detail,
                    )
                    await publish_context_usage("after_main_agent_call", step_index=step_index)

                    if action.interrupted:
                        break

                    if pending_enhancement is not None:
                        enhanced_after = self._current_technical_solution_markdown(project_id)
                        enhancement_diff = self._technical_solution_diff(
                            pending_enhancement.before_enhancement,
                            enhanced_after,
                        )
                        summary_result = await self._run_technical_solution_enhancement_summary(
                            project_id,
                            state,
                            review_markdown=pending_enhancement.review_markdown,
                            enhanced_technical_solution_markdown=enhanced_after,
                            enhancement_diff=enhancement_diff,
                            on_retry_event=on_llm_retry_event,
                        )
                        await self._append_technical_solution_enhancement_record(
                            project_id,
                            state,
                            user_request=payload.message,
                            initial_after=pending_enhancement.initial_after,
                            initial_diff=pending_enhancement.initial_diff,
                            assessment=pending_enhancement.assessment,
                            review_markdown=pending_enhancement.review_markdown,
                            enhanced_after=enhanced_after,
                            enhancement_diff=enhancement_diff,
                            summary=summary_result,
                        )
                        await self._publish_technical_solution_enhancement_status(
                            project_id,
                            state,
                            phase="completed",
                            status="done",
                            progress=100,
                            summary="增强模式：已完成技术方案增强记录",
                        )
                        break

                    if payload.quality_mode == "enhanced" and not enhancement_attempted:
                        technical_solution_after = self._current_technical_solution_markdown(project_id)
                        if technical_solution_after != technical_solution_before:
                            enhancement_attempted = True
                            initial_diff = self._technical_solution_diff(technical_solution_before, technical_solution_after)
                            assessment = await self._run_technical_solution_change_assessment(
                                project_id,
                                state,
                                user_request=payload.message,
                                technical_solution_markdown_value=technical_solution_after,
                                technical_solution_diff=initial_diff,
                                on_retry_event=on_llm_retry_event,
                            )
                            if assessment is None:
                                await self._publish_technical_solution_enhancement_status(
                                    project_id,
                                    state,
                                    phase="failed",
                                    status="failed",
                                    progress=100,
                                    summary="增强模式：技术方案增强未完成",
                                )
                                break

                            if not assessment.should_review:
                                await self._publish_technical_solution_enhancement_status(
                                    project_id,
                                    state,
                                    phase="completed",
                                    status="done",
                                    progress=100,
                                    summary="增强模式：已完成",
                                )
                                break

                            advice = await self._run_technical_solution_improvement_advice(
                                project_id,
                                state,
                                user_request=payload.message,
                                technical_solution_markdown_value=technical_solution_after,
                                technical_solution_diff=initial_diff,
                                on_retry_event=on_llm_retry_event,
                            )
                            if advice is None:
                                await self._publish_technical_solution_enhancement_status(
                                    project_id,
                                    state,
                                    phase="failed",
                                    status="failed",
                                    progress=100,
                                    summary="增强模式：技术方案增强未完成",
                                )
                                break

                            await self._append_technical_solution_enhancement_feedback(project_id, state, advice)
                            pending_enhancement = _PendingTechnicalSolutionEnhancement(
                                initial_after=technical_solution_after,
                                initial_diff=initial_diff,
                                assessment=assessment,
                                review_markdown=advice.review_markdown,
                                before_enhancement=technical_solution_after,
                            )
                            technical_solution_before = technical_solution_after
                            await self._sleep()
                            continue
                    break

                tool_calls = action.tool_calls or []
                assistant_message = action.assistant_message
                if assistant_message is None:
                    raise ApiError(502, "main_agent_invalid_action", "主 agent tool_calls 缺少 assistant_message。")
                logger.info("round step=%d action=tool_calls count=%d", step_index, len(tool_calls))
                await self.events.audit_events(
                    project_id,
                    state,
                    events=action.audit_events or [],
                )
                await self.events.agent_message(
                    project_id,
                    state,
                    message=assistant_message,
                    model=self.settings.openai_model,
                )
                messages.append(prepare_messages_for_model_request([assistant_message])[0])
                tool_preamble = assistant_message_text(assistant_message)
                if tool_preamble:
                    await self.events.agent_output(project_id, state, tool_preamble)
                await publish_context_usage("after_main_agent_call", step_index=step_index)

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
                        and tool_call.arguments.get("action") in {"write", "update", "delete"}
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

                    messages.append(_tool_result_message(tool_call, result, state))
                await publish_context_usage("after_tool_results", step_index=step_index)
                await self._sleep()

            await self._sleep(self.settings.round_finish_delay)
            committed, commit_error = await self._commit(project_id, changed_payload)
            await finalize_llm_retry_status(
                "failed" if final_reply_status == "interrupted" else "done",
                final_reply_detail if final_reply_status == "interrupted" else None,
            )
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
                    "reply_status": final_reply_status,
                    "reply_detail": final_reply_detail,
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
            await finalize_llm_retry_status("failed", failure_message)
            partial_changed = changed_payload.get("changed") is True
            failed_reply = (
                "已完成部分修改，但模型连接失败，未生成最终回复。请重试继续处理。"
                if partial_changed
                else "本轮未完成，请重试或补充信息。"
            )
            self.store.append_session_event(
                project_id,
                state.session_id,
                event_type="agent_output",
                scope="main",
                round_id=state.round_id,
                message_id=state.message_id,
                payload={
                    "text": failed_reply,
                    "status": "failed",
                    "code": failure_code,
                    "message": failure_message,
                    **changed_payload,
                },
            )
            await publish_context_usage("round_failed")
            committed = False
            commit_error = None
            if partial_changed:
                committed, commit_error = await self._commit(project_id, changed_payload)
            await self._set_project_idle(project_id)
            await self.bus.publish(
                key,
                "round_failed",
                {
                    "code": failure_code,
                    "message": failure_message,
                    "reply": failed_reply,
                    **changed_payload,
                    "committed": committed,
                    "commit_error": commit_error,
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
            summary=self._safe_tool_started_summary(declaration, tool_call.arguments),
            call_id=tool_call.tool_call_id,
        )
        try:
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
                    figure_review_states=state.figure_review_states,
                    figure_drawio_versions=state.figure_drawio_versions,
                ),
            )
        except Exception as exc:
            result = tool_failed("tool_runtime_error", str(exc))
            await self.events.tool_finished(
                project_id,
                state,
                tool=tool_call.tool,
                result=result,
                summary=self._safe_tool_finished_summary(declaration, tool_call.arguments, result),
                call_id=tool_call.tool_call_id,
            )
            raise
        await self.events.tool_finished(
            project_id,
            state,
            tool=tool_call.tool,
            result=result,
            summary=self._safe_tool_finished_summary(declaration, tool_call.arguments, result),
            call_id=tool_call.tool_call_id,
        )
        logger.info(
            "round tool_call id=%s tool=%s status=%s",
            tool_call.tool_call_id,
            tool_call.tool,
            result.get("status"),
        )
        return result

    def _current_technical_solution_markdown(self, project_id: str) -> str:
        return technical_solution_markdown(self.store.get_disclosure(project_id))

    async def _record_and_publish_main_event(
        self,
        project_id: str,
        state: RoundState,
        *,
        event_type: SessionEventType,
        payload: dict[str, Any],
        event_name: str | None = None,
    ) -> None:
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type=event_type,
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            payload=payload,
        )
        if event_name is None:
            return
        await self.bus.publish(
            (project_id, state.session_id),
            event_name,
            {
                **payload,
                "scope": "main",
                "round_id": state.round_id,
                "message_id": state.message_id,
            },
        )

    @staticmethod
    def _technical_solution_trace_context(project_id: str, state: RoundState) -> dict[str, str]:
        return {
            "project_id": project_id,
            "session_id": state.session_id,
            "round_id": state.round_id,
            "message_id": state.message_id,
        }

    @staticmethod
    def _technical_solution_error_payload(
        exc: Exception,
        *,
        validation_code: str,
        fallback_code: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "code": validation_code if isinstance(exc, TechnicalSolutionStructuredOutputValidationError) else fallback_code,
            "message": str(exc),
        }
        if isinstance(exc, TechnicalSolutionStructuredOutputValidationError):
            payload["attempts"] = exc.attempts
        return payload

    @staticmethod
    def _safe_tool_started_summary(declaration: Any, arguments: dict[str, Any]) -> str:
        try:
            return declaration.started_summary(arguments)
        except Exception as exc:
            logger.warning("tool started summary failed tool=%s error=%s", getattr(declaration, "name", ""), exc)
            return f"开始执行 {getattr(declaration, 'name', '工具')}"

    @staticmethod
    def _safe_tool_finished_summary(
        declaration: Any,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        try:
            return declaration.finished_summary(arguments, result)
        except Exception as exc:
            logger.warning("tool finished summary failed tool=%s error=%s", getattr(declaration, "name", ""), exc)
            return "执行失败" if result.get("status") == "failed" else f"{getattr(declaration, 'name', '工具')} 已完成"

    @staticmethod
    def _technical_solution_diff(before: str, after: str) -> str:
        return "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="before.md",
                tofile="after.md",
                lineterm="",
            )
        ).strip()

    async def _publish_technical_solution_enhancement_status(
        self,
        project_id: str,
        state: RoundState,
        *,
        phase: str,
        status: str,
        progress: int,
        summary: str,
        detail: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "progress": max(0, min(100, progress)),
            "summary": summary,
        }
        if detail:
            payload["detail"] = detail
        await self._record_and_publish_main_event(
            project_id,
            state,
            event_type="technical_solution_enhancement_status",
            payload=payload,
            event_name="quality_enhancement_status",
        )

    async def _run_technical_solution_change_assessment(
        self,
        project_id: str,
        state: RoundState,
        *,
        user_request: str,
        technical_solution_markdown_value: str,
        technical_solution_diff: str,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionChangeAssessmentResult | None:
        await self._publish_technical_solution_enhancement_status(
            project_id,
            state,
            phase="assessing",
            status="running",
            progress=25,
            summary="增强模式：正在评估本轮修改...",
        )
        try:
            result = await self.technical_solution_change_assessor.assess(
                user_request=user_request,
                technical_solution_markdown=technical_solution_markdown_value,
                technical_solution_diff=technical_solution_diff,
                trace_context=self._technical_solution_trace_context(project_id, state),
                on_retry_event=on_retry_event,
            )
        except Exception as exc:
            logger.warning(
                "technical solution change assessment skipped project=%s session=%s round=%s error=%s",
                project_id,
                state.session_id,
                state.round_id,
                exc,
            )
            payload = self._technical_solution_error_payload(
                exc,
                validation_code="technical_solution_change_assessment_validation_failed",
                fallback_code="technical_solution_change_assessment_failed",
            )
            await self._record_and_publish_main_event(
                project_id,
                state,
                event_type="technical_solution_change_assessment",
                payload=payload,
                event_name="technical_solution_change_assessment",
            )
            return None

        payload = {"status": "success", **result.as_payload()}
        await self._record_and_publish_main_event(
            project_id,
            state,
            event_type="technical_solution_change_assessment",
            payload=payload,
            event_name="technical_solution_change_assessment",
        )
        return result

    async def _run_technical_solution_improvement_advice(
        self,
        project_id: str,
        state: RoundState,
        *,
        user_request: str,
        technical_solution_markdown_value: str,
        technical_solution_diff: str,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionImprovementAdviceResult | None:
        await self._publish_technical_solution_enhancement_status(
            project_id,
            state,
            phase="enhancing",
            status="running",
            progress=70,
            summary="增强模式：正在完善技术方案...",
        )
        recent_history = self.store.recent_technical_solution_enhancement_history(project_id, limit=3)
        try:
            result = await self.technical_solution_improvement_advisor.advise(
                technical_solution_markdown=technical_solution_markdown_value,
                user_request=user_request,
                technical_solution_diff=technical_solution_diff,
                recent_history=recent_history,
                trace_context=self._technical_solution_trace_context(project_id, state),
                on_retry_event=on_retry_event,
            )
        except Exception as exc:
            logger.warning(
                "technical solution improvement advice skipped project=%s session=%s round=%s error=%s",
                project_id,
                state.session_id,
                state.round_id,
                exc,
            )
            payload = self._technical_solution_error_payload(
                exc,
                validation_code="technical_solution_improvement_advice_validation_failed",
                fallback_code="technical_solution_improvement_advice_failed",
            )
            await self._record_and_publish_main_event(
                project_id,
                state,
                event_type="technical_solution_improvement_advice",
                payload=payload,
                event_name="technical_solution_improvement_advice",
            )
            return None

        payload = {"status": "success", **result.as_payload()}
        await self._record_and_publish_main_event(
            project_id,
            state,
            event_type="technical_solution_improvement_advice",
            payload=payload,
            event_name="technical_solution_improvement_advice",
        )
        return result

    async def _append_technical_solution_enhancement_feedback(
        self,
        project_id: str,
        state: RoundState,
        result: TechnicalSolutionImprovementAdviceResult,
    ) -> None:
        feedback = enhancement_feedback_user_message(result)
        self.store.append_session_event(
            project_id,
            state.session_id,
            event_type="technical_solution_enhancement_feedback",
            scope="main",
            round_id=state.round_id,
            message_id=state.message_id,
            payload={
                "text": feedback,
                "review_markdown": result.review_markdown,
                "hidden": True,
            },
        )

    async def _run_technical_solution_enhancement_summary(
        self,
        project_id: str,
        state: RoundState,
        *,
        review_markdown: str,
        enhanced_technical_solution_markdown: str,
        enhancement_diff: str,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionEnhancementSummaryResult | None:
        await self._publish_technical_solution_enhancement_status(
            project_id,
            state,
            phase="summarizing",
            status="running",
            progress=90,
            summary="增强模式：正在整理增强记录...",
        )
        try:
            return await self.technical_solution_enhancement_summarizer.summarize(
                review_markdown=review_markdown,
                enhanced_technical_solution_markdown=enhanced_technical_solution_markdown,
                enhancement_diff=enhancement_diff,
                trace_context=self._technical_solution_trace_context(project_id, state),
                on_retry_event=on_retry_event,
            )
        except Exception as exc:
            logger.warning(
                "technical solution enhancement summary skipped project=%s session=%s round=%s error=%s",
                project_id,
                state.session_id,
                state.round_id,
                exc,
            )
            return None

    async def _append_technical_solution_enhancement_record(
        self,
        project_id: str,
        state: RoundState,
        *,
        user_request: str,
        initial_after: str,
        initial_diff: str,
        assessment: TechnicalSolutionChangeAssessmentResult,
        review_markdown: str,
        enhanced_after: str,
        enhancement_diff: str,
        summary: TechnicalSolutionEnhancementSummaryResult | None,
    ) -> None:
        record = {
            "id": generate_id("tseh"),
            "created_at": now_iso(),
            "user_request": user_request,
            "initial_after": initial_after,
            "initial_diff": initial_diff,
            "assessment": assessment.as_payload(),
            "advisor": {"review_markdown": review_markdown},
            "enhanced_after": enhanced_after,
            "enhancement_diff": enhancement_diff,
            "summary": summary.as_payload() if summary is not None else None,
        }
        payload = {
            "status": "success" if summary is not None else "summary_skipped",
            "applied_summary": summary.applied_summary if summary is not None else "",
            "record": record,
        }
        await self._record_and_publish_main_event(
            project_id,
            state,
            event_type="technical_solution_enhancement_summary",
            payload=payload,
            event_name="technical_solution_enhancement_summary",
        )

    @staticmethod
    def _invalid_tool_arguments_json_result(message: str) -> dict[str, Any]:
        return tool_failed("invalid_tool_arguments_json", message)

    async def _commit(self, project_id: str, changed_payload: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
        return await asyncio.to_thread(
            self.store.commit_workspace,
            project_id,
            build_commit_message(changed_payload),
        )

    async def _summarize_session_title(self, user_message: str) -> str | None:
        try:
            result = await self.llm_client.generate_json(
                system_prompt="你负责把用户发起的一轮专利写作对话概括成侧边栏标题。",
                user_prompt=(
                    "请为下面这条用户消息生成一个中文短标题，并严格返回 JSON 对象。"
                    f"JSON schema: {{\"title\":\"不超过 {SESSION_TITLE_MAX_CHARS} 个字符的标题\"}}。"
                    "不要返回 Markdown，不要返回解释，不要返回额外字段。\n\n"
                    f"用户消息：\n{user_message}"
                ),
                temperature=0.1,
                timeout=min(self.settings.llm_timeout, SESSION_TITLE_TIMEOUT),
                trace_context={"project_phase": "session_title"},
            )
        except Exception as exc:
            logger.warning("session title generation failed: %s", exc)
            return None

        raw_title = result.get("title")
        if not isinstance(raw_title, str):
            logger.warning("session title generation returned invalid payload: %s", result)
            return None
        return self._clean_session_title(raw_title) or None

    @staticmethod
    def _clean_session_title(raw_title: str) -> str:
        normalized = " ".join(raw_title.split()).strip()
        if not normalized:
            return ""
        lowered = normalized.lower()
        if "<analysis" in lowered or "<summary" in lowered:
            return ""
        normalized = normalized.lstrip("#-0123456789.、)） ")
        normalized = normalized.strip("`'\"“”‘’《》「」 ")
        for prefix in ("标题：", "标题:", "对话标题：", "对话标题:", "session title:", "Session title:"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
                break
        return normalized[:SESSION_TITLE_MAX_CHARS].strip()

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
