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
            completion = await client.chat.completions.create(
                model=self.settings.openai_model,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
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
        temperature: float = 0.2,
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
            stream = await client.chat.completions.create(
                model=self.settings.openai_model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    *messages,
                ],
                tools=tools,
                tool_choice="auto",
                stream=True,
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
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: Any = None
        async for chunk in stream:
            usage = getattr(chunk, "usage", usage)
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta

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
        if tool_calls:
            first = tool_calls[min(tool_calls.keys())]
            name = str(first["function"].get("name") or "").strip()
            if not name:
                raise ApiError(502, "llm_invalid_tool_call", "模型返回的 tool_call 缺少 name。")
            arguments = self._parse_tool_arguments(first["function"].get("arguments"))
            logger.info(
                "generate_with_tools_stream done elapsed=%.2fs usage=%s decision=tool_call tool=%s",
                elapsed,
                _describe_usage(usage),
                name,
            )
            return {
                "type": "tool_call",
                "tool": name,
                "arguments": arguments,
                "tool_call_id": str(first.get("id") or ""),
            }

        text = "".join(content_parts)
        logger.info(
            "generate_with_tools_stream done elapsed=%.2fs usage=%s decision=respond text_len=%d",
            elapsed,
            _describe_usage(usage),
            len(text),
        )
        return {"type": "respond", "text": text}

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
    def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if raw_arguments is None or raw_arguments == "":
            return {}
        if not isinstance(raw_arguments, str):
            raise ApiError(502, "llm_invalid_tool_call", "tool_call.arguments 格式不支持。")
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ApiError(502, "llm_invalid_tool_call", "tool_call.arguments 不是合法 JSON。") from exc
        if not isinstance(parsed, dict):
            raise ApiError(502, "llm_invalid_tool_call", "tool_call.arguments 必须为对象。")
        return parsed


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
