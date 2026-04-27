from __future__ import annotations

import json
from typing import Any

import httpx

from ...core import ApiError, Settings


class OpenAICompatibleClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http_client = http_client

    async def generate_json(self, *, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> dict[str, Any]:
        if not self.settings.openai_compat_api_key:
            raise ApiError(
                500,
                "llm_api_key_missing",
                "未配置 OPENAI_COMPAT_API_KEY，无法调用 OpenAI 兼容模型。",
            )

        payload = {
            "model": self.settings.openai_model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        url = f"{self.settings.openai_compat_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_compat_api_key}",
            "Content-Type": "application/json",
        }

        if self._http_client is not None:
            response = await self._http_client.post(url, json=payload, headers=headers, timeout=self.settings.llm_timeout)
        else:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                response = await client.post(url, json=payload, headers=headers)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip() or str(exc)
            raise ApiError(502, "llm_http_error", f"模型调用失败：{detail}") from exc

        data = response.json()
        content = self._extract_content(data)
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ApiError(502, "llm_invalid_json", "模型返回的内容不是合法 JSON。") from exc

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not choices:
            raise ApiError(502, "llm_empty_response", "模型未返回 choices。")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            return "".join(text_parts)
        raise ApiError(502, "llm_invalid_response", "模型返回的 message.content 格式不支持。")
