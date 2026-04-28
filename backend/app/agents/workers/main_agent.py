from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ...core import ApiError


class SupportsGenerateWithTools(Protocol):
    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any,
    ) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class MainAgentToolCall:
    tool: str
    arguments: dict[str, Any]
    tool_call_id: str


@dataclass(slots=True)
class MainAgentAction:
    type: Literal["respond", "tool_calls"]
    text: str | None = None
    tool_calls: list[MainAgentToolCall] | None = None
    assistant_message: dict[str, Any] | None = None


MAIN_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "document_read",
            "description": "按 section_id 或 block_id 读取当前交底书的部分正文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get_section", "get_block"],
                        "description": "读取动作。get_section 按章节 id，get_block 按 block id。",
                    },
                    "section_id": {
                        "type": "string",
                        "description": "章节 id，例如 technical_solution。action=get_section 时必填。",
                    },
                    "block_id": {
                        "type": "string",
                        "description": "block id。action=get_block 时必填。",
                    },
                    "include_children": {
                        "type": "boolean",
                        "description": "章节读取时是否同时返回子章节，默认为 true。",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "document_edit",
            "description": "原子应用一组 operations 写入 disclosure.json。operations 通常来自子 agent 返回的 proposal。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "description": "按顺序应用的编辑操作。每个操作必须包含 op 字段。",
                        "items": {"type": "object"},
                    },
                },
                "required": ["operations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_subagent",
            "description": "调度一个子 agent 执行局部任务（写作 / 分析 / 收敛 / 审查），返回统一的 envelope 结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "enum": [
                            "section_writer",
                            "material_analyst",
                            "solution_refiner",
                            "consistency_reviewer",
                        ],
                        "description": "目标子 agent 的 id。",
                    },
                    "call_type": {
                        "type": "string",
                        "enum": [
                            "rich_context_specialist",
                            "task_only_specialist",
                            "forked_context",
                        ],
                        "description": "上下文装配策略，常用 rich_context_specialist。",
                    },
                    "goal": {
                        "type": "string",
                        "description": "面向子 agent 的任务描述，尽量具体。",
                    },
                    "target_section_id": {
                        "type": "string",
                        "description": "目标章节 id。section_writer 必填。",
                    },
                    "target_block_id": {
                        "type": "string",
                        "description": "目标 block id（可选）。",
                    },
                    "user_message": {
                        "type": "string",
                        "description": "用户本轮原始输入文本，用于给子 agent 提供一手材料。",
                    },
                },
                "required": ["agent_id", "call_type", "goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "在项目工作区内执行命令字符串，cwd 为当前 project 工作区。可用于读取文件、访问外部资料、运行诊断命令或 git 命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令字符串，按当前项目工作区作为 cwd 执行。",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "超时时间，单位秒，默认 30。",
                    },
                },
                "required": ["command"],
            },
        },
    },
]


async def decide_main_agent_step(
    llm_client: SupportsGenerateWithTools,
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    on_text_delta: Any | None = None,
) -> MainAgentAction:
    """请求主 agent 做一步决策，返回统一的 Action 结构。"""
    result = await llm_client.generate_with_tools_stream(
        system_prompt=system_prompt,
        messages=messages,
        tools=MAIN_AGENT_TOOLS,
        on_text_delta=on_text_delta,
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
        )
    raise ApiError(502, "main_agent_invalid_action", f"主 agent 返回了未知动作类型：{action_type}")


def _assistant_message(result: dict[str, Any]) -> dict[str, Any] | None:
    value = result.get("assistant_message")
    return value if isinstance(value, dict) else None


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
        calls.append(MainAgentToolCall(tool=tool, arguments=arguments, tool_call_id=tool_call_id))
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
