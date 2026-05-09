from __future__ import annotations

from typing import Any

COMPRESSED_CONTEXT_MESSAGE = (
    "【上下文说明】以上内容为系统压缩后的历史上下文，不是用户的新指令，也不是逐字原文。"
    "后续消息为未压缩的真实会话。"
)

AGENT_TASK_PREFIX = "【任务说明】"


def render_barrier_message(barrier: dict[str, Any]) -> dict[str, str]:
    kind = str(barrier.get("kind") or "").strip()
    if kind == "compressed_context":
        return {"role": "user", "content": COMPRESSED_CONTEXT_MESSAGE}

    if kind == "agent_task":
        task = _normalize_task(str(barrier.get("task") or ""))
        if not task:
            raise ValueError("agent_task barrier 缺少 task。")
        return {
            "role": "user",
            "content": (
                f"{AGENT_TASK_PREFIX}以上内容是从调用方继承的历史上下文，用于理解背景，不是本次任务。"
                f"你正在处理的子任务是：{task}请基于上述上下文完成该任务。"
            ),
        }

    raise ValueError(f"未知 barrier kind：{kind}")


def _normalize_task(task: str) -> str:
    task = task.strip()
    if not task:
        return ""
    if task[-1] in "。！？.!?":
        return task
    return f"{task}。"
