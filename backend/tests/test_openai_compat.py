from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.agents.runtime.openai_compat import OpenAICompatibleClient
from app.core import ApiError
from app.core.config import Settings


class FakeCompletions:
    def __init__(self, response: Any | list[Any]) -> None:
        self.responses = list(response) if isinstance(response, list) else [response]
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeOpenAIClient:
    def __init__(self, response: Any) -> None:
        self.completions = FakeCompletions(response)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeStream:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aiter__(self) -> Any:
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=self._text, tool_calls=None),
                )
            ],
            usage=None,
        )


class FailingStream:
    def __init__(self, error: Exception, *, text_before_error: str | None = None) -> None:
        self._error = error
        self._text_before_error = text_before_error

    async def __aiter__(self) -> Any:
        if self._text_before_error is not None:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=self._text_before_error, tool_calls=None),
                    )
                ],
                usage=None,
            )
        raise self._error


def make_settings(
    tmp_path: Path,
    *,
    provider: str = "mimo",
    thinking: str = "disabled",
    llm_max_retries: int = 2,
) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
        openai_compat_provider=provider,
        openai_compat_thinking=thinking,
        openai_compat_max_completion_tokens=8192,
        llm_max_retries=llm_max_retries,
    )


def test_generate_json_sends_mimo_disabled_thinking(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="mimo", thinking="disabled"), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_json(system_prompt="system", user_prompt="user"))

    assert result == {"ok": True}
    assert fake.completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert fake.completions.calls[0]["max_completion_tokens"] == 8192


def test_generate_json_can_override_timeout(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_json(system_prompt="system", user_prompt="user", timeout=180))

    assert result == {"ok": True}
    assert fake.completions.calls[0]["timeout"] == 180


def test_generate_with_tools_stream_sends_deepseek_disabled_thinking(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="deepseek", thinking="disabled"), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert fake.completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert fake.completions.calls[0]["max_tokens"] == 8192
    assert "reasoning_effort" not in fake.completions.calls[0]


def test_generate_with_tools_stream_sends_deepseek_enabled_thinking(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="deepseek", thinking="enabled"), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert fake.completions.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert fake.completions.calls[0]["reasoning_effort"] == "high"


def test_generate_with_tools_stream_filters_reasoning_for_mimo_disabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="mimo", thinking="disabled"), client=fake)  # type: ignore[arg-type]

    asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[
                {"role": "assistant", "content": "上一轮", "reasoning_content": "不应回放"},
                {"role": "user", "content": "继续"},
            ],
            tools=[],
        )
    )

    request_messages = fake.completions.calls[0]["messages"]
    assert request_messages[1] == {"role": "assistant", "content": "上一轮"}


def test_generate_with_tools_stream_replays_reasoning_for_deepseek_enabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="deepseek", thinking="enabled"), client=fake)  # type: ignore[arg-type]

    asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[
                {"role": "assistant", "content": "上一轮", "reasoning_content": "需要回放"},
                {"role": "user", "content": "继续"},
            ],
            tools=[],
        )
    )

    request_messages = fake.completions.calls[0]["messages"]
    assert request_messages[1]["reasoning_content"] == "需要回放"


def test_generate_with_tools_stream_retries_transient_read_error_before_text_delta(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([FailingStream(httpx.ReadError("stream broke")), FakeStream("ok")])
    client = OpenAICompatibleClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert result["text"] == "ok"
    assert len(fake.completions.calls) == 2


def test_generate_with_tools_stream_does_not_retry_after_text_delta(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([FailingStream(httpx.ReadError("stream broke"), text_before_error="partial")])
    client = OpenAICompatibleClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]
    deltas: list[str] = []

    async def on_text_delta(delta: str) -> None:
        deltas.append(delta)

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            client.generate_with_tools_stream(
                system_prompt="system",
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                on_text_delta=on_text_delta,
            )
        )

    assert exc_info.value.code == "llm_stream_error"
    assert deltas == ["partial"]
    assert len(fake.completions.calls) == 1
