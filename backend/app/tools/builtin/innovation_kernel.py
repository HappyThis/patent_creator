from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...domain.document_tool_results import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool


class InnovationKernelKitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["read", "write"] = Field(
        description="read 读取当前 session 的完整创新内核；write 覆盖写入当前 session 的完整创新内核。"
    )
    kernel_markdown: str | None = Field(
        default=None,
        description="write 时必填，必须是完整创新内核 markdown；read 时不要提供。",
    )


@agent_tool(args_model=InnovationKernelKitArguments)
async def innovation_kernel_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    *,
    context: Any,
) -> dict[str, Any]:
    """读取或覆盖当前 session 的创新内核 markdown。

    Returns:
        read 返回当前 kernel_markdown；write 返回覆盖保存后的 kernel_markdown。失败时返回 failed 和 code/message。

    Rules:
        - 创新内核是交底书生成前的当前态核心事实源，不是交底书章节，也没有历史版本。
        - action 只能是 read 或 write。
        - read 只读取当前完整创新内核；不存在时返回 failed。
        - write 只负责保存调用方提供的完整 kernel_markdown；不生成、不补全、不解析模型输出。
        - write 是覆盖写，不支持 patch。调用方若不确定当前内核内容，应先 read，再生成完整新版并 write。

    Examples:
        - 读取当前创新内核: {"action":"read"}
        - 覆盖写入当前创新内核: {"action":"write","kernel_markdown":"# 创新内核\\n\\n..."}
    """
    action = str(arguments.get("action") or "").strip()
    if action not in {"read", "write"}:
        return tool_failed("invalid_action", "innovation_kernel_kit.action 必须是 read 或 write。")
    if not context.session_id:
        return tool_failed("innovation_kernel_session_unavailable", "innovation_kernel_kit 缺少 session 上下文。")

    if action == "read":
        current = store.get_innovation_kernel(project_id, context.session_id)
        if current is None or not current.kernel_markdown.strip():
            return tool_failed("innovation_kernel_not_found", "当前 session 暂无创新内核。")
        return tool_success(current.model_dump())

    kernel_markdown = str(arguments.get("kernel_markdown") or "").strip()
    if not kernel_markdown:
        return tool_failed("innovation_kernel_empty_content", "innovation_kernel_kit.write 需要非空 kernel_markdown。")

    record = store.save_innovation_kernel(
        project_id,
        context.session_id,
        kernel_markdown=kernel_markdown,
        source="write",
    )
    return tool_success(record.model_dump())
