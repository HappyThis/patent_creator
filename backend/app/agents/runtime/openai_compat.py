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
from .model_profiles import resolve_model_profile

logger = logging.getLogger("patent_creator.llm")

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


class OpenAICompatibleClient:
    """基于官方 openai SDK 的异步封装。

    - 指向 OpenAI 兼容的服务（默认 DeepSeek）
    - 对外暴露三类能力：
      - generate_text: 普通文本输出，供上下文压缩等低协议负担任务使用
      - generate_json: JSON 输出，供确需结构化结果的内部任务使用
      - generate_with_tools_stream: 流式 tool-calling，供主 agent loop 使用
    """

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = client

    def _require_client(self) -> AsyncOpenAI:
        if self._client is not None:
            return self._client
        if not self.settings.openai_compat_api_key:
            raise ApiError(
                500,
                "llm_api_key_missing",
                "未配置 OPENAI_COMPAT_API_KEY，无法调用 OpenAI 兼容模型。",
            )
        self._client = AsyncOpenAI(
            base_url=self.settings.openai_compat_base_url,
            api_key=self.settings.openai_compat_api_key,
            timeout=self.settings.llm_timeout,
            max_retries=self.settings.llm_max_retries,
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
    ) -> str:
        client = self._require_client()
        profile = resolve_model_profile(self.settings)
        prepared_messages = profile.prepare_messages_for_request(messages or [])
        if self.settings.log_llm_payload:
            logger.debug(
                "generate_text request model=%s system_len=%d messages_count=%d user_len=%d timeout=%s",
                self.settings.openai_model,
                len(system_prompt),
                len(prepared_messages),
                len(user_prompt),
                timeout if timeout is not None else self.settings.llm_timeout,
            )
        started = time.monotonic()
        try:
            request_payload: dict[str, Any] = {
                "model": self.settings.openai_model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *prepared_messages,
                    {"role": "user", "content": user_prompt},
                ],
            }
            profile.apply_chat_parameters(request_payload)
            self._write_llm_payload_trace(
                kind="generate_text",
                request_payload=request_payload,
                trace_context=trace_context,
                request_options={"timeout": timeout if timeout is not None else self.settings.llm_timeout},
            )
            completion = await client.chat.completions.create(
                **request_payload,
                timeout=timeout,
            )
        except APIStatusError as exc:
            logger.warning("generate_text http_error status=%s body=%s", exc.status_code, _describe_api_error(exc))
            raise ApiError(502, "llm_http_error", f"模型调用失败：{_describe_api_error(exc)}") from exc
        except APIError as exc:
            logger.warning("generate_text api_error %s", exc)
            raise ApiError(502, "llm_http_error", f"模型调用失败：{exc}") from exc

        elapsed = time.monotonic() - started
        usage = getattr(completion, "usage", None)
        logger.info(
            "generate_text done model=%s elapsed=%.2fs timeout=%s usage=%s",
            self.settings.openai_model,
            elapsed,
            timeout if timeout is not None else self.settings.llm_timeout,
            _describe_usage(usage),
        )
        content = self._extract_text(completion)
        if self.settings.log_llm_payload:
            logger.debug("generate_text raw_content_len=%d", len(content))
        return content

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        if self.settings.log_llm_payload:
            logger.debug(
                "generate_json request model=%s system_len=%d user_len=%d timeout=%s",
                self.settings.openai_model,
                len(system_prompt),
                len(user_prompt),
                timeout if timeout is not None else self.settings.llm_timeout,
            )
        started = time.monotonic()
        try:
            request_payload: dict[str, Any] = {
                "model": self.settings.openai_model,
                "response_format": {"type": "json_object"},
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            profile = resolve_model_profile(self.settings)
            profile.apply_chat_parameters(request_payload)
            self._write_llm_payload_trace(
                kind="generate_json",
                request_payload=request_payload,
                trace_context=trace_context,
                request_options={"timeout": timeout if timeout is not None else self.settings.llm_timeout},
            )
            completion = await client.chat.completions.create(
                **request_payload,
                timeout=timeout,
            )
        except APIStatusError as exc:
            logger.warning("generate_json http_error status=%s body=%s", exc.status_code, _describe_api_error(exc))
            raise ApiError(502, "llm_http_error", f"模型调用失败：{_describe_api_error(exc)}") from exc
        except APIError as exc:
            logger.warning("generate_json api_error %s", exc)
            raise ApiError(502, "llm_http_error", f"模型调用失败：{exc}") from exc

        elapsed = time.monotonic() - started
        usage = getattr(completion, "usage", None)
        logger.info(
            "generate_json done model=%s elapsed=%.2fs timeout=%s usage=%s",
            self.settings.openai_model,
            elapsed,
            timeout if timeout is not None else self.settings.llm_timeout,
            _describe_usage(usage),
        )
        content = self._extract_text(completion)
        if self.settings.log_llm_payload:
            logger.debug("generate_json raw_content_len=%d", len(content))
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
        response_format_json: bool = False,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        tool_names = [t.get("function", {}).get("name") for t in tools]
        logger.info(
            "generate_with_tools_stream request model=%s tools=%s messages_count=%d",
            self.settings.openai_model,
            tool_names,
            len(messages),
        )
        profile = resolve_model_profile(self.settings)
        request_payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                *profile.prepare_messages_for_request(messages),
            ],
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        profile.apply_chat_parameters(request_payload)
        if response_format_json:
            request_payload["response_format"] = {"type": "json_object"}
        self._write_llm_payload_trace(
            kind="generate_with_tools_stream",
            request_payload=request_payload,
            trace_context={**(trace_context or {}), "tool_names": tool_names},
        )

        max_attempts = max(1, self.settings.llm_max_retries + 1)
        for attempt in range(1, max_attempts + 1):
            text_delta_emitted = False

            async def guarded_on_text_delta(delta: str) -> None:
                nonlocal text_delta_emitted
                text_delta_emitted = True
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
                stream = await client.chat.completions.create(**request_payload)
                return await self._consume_tools_stream(
                    stream,
                    started=started,
                    on_text_delta=guarded_on_text_delta if on_text_delta is not None else None,
                )
            except APIStatusError as exc:
                logger.warning(
                    "generate_with_tools_stream http_error status=%s body=%s",
                    exc.status_code,
                    _describe_api_error(exc),
                )
                raise ApiError(502, "llm_http_error", f"模型调用失败：{_describe_api_error(exc)}") from exc
            except TRANSIENT_STREAM_ERRORS as exc:
                if text_delta_emitted or attempt >= max_attempts:
                    logger.warning(
                        "generate_with_tools_stream transient_error final attempt=%d/%d emitted_text=%s error=%s",
                        attempt,
                        max_attempts,
                        text_delta_emitted,
                        exc,
                    )
                    raise ApiError(502, "llm_stream_error", f"模型流式响应中断：{exc}") from exc
                delay = min(2 ** (attempt - 1), 5)
                logger.warning(
                    "generate_with_tools_stream transient_error retrying attempt=%d/%d delay=%ss error=%s",
                    attempt,
                    max_attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            except APIError as exc:
                logger.warning("generate_with_tools_stream api_error %s", exc)
                raise ApiError(502, "llm_http_error", f"模型调用失败：{exc}") from exc

        raise ApiError(502, "llm_stream_error", "模型流式响应中断。")

    async def _consume_tools_stream(
        self,
        stream: Any,
        *,
        started: float,
        on_text_delta: Callable[[str], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: Any = None
        async for chunk in stream:
            usage = getattr(chunk, "usage", usage)
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta

            reasoning_delta = getattr(delta, "reasoning_content", None)
            if isinstance(reasoning_delta, str) and reasoning_delta:
                reasoning_parts.append(reasoning_delta)

            content_delta = getattr(delta, "content", None)
            if isinstance(content_delta, str) and content_delta:
                content_parts.append(content_delta)
                if on_text_delta is not None:
                    await on_text_delta(content_delta)
            elif isinstance(content_delta, list):
                for item in content_delta:
                    item_type = getattr(item, "type", None) if not isinstance(item, dict) else item.get("type")
                    if item_type != "text":
                        continue
                    text = getattr(item, "text", None) if not isinstance(item, dict) else item.get("text")
                    if text:
                        text_str = str(text)
                        content_parts.append(text_str)
                        if on_text_delta is not None:
                            await on_text_delta(text_str)

            for tool_call in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(tool_call, "index", 0) or 0)
                item = tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                call_id = getattr(tool_call, "id", None)
                if call_id:
                    item["id"] = str(call_id)
                tool_kind = getattr(tool_call, "type", None)
                if tool_kind:
                    item["type"] = str(tool_kind)
                function = getattr(tool_call, "function", None)
                if function is None:
                    continue
                function_name = getattr(function, "name", None)
                if function_name:
                    item["function"]["name"] = str(function_name)
                function_arguments = getattr(function, "arguments", None)
                if function_arguments:
                    item["function"]["arguments"] += str(function_arguments)

        elapsed = time.monotonic() - started
        content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts)
        if tool_calls:
            ordered_tool_calls = [tool_calls[index] for index in sorted(tool_calls)]
            assistant_message = self._assistant_message(
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=ordered_tool_calls,
                usage=usage,
            )
            parsed_tool_calls = [
                self._parse_tool_call(item, index) for index, item in enumerate(ordered_tool_calls)
            ]
            logger.info(
                "generate_with_tools_stream done elapsed=%.2fs usage=%s decision=tool_calls count=%d",
                elapsed,
                _describe_usage(usage),
                len(parsed_tool_calls),
            )
            return {
                "type": "tool_calls",
                "tool_calls": parsed_tool_calls,
                "assistant_message": assistant_message,
            }

        logger.info(
            "generate_with_tools_stream done elapsed=%.2fs usage=%s decision=respond text_len=%d",
            elapsed,
            _describe_usage(usage),
            len(content),
        )
        return {
            "type": "respond",
            "text": content,
            "assistant_message": self._assistant_message(
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=[],
                usage=usage,
            ),
        }

    @staticmethod
    def _assistant_message(
        *,
        content: str,
        reasoning_content: str,
        tool_calls: list[dict[str, Any]],
        usage: Any = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage_payload = _usage_payload(usage)
        if usage_payload:
            message["usage"] = usage_payload
        return message

    @classmethod
    def _parse_tool_call(cls, raw_call: dict[str, Any], index: int) -> dict[str, Any]:
        tool_call_id = str(raw_call.get("id") or "").strip()
        if not tool_call_id:
            raise ApiError(502, "llm_invalid_tool_call", f"模型返回的 tool_calls[{index}] 缺少 id。")
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise ApiError(502, "llm_invalid_tool_call", f"模型返回的 tool_calls[{index}] 缺少 function。")
        name = str(function.get("name") or "").strip()
        if not name:
            raise ApiError(502, "llm_invalid_tool_call", f"模型返回的 tool_calls[{index}] 缺少 function.name。")
        arguments, arguments_error = cls._parse_tool_arguments(
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

    @staticmethod
    def _extract_text(completion: Any) -> str:
        if not completion.choices:
            raise ApiError(502, "llm_empty_response", "模型未返回 choices。")
        message = completion.choices[0].message
        content = OpenAICompatibleClient._message_content_to_text(message)
        if content.strip():
            return content
        return OpenAICompatibleClient._message_reasoning_to_text(message)

    @staticmethod
    def _message_content_to_text(message: Any) -> str:
        content = getattr(message, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                # SDK 对象或 dict 都可能出现
                item_type = getattr(item, "type", None) if not isinstance(item, dict) else item.get("type")
                if item_type == "text":
                    text = getattr(item, "text", None) if not isinstance(item, dict) else item.get("text")
                    if text:
                        text_parts.append(str(text))
            return "".join(text_parts)
        raise ApiError(502, "llm_invalid_response", "模型返回的 message.content 格式不支持。")

    @staticmethod
    def _message_reasoning_to_text(message: Any) -> str:
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning is None:
            reasoning = getattr(message, "reasoning", None)
        if reasoning is None:
            model_extra = getattr(message, "model_extra", None)
            if isinstance(model_extra, dict):
                reasoning = model_extra.get("reasoning_content") or model_extra.get("reasoning")
        if reasoning is None:
            return ""
        if isinstance(reasoning, str):
            return reasoning
        if isinstance(reasoning, list):
            return "".join(str(item) for item in reasoning if item)
        if isinstance(reasoning, dict):
            for key in ("content", "text", "summary"):
                value = reasoning.get(key)
                if value:
                    return str(value)
            return json.dumps(reasoning, ensure_ascii=False)
        return str(reasoning)

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
            "provider": self.settings.openai_compat_provider,
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

    @staticmethod
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
            message = f"{tool_name} 的 arguments 不是合法 JSON：{exc}"
            logger.warning(
                "tool_call invalid_json id=%s tool=%s error=%s",
                tool_call_id,
                tool_name,
                exc,
            )
            return {}, message
        if not isinstance(parsed, dict):
            return {}, f"{tool_name} 的 arguments 必须为对象。"
        return parsed, None


def _describe_api_error(exc: APIStatusError) -> str:
    try:
        body = exc.response.text
    except Exception:  # pragma: no cover - 防御性
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
        return str(value)


def _describe_usage(usage: Any) -> str:
    if usage is None:
        return "-"
    try:
        return (
            f"prompt={getattr(usage, 'prompt_tokens', '?')}"
            f" completion={getattr(usage, 'completion_tokens', '?')}"
            f" total={getattr(usage, 'total_tokens', '?')}"
        )
    except Exception:  # pragma: no cover
        return str(usage)


def _usage_payload(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        raw = usage
    else:
        model_dump = getattr(usage, "model_dump", None)
        raw = model_dump() if callable(model_dump) else {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if isinstance(value, int):
                raw[key] = value

    payload: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(key)
        if isinstance(value, int) and value >= 0:
            payload[key] = value
    return payload
