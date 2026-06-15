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


def test_context_compression_user_prompt_defines_xml_summary_protocol() -> None:
    prompt = context_compression_user_prompt()

    assert "请只执行上下文滚动压缩" in prompt
    assert "系统内部的上下文维护指令" in prompt
    assert "不得把“用户要求只做上下文压缩”" in prompt
    assert "最终交接版本" in prompt
    assert "已成功写入的内容按最终落盘结果记录" in prompt
    assert "不要把本条压缩指令本身" in prompt
    assert "<analysis>" in prompt
    assert "<summary>" in prompt
    assert "不要输出 JSON" in prompt
    assert "## 当前任务" in prompt
    assert "## 执行进度" in prompt
    assert "## 已完成事项" in prompt
    assert "## 关键事实与证据" in prompt
    assert "## 待办与下一步" in prompt
    assert "## 风险与约束" in prompt
    assert "工具调用 ID" in prompt
    assert "target_estimated_tokens" not in prompt
