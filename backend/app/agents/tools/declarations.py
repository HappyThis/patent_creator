from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...runtime.executor.subagent_pipe import SubagentPipe
from ...runtime.executor.tools.document import document_edit, document_read
from ...runtime.executor.tools.shell import exec_command
from ...runtime.executor.tools.subagent import execute_subagent
from ..registry import SUBAGENTS
from ..tool_metadata import ToolFunctionMetadata, get_tool_metadata
from ..types import SubagentDeclaration

MAIN_AGENT_TOOL_NAMES = ("document_read", "document_edit", "execute_subagent", "exec_command")
SUBAGENT_PROTOCOL_TOOL_NAMES = ("write_pipe", "finish")


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    function: Callable[..., Any]
    metadata: ToolFunctionMetadata

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self.metadata.args_model.model_json_schema()

    @property
    def result_contract(self) -> str:
        return self.metadata.result_contract

    @property
    def usage_rules(self) -> tuple[str, ...]:
        return self.metadata.usage_rules

    @property
    def examples(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        return self.metadata.examples

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_openai_tools(tool_names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [get_tool_declaration(tool_name).openai_tool() for tool_name in tool_names]


def subagent_tool_names(declaration: SubagentDeclaration) -> tuple[str, ...]:
    return (*declaration.tool_permissions, *SUBAGENT_PROTOCOL_TOOL_NAMES)


def build_subagent_tools(declaration: SubagentDeclaration) -> list[dict[str, Any]]:
    return build_openai_tools(subagent_tool_names(declaration))


def render_tool_manual(tool_names: tuple[str, ...] | list[str]) -> str:
    lines = [
        "以下工具说明由工具函数自动生成，是工具调用的唯一准确信息源。",
        "通用要求：arguments 必须是严格 JSON 对象；字符串使用双引号；不能使用注释、尾随逗号或未转义换行。",
    ]
    for declaration in [get_tool_declaration(tool_name) for tool_name in tool_names]:
        lines.extend(
            [
                "",
                f"### {declaration.name}",
                f"- 用途：{declaration.description}",
                f"- 参数：{_render_schema_summary(declaration.parameters)}",
                f"- 返回：{declaration.result_contract}",
            ]
        )
        if declaration.usage_rules:
            lines.append("- 使用规则：")
            lines.extend(f"  - {rule}" for rule in declaration.usage_rules)
        if declaration.name == "execute_subagent":
            lines.extend(_render_execute_subagent_catalog())
        if declaration.examples:
            lines.append("- 调用实例：")
            lines.extend(f"  - {label}：{_compact_json(arguments)}" for label, arguments in declaration.examples)
    return "\n".join(lines)


def get_tool_declaration(tool_name: str) -> ToolDeclaration:
    try:
        return _TOOL_REGISTRY[tool_name]
    except KeyError as exc:
        raise KeyError(f"unknown tool declaration: {tool_name}") from exc


def _render_schema_summary(schema: dict[str, Any]) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "无参数。"
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    parts: list[str] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            parts.append(name)
            continue
        type_name = _schema_type_name(spec)
        marker = "必填" if name in required_names else "可选"
        enum = spec.get("enum")
        enum_text = f"，可选值：{', '.join(map(str, enum))}" if isinstance(enum, list) else ""
        description = spec.get("description")
        description_text = f"，{description}" if isinstance(description, str) and description else ""
        parts.append(f"{name}({type_name}，{marker}{enum_text}{description_text})")
    return "；".join(parts) + "。"


def _schema_type_name(spec: dict[str, Any]) -> str:
    type_name = spec.get("type")
    if isinstance(type_name, str):
        return type_name
    any_of = spec.get("anyOf")
    if isinstance(any_of, list):
        names = [item.get("type") for item in any_of if isinstance(item, dict) and item.get("type") != "null"]
        if names:
            return "|".join(str(name) for name in names)
    return "any"


def _compact_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_execute_subagent_catalog() -> list[str]:
    lines = ["- 可用子 agent："]
    for declaration in SUBAGENTS.values():
        tools = "、".join(declaration.tool_permissions)
        lines.extend(
            [
                f"  - {declaration.id}",
                f"    - 职责：{declaration.description}",
                f"    - 输入要求：{declaration.input_expectation}",
                f"    - 返回值：{declaration.output_contract}",
                f"    - 使用边界：{declaration.usage_guidance}",
                f"    - 可用工具：{tools}",
            ]
        )
    return lines


def _build_tool_registry(functions: tuple[Callable[..., Any], ...]) -> dict[str, ToolDeclaration]:
    registry: dict[str, ToolDeclaration] = {}
    for function in functions:
        metadata = get_tool_metadata(function)
        registry[metadata.name] = ToolDeclaration(function=function, metadata=metadata)
    return registry


_TOOL_FUNCTIONS = (
    document_read,
    document_edit,
    execute_subagent,
    exec_command,
    SubagentPipe.write,
    SubagentPipe.finish,
)
_TOOL_REGISTRY = _build_tool_registry(_TOOL_FUNCTIONS)
