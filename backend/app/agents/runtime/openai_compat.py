from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from openai import APIError, APIStatusError, AsyncOpenAI

from ...core import ApiError, Settings

logger = logging.getLogger("patent_creator.llm")


class OpenAICompatibleClient:
    """基于官方 openai SDK 的异步封装。

    - 指向 OpenAI 兼容的服务（默认 DeepSeek）
    - 对外暴露两个能力：
      - generate_json: JSON 输出，供子 agent 使用
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

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        client = self._require_client()
        if self.settings.log_llm_payload:
            logger.debug(
                "generate_json request model=%s system_len=%d user_len=%d",
                self.settings.openai_model,
                len(system_prompt),
                len(user_prompt),
            )
        started = time.monotonic()
        try:
            request_payload: dict[str, Any] = {
                "model": self.settings.openai_model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            extra_body = self._thinking_extra_body()
            if extra_body:
                request_payload["extra_body"] = extra_body
            completion = await client.chat.completions.create(
                **request_payload,
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
            "generate_json done model=%s elapsed=%.2fs usage=%s",
            self.settings.openai_model,
            elapsed,
            _describe_usage(usage),
        )
        content = self._extract_text(completion)
        if self.settings.log_llm_payload:
            logger.debug("generate_json raw_content=%s", content)
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
    ) -> dict[str, Any]:
        client = self._require_client()
        tool_names = [t.get("function", {}).get("name") for t in tools]
        logger.info(
            "generate_with_tools_stream request model=%s tools=%s messages_count=%d",
            self.settings.openai_model,
            tool_names,
            len(messages),
        )
        started = time.monotonic()
        try:
            request_payload: dict[str, Any] = {
                "model": self.settings.openai_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *messages,
                ],
                "tools": tools,
                "tool_choice": "auto",
                "stream": True,
            }
            extra_body = self._thinking_extra_body()
            if extra_body:
                request_payload["extra_body"] = extra_body
            if response_format_json:
                request_payload["response_format"] = {"type": "json_object"}
            stream = await client.chat.completions.create(
                **request_payload,
            )
        except APIStatusError as exc:
            logger.warning(
                "generate_with_tools_stream http_error status=%s body=%s",
                exc.status_code,
                _describe_api_error(exc),
            )
            raise ApiError(502, "llm_http_error", f"模型调用失败：{_describe_api_error(exc)}") from exc
        except APIError as exc:
            logger.warning("generate_with_tools_stream api_error %s", exc)
            raise ApiError(502, "llm_http_error", f"模型调用失败：{exc}") from exc

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
                call_type = getattr(tool_call, "type", None)
                if call_type:
                    item["type"] = str(call_type)
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
            ),
        }

    @staticmethod
    def _assistant_message(
        *,
        content: str,
        reasoning_content: str,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    def _thinking_extra_body(self) -> dict[str, Any] | None:
        if not self.settings.openai_compat_enable_thinking:
            return None
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}

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
        return OpenAICompatibleClient._message_content_to_text(message)

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
