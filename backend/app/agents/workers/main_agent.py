from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ...tools import MAIN_AGENT_TOOL_NAMES, build_openai_tools
from ...core import ApiError


class SupportsGenerateWithTools(Protocol):
    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any,
        on_audit_event: Any = None,
        response_format_json: bool = False,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class MainAgentToolCall:
    tool: str
    arguments: dict[str, Any]
    tool_call_id: str
    arguments_error: str | None = None


@dataclass(slots=True)
class MainAgentAction:
    type: Literal["respond", "tool_calls"]
    text: str | None = None
    tool_calls: list[MainAgentToolCall] | None = None
    assistant_message: dict[str, Any] | None = None
    audit_events: list[dict[str, Any]] | None = None


def build_main_agent_tools() -> list[dict[str, Any]]:
    return build_openai_tools(MAIN_AGENT_TOOL_NAMES)


MAIN_AGENT_TOOLS: list[dict[str, Any]] = build_main_agent_tools()


async def decide_main_agent_step(
    llm_client: SupportsGenerateWithTools,
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    on_text_delta: Any | None = None,
    on_audit_event: Any | None = None,
    trace_context: dict[str, Any] | None = None,
) -> MainAgentAction:
    """请求主 agent 做一步决策，返回统一的 Action 结构。"""
    result = await llm_client.generate_with_tools_stream(
        system_prompt=system_prompt,
        messages=messages,
        tools=MAIN_AGENT_TOOLS,
        on_text_delta=on_text_delta,
        on_audit_event=on_audit_event,
        trace_context=trace_context,
    )
    action_type = result.get("type")
    if action_type == "respond":
        text = str(result.get("text") or "").strip()
        assistant_message = _assistant_message(result)
        if assistant_message is None:
            raise ApiError(502, "main_agent_invalid_action", "主 agent respond 缺少 assistant_message。")
        return MainAgentAction(
            type="respond",
            text=text,
            assistant_message=assistant_message,
            audit_events=_audit_events(result),
        )
    if action_type == "tool_calls":
        tool_calls = _tool_calls(result)
        assistant_message = _assistant_message(result)
        if assistant_message is None:
            raise ApiError(502, "main_agent_invalid_action", "主 agent tool_calls 缺少 assistant_message。")
        _validate_tool_call_message(assistant_message, tool_calls)
        return MainAgentAction(
            type="tool_calls",
            tool_calls=tool_calls,
            assistant_message=assistant_message,
            audit_events=_audit_events(result),
        )
    raise ApiError(502, "main_agent_invalid_action", f"主 agent 返回了未知动作类型：{action_type}")


def _assistant_message(result: dict[str, Any]) -> dict[str, Any] | None:
    value = result.get("assistant_message")
    return value if isinstance(value, dict) else None


def _audit_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = result.get("audit_events")
    if not isinstance(raw_events, list):
        return []
    return [item for item in raw_events if isinstance(item, dict)]


def _tool_calls(result: dict[str, Any]) -> list[MainAgentToolCall]:
    raw_calls = result.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ApiError(502, "main_agent_invalid_action", "主 agent tool_calls 必须为非空数组。")

    calls: list[MainAgentToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            raise ApiError(502, "main_agent_invalid_action", f"tool_calls[{index}] 必须为对象。")
        tool = str(raw_call.get("tool") or "").strip()
        if not tool:
            raise ApiError(502, "main_agent_invalid_action", f"tool_calls[{index}] 缺少 tool。")
        tool_call_id = str(raw_call.get("tool_call_id") or "").strip()
        if not tool_call_id:
            raise ApiError(502, "main_agent_invalid_action", f"tool_calls[{index}] 缺少 tool_call_id。")
        arguments = raw_call.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ApiError(502, "main_agent_invalid_action", f"tool_calls[{index}].arguments 必须为对象。")
        arguments_error = raw_call.get("arguments_error")
        if arguments_error is not None and not isinstance(arguments_error, str):
            raise ApiError(502, "main_agent_invalid_action", f"tool_calls[{index}].arguments_error 必须为字符串。")
        calls.append(
            MainAgentToolCall(
                tool=tool,
                arguments=arguments,
                tool_call_id=tool_call_id,
                arguments_error=arguments_error,
            )
        )
    return calls


def _validate_tool_call_message(message: dict[str, Any], tool_calls: list[MainAgentToolCall]) -> None:
    message_tool_calls = message.get("tool_calls")
    if not isinstance(message_tool_calls, list) or len(message_tool_calls) != len(tool_calls):
        raise ApiError(502, "main_agent_invalid_action", "assistant_message.tool_calls 数量必须和 tool_calls 一致。")

    expected_ids = [call.tool_call_id for call in tool_calls]
    actual_ids: list[str] = []
    for index, raw_call in enumerate(message_tool_calls):
        if not isinstance(raw_call, dict):
            raise ApiError(502, "main_agent_invalid_action", f"assistant_message.tool_calls[{index}] 必须为对象。")
        actual_ids.append(str(raw_call.get("id") or ""))
    if actual_ids != expected_ids:
        raise ApiError(502, "main_agent_invalid_action", "assistant_message.tool_calls id 顺序必须和 tool_calls 一致。")
