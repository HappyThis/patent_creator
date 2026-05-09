from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProposalType = Literal["analysis_result", "document_edit_proposal", "review_report"]


@dataclass(slots=True, frozen=True)
class SubagentDeclaration:
    id: str
    description: str
    input_expectation: str
    output_contract: str
    tool_permissions: tuple[str, ...]
    default_proposal_type: ProposalType
