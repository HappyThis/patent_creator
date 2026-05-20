from __future__ import annotations

from dataclasses import dataclass
import json
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class NormalizedArguments:
    arguments: dict[str, Any]
    normalized_paths: tuple[str, ...]


def normalize_stringified_json_arguments(
    args_model: type[BaseModel],
    arguments: dict[str, Any],
) -> NormalizedArguments:
    """Parse JSON strings only where the tool schema expects objects or arrays."""
    normalized, paths = _normalize_model(args_model, arguments, ())
    if not isinstance(normalized, dict):
        return NormalizedArguments(arguments=arguments, normalized_paths=())
    return NormalizedArguments(arguments=normalized, normalized_paths=tuple(paths))


def _normalize_model(
    model: type[BaseModel],
    value: Any,
    path: tuple[str, ...],
) -> tuple[Any, list[str]]:
    if not isinstance(value, dict):
        return value, []

    changed = False
    output = dict(value)
    normalized_paths: list[str] = []
    for field_name, field in model.model_fields.items():
        if field_name not in output:
            continue
        child, child_paths = _normalize_for_annotation(field.annotation, output[field_name], (*path, field_name))
        if child_paths:
            output[field_name] = child
            changed = True
            normalized_paths.extend(child_paths)
    return (output if changed else value), normalized_paths


def _normalize_for_annotation(annotation: Any, value: Any, path: tuple[str, ...]) -> tuple[Any, list[str]]:
    if value is None:
        return value, []

    kind, inner = _json_container_kind(annotation)
    if kind is None:
        return value, []

    decoded = value
    normalized_paths: list[str] = []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value, []
        if kind == "object" and not isinstance(decoded, dict):
            return value, []
        if kind == "array" and not isinstance(decoded, list):
            return value, []
        normalized_paths.append(".".join(path))

    if kind == "object":
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            child, child_paths = _normalize_model(inner, decoded, path)
            normalized_paths.extend(child_paths)
            return child, normalized_paths
        return decoded, normalized_paths

    if kind == "array":
        child_annotation = inner
        if child_annotation is None or not isinstance(decoded, list):
            return decoded, normalized_paths
        changed = False
        items: list[Any] = []
        for index, item in enumerate(decoded):
            child, child_paths = _normalize_for_annotation(child_annotation, item, (*path, str(index)))
            items.append(child)
            if child_paths:
                changed = True
                normalized_paths.extend(child_paths)
        return (items if changed else decoded), normalized_paths

    return decoded, normalized_paths


def _json_container_kind(annotation: Any) -> tuple[str | None, Any]:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, UnionType):
        non_null_args = [arg for arg in args if arg is not type(None)]
        if any(arg is str for arg in non_null_args):
            return None, None
        candidates = [_json_container_kind(arg) for arg in non_null_args]
        candidates = [candidate for candidate in candidates if candidate[0] is not None]
        if len(candidates) == 1:
            return candidates[0]
        return None, None

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object", annotation

    if origin is list:
        return "array", args[0] if args else None

    if origin is dict:
        return "object", None

    return None, None
