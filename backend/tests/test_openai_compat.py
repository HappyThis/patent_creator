from __future__ import annotations

import asyncio
import json
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
        log_dir=tmp_path / "logs",
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


def test_generate_json_sends_openai_profile_without_provider_specific_parameters(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="openai", thinking="enabled"), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_json(system_prompt="system", user_prompt="user"))

    assert result == {"ok": True}
    call = fake.completions.calls[0]
    assert call["max_completion_tokens"] == 8192
    assert "max_tokens" not in call
    assert "extra_body" not in call
    assert "reasoning_effort" not in call


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


def test_generate_text_does_not_request_json_response_format(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="plain memory"))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_text(system_prompt="system", user_prompt="user", timeout=180))

    assert result == "plain memory"
    assert fake.completions.calls[0]["timeout"] == 180
    assert "response_format" not in fake.completions.calls[0]


def test_generate_text_uses_reasoning_when_content_is_empty(tmp_path: Path) -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="", reasoning_content="# 分析\n\n压缩状态"))],
        usage=None,
    )
    fake = FakeOpenAIClient(completion)
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="deepseek", thinking="enabled"), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_text(system_prompt="system", user_prompt="user"))

    assert result == "# 分析\n\n压缩状态"


def test_generate_with_tools_stream_writes_payload_trace_when_enabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    settings = make_settings(tmp_path)
    settings.log_llm_payload = True
    client = OpenAICompatibleClient(settings, client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system prompt",
            messages=[{"role": "user", "content": "hello"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "document_read",
                        "description": "读取文档",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            trace_context={
                "scope": "main",
                "project_id": "proj_test",
                "session_id": "sess_test",
                "round_id": "round_test",
                "step_index": 3,
            },
        )
    )

    assert result["type"] == "respond"
    trace_dir = settings.log_dir / "llm_payloads"
    payload_files = sorted(path for path in trace_dir.glob("*.json") if path.name != "index.jsonl")
    assert len(payload_files) == 1
    payload = json.loads(payload_files[0].read_text(encoding="utf-8"))
    assert payload["metadata"]["scope"] == "main"
    assert payload["metadata"]["project_id"] == "proj_test"
    assert payload["metadata"]["step_index"] == 3
    assert payload["request_payload"]["messages"][0] == {"role": "system", "content": "system prompt"}
    assert payload["request_payload"]["tools"][0]["function"]["name"] == "document_read"
    assert "api_key" not in json.dumps(payload, ensure_ascii=False).lower()

    index_lines = (trace_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(index_lines) == 1
    index = json.loads(index_lines[0])
    assert index["scope"] == "main"
    assert index["payload_file"] == str(payload_files[0])


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


def test_generate_with_tools_stream_replays_reasoning_for_mimo_enabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="mimo", thinking="enabled"), client=fake)  # type: ignore[arg-type]

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
    assert fake.completions.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort" not in fake.completions.calls[0]
    assert request_messages[1]["reasoning_content"] == "需要回放"


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


def test_generate_with_tools_stream_filters_reasoning_for_openai_enabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream("ok"))
    client = OpenAICompatibleClient(make_settings(tmp_path, provider="openai", thinking="enabled"), client=fake)  # type: ignore[arg-type]

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
    assert "extra_body" not in fake.completions.calls[0]
    assert "reasoning_effort" not in fake.completions.calls[0]


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
