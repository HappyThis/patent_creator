from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.document_reading import disclosure_outline as read_disclosure_outline
from ...domain.document_reading import disclosure_read_section as read_disclosure_section
from ...domain.document_reading import disclosure_search as search_disclosure
from ...domain.document_tool_results import tool_failed
from ...domain.document_writing import edit_disclosure
from ...storage.workspace_store import WorkspaceStore
from ..argument_normalization import normalize_stringified_json_arguments
from ..metadata import agent_tool

logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DisclosureOutlineArguments(_StrictModel):
    limit: int = Field(default=100, ge=1, le=300, description="最多返回多少个 outline item，默认 100，最大 300。")
    offset: int = Field(default=0, ge=0, description="分页偏移，从 0 开始。")


class DisclosureSearchArguments(_StrictModel):
    query: str = Field(description="搜索关键词；regex=true 时作为正则表达式。搜索固定大小写不敏感。")
    regex: bool = Field(default=False, description="是否把 query 当作正则表达式，默认 false。")
    limit: int = Field(default=50, ge=1, le=200, description="最多返回多少个匹配 block，默认 50，最大 200。")
    offset: int = Field(default=0, ge=0, description="分页偏移，从 0 开始。")


class DisclosureReadSectionArguments(_StrictModel):
    section_id: str = Field(description="要精读的 section_id。")
    limit: int = Field(default=20, ge=1, le=100, description="最多返回多少个直接 block，默认 20，最大 100。")
    offset: int = Field(default=0, ge=0, description="block 分页偏移，从 0 开始；title block 固定为 index=0。")
    block_ids: list[str] | None = Field(default=None, description="只读取该 section 下指定的直接 block；提供后忽略 limit/offset。")


class BlockArguments(_StrictModel):
    type: Literal["title", "paragraph", "list", "image", "table", "formula", "figure"] = Field(
        description="block 类型。title/paragraph 使用 text；list 使用 ordered 和 items；image 使用 src，可选 caption/alt；table 使用 columns 和 rows；formula 使用 latex；figure 使用 figure_id 且只能放在附录章节。paragraph/list/table 文本支持 $...$ 行内 LaTeX，也支持 [式(1)](formula:blk_000001) 引用块级公式。"
    )
    text: str | None = Field(default=None, description="type=title 或 paragraph 时必填；paragraph 文本可用 $D_i$、$Active_i$ 这类 $...$ 行内 LaTeX。")
    ordered: bool | None = Field(default=None, description="type=list 时必填，是否为有序列表。")
    items: list[str] | None = Field(default=None, description="type=list 时必填，列表项文本；支持 $...$ 行内 LaTeX。")
    src: str | None = Field(default=None, description="type=image 时必填，图片资源路径或 URL。")
    caption: str | None = Field(default=None, description="type=image 时可选，图片标题。")
    alt: str | None = Field(default=None, description="type=image 时可选，替代文本。")
    columns: list[str] | None = Field(default=None, description="type=table 时必填，表头列名；支持 $...$ 行内 LaTeX。")
    rows: list[list[str]] | None = Field(default=None, description="type=table 时必填，表格行；单元格支持 $...$ 行内 LaTeX。")
    latex: str | None = Field(default=None, description="type=formula 时必填，块级公式的 LaTeX 源码；预览中会按文档顺序自动编号为式(1)、式(2)。")
    figure_id: str | None = Field(default=None, description="type=figure 时必填，附图资产 id，例如 fig_000001。")


class PositionArguments(_StrictModel):
    mode: Literal["start", "end", "index", "before", "after"] = Field(default="end", description="插入位置。")
    index: int | None = Field(default=None, description="mode=index 时使用；block index 按 title=0、正文从 1 开始。")
    block_id: str | None = Field(default=None, description="插入 block 时 mode=before/after 的锚点 block_id。")
    section_id: str | None = Field(default=None, description="插入 section 时 mode=before/after 的锚点子 section_id。")


class SectionInsertArguments(_StrictModel):
    title: str = Field(description="新子章节标题。正文后续通过 insert_block 小步写入。")


class DisclosureEditArguments(_StrictModel):
    section_id: str = Field(description="编辑工作区 section_id；只能操作该 section 的直接 block 或直接子 section。")
    operation: Literal["replace_block", "delete_block", "insert_block", "insert_section", "delete_section"] = Field(
        description="编辑操作。改标题使用 replace_block 替换 title block；不提供整章重写。"
    )
    block_id: str | None = Field(default=None, description="replace_block/delete_block 的目标 block_id。")
    target_section_id: str | None = Field(default=None, description="delete_section 的目标直接子 section_id。")
    position: PositionArguments | None = Field(default=None, description="insert_block/insert_section 的插入位置。")
    block: BlockArguments | None = Field(default=None, description="replace_block/insert_block 的 block 内容。")
    section: SectionInsertArguments | None = Field(default=None, description="insert_section 的子章节内容，仅包含 title。")


@agent_tool(args_model=DisclosureOutlineArguments)
def disclosure_outline(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """读取交底书目录索引，按深度优先列出 section 和直接 block，block 只返回 preview。

    Returns:
        返回 items、returned、total、offset、next_offset、truncated；每个 item 带 locator。

    Rules:
        - 当需要了解交底书结构、寻找可编辑位置或判断章节层级时，先用本工具定位。
        - 本工具用于定位，不返回正文全文；不要基于 preview 直接改写关键正文。
        - title 是 block，作为 section.title 返回；普通正文 block 从 index=1 开始。
        - 结果分页返回；truncated 为 true 时用 next_offset 继续读取。

    Examples:
        - 查看目录索引: {"limit":100,"offset":0}
    """
    parsed = _validate_tool_arguments(DisclosureOutlineArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return read_disclosure_outline(store.get_disclosure(project_id), limit=payload["limit"], offset=payload["offset"])


@agent_tool(args_model=DisclosureSearchArguments)
def disclosure_search(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """全文搜索交底书 block，支持普通关键词或正则，固定大小写不敏感。

    Returns:
        返回 matches、returned、total、offset、next_offset、truncated；命中单位固定为 block。

    Rules:
        - 当不知道概念、术语或目标文本在哪个章节时，先用本工具定位。
        - 只用于定位命中的 block，不返回全文；不要基于搜索摘要直接改写关键正文。
        - 不支持 section 范围过滤和 block 类型过滤；找到结果后用 disclosure_read_section 精读命中的 section 或 block。
        - 结果分页返回；truncated 为 true 时用 next_offset 继续搜索。

    Examples:
        - 搜索关键词: {"query":"任务状态","regex":false,"limit":50,"offset":0}
    """
    parsed = _validate_tool_arguments(DisclosureSearchArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return search_disclosure(
        store.get_disclosure(project_id),
        query=payload["query"],
        regex=payload["regex"],
        limit=payload["limit"],
        offset=payload["offset"],
    )


@agent_tool(args_model=DisclosureReadSectionArguments)
def disclosure_read_section(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """精读一个交底书 section 的直接内容，可按 block 分页或读取指定直接 block。

    Returns:
        返回 section、returned、total、offset、next_offset、truncated；section.sections 只包含直接子 section 摘要。读取一级固定章节且该章节为空时，额外返回纯 Markdown 字符串 writing_guide_markdown。

    Rules:
        - 当写作、评价或修改依赖当前正文时，应先精读相关 section 或目标 block。
        - writing_guide_markdown 是空章节写作要领，不是交底书正文；写入前应结合用户信息生成具体内容。
        - block_ids 必须属于该 section 的直接 block；读子 section 内容必须改用子 section_id 再调用。
        - 分页对象是 title block + 当前 section 的直接 blocks，不展开子 section 正文。
        - title block 固定 index=0；正文 block 从 index=1 开始。

    Examples:
        - 精读章节: {"section_id":"sec_000007","limit":20,"offset":0}
        - 精读指定 block: {"section_id":"sec_000007","block_ids":["blk_000012"]}
    """
    parsed = _validate_tool_arguments(DisclosureReadSectionArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return read_disclosure_section(
        store.get_disclosure(project_id),
        section_id=payload["section_id"],
        limit=payload["limit"],
        offset=payload["offset"],
        block_ids=payload.get("block_ids"),
    )


@agent_tool(args_model=DisclosureEditArguments)
def disclosure_edit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """小步编辑交底书；只能操作指定 section 的直接 block 或直接子 section。

    Returns:
        返回 changed_section_ids、changed_block_ids、primary_section_id、primary_block_id、change_scope；失败时返回 code、message 和可选 retry_hint。

    Rules:
        - 只能写最终态正文，不要写对话过程、修改过程、工具操作、方案迭代说明或内部判断。
        - 没有整章重写；重写章节必须拆成删除、插入 section、逐个 insert/replace block。
        - section 负责结构，block 承接内容；编辑子 section 前，必须改用该子 section 的 section_id 作为工作区，不要跨 section 操作。
        - 当一个章节包含两个以上独立机制、流程阶段、模块、实施例、规则组或异常分支时，应优先 insert_section 拆成子章节，不要把多个主题压进同一个长 paragraph。
        - 改章节标题使用 replace_block 替换该 section 的 title block。
        - title block 只能 replace，不能 delete，也不能在其前方 insert block。
        - insert_section 只创建子章节标题；正文后续通过 insert_block 小步写入。
        - 单次新增/替换文本总量不得超过 1500 字。
        - 段落、列表项和表格文本支持 $...$ 行内 LaTeX；涉及下标、变量、集合、逻辑条件时写作 $D_i$、$Active_i$，不要裸写 D_i、Active_i。
        - 独立公式使用 type=formula 的块级 LaTeX；不要把完整公式塞进普通段落。
        - 块级公式在预览中自动编号；正文引用公式时使用 [式(1)](formula:<formula_block_id>)，block_id 以实际公式 block_id 为准。

    Examples:
        - 替换段落: {"section_id":"sec_000007","operation":"replace_block","block_id":"blk_000012","block":{"type":"paragraph","text":"替换后的段落。"}}
        - 插入段落: {"section_id":"sec_000007","operation":"insert_block","position":{"mode":"end"},"block":{"type":"paragraph","text":"新增段落。"}}
        - 新增子章节: {"section_id":"sec_000007","operation":"insert_section","position":{"mode":"end"},"section":{"title":"任务状态管理机制"}}
    """
    parsed = _validate_tool_arguments(DisclosureEditArguments, arguments)
    if parsed["status"] == "failed":
        return parsed
    payload = parsed["output"]["arguments"]
    return _apply_disclosure_edit(store, project_id, lambda disclosure: edit_disclosure(disclosure, payload))


def _validate_tool_arguments(args_model: type[BaseModel], arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_stringified_json_arguments(args_model, arguments)
    if normalized.normalized_paths:
        logger.info(
            "normalized stringified JSON tool arguments model=%s paths=%s",
            args_model.__name__,
            ",".join(normalized.normalized_paths),
        )
    try:
        parsed = args_model.model_validate(normalized.arguments)
    except ValidationError as exc:
        return tool_failed(
            "invalid_tool_arguments",
            _validation_message(exc),
            retry_hint="请严格按照当前工具的 parameters schema 重新调用。",
        )
    return {"status": "success", "output": {"arguments": parsed.model_dump(exclude_none=True)}}


def _apply_disclosure_edit(
    store: WorkspaceStore,
    project_id: str,
    writer: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    disclosure = store.get_disclosure(project_id)
    result = writer(disclosure)
    if result["status"] == "failed":
        result["output"].setdefault(
            "retry_hint",
            "请先用 disclosure_outline / disclosure_search / disclosure_read_section 定位，再用 disclosure_edit 小步修改。",
        )
    if result["status"] == "success":
        store.save_disclosure(project_id, disclosure)
    return result


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
    message = str(first.get("msg") or "参数不符合工具 schema。")
    return f"工具参数不符合 schema：{location}: {message}"
