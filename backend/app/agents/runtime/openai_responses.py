from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpcore
import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from ...core import ApiError, Settings

logger = logging.getLogger("patent_creator.llm")

RetryEventSink = Callable[[dict[str, Any]], Awaitable[None]]

TRANSIENT_STREAM_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpcore.ConnectError,
    httpcore.ReadError,
    httpcore.RemoteProtocolError,
)

RETRYABLE_LLM_ERRORS = (
    APIStatusError,
    APIConnectionError,
    APITimeoutError,
    APIError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpcore.ConnectError,
    httpcore.ReadError,
    httpcore.RemoteProtocolError,
)

RETRYABLE_STREAM_API_ERROR_CODES = {"llm_stream_error", "llm_empty_response"}


class OpenAIResponsesClient:
    """OpenAI Responses API runtime used by the patent-writing agent."""

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = client

    def _require_client(self) -> AsyncOpenAI:
        if self._client is not None:
            return self._client
        if not self.settings.openai_api_key:
            raise ApiError(
                500,
                "llm_api_key_missing",
                "未配置 OPENAI_API_KEY，无法调用 OpenAI Responses API。",
            )
        self._client = AsyncOpenAI(
            base_url=self.settings.openai_base_url,
            api_key=self.settings.openai_api_key,
            timeout=self.settings.llm_timeout,
            max_retries=self.settings.openai_sdk_max_retries,
        )
        return self._client

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: RetryEventSink | None = None,
    ) -> str:
        client = self._require_client()
        input_items = [*_messages_to_response_input(messages or []), {"role": "user", "content": user_prompt}]
        request_payload = self._base_request_payload(
            instructions=system_prompt,
            input_items=input_items,
            temperature=temperature,
        )
        self._write_llm_payload_trace(
            kind="generate_text",
            request_payload=request_payload,
            trace_context=trace_context,
            request_options={"timeout": timeout if timeout is not None else self.settings.llm_timeout},
        )
        started = time.monotonic()
        response = await self._create_response_with_retries(
            kind="generate_text",
            create_call=lambda: client.responses.create(**request_payload, timeout=timeout),
            on_retry_event=on_retry_event,
        )

        logger.info(
            "generate_text done model=%s elapsed=%.2fs timeout=%s usage=%s",
            self.settings.openai_model,
            time.monotonic() - started,
            timeout if timeout is not None else self.settings.llm_timeout,
            _describe_usage(getattr(response, "usage", None)),
        )
        return _extract_response_text(response)

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: RetryEventSink | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        request_payload = self._base_request_payload(
            instructions=system_prompt,
            input_items=[{"role": "user", "content": user_prompt}],
            temperature=temperature,
        )
        request_payload["text"] = {"format": {"type": "json_object"}}
        self._write_llm_payload_trace(
            kind="generate_json",
            request_payload=request_payload,
            trace_context=trace_context,
            request_options={"timeout": timeout if timeout is not None else self.settings.llm_timeout},
        )
        started = time.monotonic()
        response = await self._create_response_with_retries(
            kind="generate_json",
            create_call=lambda: client.responses.create(**request_payload, timeout=timeout),
            on_retry_event=on_retry_event,
        )

        logger.info(
            "generate_json done model=%s elapsed=%.2fs timeout=%s usage=%s",
            self.settings.openai_model,
            time.monotonic() - started,
            timeout if timeout is not None else self.settings.llm_timeout,
            _describe_usage(getattr(response, "usage", None)),
        )
        content = _extract_response_text(response)
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("generate_json invalid_json content=%s", content[:500])
            raise ApiError(502, "llm_invalid_json", "模型返回的内容不是合法 JSON。") from exc

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_audit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_retry_event: RetryEventSink | None = None,
        response_format_json: bool = False,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        tool_names = [t.get("function", {}).get("name") for t in tools]
        logger.info(
            "generate_with_tools_stream request model=%s tools=%s messages_count=%d web_search=%s",
            self.settings.openai_model,
            tool_names,
            len(messages),
            self.settings.openai_web_search_enabled,
        )
        request_payload = self._base_request_payload(
            instructions=system_prompt,
            input_items=_messages_to_response_input(messages),
            temperature=None,
        )
        request_payload["tools"] = self._responses_tools(tools)
        request_payload["tool_choice"] = "auto"
        request_payload["stream"] = True
        if response_format_json:
            request_payload["text"] = {"format": {"type": "json_object"}}
        self._write_llm_payload_trace(
            kind="generate_with_tools_stream",
            request_payload=request_payload,
            trace_context={**(trace_context or {}), "tool_names": tool_names},
        )

        max_attempts = self._max_llm_attempts()
        for attempt in range(1, max_attempts + 1):
            text_delta_emitted = False
            text_parts: list[str] = []

            async def guarded_on_text_delta(delta: str) -> None:
                nonlocal text_delta_emitted
                text_delta_emitted = True
                text_parts.append(delta)
                if on_text_delta is not None:
                    await on_text_delta(delta)

            try:
                started = time.monotonic()
                if attempt > 1:
                    logger.info(
                        "generate_with_tools_stream retry attempt=%d/%d model=%s",
                        attempt,
                        max_attempts,
                        self.settings.openai_model,
                    )
                stream = await client.responses.create(**request_payload)
                return await self._consume_responses_stream(
                    stream,
                    started=started,
                    on_text_delta=guarded_on_text_delta if on_text_delta is not None else None,
                    on_audit_event=on_audit_event,
                    text_delta_emitted=lambda: text_delta_emitted,
                )
            except ApiError as exc:
                if text_delta_emitted and exc.code in RETRYABLE_STREAM_API_ERROR_CODES:
                    logger.warning(
                        "generate_with_tools_stream interrupted_after_text_delta attempt=%d/%d code=%s",
                        attempt,
                        max_attempts,
                        exc.code,
                    )
                    return _partial_stream_result("".join(text_parts), interrupted_message=exc.message)
                if exc.code not in RETRYABLE_STREAM_API_ERROR_CODES or attempt >= max_attempts:
                    raise
                logger.warning(
                    "generate_with_tools_stream api_stream_error retrying attempt=%d/%d code=%s error=%s",
                    attempt,
                    max_attempts,
                    exc.code,
                    exc,
                )
                await self._wait_before_retry(
                    kind="generate_with_tools_stream",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    on_retry_event=on_retry_event,
                    error=exc,
                )
            except APIStatusError as exc:
                if text_delta_emitted:
                    logger.warning(
                        "generate_with_tools_stream http_error_after_text_delta attempt=%d/%d status=%s",
                        attempt,
                        max_attempts,
                        exc.status_code,
                    )
                    return _partial_stream_result("".join(text_parts), interrupted_message=_describe_api_error(exc))
                if attempt >= max_attempts:
                    logger.warning(
                        "generate_with_tools_stream http_error final attempt=%d/%d status=%s body=%s",
                        attempt,
                        max_attempts,
                        exc.status_code,
                        _describe_api_error(exc),
                    )
                    raise _api_error_from_llm_exception(exc) from exc
                logger.warning(
                    "generate_with_tools_stream http_error retrying attempt=%d/%d status=%s body=%s",
                    attempt,
                    max_attempts,
                    exc.status_code,
                    _describe_api_error(exc),
                )
                await self._wait_before_retry(
                    kind="generate_with_tools_stream",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    on_retry_event=on_retry_event,
                    error=exc,
                )
            except TRANSIENT_STREAM_ERRORS as exc:
                if text_delta_emitted or attempt >= max_attempts:
                    logger.warning(
                        "generate_with_tools_stream transient_error final attempt=%d/%d emitted_text=%s error=%s",
                        attempt,
                        max_attempts,
                        text_delta_emitted,
                        exc,
                    )
                    if text_delta_emitted:
                        return _partial_stream_result("".join(text_parts), interrupted_message=str(exc))
                    raise ApiError(502, "llm_stream_error", f"模型流式响应中断：{exc}") from exc
                logger.warning(
                    "generate_with_tools_stream transient_error retrying attempt=%d/%d error=%s",
                    attempt,
                    max_attempts,
                    exc,
                )
                await self._wait_before_retry(
                    kind="generate_with_tools_stream",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    on_retry_event=on_retry_event,
                    error=exc,
                )
            except APIError as exc:
                if text_delta_emitted:
                    logger.warning(
                        "generate_with_tools_stream api_error_after_text_delta attempt=%d/%d error=%s",
                        attempt,
                        max_attempts,
                        exc,
                    )
                    return _partial_stream_result("".join(text_parts), interrupted_message=str(exc))
                if attempt >= max_attempts:
                    logger.warning("generate_with_tools_stream api_error final attempt=%d/%d error=%s", attempt, max_attempts, exc)
                    raise _api_error_from_llm_exception(exc) from exc
                logger.warning("generate_with_tools_stream api_error retrying attempt=%d/%d error=%s", attempt, max_attempts, exc)
                await self._wait_before_retry(
                    kind="generate_with_tools_stream",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    on_retry_event=on_retry_event,
                    error=exc,
                )

        raise ApiError(502, "llm_stream_error", "模型流式响应中断。")

    async def _create_response_with_retries(
        self,
        *,
        kind: str,
        create_call: Callable[[], Awaitable[Any]],
        on_retry_event: RetryEventSink | None,
    ) -> Any:
        max_attempts = self._max_llm_attempts()
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    logger.info("%s retry attempt=%d/%d model=%s", kind, attempt, max_attempts, self.settings.openai_model)
                return await create_call()
            except RETRYABLE_LLM_ERRORS as exc:
                if attempt >= max_attempts:
                    logger.warning("%s final_error attempt=%d/%d error=%s", kind, attempt, max_attempts, exc)
                    raise _api_error_from_llm_exception(exc) from exc
                logger.warning("%s retryable_error attempt=%d/%d error=%s", kind, attempt, max_attempts, exc)
                await self._wait_before_retry(
                    kind=kind,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    on_retry_event=on_retry_event,
                    error=exc,
                )
        raise ApiError(502, "llm_http_error", "模型调用失败。")

    async def _wait_before_retry(
        self,
        *,
        kind: str,
        attempt: int,
        max_attempts: int,
        on_retry_event: RetryEventSink | None,
        error: Exception,
    ) -> None:
        next_attempt = attempt + 1
        retry_index = attempt
        max_retries = max(0, max_attempts - 1)
        delay = max(0.0, self.settings.llm_retry_delay_seconds)
        error_message = _retry_error_message(error)
        retry_at_ms = int((time.time() + delay) * 1000) if delay > 0 else None
        logger.info(
            "%s retry_wait next_attempt=%d/%d delay=%.2fs error_type=%s",
            kind,
            next_attempt,
            max_attempts,
            delay,
            type(error).__name__,
        )
        if on_retry_event is not None:
            await on_retry_event(
                {
                    "status": "waiting",
                    "reason": "模型连接失败",
                    "attempt": next_attempt,
                    "max_attempts": max_attempts,
                    "retry_index": retry_index,
                    "max_retries": max_retries,
                    "retry_after_seconds": delay,
                    "retry_at_ms": retry_at_ms,
                    "error_type": type(error).__name__,
                    "error_message": error_message,
                    "kind": kind,
                }
            )
        if delay > 0:
            await asyncio.sleep(delay)
        if on_retry_event is not None:
            await on_retry_event(
                {
                    "status": "retrying",
                    "reason": "模型连接失败",
                    "attempt": next_attempt,
                    "max_attempts": max_attempts,
                    "retry_index": retry_index,
                    "max_retries": max_retries,
                    "retry_after_seconds": 0,
                    "retry_at_ms": None,
                    "error_type": type(error).__name__,
                    "error_message": error_message,
                    "kind": kind,
                }
            )

    def _max_llm_attempts(self) -> int:
        return max(1, self.settings.llm_max_retries + 1)

    def _base_request_payload(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        temperature: float | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
        }
        if self.settings.openai_max_output_tokens > 0:
            payload["max_output_tokens"] = self.settings.openai_max_output_tokens
        if self.settings.openai_reasoning_effort:
            payload["reasoning"] = {"effort": self.settings.openai_reasoning_effort}
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def _responses_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        response_tools: list[dict[str, Any]] = []
        if self.settings.openai_web_search_enabled:
            web_search: dict[str, Any] = {"type": "web_search"}
            context_size = self.settings.openai_web_search_context_size
            if context_size in {"low", "medium", "high"}:
                web_search["search_context_size"] = context_size
            response_tools.append(web_search)
        for tool in tools:
            if tool.get("type") != "function":
                continue
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            response_tools.append(
                {
                    "type": "function",
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                    "strict": False,
                }
            )
        return response_tools

    async def _consume_responses_stream(
        self,
        stream: Any,
        *,
        started: float,
        on_text_delta: Callable[[str], Awaitable[None]] | None,
        on_audit_event: Callable[[dict[str, Any]], Awaitable[None]] | None,
        text_delta_emitted: Callable[[], bool],
    ) -> dict[str, Any]:
        completed_response: Any = None
        completed_output_items: list[Any] = []
        async for event in stream:
            event_type = _field(event, "type")
            if event_type == "response.output_text.delta":
                delta = _field(event, "delta")
                if isinstance(delta, str) and delta and on_text_delta is not None:
                    await on_text_delta(delta)
                continue
            if event_type == "response.output_item.done":
                item = _field(event, "item")
                if item is not None:
                    completed_output_items.append(item)
                    if on_audit_event is not None:
                        for audit_event in _extract_audit_events_from_items([item]):
                            await on_audit_event(audit_event)
                continue
            if event_type == "response.completed":
                completed_response = _field(event, "response")
                continue
            if event_type in {"response.failed", "response.incomplete", "error"}:
                raise ApiError(502, "llm_stream_error", f"Responses stream ended with {event_type}: {_jsonable(event)}")

        elapsed = time.monotonic() - started
        if completed_response is None:
            raise ApiError(502, "llm_empty_response", "模型未返回完整 Responses 结果。")
        completed_response = _ensure_response_output(completed_response, completed_output_items)
        result = _response_to_agent_result(completed_response)
        if on_text_delta is not None and not text_delta_emitted():
            completed_text = str(result.get("text") or result.get("assistant_message", {}).get("content") or "")
            if completed_text:
                await on_text_delta(completed_text)
        logger.info(
            "generate_with_tools_stream done elapsed=%.2fs usage=%s decision=%s",
            elapsed,
            _describe_usage(getattr(completed_response, "usage", None)),
            result.get("type"),
        )
        return result

    def _write_llm_payload_trace(
        self,
        *,
        kind: str,
        request_payload: dict[str, Any],
        trace_context: dict[str, Any] | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.log_llm_payload:
            return

        timestamp = datetime.now().astimezone().isoformat()
        metadata = {
            "timestamp": timestamp,
            "kind": kind,
            "model": self.settings.openai_model,
            "pid": os.getpid(),
            **_jsonable(trace_context or {}),
        }
        payload = {
            "metadata": metadata,
            "request_payload": _jsonable(request_payload),
            "request_options": _jsonable(request_options or {}),
        }

        trace_dir = self.settings.log_dir / "llm_payloads"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / _trace_file_name(kind=kind, metadata=metadata)
        trace_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        index_entry = {
            "timestamp": timestamp,
            "kind": kind,
            "model": self.settings.openai_model,
            "scope": metadata.get("scope"),
            "agent_id": metadata.get("agent_id"),
            "project_id": metadata.get("project_id"),
            "session_id": metadata.get("session_id"),
            "round_id": metadata.get("round_id"),
            "step_index": metadata.get("step_index"),
            "payload_file": str(trace_path),
        }
        with (trace_dir / "index.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(index_entry, ensure_ascii=False, separators=(",", ":")) + "\n")

        logger.info(
            "llm payload trace written kind=%s scope=%s agent_id=%s step=%s path=%s",
            kind,
            metadata.get("scope"),
            metadata.get("agent_id"),
            metadata.get("step_index"),
            trace_path,
        )


def _messages_to_response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant":
            content = str(message.get("content") or "")
            if content:
                input_items.append({"role": "assistant", "content": content})
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tool_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            continue
        if role in {"user", "system", "developer"}:
            input_items.append({"role": role, "content": str(message.get("content") or "")})
    return input_items


def _response_to_agent_result(response: Any) -> dict[str, Any]:
    text = _extract_response_text(response)
    tool_calls = _extract_function_tool_calls(response)
    assistant_message = _assistant_message(content=text, tool_calls=tool_calls, usage=getattr(response, "usage", None))
    audit_events = _extract_audit_events(response)
    if tool_calls:
        return {
            "type": "tool_calls",
            "tool_calls": [_parse_tool_call(item, index) for index, item in enumerate(tool_calls)],
            "assistant_message": assistant_message,
            "audit_events": audit_events,
        }
    return {
        "type": "respond",
        "text": text,
        "assistant_message": assistant_message,
        "audit_events": audit_events,
    }


def _ensure_response_output(response: Any, output_items: list[Any]) -> Any:
    if _output_items(response) or not output_items:
        return response
    response_dict = _as_dict(response)
    output = [_jsonable(item) for item in output_items]
    response_dict["output"] = output
    response_dict["output_text"] = _extract_output_text_from_items(output)
    return response_dict


def _assistant_message(*, content: str, tool_calls: list[dict[str, Any]], usage: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage_payload = _usage_payload(usage)
    if usage_payload:
        message["usage"] = usage_payload
    return message


def _partial_stream_result(text: str, *, interrupted_message: str) -> dict[str, Any]:
    return {
        "type": "respond",
        "text": text,
        "assistant_message": _assistant_message(content=text, tool_calls=[]),
        "audit_events": [],
        "interrupted": True,
        "interrupted_message": interrupted_message,
    }


def _api_error_from_llm_exception(exc: Exception) -> ApiError:
    if isinstance(exc, APIStatusError):
        return ApiError(502, "llm_http_error", f"模型调用失败：{_describe_api_error(exc)}")
    if isinstance(exc, APIError):
        return ApiError(502, "llm_http_error", f"模型调用失败：{exc}")
    return ApiError(502, "llm_http_error", f"模型调用失败：{exc}")


def _retry_error_message(error: Exception) -> str:
    if isinstance(error, APIStatusError):
        message = _describe_api_error(error)
    elif isinstance(error, ApiError):
        message = error.message
    else:
        message = str(error)
    normalized = " ".join(message.split()).strip()
    if not normalized:
        normalized = type(error).__name__
    return normalized[:300]


def _extract_function_tool_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in _output_items(response):
        item_dict = _as_dict(item)
        if item_dict.get("type") != "function_call":
            continue
        name = str(item_dict.get("name") or "")
        arguments = str(item_dict.get("arguments") or "{}")
        call_id = str(item_dict.get("call_id") or item_dict.get("id") or "")
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return calls


def _extract_audit_events(response: Any) -> list[dict[str, Any]]:
    return _extract_audit_events_from_items(_output_items(response))


def _extract_audit_events_from_items(items: list[Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in items:
        item_dict = _as_dict(item)
        item_type = str(item_dict.get("type") or "")
        if item_type == "web_search_call":
            events.append(
                {
                    "category": "web_search",
                    "source": "openai_responses",
                    "item": _jsonable(item_dict),
                }
            )
            continue
        if item_type != "message":
            continue
        annotations = _message_url_annotations(item_dict)
        if annotations:
            events.append(
                {
                    "category": "web_search",
                    "source": "openai_responses",
                    "annotations": annotations,
                }
            )
    return events


def _message_url_annotations(message_item: dict[str, Any]) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for content in message_item.get("content") or []:
        content_dict = _as_dict(content)
        for annotation in content_dict.get("annotations") or []:
            annotation_dict = _as_dict(annotation)
            if str(annotation_dict.get("type") or "") != "url_citation":
                continue
            annotations.append(_jsonable(annotation_dict))
    return annotations


def _parse_tool_call(raw_call: dict[str, Any], index: int) -> dict[str, Any]:
    tool_call_id = str(raw_call.get("id") or "").strip()
    if not tool_call_id:
        raise ApiError(502, "llm_invalid_tool_call", f"模型返回的 tool_calls[{index}] 缺少 id。")
    function = raw_call.get("function")
    if not isinstance(function, dict):
        raise ApiError(502, "llm_invalid_tool_call", f"模型返回的 tool_calls[{index}] 缺少 function。")
    name = str(function.get("name") or "").strip()
    if not name:
        raise ApiError(502, "llm_invalid_tool_call", f"模型返回的 tool_calls[{index}] 缺少 function.name。")
    arguments, arguments_error = _parse_tool_arguments(
        function.get("arguments"),
        tool_call_id=tool_call_id,
        tool_name=name,
    )
    return {
        "tool": name,
        "arguments": arguments,
        "tool_call_id": tool_call_id,
        "arguments_error": arguments_error,
    }


def _parse_tool_arguments(raw_arguments: Any, *, tool_call_id: str, tool_name: str) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    if raw_arguments is None or raw_arguments == "":
        return {}, None
    if not isinstance(raw_arguments, str):
        return {}, f"{tool_name} 的 arguments 格式不支持。"
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        logger.warning("tool_call invalid_json id=%s tool=%s error=%s", tool_call_id, tool_name, exc)
        return {}, f"{tool_name} 的 arguments 不是合法 JSON：{exc}"
    if not isinstance(parsed, dict):
        return {}, f"{tool_name} 的 arguments 必须为对象。"
    return parsed, None


def _extract_response_text(response: Any) -> str:
    output_text = _field(response, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    return _extract_output_text_from_items(_output_items(response))


def _extract_output_text_from_items(items: list[Any]) -> str:
    parts: list[str] = []
    for item in items:
        item_dict = _as_dict(item)
        if item_dict.get("type") != "message":
            continue
        for content in item_dict.get("content") or []:
            content_dict = _as_dict(content)
            if content_dict.get("type") in {"output_text", "text"}:
                text = content_dict.get("text")
                if text:
                    parts.append(str(text))
    return "".join(parts)


def _output_items(response: Any) -> list[Any]:
    output = _field(response, "output")
    return output if isinstance(output, list) else []


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _describe_api_error(exc: APIStatusError) -> str:
    try:
        body = exc.response.text
    except Exception:
        body = ""
    body = (body or "").strip()
    return body or str(exc)


def _trace_file_name(*, kind: str, metadata: dict[str, Any]) -> str:
    now = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    parts = [now, _safe_file_part(str(metadata.get("scope") or "llm"))]
    agent_id = metadata.get("agent_id")
    if agent_id:
        parts.append(_safe_file_part(str(agent_id)))
    step_index = metadata.get("step_index")
    if step_index is not None:
        parts.append(f"step-{_safe_file_part(str(step_index))}")
    parts.extend([_safe_file_part(kind), uuid4().hex[:8]])
    return "-".join(parts) + ".json"


def _safe_file_part(value: str) -> str:
    chars: list[str] = []
    for char in value.strip():
        if char.isalnum() or char in {"-", "_"}:
            chars.append(char)
        else:
            chars.append("_")
    text = "".join(chars).strip("_")
    return text[:80] or "unknown"


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _jsonable(model_dump())
        if hasattr(value, "__dict__"):
            return _jsonable(dict(value.__dict__))
        return str(value)


def _describe_usage(usage: Any) -> str:
    if usage is None:
        return "-"
    raw = _as_dict(usage)
    if raw:
        return (
            f"input={raw.get('input_tokens', raw.get('prompt_tokens', '?'))}"
            f" output={raw.get('output_tokens', raw.get('completion_tokens', '?'))}"
            f" total={raw.get('total_tokens', '?')}"
        )
    return str(usage)


def _usage_payload(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    raw = usage if isinstance(usage, dict) else _as_dict(usage)
    payload: dict[str, int] = {}
    aliases = {
        "input_tokens": "prompt_tokens",
        "output_tokens": "completion_tokens",
        "total_tokens": "total_tokens",
        "prompt_tokens": "prompt_tokens",
        "completion_tokens": "completion_tokens",
    }
    for raw_key, payload_key in aliases.items():
        value = raw.get(raw_key)
        if isinstance(value, int) and value >= 0:
            payload[payload_key] = value
    return payload
