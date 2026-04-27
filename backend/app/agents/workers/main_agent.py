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
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class MainAgentAction:
    type: Literal["respond", "tool_call"]
    text: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    tool_call_id: str | None = None


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
]


async def decide_main_agent_step(
    llm_client: SupportsGenerateWithTools,
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    on_text_delta: Any | None = None,
    temperature: float = 0.2,
) -> MainAgentAction:
    """请求主 agent 做一步决策，返回统一的 Action 结构。"""
    result = await llm_client.generate_with_tools_stream(
        system_prompt=system_prompt,
        messages=messages,
        tools=MAIN_AGENT_TOOLS,
        on_text_delta=on_text_delta,
        temperature=temperature,
    )
    action_type = result.get("type")
    if action_type == "respond":
        text = str(result.get("text") or "").strip()
        return MainAgentAction(type="respond", text=text)
    if action_type == "tool_call":
        tool = str(result.get("tool") or "").strip()
        if not tool:
            raise ApiError(502, "main_agent_invalid_action", "主 agent tool_call 缺少 tool。")
        arguments = result.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ApiError(502, "main_agent_invalid_action", "主 agent tool_call arguments 必须为对象。")
        return MainAgentAction(
            type="tool_call",
            tool=tool,
            arguments=arguments,
            tool_call_id=str(result.get("tool_call_id") or ""),
        )
    raise ApiError(502, "main_agent_invalid_action", f"主 agent 返回了未知动作类型：{action_type}")
