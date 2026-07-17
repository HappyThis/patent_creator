from __future__ import annotations

import pytest

from app.runtime.context.barrier import render_barrier_message
from app.runtime.context.prompts import context_compression_user_prompt


def test_barrier_renderer_only_outputs_compressed_context_messages() -> None:
    compressed = render_barrier_message({"kind": "compressed_context"})

    assert compressed["role"] == "user"
    assert "系统压缩后的累计工作状态" in compressed["content"]
    with pytest.raises(ValueError):
        render_barrier_message({"kind": "agent_task", "task": "检查提示词冲突"})


def test_context_compression_user_prompt_defines_compact_markdown_handoff() -> None:
    prompt = context_compression_user_prompt()

    assert "只执行压缩，不继续用户任务，不调用工具" in prompt
    assert "内部维护指令，不是用户请求" in prompt
    assert "不得把压缩动作、本条指令" in prompt
    assert "必须忠实保留旧摘要中仍有效的信息" in prompt
    assert "不要输出 JSON" in prompt
    assert "## 当前任务与用户意图" in prompt
    assert "## 执行进度" in prompt
    assert "## 关键事实与证据" in prompt
    assert "## 待办与下一步" in prompt
    assert "## 风险与约束" in prompt
    assert "tool_calls" in prompt
    assert "<analysis>" not in prompt
    assert "<summary>" not in prompt
    assert "target_estimated_tokens" not in prompt
