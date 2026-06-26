from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.agents.runtime.openai_responses import OpenAIResponsesClient
from app.core import ApiError
from app.core.config import Settings


class FakeResponses:
    def __init__(self, response: Any | list[Any]) -> None:
        self.responses = list(response) if isinstance(response, list) else [response]
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            response = self.responses.pop(0)
        else:
            response = self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


class FakeOpenAIClient:
    def __init__(self, response: Any) -> None:
        self.responses = FakeResponses(response)


class FakeStream:
    def __init__(self, *events: Any) -> None:
        self._events = events

    async def __aiter__(self) -> Any:
        for event in self._events:
            yield event


class FailingStream:
    def __init__(self, error: Exception, *, text_before_error: str | None = None) -> None:
        self._error = error
        self._text_before_error = text_before_error

    async def __aiter__(self) -> Any:
        if self._text_before_error is not None:
            yield SimpleNamespace(type="response.output_text.delta", delta=self._text_before_error)
        raise self._error


class OutputItemThenFailingStream:
    def __init__(self, item: Any, error: Exception) -> None:
        self._item = item
        self._error = error

    async def __aiter__(self) -> Any:
        yield SimpleNamespace(type="response.output_item.done", item=self._item)
        raise self._error


def make_settings(
    tmp_path: Path,
    *,
    llm_max_retries: int = 2,
    web_search_enabled: bool = True,
) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        git_user_name="Test User",
        git_user_email="test@example.com",
        openai_api_key="test-key",
        openai_web_search_enabled=web_search_enabled,
        llm_max_retries=llm_max_retries,
        llm_retry_delay_seconds=0,
    )


def response_with_text(text: str) -> Any:
    return SimpleNamespace(
        output_text=text,
        output=[],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
    )


def completed_event(text: str, *, output: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            output_text=text,
            output=output or [],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    )


def function_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "disclosure_outline",
            "description": "read outline",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_generate_json_uses_responses_json_format(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(response_with_text('{"ok": true}'))
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_json(system_prompt="system", user_prompt="user", timeout=180))

    assert result == {"ok": True}
    call = fake.responses.calls[0]
    assert call["model"] == "gpt-5.5"
    assert call["instructions"] == "system"
    assert call["input"] == [{"role": "user", "content": "user"}]
    assert call["store"] is False
    assert call["text"] == {"format": {"type": "json_object"}}
    assert call["reasoning"] == {"effort": "high"}
    assert call["timeout"] == 180


def test_generate_json_retries_retryable_llm_error(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([httpx.ConnectError("connect failed"), response_with_text('{"ok": true}')])
    client = OpenAIResponsesClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]
    retry_events: list[dict[str, Any]] = []

    async def on_retry_event(event: dict[str, Any]) -> None:
        retry_events.append(event)

    result = asyncio.run(
        client.generate_json(
            system_prompt="system",
            user_prompt="user",
            on_retry_event=on_retry_event,
        )
    )

    assert result == {"ok": True}
    assert len(fake.responses.calls) == 2
    assert [event["status"] for event in retry_events] == ["waiting", "retrying"]
    assert retry_events[0]["attempt"] == 2
    assert retry_events[0]["max_attempts"] == 2
    assert retry_events[0]["retry_index"] == 1
    assert retry_events[0]["max_retries"] == 1
    assert "connect failed" in retry_events[0]["error_message"]


def test_generate_json_retries_invalid_json_response(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([response_with_text("not json"), response_with_text('{"ok": true}')])
    client = OpenAIResponsesClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]
    retry_events: list[dict[str, Any]] = []

    async def on_retry_event(event: dict[str, Any]) -> None:
        retry_events.append(event)

    result = asyncio.run(
        client.generate_json(
            system_prompt="system",
            user_prompt="user",
            on_retry_event=on_retry_event,
        )
    )

    assert result == {"ok": True}
    assert len(fake.responses.calls) == 2
    assert [event["status"] for event in retry_events] == ["waiting", "retrying"]
    assert retry_events[0]["error_type"] == "ApiError"
    assert retry_events[0]["error_message"] == "模型返回的内容不是合法 JSON。"


def test_generate_json_final_invalid_json_response_fails(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([response_with_text("not json"), response_with_text("still not json")])
    client = OpenAIResponsesClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(client.generate_json(system_prompt="system", user_prompt="user"))

    assert exc_info.value.code == "llm_invalid_json"
    assert len(fake.responses.calls) == 2


def test_generate_text_does_not_request_json_format(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(response_with_text("plain memory"))
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(client.generate_text(system_prompt="system", user_prompt="user"))

    assert result == "plain memory"
    call = fake.responses.calls[0]
    assert call["input"] == [{"role": "user", "content": "user"}]
    assert "text" not in call


def test_generate_with_tools_stream_adds_web_search_by_default(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream(completed_event("ok")))
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[function_tool()],
        )
    )

    assert result["type"] == "respond"
    call = fake.responses.calls[0]
    assert call["stream"] is True
    assert call["tool_choice"] == "auto"
    assert call["tools"][0] == {"type": "web_search", "search_context_size": "low"}
    assert call["tools"][1]["type"] == "function"
    assert call["tools"][1]["name"] == "disclosure_outline"


def test_generate_with_tools_stream_can_disable_web_search(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream(completed_event("ok")))
    client = OpenAIResponsesClient(make_settings(tmp_path, web_search_enabled=False), client=fake)  # type: ignore[arg-type]

    asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[function_tool()],
        )
    )

    assert [tool["type"] for tool in fake.responses.calls[0]["tools"]] == ["function"]


def test_generate_with_tools_stream_converts_function_call_history(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream(completed_event("ok")))
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[
                {"role": "user", "content": "read"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "disclosure_outline", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": '{"status":"success"}'},
            ],
            tools=[function_tool()],
        )
    )

    assert fake.responses.calls[0]["input"] == [
        {"role": "user", "content": "read"},
        {"type": "function_call", "call_id": "call_1", "name": "disclosure_outline", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"status":"success"}'},
    ]


def test_generate_with_tools_stream_returns_function_calls(tmp_path: Path) -> None:
    call_item = SimpleNamespace(
        type="function_call",
        call_id="call_outline",
        name="disclosure_outline",
        arguments='{"section_id":"sec_1"}',
    )
    fake = FakeOpenAIClient(FakeStream(completed_event("", output=[call_item])))
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[function_tool()],
        )
    )

    assert result["type"] == "tool_calls"
    assert result["tool_calls"] == [
        {
            "tool": "disclosure_outline",
            "arguments": {"section_id": "sec_1"},
            "tool_call_id": "call_outline",
            "arguments_error": None,
        }
    ]
    assert result["assistant_message"]["tool_calls"][0]["id"] == "call_outline"


def test_generate_with_tools_stream_uses_output_item_done_when_completed_output_is_empty(tmp_path: Path) -> None:
    done_item = SimpleNamespace(
        type="message",
        role="assistant",
        content=[SimpleNamespace(type="output_text", text="done text")],
    )
    completed = SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            output_text="",
            output=[],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    )
    fake = FakeOpenAIClient(
        FakeStream(
            SimpleNamespace(type="response.output_item.done", item=done_item),
            completed,
        )
    )
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert result["text"] == "done text"
    assert result["assistant_message"]["content"] == "done text"


def test_generate_with_tools_stream_returns_web_search_audit_events(tmp_path: Path) -> None:
    search_item = SimpleNamespace(
        type="web_search_call",
        id="ws_1",
        status="completed",
        action=SimpleNamespace(type="search", query="openclaw"),
    )
    message_item = SimpleNamespace(
        type="message",
        role="assistant",
        content=[
            SimpleNamespace(
                type="output_text",
                text="OpenClaw is ...",
                annotations=[
                    SimpleNamespace(
                        type="url_citation",
                        url="https://example.com/openclaw",
                        title="OpenClaw",
                        start_index=0,
                        end_index=8,
                    )
                ],
            )
        ],
    )
    fake = FakeOpenAIClient(FakeStream(completed_event("OpenClaw is ...", output=[search_item, message_item])))
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "research OpenClaw"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert result["audit_events"] == [
        {
            "category": "web_search",
            "source": "openai_responses",
            "item": {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "openclaw"},
            },
        },
        {
            "category": "web_search",
            "source": "openai_responses",
            "annotations": [
                {
                    "type": "url_citation",
                    "url": "https://example.com/openclaw",
                    "title": "OpenClaw",
                    "start_index": 0,
                    "end_index": 8,
                }
            ],
        },
    ]


def test_generate_with_tools_stream_streams_web_search_audit_events(tmp_path: Path) -> None:
    search_item = SimpleNamespace(
        type="web_search_call",
        id="ws_live",
        status="completed",
        action=SimpleNamespace(type="search", query="China news today"),
    )
    completed = SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            output_text="done",
            output=[],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    )
    fake = FakeOpenAIClient(
        FakeStream(
            SimpleNamespace(type="response.output_item.done", item=search_item),
            completed,
        )
    )
    client = OpenAIResponsesClient(make_settings(tmp_path), client=fake)  # type: ignore[arg-type]
    audit_events: list[dict[str, Any]] = []

    async def on_audit_event(event: dict[str, Any]) -> None:
        audit_events.append(event)

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "news"}],
            tools=[],
            on_audit_event=on_audit_event,
        )
    )

    assert result["type"] == "respond"
    assert audit_events == [
        {
            "category": "web_search",
            "source": "openai_responses",
            "item": {
                "type": "web_search_call",
                "id": "ws_live",
                "status": "completed",
                "action": {"type": "search", "query": "China news today"},
            },
        }
    ]


def test_generate_with_tools_stream_writes_payload_trace_when_enabled(tmp_path: Path) -> None:
    fake = FakeOpenAIClient(FakeStream(completed_event("ok")))
    settings = make_settings(tmp_path)
    settings.log_llm_payload = True
    client = OpenAIResponsesClient(settings, client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system prompt",
            messages=[{"role": "user", "content": "hello"}],
            tools=[function_tool()],
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
    assert payload["request_payload"]["instructions"] == "system prompt"
    assert payload["request_payload"]["input"][0] == {"role": "user", "content": "hello"}
    assert payload["request_payload"]["tools"][0]["type"] == "web_search"
    assert "api_key" not in json.dumps(payload, ensure_ascii=False).lower()

    index_lines = (trace_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(index_lines) == 1
    index = json.loads(index_lines[0])
    assert index["scope"] == "main"
    assert index["payload_file"] == str(payload_files[0])


def test_generate_with_tools_stream_retries_transient_read_error_before_text_delta(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([FailingStream(httpx.ReadError("stream broke")), FakeStream(completed_event("ok"))])
    client = OpenAIResponsesClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    )

    assert result["type"] == "respond"
    assert result["text"] == "ok"
    assert len(fake.responses.calls) == 2


def test_generate_with_tools_stream_retries_when_only_unseen_tool_item_was_received(tmp_path: Path) -> None:
    call_item = SimpleNamespace(
        type="function_call",
        call_id="call_outline",
        name="disclosure_outline",
        arguments='{"section_id":"sec_1"}',
    )
    fake = FakeOpenAIClient(
        [
            OutputItemThenFailingStream(call_item, httpx.ReadError("stream broke")),
            FakeStream(completed_event("ok")),
        ]
    )
    client = OpenAIResponsesClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[function_tool()],
        )
    )

    assert result["type"] == "respond"
    assert result["text"] == "ok"
    assert len(fake.responses.calls) == 2


def test_generate_with_tools_stream_does_not_retry_after_text_delta(tmp_path: Path) -> None:
    fake = FakeOpenAIClient([FailingStream(httpx.ReadError("stream broke"), text_before_error="partial")])
    client = OpenAIResponsesClient(make_settings(tmp_path, llm_max_retries=1), client=fake)  # type: ignore[arg-type]
    deltas: list[str] = []

    async def on_text_delta(delta: str) -> None:
        deltas.append(delta)

    result = asyncio.run(
        client.generate_with_tools_stream(
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            on_text_delta=on_text_delta,
        )
    )

    assert result["type"] == "respond"
    assert result["text"] == "partial"
    assert result["assistant_message"]["content"] == "partial"
    assert result["interrupted"] is True
    assert deltas == ["partial"]
    assert len(fake.responses.calls) == 1
