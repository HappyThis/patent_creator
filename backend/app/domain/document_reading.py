from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from .disclosure import STANDARD_SECTION_TITLES, find_section, section_title_text
from .document_tool_results import ToolResult, tool_failed, tool_success

DEFAULT_PREVIEW_CHARS = 40

SECTION_WRITING_GUIDES: dict[str, str] = {
    "发明名称": """## 发明名称写作要领

- 用一句话概括技术对象和核心机制，让代理人一眼知道方案解决的是什么技术事项。
- 名称应具体、克制，不写营销词、效果词，也不要只写宽泛领域名。
- 可以使用“基于……的……方法/系统/装置”这类中性表达，但不要为了命名而补编未确认的技术特征。
- 如果核心机制尚不明确，应先补充询问或暂用保守名称。""",
    "技术领域": """## 技术领域写作要领

- 用 1-2 句话定位方案所属的技术领域，服务于代理人检索和理解。
- 只写领域归属和应用场景边界，不展开背景问题、技术方案、创新点或技术效果。
- 领域范围要贴近实际方案，不要写成产品介绍，也不要故意扩大到无事实支撑的上位领域。""",
    "背景技术": """## 背景技术写作要领

- 说明该领域中常见的系统、流程、技术环境或协作关系，让代理人知道问题出现在哪里。
- 优先交代客观上下文：参与对象、输入输出、数据/状态流转、已有组件、典型工作方式。
- 不写本方案的改进手段，不提前评价创新点，也不要使用“本发明”等正式专利口吻。
- 缺少背景事实时宁可保持简短，不要编造行业通用做法。""",
    "现有技术及其缺陷": """## 现有技术及其缺陷写作要领

- 按“现有做法是什么 -> 在什么条件下不够 -> 造成什么技术后果”说明，不要求固定段落标题。
- 缺陷要落到技术层面，例如状态不一致、调度冲突、信息缺失、资源无法释放、人工介入导致流程不可控。
- 不要只写业务痛点、体验问题或管理目标，也不要把拟采用的方案写成现有技术。
- 如果只能确认问题、不能确认现有做法，应明确保守描述，不要虚构竞品或行业方案。""",
    "要解决的技术问题": """## 要解决的技术问题写作要领

- 将前文缺陷收束为 1-3 个明确技术问题，避免罗列过多目标。
- 每个问题应能回答：什么对象、在什么条件下、出现什么技术障碍、为什么需要技术性处理。
- 技术问题应与后续技术方案能够对应，不写商业目标、用户体验口号或纯效果描述。
- 事实不足时先保留问题边界，不要把尚未确认的解决手段写进问题本身。""",
    "技术方案": """## 技术方案写作要领

- 这是交底书的核心，要让专利代理人员看完后能复述技术原理、运行方式和关键边界。
- 围绕本方案真正的技术主线组织内容，可以按流程、模块、状态机、数据链路、控制策略或异常处理展开，不要求固定写作顺序。
- 写作时检查是否交代清楚关键组成、输入输出、数据或状态变化、判断条件、执行动作、异常分支和边界约束。
- 避免只写“系统根据状态进行处理”“根据策略执行任务”这类泛化描述；应说明状态/策略从何而来、包含什么、如何影响后续动作。
- 两个以上独立机制、流程阶段、模块、规则组或异常分支，建议拆成子章节。
- 图表、公式、流程图、架构图或时序图只有在能降低理解成本时才加入；缺少事实依据时不要补编技术细节。""",
    "具体实施方式": """## 具体实施方式写作要领

- 这里用于支撑“技术方案”如何落地，不是重新泛泛讲一遍原理。
- 可从步骤、模块协作、数据结构、接口、配置、参数、伪流程、运行环境或异常处理切入，选择最能说明可实施性的方式。
- 写清关键条件、分支、失败处理、替代实现和可选参数，但不要堆砌与核心方案无关的工程细节。
- 可以拆子章节写不同实施路径或典型场景；无法确认的实现细节应留白或追问，不要为了完整而编造。""",
    "关键创新点及权利要求建议": """## 关键创新点及权利要求建议写作要领

- 保护点在精不在多，通常 1-3 条，最多不超过 3 条。
- 每条应聚焦一个值得保护的必要特征组合，说明核心要素、协作关系以及它带来的技术结果。
- 不要把实施细节、可选参数、同义表述或普通从属特征拆成很多条。
- 用工程语言说明建议保护什么，不要直接写正式权利要求书；保护点必须能从技术方案中找到支撑。
- 如果当前资料不足以判断保护点，应先追问或只写已确认的核心组合。""",
    "附录": """## 附录写作要领

- 放置有助于理解正文的图、表、公式、流程图、架构图、时序图等材料。
- figure block 只应放在附录；正文其他章节用 [图1](figure:fig_000001) 这类引用连接到图。
- 图表应服务于正文解释，优先表达结构关系、流程关系、状态变化或量化关系，不加入装饰性材料。
- 如果图表无法比文字更清楚，或缺少足够事实支撑，就不要强行生成。""",
}


def disclosure_outline(disclosure: dict[str, Any], *, limit: int, offset: int) -> ToolResult:
    items = build_outline_items(disclosure["sections"])
    return tool_success({**_page(items, limit=limit, offset=offset, key="items")})


def disclosure_search(
    disclosure: dict[str, Any],
    *,
    query: str,
    regex: bool,
    limit: int,
    offset: int,
) -> ToolResult:
    matcher_result = _build_matcher(query, regex=regex)
    if isinstance(matcher_result, dict):
        return matcher_result
    matches = search_blocks(disclosure["sections"], matcher_result)
    return tool_success({"query": query, "regex": regex, **_page(matches, limit=limit, offset=offset, key="matches")})


def disclosure_read_section(
    disclosure: dict[str, Any],
    *,
    section_id: str,
    limit: int,
    offset: int,
    block_ids: list[str] | None = None,
) -> ToolResult:
    path = find_section_path(disclosure["sections"], section_id)
    if path is None:
        return tool_failed("section_not_found", f"section_id 不存在：{section_id}")
    section = path[-1]
    section_locator = section_locator_for(disclosure["sections"], path)
    readable_blocks = [section["title"], *section.get("blocks", [])]
    block_entries = [read_block_entry(block, section_path=[item["id"] for item in path], index=index) for index, block in enumerate(readable_blocks)]

    if block_ids:
        block_id_set = set(block_ids)
        known_ids = {entry["locator"]["block_id"] for entry in block_entries}
        missing = [block_id for block_id in block_ids if block_id not in known_ids]
        if missing:
            return tool_failed(
                "block_not_in_section",
                f"block_ids 必须属于 section_id 的直接 blocks：{', '.join(missing)}",
            )
        selected = [entry for entry in block_entries if entry["locator"]["block_id"] in block_id_set]
        page_payload = {
            "blocks": selected,
            "returned": len(selected),
            "total": len(selected),
            "offset": None,
            "next_offset": None,
            "truncated": False,
        }
    else:
        page_payload = _page(block_entries, limit=limit, offset=offset, key="blocks")

    child_sections = []
    for index, child in enumerate(section.get("sections", [])):
        child_path = [*path, child]
        child_sections.append(
            {
                "locator": section_locator_for(disclosure["sections"], child_path, index_override=index),
                "title": {
                    "locator": block_locator(child["title"], [item["id"] for item in child_path], 0),
                    "preview": preview_text(block_text(child["title"])),
                },
            }
        )

    output = {
        "section": {
            "locator": section_locator,
            "title": read_block_entry(section["title"], section_path=[item["id"] for item in path], index=0),
            "blocks": page_payload.pop("blocks"),
            "sections": child_sections,
        },
        **page_payload,
    }
    writing_guide = section_writing_guide_for(path)
    if writing_guide:
        output["writing_guide_markdown"] = writing_guide
    return tool_success(output)


def section_writing_guide_for(section_path: list[dict[str, Any]]) -> str | None:
    if len(section_path) != 1:
        return None
    section = section_path[0]
    if section.get("blocks") or section.get("sections"):
        return None
    title = section_title_text(section)
    if title not in STANDARD_SECTION_TITLES:
        return None
    return SECTION_WRITING_GUIDES.get(title)


def build_outline_items(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def visit(section_list: list[dict[str, Any]], path: list[dict[str, Any]]) -> None:
        for index, section in enumerate(section_list):
            section_path = [*path, section]
            section_ids = [item["id"] for item in section_path]
            items.append(
                {
                    "kind": "section",
                    "locator": section_locator_from_parts(section["id"], section_ids, index),
                    "title": {
                        "locator": block_locator(section["title"], section_ids, 0),
                        "preview": preview_text(block_text(section["title"])),
                    },
                }
            )
            for block_index, block in enumerate(section.get("blocks", []), start=1):
                items.append(
                    {
                        "kind": "block",
                        "locator": block_locator(block, section_ids, block_index),
                        "preview": preview_text(block_text(block)),
                    }
                )
            visit(section.get("sections", []), section_path)

    visit(sections, [])
    return items


def search_blocks(sections: list[dict[str, Any]], matcher: Callable[[str], bool]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    def visit(section_list: list[dict[str, Any]], path: list[dict[str, Any]]) -> None:
        for section in section_list:
            section_path = [*path, section]
            section_ids = [item["id"] for item in section_path]
            all_blocks = [section["title"], *section.get("blocks", [])]
            for index, block in enumerate(all_blocks):
                text = block_text(block)
                if matcher(text):
                    matches.append(
                        {
                            "locator": block_locator(block, section_ids, index),
                            "preview": preview_text(text),
                        }
                    )
            visit(section.get("sections", []), section_path)

    visit(sections, [])
    return matches


def find_section_path(sections: list[dict[str, Any]], section_id: str) -> list[dict[str, Any]] | None:
    for section in sections:
        if section["id"] == section_id:
            return [section]
        child_path = find_section_path(section.get("sections", []), section_id)
        if child_path:
            return [section, *child_path]
    return None


def section_locator_for(
    root_sections: list[dict[str, Any]],
    section_path: list[dict[str, Any]],
    *,
    index_override: int | None = None,
) -> dict[str, Any]:
    section = section_path[-1]
    if index_override is not None:
        index = index_override
    elif len(section_path) == 1:
        index = next(index for index, item in enumerate(root_sections) if item["id"] == section["id"])
    else:
        parent = section_path[-2]
        index = next(index for index, item in enumerate(parent.get("sections", [])) if item["id"] == section["id"])
    return section_locator_from_parts(section["id"], [item["id"] for item in section_path], index)


def section_locator_from_parts(section_id: str, section_path: list[str], index: int) -> dict[str, Any]:
    return {
        "kind": "section",
        "section_id": section_id,
        "section_path": section_path,
        "index": index,
    }


def block_locator(block: dict[str, Any], section_path: list[str], index: int) -> dict[str, Any]:
    return {
        "kind": "block",
        "section_id": section_path[-1],
        "section_path": section_path,
        "block_id": block["id"],
        "block_type": block["type"],
        "index": index,
    }


def read_block_entry(block: dict[str, Any], *, section_path: list[str], index: int) -> dict[str, Any]:
    payload = copy.deepcopy(block)
    payload["locator"] = block_locator(block, section_path, index)
    return payload


def preview_text(text: str, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    if len(text) <= preview_chars * 2:
        return text
    omitted = len(text) - preview_chars * 2
    return f"{text[:preview_chars]}…（省略 {omitted} 字）…{text[-preview_chars:]}"


def block_text(block: dict[str, Any]) -> str:
    if block["type"] in {"title", "paragraph"}:
        return block["text"]
    if block["type"] == "list":
        return "\n".join(block["items"])
    if block["type"] == "image":
        return "\n".join(value for value in [block.get("alt"), block.get("caption"), block.get("src")] if value)
    if block["type"] == "formula":
        return block["latex"]
    if block["type"] == "figure":
        return block["figure_id"]
    return "\n".join([" ".join(block["columns"]), *[" ".join(row) for row in block["rows"]]])


def _build_matcher(query: str, *, regex: bool) -> Callable[[str], bool] | ToolResult:
    if not query:
        return tool_failed("invalid_operation", "query 字段缺失。")
    if regex:
        try:
            compiled = re.compile(query, flags=re.IGNORECASE)
        except re.error as exc:
            return tool_failed("invalid_operation", f"regex 无效：{exc}")
        return lambda text: compiled.search(text) is not None
    folded_query = query.casefold()
    return lambda text: folded_query in text.casefold()


def _page(items: list[dict[str, Any]], *, limit: int, offset: int, key: str) -> dict[str, Any]:
    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    return {
        key: page,
        "returned": len(page),
        "total": total,
        "offset": offset,
        "next_offset": next_offset,
        "truncated": next_offset is not None,
    }
