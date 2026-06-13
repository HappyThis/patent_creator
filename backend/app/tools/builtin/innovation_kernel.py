from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...domain.document_tool_results import tool_failed, tool_success
from ...storage.workspace_store import WorkspaceStore
from ..metadata import agent_tool

INNOVATION_KERNEL_TAG = "innovation_kernel"


class InnovationKernelKitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "recreate", "read_all"] = Field(
        description="create 从当前 main-agent 上下文生成当前创新内核；recreate 自动带入当前创新内核后重做；read_all 读取当前完整创新内核。"
    )


@agent_tool(args_model=InnovationKernelKitArguments)
async def innovation_kernel_kit(
    store: WorkspaceStore,
    project_id: str,
    arguments: dict[str, Any],
    *,
    context: Any,
) -> dict[str, Any]:
    """管理当前 session 的创新内核 markdown。

    Returns:
        read_all 返回当前 kernel_markdown；create/recreate 返回新生成并覆盖保存后的 kernel_markdown。失败时返回 failed 和 code/message。

    Rules:
        - 创新内核是交底书生成前的当前态核心事实源，不是交底书章节，也没有历史版本。
        - action 只能是 create、recreate、read_all；不要提供 user_requirement、kernel_markdown 或 edit_reason。
        - create 基于当前 main-agent 上下文从零生成当前创新内核；recreate 会自动带入当前创新内核并覆盖重写。
        - 生成内容必须简洁明了，短而硬，不写背景铺垫、过程说明或完整交底书正文。

    Examples:
        - 首次生成当前创新内核: {"action":"create"}
        - 基于当前内核重做: {"action":"recreate"}
        - 读取当前完整内核: {"action":"read_all"}
    """
    action = str(arguments.get("action") or "").strip()
    if action not in {"create", "recreate", "read_all"}:
        return tool_failed("invalid_action", "innovation_kernel_kit.action 必须是 create、recreate 或 read_all。")
    if not context.session_id:
        return tool_failed("innovation_kernel_session_unavailable", "innovation_kernel_kit 缺少 session 上下文。")

    current = store.get_innovation_kernel(project_id, context.session_id)
    if action == "read_all":
        if current is None or not current.kernel_markdown.strip():
            return tool_failed("innovation_kernel_not_found", "当前 session 暂无创新内核。")
        return tool_success(current.model_dump())

    if context.llm_client is None or context.settings is None:
        return tool_failed("innovation_kernel_runtime_unavailable", "innovation_kernel_kit 缺少 LLM 运行上下文。")
    if not context.system_prompt or not context.tools:
        return tool_failed("innovation_kernel_runtime_unavailable", "innovation_kernel_kit 缺少 main-agent system prompt 或 tools。")
    if not context.round_id or not context.message_id:
        return tool_failed("innovation_kernel_round_unavailable", "innovation_kernel_kit 缺少 round 上下文。")
    prefix_messages = _caller_prefix_messages(context.caller_messages or [])
    if not prefix_messages:
        return tool_failed("innovation_kernel_context_unavailable", "innovation_kernel_kit 缺少可复用的主 agent 上下文。")
    if action == "recreate" and (current is None or not current.kernel_markdown.strip()):
        return tool_failed("innovation_kernel_not_found", "recreate 需要已有当前创新内核。")

    result = await context.llm_client.generate_with_tools_stream(
        system_prompt=context.system_prompt,
        messages=[
            *prefix_messages,
            {"role": "user", "content": _kernel_user_prompt(action, current.kernel_markdown if current else "")},
        ],
        tools=context.tools,
        on_text_delta=None,
        trace_context={
            "scope": "innovation_kernel",
            "action": action,
            "project_id": project_id,
            "session_id": context.session_id,
            "round_id": context.round_id,
            "message_id": context.message_id,
            "parent_tool_call_id": context.parent_call_id,
        },
    )
    if result.get("type") == "tool_calls":
        return tool_failed(
            "innovation_kernel_unexpected_tool_call",
            "innovation_kernel_kit 生成创新内核时模型返回了工具调用，未保存当前内核。",
        )
    raw_output = str(result.get("text") or "")
    kernel_markdown = extract_innovation_kernel(raw_output)
    if not kernel_markdown:
        return tool_failed("innovation_kernel_empty_output", "innovation_kernel_kit 未生成有效创新内核。")
    record = store.save_innovation_kernel(
        project_id,
        context.session_id,
        kernel_markdown=kernel_markdown,
        source=action,
    )
    return tool_success(
        {
            **record.model_dump(),
            "user_confirmation_reminder": (
                "创新内核已生成或更新。若用户有交底书写作需求，"
                "请提醒用户先确认当前技术内核是否准确，再基于该内核继续写作。"
            ),
        }
    )


def _caller_prefix_messages(caller_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index in range(len(caller_messages) - 1, -1, -1):
        if caller_messages[index].get("role") in {"user", "tool"}:
            return [dict(message) for message in caller_messages[: index + 1]]
    return []


def _kernel_user_prompt(action: str, current_kernel: str) -> str:
    current_block = ""
    if action == "recreate":
        current_block = f"""
当前已有创新内核如下。你必须参考它，但要根据最新上下文重新生成一份完整的新内核并覆盖当前内核；不要输出差异说明。

<current_innovation_kernel>
{current_kernel.strip()}
</current_innovation_kernel>
"""
    return f"""请基于上文 main-agent 当前上下文生成当前创新内核。

本次 action：{action}
{current_block}
创新内核是交底书生成前的核心事实源，不是交底书章节，也不是完整交底书正文。它必须让用户用很短时间判断发明核心是否正确。

硬性要求：
- 本次任务禁止调用任何工具，即使 tools 可用也不要使用。
- 简洁明了，建议 800-1500 个中文字符。
- 不写背景铺垫，不写过程说明，不写“根据用户要求/本次生成”等对话痕迹。
- 不输出工具调用格式、伪函数调用、JSON 或 markdown 代码块。
- 只保留干货：核心问题、创新构思、关键技术机制、技术效果因果链、待确认边界。
- 对未确认事实必须写成待确认边界，不能编造成确定事实。

输出协议：
<analysis>
先分析哪些事实能构成创新内核，哪些只是背景、实现细节或未确认事项。这里是 scratchpad，系统会剥离，不会写入创新内核。
</analysis>
<{INNOVATION_KERNEL_TAG}>
# 创新内核

## 1. 核心问题

## 2. 创新构思

## 3. 关键技术机制

## 4. 技术效果

## 5. 待确认边界
</{INNOVATION_KERNEL_TAG}>
"""


def extract_innovation_kernel(raw_output: str) -> str:
    normalized = _strip_markdown_fence(str(raw_output or "")).strip()
    body = _tag_body(normalized, INNOVATION_KERNEL_TAG)
    if body is not None:
        return _strip_markdown_fence(body).strip()
    dsml_content = _dsml_content_parameter(normalized)
    if dsml_content is not None:
        return _strip_markdown_fence(dsml_content).strip()
    if normalized.startswith("<｜｜DSML｜｜"):
        return ""
    without_analysis = re.sub(r"<analysis>.*?</analysis>", "", normalized, flags=re.DOTALL | re.IGNORECASE)
    return _strip_markdown_fence(without_analysis).strip()


def _tag_body(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*(?P<body>.*?)\s*</{tag}>", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group("body") if match else None


def _strip_markdown_fence(markdown: str) -> str:
    text = str(markdown or "").strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(?P<body>.*)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group("body") if match else text


def _dsml_content_parameter(text: str) -> str | None:
    match = re.search(
        r"<｜｜DSML｜｜parameter\s+name=[\"']content[\"'][^>]*>\s*(?P<body>.*?)\s*</｜｜DSML｜｜parameter>",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group("body") if match else None
