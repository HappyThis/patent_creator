from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ....agents.tool_metadata import agent_tool
from ....domain.document_tools import apply_document_edit, read_document, tool_failed
from ....storage.workspace_store import WorkspaceStore
from ..registry import can_use_tool
from ..types import AgentScope


class DocumentReadArguments(BaseModel):
    action: Literal["get_meta", "get_project_context", "get_outline", "get_section", "get_block", "search_blocks"] = Field(
        description="读取动作。get_project_context 返回标题和完整目录树；get_section 按章节 id；get_block 按 block id；search_blocks 按关键词搜索正文。"
    )
    section_id: str | None = Field(default=None, description="系统生成的章节 id，例如 sec_000007。action=get_section 时必填；action=search_blocks 时可选，用于限制搜索范围。标准章节语义看 outline 中的 type。")
    block_id: str | None = Field(default=None, description="block id。action=get_block 时必填。")
    query: str | None = Field(default=None, description="搜索关键词。action=search_blocks 时必填。")
    include_children: bool | None = Field(default=None, description="action=get_section 时是否同时返回子章节；未提供时默认为 false。")


class DocumentEditArguments(BaseModel):
    operations: list[dict[str, Any]] = Field(
        description="按顺序应用的编辑操作。每次 document_edit 的正文写入总量不得超过 1500 字；长内容必须拆成多次小步写入。每个操作必须包含 op 字段。append_child_section 必须使用 parent_section_id 和 section；新增或替换 section 的 section 对象不允许携带 id。"
    )


@agent_tool(
    args_model=DocumentReadArguments,
)
def document_read(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    scope: AgentScope,
) -> dict[str, Any]:
    """按 section_id、block_id 或关键词读取当前交底书的部分正文。

    Returns:
        返回读取结果；失败时返回 failed，并包含 code 和 message。

    Rules:
        - 默认上下文不包含完整文档；需要结构时先读取 get_project_context。
        - 目录中的 id 是系统生成 section_id；目录中的 type 才是技术方案、技术效果等章节语义。
        - 不知道概念在哪一节时，先用 search_blocks 搜索，再读取命中的章节或 block。

    Examples:
        - 读取项目上下文: {"action":"get_project_context"}
        - 读取章节: {"action":"get_section","section_id":"sec_000007","include_children":true}
        - 搜索正文: {"action":"search_blocks","query":"消息平台"}
    """
    if not can_use_tool(scope, "document_read"):
        return tool_failed("permission_denied", "当前调用方不允许读取文档。")
    return read_document(store.get_disclosure(project_id), arguments)


@agent_tool(
    args_model=DocumentEditArguments,
)
def document_edit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    scope: AgentScope,
) -> dict[str, Any]:
    """原子应用一组 operations 写入当前交底书文档。

    Returns:
        返回编辑后的摘要；失败时返回 failed，并包含 code 和 message。正文写入超过 1500 字时返回 edit_too_large。

    Rules:
        - 单次正文写入总量不得超过 1500 字；长内容必须拆成多次调用。
        - 技术方案等长章节默认小步写入：先 replace_section_blocks 写根章节总述，再逐个 append_child_section 写子章节。
        - blocks/block 正文只能放在 text 字段，不要使用 content 字段。
        - append_child_section 只能使用 parent_section_id 和 section 字段，不得使用 section_id、child 或 child_section 表示父章节或子章节。
        - 新增或替换 section 的 section 对象不允许携带 id；工具会生成或保留 section_id。

    Examples:
        - 替换章节正文: {"operations":[{"op":"replace_section_blocks","section_id":"sec_000004","blocks":[{"type":"paragraph","text":"这里写入新的段落正文。"}]}]}
        - 追加段落: {"operations":[{"op":"append_block","section_id":"sec_000007","block":{"type":"paragraph","text":"这里写入追加段落。"}}]}
        - 追加子章节: {"operations":[{"op":"append_child_section","parent_section_id":"sec_000007","section":{"type":"custom","title":"关键模块","blocks":[{"type":"paragraph","text":"这里写入子章节正文。"}],"children":[]}}]}
    """
    if not can_use_tool(scope, "document_edit"):
        return tool_failed("permission_denied", "子 agent 不允许调用 document_edit。")
    disclosure = store.get_disclosure(project_id)
    result = apply_document_edit(disclosure, arguments)
    if result["status"] == "success":
        store.save_disclosure(project_id, disclosure)
    return result
