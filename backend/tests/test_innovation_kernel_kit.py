from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.runtime import ContextManager, ExecutorEngine
from app.runtime.context.builder import INNOVATION_KERNEL_CONTEXT_PREFIX
from app.runtime.executor import ToolRuntimeContext
from app.storage.workspace_store import WorkspaceStore
from app.tools.builtin.innovation_kernel import extract_innovation_kernel


class KernelLLMClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[dict[str, Any]] = []

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> str:
        self.prompts.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "messages": list(messages or []),
                "trace_context": trace_context or {},
            }
        )
        if not self.outputs:
            raise AssertionError("KernelLLMClient outputs exhausted")
        return self.outputs.pop(0)

    async def generate_with_tools_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Any = None,
        response_format_json: bool = False,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.prompts.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "tools": list(tools),
                "trace_context": trace_context or {},
            }
        )
        if not self.outputs:
            raise AssertionError("KernelLLMClient outputs exhausted")
        output = self.outputs.pop(0)
        if output == "__tool_call__":
            return {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "tool": "innovation_kernel_kit",
                        "arguments": {"action": "recreate"},
                        "tool_call_id": "call_nested",
                    }
                ],
                "assistant_message": {"role": "assistant", "content": "", "tool_calls": []},
            }
        return {
            "type": "respond",
            "text": output,
            "assistant_message": {"role": "assistant", "content": output},
        }


def make_runtime(tmp_path: Path) -> tuple[WorkspaceStore, ExecutorEngine, ContextManager, str, str, Settings]:
    settings = Settings(data_dir=tmp_path / "data", git_user_name="Test User", git_user_email="test@example.com")
    store = WorkspaceStore(settings.data_dir, settings.git_user_name, settings.git_user_email)
    project = store.create_project("创新内核测试")
    session_id = "sess_kernel"
    store.append_session_event(
        project.project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        payload={"text": "基于材料生成交底书。"},
    )
    return store, ExecutorEngine(store), ContextManager(store, settings), project.project_id, session_id, settings


def run_kernel_tool(
    executor: ExecutorEngine,
    project_id: str,
    session_id: str,
    llm: KernelLLMClient,
    settings: Settings,
    action: str,
    caller_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        executor.execute_tool(
            project_id,
            "innovation_kernel_kit",
            {"action": action},
            runtime_context=ToolRuntimeContext(
                session_id=session_id,
                round_id="round_1",
                message_id="msg_1",
                parent_call_id="call_kernel",
                caller_messages=caller_messages or [{"role": "user", "content": "请生成交底书。"}],
                system_prompt="MAIN_SYSTEM_PROMPT",
                tools=[{"type": "function", "function": {"name": "same_order_tool"}}],
                llm_client=llm,
                settings=settings,
            ),
        )
    )


def test_innovation_kernel_kit_create_read_and_recreate_current_only(tmp_path: Path) -> None:
    store, executor, _manager, project_id, session_id, settings = make_runtime(tmp_path)
    llm = KernelLLMClient(
        [
            "<analysis>x</analysis><innovation_kernel># 创新内核\n\n## 1. 核心问题\n旧问题</innovation_kernel>",
            "<analysis>x</analysis><innovation_kernel># 创新内核\n\n## 1. 核心问题\n新问题</innovation_kernel>",
        ]
    )

    created = run_kernel_tool(executor, project_id, session_id, llm, settings, "create")
    assert created["status"] == "success"
    assert created["output"]["source"] == "create"
    assert "确认" in created["output"]["user_confirmation_reminder"]
    assert "旧问题" in created["output"]["kernel_markdown"]

    read = run_kernel_tool(executor, project_id, session_id, llm, settings, "read_all")
    assert read["status"] == "success"
    assert read["output"]["kernel_markdown"] == created["output"]["kernel_markdown"]

    recreated = run_kernel_tool(executor, project_id, session_id, llm, settings, "recreate")
    assert recreated["status"] == "success"
    assert recreated["output"]["source"] == "recreate"
    assert "确认" in recreated["output"]["user_confirmation_reminder"]
    assert "新问题" in recreated["output"]["kernel_markdown"]
    assert "旧问题" not in store.get_innovation_kernel(project_id, session_id).kernel_markdown
    assert llm.prompts[-1]["system_prompt"] == "MAIN_SYSTEM_PROMPT"
    assert llm.prompts[-1]["tools"] == [{"type": "function", "function": {"name": "same_order_tool"}}]
    assert "<analysis>" in llm.prompts[-1]["messages"][-1]["content"]
    assert "系统会剥离" in llm.prompts[-1]["messages"][-1]["content"]
    assert "当前已有创新内核" in llm.prompts[-1]["messages"][-1]["content"]
    assert "旧问题" in llm.prompts[-1]["messages"][-1]["content"]

    kernel_files = list((store.project_dir(project_id) / "sessions").glob("*.innovation_kernel.json"))
    assert len(kernel_files) == 1


def test_innovation_kernel_kit_requires_current_kernel_for_recreate(tmp_path: Path) -> None:
    _store, executor, _manager, project_id, session_id, settings = make_runtime(tmp_path)
    result = run_kernel_tool(executor, project_id, session_id, KernelLLMClient([]), settings, "recreate")

    assert result["status"] == "failed"
    assert result["output"]["code"] == "innovation_kernel_not_found"


def test_innovation_kernel_kit_rejects_nested_tool_call_output(tmp_path: Path) -> None:
    store, executor, _manager, project_id, session_id, settings = make_runtime(tmp_path)
    store.save_innovation_kernel(
        project_id,
        session_id,
        kernel_markdown="# 创新内核\n\n## 1. 核心问题\n当前问题",
        source="create",
    )

    result = run_kernel_tool(executor, project_id, session_id, KernelLLMClient(["__tool_call__"]), settings, "recreate")

    assert result["status"] == "failed"
    assert result["output"]["code"] == "innovation_kernel_unexpected_tool_call"
    assert store.get_innovation_kernel(project_id, session_id).kernel_markdown.endswith("当前问题")


def test_context_manager_injects_current_kernel_without_event_history(tmp_path: Path) -> None:
    store, _executor, manager, project_id, session_id, _settings = make_runtime(tmp_path)
    store.save_innovation_kernel(
        project_id,
        session_id,
        kernel_markdown="# 创新内核\n\n## 1. 核心问题\n当前问题",
        source="create",
    )

    messages = manager.build_main_agent_messages(
        project_id,
        session_id,
        user_message="继续写交底书。",
        active_section_id=None,
        active_block_id=None,
        current_message_id="msg_2",
    )

    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith(INNOVATION_KERNEL_CONTEXT_PREFIX)
    assert "当前问题" in messages[0]["content"]
    events = store.read_session_events(project_id, session_id)
    assert [event.type for event in events] == ["user_input"]


def test_extract_innovation_kernel_strips_analysis_and_fences() -> None:
    assert extract_innovation_kernel("<analysis>x</analysis><innovation_kernel># K</innovation_kernel>") == "# K"
    assert extract_innovation_kernel("```markdown\n<innovation_kernel>\n# K\n</innovation_kernel>\n```") == "# K"
    dsml_wrapped = (
        "<｜｜DSML｜｜tool_calls>\n"
        "<｜｜DSML｜｜invoke name=\"innovation_kernel_kit\">\n"
        "<｜｜DSML｜｜parameter name=\"content\" string=\"true\"># 创新内核\n\n## 1. 核心问题\n正文"
        "</｜｜DSML｜｜parameter>\n"
        "</｜｜DSML｜｜invoke>\n"
        "</｜｜DSML｜｜tool_calls>"
    )
    assert extract_innovation_kernel(dsml_wrapped) == "# 创新内核\n\n## 1. 核心问题\n正文"
    assert extract_innovation_kernel("<｜｜DSML｜｜tool_calls></｜｜DSML｜｜tool_calls>") == ""
