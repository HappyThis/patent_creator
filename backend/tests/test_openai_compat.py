from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.agents.runtime.openai_compat import OpenAICompatibleClient
from app.core.config import Settings


class FakeCompletions:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


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


def make_settings(tmp_path: Path, *, thinking_enabled: bool) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_compat_api_key="test-key",
        openai_compat_enable_thinking=thinking_enabled,
    )


def test_generate_json_omits_provider_thinking_when_disabled(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path, thinking_enabled=False), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_json(system_prompt="system", user_prompt="user"))

    assert result == {"ok": True}
    assert "extra_body" not in fake.completions.calls[0]


def test_generate_json_can_override_timeout(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path, thinking_enabled=False), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_json(system_prompt="system", user_prompt="user", timeout=180))

    assert result == {"ok": True}
    assert fake.completions.calls[0]["timeout"] == 180


def test_generate_with_tools_stream_sends_provider_thinking_when_enabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, thinking_enabled=True), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert fake.completions.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
