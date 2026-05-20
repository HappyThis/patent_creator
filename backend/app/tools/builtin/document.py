from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.document_reading import read_document
from ...domain.document_tool_results import tool_failed
from ...domain.document_writing import (
    append_block,
    append_child_section,
    clear_section_blocks,
    replace_block,
    replace_section_blocks,
)
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool


class DocumentReadArguments(BaseModel):
    action: Literal["get_meta", "get_project_context", "get_outline", "get_section", "get_block", "search_blocks"] = Field(
        description="读取动作。get_project_context 返回标题和完整目录树；get_section 按章节 id；get_block 按 block id；search_blocks 按关键词搜索正文。"
    )
    section_id: str | None = Field(default=None, description="系统生成的章节 id，例如 sec_000007。action=get_section 时必填；action=search_blocks 时可选，用于限制搜索范围。标准章节语义看 outline 中的 type。")
    block_id: str | None = Field(default=None, description="block id。action=get_block 时必填。")
    query: str | None = Field(default=None, description="搜索关键词。action=search_blocks 时必填。")
    include_children: bool | None = Field(default=None, description="action=get_section 时是否同时返回子章节；未提供时默认为 false。")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockArguments(_StrictModel):
    type: Literal["paragraph", "list", "image", "table"] = Field(
        description="block 类型。paragraph 使用 text；list 使用 ordered 和 items；image 使用 src，可选 caption/alt；table 使用 columns 和 rows。"
    )
    text: str | None = Field(default=None, description="type=paragraph 时必填，段落正文。")
    ordered: bool | None = Field(default=None, description="type=list 时必填，是否为有序列表。")
    items: list[str] | None = Field(default=None, description="type=list 时必填，列表项文本。")
    src: str | None = Field(default=None, description="type=image 时必填，图片资源路径或 URL。")
    caption: str | None = Field(default=None, description="type=image 时可选，图片标题。")
    alt: str | None = Field(default=None, description="type=image 时可选，替代文本。")
    columns: list[str] | None = Field(default=None, description="type=table 时必填，表头列名。")
    rows: list[list[str]] | None = Field(default=None, description="type=table 时必填，表格行。")


class ReplaceSectionBlocksArguments(_StrictModel):
    section_id: str = Field(description="要替换正文 blocks 的 section_id，例如 sec_000007。")
    blocks: list[BlockArguments] = Field(
        description="新的 blocks 列表。单次正文写入总量不得超过 1500 字；长内容必须拆成多次小步写入。"
    )


class AppendBlockArguments(_StrictModel):
    section_id: str = Field(description="要追加 block 的 section_id，例如 sec_000007。")
    block: BlockArguments = Field(description="要追加的单个 block。")


class ReplaceBlockArguments(_StrictModel):
    block_id: str = Field(description="要替换的 block_id，例如 blk_000001。")
    block: BlockArguments = Field(description="替换后的 block。工具会保留原 block_id。")


class AppendChildSectionArguments(_StrictModel):
    parent_section_id: str = Field(description="父章节 section_id，例如技术方案章节 sec_000007。")
    title: str = Field(description="新增子章节标题。")
    blocks: list[BlockArguments] = Field(
        description="新增子章节正文 blocks。子章节 type 由工具固定为 custom，section_id 由系统生成，不能自行提供 id。"
    )


class ClearSectionBlocksArguments(_StrictModel):
    section_id: str = Field(description="要清空正文 blocks 的 section_id。只清空 blocks，不删除章节节点或子章节。")


@agent_tool(
    args_model=DocumentReadArguments,
)
def document_read(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
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
    return read_document(store.get_disclosure(project_id), arguments)


@agent_tool(
    args_model=ReplaceSectionBlocksArguments,
    name="document_replace_section_blocks",
)
def document_replace_section_blocks(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """替换指定章节的正文 blocks，不改变章节标题和子章节。

    Returns:
        返回编辑后的摘要；失败时返回 failed，并包含 code、message 和可选 retry_hint。正文写入超过 1500 字时返回 edit_too_large。

    Rules:
        - 适合写入根章节总述或重写某章节的短 blocks。
        - 单次正文写入总量不得超过 1500 字；长内容必须拆成多次调用。
        - 段落正文只能放在 paragraph block 的 text 字段，不要使用 content 字段。

    Examples:
        - 替换章节正文: {"section_id":"sec_000007","blocks":[{"type":"paragraph","text":"这里写入新的段落正文。"}]}
    """
    parsed = _validate_tool_arguments(ReplaceSectionBlocksArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return _apply_document_write(
        store,
        project_id,
        lambda disclosure: replace_section_blocks(disclosure, payload["section_id"], payload["blocks"]),
    )


@agent_tool(
    args_model=AppendBlockArguments,
    name="document_append_block",
)
def document_append_block(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """向指定章节末尾追加一个 block。

    Returns:
        返回编辑后的摘要；失败时返回 failed，并包含 code、message 和可选 retry_hint。正文写入超过 1500 字时返回 edit_too_large。

    Rules:
        - 一次只追加一个 block；需要多段正文时多次调用。
        - 段落正文只能放在 paragraph block 的 text 字段，不要使用 content 字段。

    Examples:
        - 追加段落: {"section_id":"sec_000007","block":{"type":"paragraph","text":"这里写入追加段落。"}}
    """
    parsed = _validate_tool_arguments(AppendBlockArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return _apply_document_write(
        store,
        project_id,
        lambda disclosure: append_block(disclosure, payload["section_id"], payload["block"]),
    )


@agent_tool(
    args_model=ReplaceBlockArguments,
    name="document_replace_block",
)
def document_replace_block(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """替换指定 block 的内容，并保留原 block_id。

    Returns:
        返回编辑后的摘要；失败时返回 failed，并包含 code、message 和可选 retry_hint。正文写入超过 1500 字时返回 edit_too_large。

    Rules:
        - 适合小范围改写已有段落、列表、图片或表格。
        - 不要在 block 参数中提供 id；工具会保留原 block_id。

    Examples:
        - 替换段落: {"block_id":"blk_000001","block":{"type":"paragraph","text":"替换后的段落正文。"}}
    """
    parsed = _validate_tool_arguments(ReplaceBlockArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return _apply_document_write(
        store,
        project_id,
        lambda disclosure: replace_block(disclosure, payload["block_id"], payload["block"]),
    )


@agent_tool(
    args_model=AppendChildSectionArguments,
    name="document_append_child_section",
)
def document_append_child_section(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """在指定父章节下追加一个 custom 子章节。

    Returns:
        返回编辑后的摘要；失败时返回 failed，并包含 code、message 和可选 retry_hint。正文写入超过 1500 字时返回 edit_too_large。

    Rules:
        - 只需要提供 parent_section_id、title 和 blocks；不要提供 section、id、type 或 children。
        - 子章节 type 固定为 custom，section_id 由系统生成。
        - 适合技术方案中的“整体架构”“处理流程”“关键模块”等短子章节。

    Examples:
        - 追加子章节: {"parent_section_id":"sec_000007","title":"关键模块","blocks":[{"type":"paragraph","text":"这里写入子章节正文。"}]}
    """
    parsed = _validate_tool_arguments(AppendChildSectionArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return _apply_document_write(
        store,
        project_id,
        lambda disclosure: append_child_section(
            disclosure,
            payload["parent_section_id"],
            payload["title"],
            payload["blocks"],
        ),
    )


@agent_tool(
    args_model=ClearSectionBlocksArguments,
    name="document_clear_section_blocks",
)
def document_clear_section_blocks(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """清空指定章节的正文 blocks，不删除章节节点和子章节。

    Returns:
        返回编辑后的摘要；失败时返回 failed，并包含 code、message 和可选 retry_hint。

    Rules:
        - 仅用于清空正文 blocks；不会删除 section，也不会删除 children。
        - 如果要替换为空以外的新正文，优先使用 document_replace_section_blocks。

    Examples:
        - 清空章节正文: {"section_id":"sec_000007"}
    """
    parsed = _validate_tool_arguments(ClearSectionBlocksArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return _apply_document_write(
        store,
        project_id,
        lambda disclosure: clear_section_blocks(disclosure, payload["section_id"]),
    )


def _validate_tool_arguments(args_model: type[BaseModel], arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = args_model.model_validate(arguments)
    except ValidationError as exc:
        return tool_failed(
            "invalid_tool_arguments",
            _validation_message(exc),
            retry_hint="请严格按照当前工具的 parameters schema 重新调用。",
        )
    return {"status": "success", "output": {"arguments": parsed.model_dump(exclude_none=True)}}


def _apply_document_write(
    store: WorkspaceStore,
    project_id: str,
    writer: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    disclosure = store.get_disclosure(project_id)
    result = writer(disclosure)
    if result["status"] == "failed":
        result["output"].setdefault(
            "retry_hint",
            "请改用当前文档编辑工具的小步参数重新调用；长内容拆成多次短正文写入。",
        )
    if result["status"] == "success":
        store.save_disclosure(project_id, disclosure)
    return result


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
    message = str(first.get("msg") or "参数不符合工具 schema。")
    return f"工具参数不符合 schema：{location}: {message}"
