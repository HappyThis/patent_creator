from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CallType = Literal["forked_context", "rich_context_specialist", "task_only_specialist"]
ProposalType = Literal["analysis_result", "document_edit_proposal", "review_report"]


@dataclass(slots=True, frozen=True)
class SubagentDeclaration:
    id: str
    description: str
    default_type: CallType
    allowed_types: tuple[CallType, ...]
    input_expectation: str
    output_contract: str
    tool_permissions: tuple[str, ...]
    default_proposal_type: ProposalType
