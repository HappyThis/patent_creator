from __future__ import annotations

import hashlib
from uuid import uuid4

from ..storage.workspace_store import WorkspaceStore

EXEC_COMMAND_INLINE_LIMIT_CHARS = 30_000
EXEC_COMMAND_PREVIEW_CHARS = 6_000
TOOL_RESULT_TURN_BUDGET_CHARS = 150_000
TOOL_RESULT_PREVIEW_CHARS = 6_000


def head_tail_preview(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 32:
        return text[:max_chars]
    marker = "\n\n...[output truncated; middle omitted]...\n\n"
    available = max_chars - len(marker)
    if available <= 0:
        return text[:max_chars]
    head_chars = available // 2
    tail_chars = available - head_chars
    return f"{text[:head_chars]}{marker}{text[-tail_chars:]}"


def write_tool_output(
    store: WorkspaceStore,
    project_id: str,
    content: str,
    *,
    stem: str,
    suffix: str,
    dedupe_key: str | None = None,
) -> str:
    output_dir = store.project_dir(project_id) / "runtime" / "tool_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in stem).strip("_")
    identifier = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16] if dedupe_key else uuid4().hex[:12]
    name = f"{safe_stem or 'tool_output'}-{identifier}{suffix}"
    path = output_dir / name
    if not path.exists():
        path.write_text(content, encoding="utf-8")
    return path.relative_to(store.project_dir(project_id)).as_posix()
