from __future__ import annotations

from collections.abc import Callable
import copy
from dataclasses import dataclass
import json
from typing import Any

from .builtin.document import (
    document_append_block,
    document_append_child_section,
    document_clear_section_blocks,
    document_read,
    document_replace_block,
    document_replace_section_blocks,
)
from .builtin.filesystem import file_glob, file_read, file_search
from .builtin.shell import exec_command
from .metadata import ToolFunctionMetadata, get_tool_metadata
from .types import AgentScope

SummaryStarted = Callable[[dict[str, Any]], str]
SummaryFinished = Callable[[dict[str, Any], dict[str, Any]], str]


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    function: Callable[..., Any]
    metadata: ToolFunctionMetadata
    scopes: frozenset[AgentScope]
    mutates_document: bool = False
    summary_started: SummaryStarted | None = None
    summary_finished: SummaryFinished | None = None

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def parameters(self) -> dict[str, Any]:
        return _inline_schema_refs(self.metadata.args_model.model_json_schema())

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

    def can_use(self, scope: AgentScope) -> bool:
        return scope in self.scopes

    def started_summary(self, arguments: dict[str, Any]) -> str:
        if self.summary_started is not None:
            return self.summary_started(arguments)
        return f"开始执行 {self.name}"

    def finished_summary(self, arguments: dict[str, Any], result: dict[str, Any]) -> str:
        if result.get("status") == "failed":
            return "执行失败"
        if self.summary_finished is not None:
            return self.summary_finished(arguments, result)
        return f"{self.name} 已完成"


def build_openai_tools(tool_names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [get_tool_declaration(tool_name).openai_tool() for tool_name in tool_names]


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


def _inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            resolved = copy.deepcopy(defs.get(name, {}))
            overrides = {key: item for key, item in value.items() if key != "$ref"}
            resolved.update(overrides)
            return visit(resolved)
        return {key: visit(item) for key, item in value.items() if key != "$defs"}

    return visit(schema)


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _document_read_started(arguments: dict[str, Any]) -> str:
    section_id = arguments.get("section_id") or arguments.get("block_id") or ""
    return f"开始读取 {section_id}" if section_id else "开始读取章节"


def _document_read_finished(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    section_id = arguments.get("section_id") or arguments.get("block_id") or ""
    return f"{section_id} 已读取" if section_id else "章节已读取"


def _document_write_started(arguments: dict[str, Any]) -> str:
    return "开始写入文档"


def _document_write_finished(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    return "文档更新已完成"


def _exec_started(arguments: dict[str, Any]) -> str:
    return "开始执行诊断命令"


def _exec_finished(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    return "诊断命令已完成"


def _file_glob_started(arguments: dict[str, Any]) -> str:
    return "开始查找文件"


def _file_glob_finished(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    return "文件查找已完成"


def _file_search_started(arguments: dict[str, Any]) -> str:
    return "开始搜索文件内容"


def _file_search_finished(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    return "文件内容搜索已完成"


def _file_read_started(arguments: dict[str, Any]) -> str:
    return "开始读取文件"


def _file_read_finished(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    return "文件读取已完成"


@dataclass(frozen=True, slots=True)
class _ToolRegistration:
    function: Callable[..., Any]
    scopes: frozenset[AgentScope]
    mutates_document: bool = False
    summary_started: SummaryStarted | None = None
    summary_finished: SummaryFinished | None = None


def _build_tool_registry(registrations: tuple[_ToolRegistration, ...]) -> dict[str, ToolDeclaration]:
    registry: dict[str, ToolDeclaration] = {}
    for registration in registrations:
        metadata = get_tool_metadata(registration.function)
        registry[metadata.name] = ToolDeclaration(
            function=registration.function,
            metadata=metadata,
            scopes=registration.scopes,
            mutates_document=registration.mutates_document,
            summary_started=registration.summary_started,
            summary_finished=registration.summary_finished,
        )
    return registry


_MAIN: frozenset[AgentScope] = frozenset({"main_agent"})

_TOOL_REGISTRATIONS = (
    _ToolRegistration(
        document_read,
        _MAIN,
        summary_started=_document_read_started,
        summary_finished=_document_read_finished,
    ),
    _ToolRegistration(
        document_replace_section_blocks,
        _MAIN,
        mutates_document=True,
        summary_started=_document_write_started,
        summary_finished=_document_write_finished,
    ),
    _ToolRegistration(
        document_append_block,
        _MAIN,
        mutates_document=True,
        summary_started=_document_write_started,
        summary_finished=_document_write_finished,
    ),
    _ToolRegistration(
        document_replace_block,
        _MAIN,
        mutates_document=True,
        summary_started=_document_write_started,
        summary_finished=_document_write_finished,
    ),
    _ToolRegistration(
        document_append_child_section,
        _MAIN,
        mutates_document=True,
        summary_started=_document_write_started,
        summary_finished=_document_write_finished,
    ),
    _ToolRegistration(
        document_clear_section_blocks,
        _MAIN,
        mutates_document=True,
        summary_started=_document_write_started,
        summary_finished=_document_write_finished,
    ),
    _ToolRegistration(file_glob, _MAIN, summary_started=_file_glob_started, summary_finished=_file_glob_finished),
    _ToolRegistration(
        file_search,
        _MAIN,
        summary_started=_file_search_started,
        summary_finished=_file_search_finished,
    ),
    _ToolRegistration(file_read, _MAIN, summary_started=_file_read_started, summary_finished=_file_read_finished),
    _ToolRegistration(exec_command, _MAIN, summary_started=_exec_started, summary_finished=_exec_finished),
)
_TOOL_REGISTRY = _build_tool_registry(_TOOL_REGISTRATIONS)

DOCUMENT_WRITE_TOOL_NAMES = tuple(
    declaration.name for declaration in _TOOL_REGISTRY.values() if declaration.mutates_document
)
MAIN_AGENT_TOOL_NAMES = tuple(
    declaration.name for declaration in _TOOL_REGISTRY.values() if declaration.can_use("main_agent")
)
