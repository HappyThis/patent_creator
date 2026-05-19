from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ....agents import SUBAGENTS
from ....agents.tool_metadata import agent_tool


class ExecuteSubagentArguments(BaseModel):
    agent_id: str = Field(
        description="目标子 agent 的 id，必须来自已注册子 agent 清单。",
        json_schema_extra={"enum": list(SUBAGENTS)},
    )
    goal: str = Field(min_length=1, description="面向子 agent 的非空自然语言任务目标；目标范围、输出要求和注意事项都写在这里，并遵守该子 agent 的输入要求和使用边界。")


@agent_tool(
    args_model=ExecuteSubagentArguments,
    name="execute_subagent",
)
def execute_subagent(arguments: ExecuteSubagentArguments) -> dict[str, Any]:
    """调度一个已注册子 agent 执行局部任务，返回合并后的 pipe content。

    Returns:
        返回子 agent 已写入 pipe 的合并内容；失败时返回 failed，并包含 code 和 message。

    Rules:
        - goal 只描述子 agent 的局部任务，不要求子 agent 直接落盘文档。
        - 选择 agent_id 时遵守子 agent 注册信息中的职责、输入要求和使用边界。

    Examples:
        - 调度局部写作任务: {"agent_id":"section_writer","goal":"为技术方案章节生成一个不超过 800 字的候选子章节正文。"}
    """
    raise NotImplementedError("execute_subagent is implemented by ExecutorEngine.execute_subagent")
