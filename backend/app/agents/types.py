from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SubagentDeclaration:
    id: str
    description: str
    input_expectation: str
    output_contract: str
    usage_guidance: str
    tool_permissions: tuple[str, ...]
