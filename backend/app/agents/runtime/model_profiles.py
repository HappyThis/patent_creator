from __future__ import annotations

from dataclasses import dataclass
import copy
from typing import Any

from ...core import ApiError, Settings


@dataclass(frozen=True, slots=True)
class ModelProfile:
    provider: str
    thinking: str
    token_limit_param: str
    max_completion_tokens: int
    reasoning_effort: str
    replay_reasoning_content: bool

    def apply_chat_parameters(self, payload: dict[str, Any]) -> None:
        if self.max_completion_tokens > 0:
            payload[self.token_limit_param] = self.max_completion_tokens
        extra_body = self.extra_body()
        if extra_body:
            payload["extra_body"] = extra_body
        if self.provider == "deepseek" and self.thinking == "enabled":
            payload["reasoning_effort"] = self.reasoning_effort

    def extra_body(self) -> dict[str, Any]:
        return {"thinking": {"type": self.thinking}}

    def prepare_messages_for_request(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = copy.deepcopy(messages)
        if self.replay_reasoning_content:
            return prepared
        for message in prepared:
            if message.get("role") == "assistant":
                message.pop("reasoning_content", None)
        return prepared


def resolve_model_profile(settings: Settings) -> ModelProfile:
    provider = settings.openai_compat_provider.strip().lower()
    thinking = settings.openai_compat_thinking.strip().lower()
    if provider not in {"mimo", "deepseek"}:
        raise ApiError(500, "unsupported_model_provider", f"不支持的模型厂商 profile：{provider}")
    if thinking not in {"disabled", "enabled"}:
        raise ApiError(500, "unsupported_thinking_mode", f"不支持的 thinking 模式：{thinking}")

    if provider == "mimo":
        return ModelProfile(
            provider=provider,
            thinking=thinking,
            token_limit_param="max_completion_tokens",
            max_completion_tokens=settings.openai_compat_max_completion_tokens,
            reasoning_effort=settings.openai_compat_reasoning_effort,
            replay_reasoning_content=False,
        )

    return ModelProfile(
        provider=provider,
        thinking=thinking,
        token_limit_param="max_tokens",
        max_completion_tokens=settings.openai_compat_max_completion_tokens,
        reasoning_effort=settings.openai_compat_reasoning_effort,
        replay_reasoning_content=thinking == "enabled",
    )


def prepare_messages_for_model_request(
    messages: list[dict[str, Any]],
    settings: Settings,
) -> list[dict[str, Any]]:
    return resolve_model_profile(settings).prepare_messages_for_request(messages)
