from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import inspect
import json
from typing import Any, TypeVar, cast

from pydantic import BaseModel

ToolCallable = TypeVar("ToolCallable", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class ToolFunctionMetadata:
    name: str
    description: str
    args_model: type[BaseModel]
    result_contract: str
    usage_rules: tuple[str, ...]
    examples: tuple[tuple[str, dict[str, Any]], ...]


def agent_tool(
    *,
    args_model: type[BaseModel],
    name: str | None = None,
) -> Callable[[ToolCallable], ToolCallable]:
    def decorate(func: ToolCallable) -> ToolCallable:
        parsed_doc = parse_tool_docstring(func.__doc__ or "")
        metadata = ToolFunctionMetadata(
            name=name or func.__name__,
            description=parsed_doc["description"],
            args_model=args_model,
            result_contract=parsed_doc["result_contract"],
            usage_rules=tuple(parsed_doc["usage_rules"]),
            examples=tuple(parsed_doc["examples"]),
        )
        cast(Any, func).__agent_tool__ = metadata
        return func

    return decorate


def get_tool_metadata(func: Callable[..., Any]) -> ToolFunctionMetadata:
    metadata = getattr(func, "__agent_tool__", None)
    if not isinstance(metadata, ToolFunctionMetadata):
        raise TypeError(f"{func!r} is not an agent tool function")
    return metadata


def parse_tool_docstring(docstring: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {"description": []}
    current = "description"
    for raw_line in inspect.cleandoc(docstring).splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered in {"returns:", "rules:", "examples:"}:
            current = lowered[:-1]
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    description = _join_text(sections.get("description", []))
    result_contract = _join_text(sections.get("returns", []))
    usage_rules = _parse_rules(sections.get("rules", []))
    examples = _parse_examples(sections.get("examples", []))
    if not description:
        raise ValueError("tool docstring must include a description")
    if not result_contract:
        raise ValueError(f"tool {description!r} docstring must include Returns section")
    return {
        "description": description,
        "result_contract": result_contract,
        "usage_rules": usage_rules,
        "examples": examples,
    }


def _join_text(lines: list[str]) -> str:
    return " ".join(line for line in lines if line).strip()


def _parse_rules(lines: list[str]) -> list[str]:
    rules: list[str] = []
    for line in lines:
        if not line:
            continue
        rules.append(line[2:].strip() if line.startswith("- ") else line)
    return rules


def _parse_examples(lines: list[str]) -> list[tuple[str, dict[str, Any]]]:
    examples: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        if not line:
            continue
        text = line[2:].strip() if line.startswith("- ") else line
        label, separator, payload = text.partition("：")
        if not separator:
            label, separator, payload = text.partition(":")
        if not separator:
            raise ValueError(f"tool example must use '<label>: <json>': {line}")
        try:
            arguments = json.loads(payload.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool example is not valid JSON: {line}") from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"tool example arguments must be a JSON object: {line}")
        examples.append((label.strip(), arguments))
    return examples
