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
        response_format_json: bool = False,
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


MAIN_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "document_read",
            "description": "按 section_id、block_id 或关键词读取当前交底书的部分正文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get_meta", "get_project_context", "get_outline", "get_section", "get_block", "search_blocks"],
                        "description": "读取动作。get_project_context 返回标题和完整目录树；get_section 按章节 id；get_block 按 block id；search_blocks 按关键词搜索正文。",
                    },
                    "section_id": {
                        "type": "string",
                        "description": "系统生成的章节 id，例如 sec_000007。action=get_section 时必填；标准章节语义看 outline 中的 type。",
                    },
                    "block_id": {
                        "type": "string",
                        "description": "block id。action=get_block 时必填。",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。action=search_blocks 时必填。",
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
                        "description": "按顺序应用的编辑操作。每个操作必须包含 op 字段。append_child_section 必须使用 parent_section_id 和 section；新增或替换 section 的 section 对象不允许携带 id。",
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
                    "goal": {
                        "type": "string",
                        "description": "面向子 agent 的自然语言任务目标；目标范围、输出要求和注意事项都写在这里。",
                    },
                },
                "required": ["agent_id", "goal"],
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
