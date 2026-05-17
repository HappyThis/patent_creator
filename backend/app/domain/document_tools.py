from __future__ import annotations

from .document_editing import (
    apply_document_edit,
    apply_operation,
    edit_output,
    normalize_operations,
    prepare_block,
    prepare_blocks,
    prepare_section,
)
from .document_reading import block_text, project_context_outline, read_document, search_blocks
from .document_schema import BLOCK_TYPES, CHANGE_SCOPE_BY_OP
from .document_tool_results import ToolResult, tool_failed, tool_success
from .document_tree import (
    collect_block_ids,
    collect_section_ids,
    dedupe,
    get_required_section,
    replace_section_in_tree,
    section_depth,
)
from .document_validation import validate_block, validate_disclosure

__all__ = [
    "BLOCK_TYPES",
    "CHANGE_SCOPE_BY_OP",
    "ToolResult",
    "apply_document_edit",
    "apply_operation",
    "block_text",
    "collect_block_ids",
    "collect_section_ids",
    "dedupe",
    "edit_output",
    "get_required_section",
    "normalize_operations",
    "prepare_block",
    "prepare_blocks",
    "prepare_section",
    "project_context_outline",
    "read_document",
    "replace_section_in_tree",
    "search_blocks",
    "section_depth",
    "tool_failed",
    "tool_success",
    "validate_block",
    "validate_disclosure",
]
