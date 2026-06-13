from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...domain.document_tool_results import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool

_SKIPPED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
}
_DEFAULT_TEXT_FILE_MAX_BYTES = 1_000_000
_GLOB_CHARS = frozenset("*?[")
_DEFAULT_GLOB_SCANNED_PATHS = 3_000
_MAX_GLOB_SCANNED_PATHS = 10_000
_DEFAULT_GLOB_SCAN_SECONDS = 1.5
_MAX_GLOB_SCAN_SECONDS = 5.0


class FileGlobArguments(BaseModel):
    path: str = Field(default=".", description="搜索起点；可使用相对当前项目工作区的路径，或用户提供的绝对源码路径。")
    pattern: str = Field(default="*", description="glob 模式，例如 **/*.py 或 src/**/*.ts。也可直接把 glob 写在 path 中。")
    limit: int = Field(default=100, ge=1, le=500, description="最多返回多少条结果，默认 100，最大 500。")
    offset: int = Field(default=0, ge=0, description="分页偏移，从 0 开始。")


    max_scanned_paths: int = Field(default=_DEFAULT_GLOB_SCANNED_PATHS, ge=1, le=_MAX_GLOB_SCANNED_PATHS)
    max_elapsed_ms: int = Field(default=int(_DEFAULT_GLOB_SCAN_SECONDS * 1000), ge=100, le=int(_MAX_GLOB_SCAN_SECONDS * 1000))


class FileSearchArguments(BaseModel):
    path: str = Field(default=".", description="搜索起点文件或目录；可使用相对当前项目工作区的路径，或用户提供的绝对源码路径。")
    pattern: str = Field(description="要搜索的文本或正则表达式。默认按普通文本搜索。")
    include_glob: str = Field(default="**/*", description="限制搜索文件的 glob，例如 **/*.py。")
    mode: Literal["lines", "files", "count"] = Field(
        default="lines",
        description="lines 返回匹配行；files 只返回命中文件；count 返回每个文件命中次数。",
    )
    regex: bool = Field(default=False, description="是否把 pattern 当作正则表达式。")
    case_sensitive: bool = Field(default=True, description="是否区分大小写。")
    context_lines: int = Field(default=0, ge=0, le=5, description="lines 模式下每条命中附带的上下文行数，最大 5。")
    limit: int = Field(default=100, ge=1, le=500, description="最多返回多少条结果，默认 100，最大 500。")
    offset: int = Field(default=0, ge=0, description="分页偏移，从 0 开始。")


class FileReadArguments(BaseModel):
    path: str = Field(description="要读取的文件路径；可使用相对当前项目工作区的路径、用户提供的绝对源码路径，或工具返回的 runtime 输出路径。")
    start_line: int = Field(default=1, ge=1, description="起始行号，从 1 开始。")
    limit: int = Field(default=120, ge=1, le=500, description="最多读取多少行，默认 120，最大 500。")


@agent_tool(args_model=FileGlobArguments)
def file_glob(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """按 glob 模式查找项目工作区内的文件或目录。

    Returns:
        返回 matches、returned、offset、next_offset 和 truncated；项目内路径返回相对路径，外部绝对路径保持绝对路径。

    Rules:
        - 查找文件时优先使用本工具，不要用 exec_command 执行 find 或大量 ls。
        - 结果分页返回；truncated 为 true 时用 next_offset 继续查询。

    Examples:
        - 查找 Python 文件：{"pattern":"**/*.py","limit":100}
    """
    base_result = _resolve_glob_base_and_pattern(
        store,
        project_id,
        str(arguments.get("path") or "."),
        str(arguments.get("pattern") or "*"),
    )
    if isinstance(base_result, dict):
        return base_result
    base, pattern = base_result

    limit = int(arguments.get("limit") or 100)
    offset = int(arguments.get("offset") or 0)
    scan_budget = min(
        int(arguments.get("max_scanned_paths") or _DEFAULT_GLOB_SCANNED_PATHS),
        _MAX_GLOB_SCANNED_PATHS,
    )
    time_budget_seconds = min(
        int(arguments.get("max_elapsed_ms") or int(_DEFAULT_GLOB_SCAN_SECONDS * 1000)) / 1000,
        _MAX_GLOB_SCAN_SECONDS,
    )
    started_at = time.monotonic()
    skipped_dirs: set[str] = set()
    page: list[str] = []
    matched = 0
    scanned = 0
    stop_reason = "completed"

    for path in _iter_glob_candidates(base, pattern, skipped_dirs):
        elapsed = time.monotonic() - started_at
        if elapsed >= time_budget_seconds:
            stop_reason = "time_budget_exceeded"
            break
        if scanned >= scan_budget:
            stop_reason = "scan_budget_exceeded"
            break
        scanned += 1
        if not _matches_relative_glob(path.relative_to(base).as_posix(), pattern):
            continue
        if not _is_allowed_result_path(path):
            continue

        matched += 1
        if matched <= offset:
            continue
        page.append(_relative_path(store, project_id, path))
        if len(page) >= limit:
            stop_reason = "limit_reached"
            break

    truncated = stop_reason != "completed"
    next_offset = offset + len(page) if truncated and page else None
    return tool_success(
        {
            "matches": page,
            "returned": len(page),
            "total": offset + len(page) if truncated else matched,
            "total_is_lower_bound": truncated,
            "offset": offset,
            "next_offset": next_offset,
            "truncated": truncated,
            "stop_reason": stop_reason,
            "scanned": scanned,
            "scan_budget": scan_budget,
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            "time_budget_ms": int(time_budget_seconds * 1000),
            "skipped_dirs": sorted(skipped_dirs),
            "effective_path": _relative_path(store, project_id, base),
            "effective_pattern": pattern,
        }
    )


@agent_tool(args_model=FileSearchArguments)
def file_search(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """搜索文本内容，可搜索项目工作区或用户提供的绝对源码路径。

    Returns:
        根据 mode 返回匹配行、命中文件或计数；结果带 offset、next_offset 和 truncated，便于分页继续搜索。

    Rules:
        - 搜索代码或文本时优先使用本工具，不要用 exec_command 执行 grep、rg 或 Select-String。
        - 结果分页返回；truncated 为 true 时用 next_offset 继续查询。
        - 搜索会跳过常见依赖、构建和版本控制目录。

    Examples:
        - 搜索调用点：{"pattern":"exec_command","mode":"lines","limit":100}
    """
    target_result = _resolve_project_path(store, project_id, str(arguments.get("path") or "."))
    if isinstance(target_result, dict):
        return target_result
    target = target_result

    pattern = str(arguments.get("pattern") or "")
    if not pattern:
        return tool_failed("invalid_operation", "pattern 字段缺失。")
    include_glob = str(arguments.get("include_glob") or "**/*")
    mode = str(arguments.get("mode") or "lines")
    if mode not in {"lines", "files", "count"}:
        return tool_failed("invalid_operation", "mode 必须是 lines、files 或 count。")
    regex = bool(arguments.get("regex", False))
    case_sensitive = bool(arguments.get("case_sensitive", True))
    context_lines = int(arguments.get("context_lines") or 0)
    limit = int(arguments.get("limit") or 100)
    offset = int(arguments.get("offset") or 0)

    try:
        matcher = _build_matcher(pattern, regex=regex, case_sensitive=case_sensitive)
    except re.error as exc:
        return tool_failed("invalid_operation", f"regex 无效：{exc}")

    results: list[dict[str, Any]] = []
    total_matches = 0
    for file_path in _iter_search_files(target, include_glob):
        file_result = _search_file(
            store,
            project_id,
            file_path,
            matcher,
            mode=mode,
            context_lines=context_lines,
        )
        if file_result is None:
            continue
        if mode == "count":
            total_matches += int(file_result.get("count") or 0)
            results.append(file_result)
            continue
        if mode == "files":
            total_matches += 1
            results.append(file_result)
            continue
        line_matches = file_result.get("matches")
        if isinstance(line_matches, list):
            total_matches += len(line_matches)
            results.extend(line_matches)

    page = results[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(results) else None
    return tool_success(
        {
            "mode": mode,
            "matches": page,
            "returned": len(page),
            "total": len(results),
            "total_line_matches": total_matches,
            "offset": offset,
            "next_offset": next_offset,
            "truncated": next_offset is not None,
        }
    )


@agent_tool(args_model=FileReadArguments)
def file_read(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """按行读取文件片段，可读取项目工作区文件或用户提供的绝对源码路径。

    Returns:
        返回带行号的 content、start_line、end_line、next_start_line 和 truncated。

    Rules:
        - 读取源码、文档或工具落盘输出时优先使用本工具，不要用 exec_command 执行 cat、head 或 tail。
        - 大文件必须分段读取；truncated 为 true 时用 next_start_line 继续读取。

    Examples:
        - 读取文件片段：{"path":"backend/app/tools/builtin/shell.py","start_line":1,"limit":120}
    """
    target_result = _resolve_project_path(store, project_id, str(arguments.get("path") or ""))
    if isinstance(target_result, dict):
        return target_result
    target = target_result
    if not target.is_file():
        return tool_failed("invalid_operation", f"不是可读取文件：{arguments.get('path')}")

    start_line = int(arguments.get("start_line") or 1)
    limit = int(arguments.get("limit") or 120)
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return tool_failed("file_read_failed", f"读取文件失败：{exc}")

    start_index = max(0, start_line - 1)
    selected = lines[start_index : start_index + limit]
    end_line = start_index + len(selected)
    numbered = "\n".join(f"{index} | {line}" for index, line in enumerate(selected, start=start_line))
    next_start_line = end_line + 1 if end_line < len(lines) else None
    return tool_success(
        {
            "path": _relative_path(store, project_id, target),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "content": numbered,
            "next_start_line": next_start_line,
            "truncated": next_start_line is not None,
        }
    )


def _resolve_project_path(store: WorkspaceStore, project_id: str, raw_path: str) -> Path | dict[str, Any]:
    if not raw_path.strip():
        return tool_failed("invalid_operation", "path 字段缺失。")
    root = store.project_dir(project_id).resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return tool_failed("invalid_operation", f"path 无法解析：{raw_path}")
    if not resolved.exists():
        return tool_failed("invalid_operation", f"path 不存在：{raw_path}")
    return resolved


def _resolve_glob_base_and_pattern(
    store: WorkspaceStore,
    project_id: str,
    raw_path: str,
    raw_pattern: str,
) -> tuple[Path, str] | dict[str, Any]:
    if not raw_path.strip():
        return tool_failed("invalid_operation", "path 字段缺失。")

    pattern = raw_pattern.strip() or "*"
    if not _contains_glob(raw_path):
        base_result = _resolve_project_path(store, project_id, raw_path)
        if isinstance(base_result, dict):
            return base_result
        return base_result, pattern

    root = store.project_dir(project_id).resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    parts = candidate.parts
    glob_index = next((index for index, part in enumerate(parts) if _contains_glob(part)), None)
    if glob_index is None:
        return tool_failed("invalid_operation", f"path 无法解析为 glob：{raw_path}")
    base = Path(*parts[:glob_index]) if glob_index > 0 else root
    glob_pattern = Path(*parts[glob_index:]).as_posix()
    try:
        resolved_base = base.resolve()
    except OSError:
        return tool_failed("invalid_operation", f"path 无法解析：{raw_path}")
    if not resolved_base.exists():
        return tool_failed("invalid_operation", f"path 不存在：{base}")
    return resolved_base, glob_pattern


def _contains_glob(value: str) -> bool:
    return any(char in value for char in _GLOB_CHARS)


def _relative_path(store: WorkspaceStore, project_id: str, path: Path) -> str:
    resolved = path.resolve()
    root = store.project_dir(project_id).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def _is_allowed_result_path(path: Path) -> bool:
    return not any(part in _SKIPPED_DIRS for part in path.parts)


def _iter_glob_candidates(base: Path, pattern: str, skipped_dirs: set[str]):
    max_depth = _literal_glob_depth(pattern)
    for current_root, dirnames, filenames in os.walk(base, topdown=True):
        current = Path(current_root)
        try:
            root_depth = len(current.relative_to(base).parts)
        except ValueError:
            root_depth = 0

        descend_dirnames = []
        for dirname in sorted(dirnames):
            if dirname in _SKIPPED_DIRS:
                skipped_dirs.add(_display_skipped_dir(base, current / dirname))
                continue
            yield current / dirname
            if max_depth is None or root_depth + 1 < max_depth:
                descend_dirnames.append(dirname)
        dirnames[:] = descend_dirnames

        for filename in sorted(filenames):
            yield current / filename


def _literal_glob_depth(pattern: str) -> int | None:
    parts = [part for part in pattern.replace("\\", "/").split("/") if part and part != "."]
    if "**" in parts:
        return None
    return max(1, len(parts))


def _matches_relative_glob(relative_path: str, pattern: str) -> bool:
    path_parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    pattern_parts = tuple(part for part in pattern.replace("\\", "/").split("/") if part and part != ".")
    return _match_glob_parts(path_parts, pattern_parts)


def _match_glob_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head, *tail = pattern_parts
    tail_tuple = tuple(tail)
    if head == "**":
        return _match_glob_parts(path_parts, tail_tuple) or (
            bool(path_parts) and _match_glob_parts(path_parts[1:], pattern_parts)
        )
    if not path_parts:
        return False
    return fnmatch.fnmatchcase(path_parts[0], head) and _match_glob_parts(path_parts[1:], tail_tuple)


def _display_skipped_dir(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _iter_search_files(target: Path, include_glob: str) -> list[Path]:
    if target.is_file():
        return [target] if _is_text_candidate(target) else []
    files: list[Path] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if not _is_text_candidate(path):
            continue
        if any(part in _SKIPPED_DIRS for part in path.relative_to(target).parts):
            continue
        relative = path.relative_to(target).as_posix()
        if include_glob not in {"*", "**/*"} and not fnmatch.fnmatch(relative, include_glob):
            continue
        files.append(path)
    return files


def _is_text_candidate(path: Path) -> bool:
    try:
        return path.stat().st_size <= _DEFAULT_TEXT_FILE_MAX_BYTES
    except OSError:
        return False


def _build_matcher(pattern: str, *, regex: bool, case_sensitive: bool):
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(pattern, flags)
        return lambda text: compiled.search(text) is not None
    needle = pattern if case_sensitive else pattern.lower()
    return lambda text: needle in (text if case_sensitive else text.lower())


def _search_file(
    store: WorkspaceStore,
    project_id: str,
    path: Path,
    matcher,
    *,
    mode: str,
    context_lines: int,
) -> dict[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    matching_indexes = [index for index, line in enumerate(lines) if matcher(line)]
    if not matching_indexes:
        return None
    rel_path = _relative_path(store, project_id, path)
    if mode == "files":
        return {"path": rel_path}
    if mode == "count":
        return {"path": rel_path, "count": len(matching_indexes)}
    matches = []
    for index in matching_indexes:
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        context = [
            {"line": line_number, "text": lines[line_number - 1]}
            for line_number in range(start + 1, end + 1)
        ]
        matches.append(
            {
                "path": rel_path,
                "line": index + 1,
                "text": lines[index],
                "context": context if context_lines else [],
            }
        )
    return {"matches": matches}
