from __future__ import annotations

COMPRESSED_CONTEXT_MESSAGE = (
    "【上下文恢复说明】以上内容是系统压缩后的累计工作状态，不是用户的新请求，也不是逐字原文。"
    "请把它当作当前会话截至目前的唯一历史依据，从“执行进度”和“待办与下一步”继续执行。"
    "如需源码、文档、章节、块 ID 或工具结果的精确原文，请主动调用可用工具重新读取。"
    "不要因为上下文被压缩而重新问候、重置任务或开始无关对话。"
)


def render_barrier_message(barrier: dict[str, object]) -> dict[str, str]:
    kind = str(barrier.get("kind") or "").strip()
    if kind == "compressed_context":
        return {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}
    raise ValueError(f"未知 barrier kind：{kind}")
