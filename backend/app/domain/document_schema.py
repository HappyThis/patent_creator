from __future__ import annotations

BLOCK_TYPES = {"paragraph", "list", "image", "table"}
CHANGE_SCOPE_BY_OP = {
    "update_meta": "meta_updated",
    "replace_section_blocks": "section_blocks_replaced",
    "append_block": "block_appended",
    "replace_block": "block_replaced",
    "append_child_section": "child_section_appended",
    "replace_section": "section_replaced",
}
