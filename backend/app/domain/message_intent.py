from __future__ import annotations

from dataclasses import dataclass

from .disclosure import STANDARD_SECTIONS, find_section, find_section_by_type


@dataclass(slots=True)
class MessageIntent:
    target_section_id: str
    matched_by: str
    matched_terms: list[str]


SECTION_KEYWORDS: dict[str, list[str]] = {
    "title": ["发明名称", "名称", "标题"],
    "technical_field": ["技术领域", "所属领域", "应用领域"],
    "background_technology": ["背景技术", "背景", "现有背景"],
    "existing_solution": ["现有技术方案", "现有方案", "已有方案"],
    "existing_solution_defects": ["现有技术缺陷", "现有方案缺陷", "缺陷", "不足", "痛点"],
    "technical_problem": ["技术问题", "要解决的技术问题", "待解决问题", "问题"],
    "technical_solution": ["技术方案", "方案", "架构", "流程", "处理流程", "模块", "步骤"],
    "key_innovations": ["关键创新点", "创新点", "创新", "发明点"],
    "embodiments": ["具体实施方式", "实施方式", "实施例", "实施方案"],
    "technical_effects": ["技术效果", "效果", "收益", "优点", "提升", "实时性收益"],
    "drawings": ["附图说明", "附图", "图示", "图1", "流程图"],
    "claim_suggestions": ["权利要求建议", "权利要求", "claim", "权项"],
}


def derive_message_intent(
    sections: list[dict],
    message: str,
    active_section_id: str | None,
) -> MessageIntent:
    normalized_message = message.strip()
    if active_section_id and find_section(sections, active_section_id):
        return MessageIntent(
            target_section_id=active_section_id,
            matched_by="active_section_id",
            matched_terms=[active_section_id],
        )

    matched_terms: list[str] = []
    for section in STANDARD_SECTIONS:
        section_type = section["type"]
        title = section["title"]
        keywords = [section_type, title, *SECTION_KEYWORDS.get(section_type, [])]
        local_matches = [term for term in keywords if term and term in normalized_message]
        if local_matches:
            matched_terms.extend(local_matches)
            target_section = find_section_by_type(sections, section_type)
            return MessageIntent(
                target_section_id=target_section["id"] if target_section else "",
                matched_by="message_keywords",
                matched_terms=matched_terms,
            )

    technical_solution = find_section_by_type(sections, "technical_solution")
    return MessageIntent(
        target_section_id=technical_solution["id"] if technical_solution else "",
        matched_by="default",
        matched_terms=[],
    )
