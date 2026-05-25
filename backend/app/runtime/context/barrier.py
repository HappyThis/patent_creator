from __future__ import annotations

COMPRESSED_CONTEXT_MESSAGE = (
    "【上下文说明】以上内容为系统压缩后的历史上下文，不是用户的新指令，也不是逐字原文。"
    "后续消息为未压缩的真实会话。"
)


def render_barrier_message(barrier: dict[str, object]) -> dict[str, str]:
    kind = str(barrier.get("kind") or "").strip()
    if kind == "compressed_context":
        return {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}
    raise ValueError(f"未知 barrier kind：{kind}")
