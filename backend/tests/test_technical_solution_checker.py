from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.technical_solution_checker import (
    TECHNICAL_SOLUTION_CHECK_TIMEOUT_SECONDS,
    TechnicalSolutionChecker,
    _checker_system_prompt,
    _checker_user_prompt,
    parse_technical_solution_change_assessment_result,
    parse_technical_solution_check_result,
    parse_technical_solution_enhancement_summary_result,
)


class FakeCheckerLLMClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[dict[str, Any]] = []

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.prompts.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "timeout": timeout,
                "trace_context": trace_context,
            }
        )
        return self.payloads.pop(0)


def test_technical_solution_check_result_strict_schema_accepts_valid_payload() -> None:
    result = parse_technical_solution_check_result(
        {
            "review_markdown": "  ## 检查意见\n\n- 技术手段不足。  ",
        }
    )

    assert result.review_markdown == "## 检查意见\n\n- 技术手段不足。"


@pytest.mark.parametrize(
    "payload",
    [
        {"gate_pass": False, "review_markdown": "意见"},
        {"review_markdown": "意见", "reason": "原因"},
        {"review_markdown": "意见", "score": 80},
        {"review_markdown": " "},
    ],
)
def test_technical_solution_check_result_strict_schema_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_technical_solution_check_result(payload)


def test_technical_solution_change_assessment_result_strict_schema() -> None:
    result = parse_technical_solution_change_assessment_result(
        {
            "should_review": True,
            "reason": "  本轮改动影响核心机制。  ",
        }
    )

    assert result.should_review is True
    assert result.reason == "本轮改动影响核心机制。"

    with pytest.raises(ValueError):
        parse_technical_solution_change_assessment_result({"gate_pass": True, "reason": "原因"})


def test_technical_solution_enhancement_summary_result_strict_schema() -> None:
    result = parse_technical_solution_enhancement_summary_result(
        {
            "applied_summary": "  已补充阶段迁移规则。  ",
        }
    )

    assert result.applied_summary == "已补充阶段迁移规则。"

    with pytest.raises(ValueError):
        parse_technical_solution_enhancement_summary_result({"applied_summary": "摘要", "reason": "原因"})


def test_technical_solution_checker_prompt_contains_schema_and_concrete_entities() -> None:
    prompt = _checker_system_prompt() + "\n\n" + _checker_user_prompt("## 技术方案\n\n系统根据策略处理任务。")

    assert "JSON Schema" in prompt
    assert '"additionalProperties": false' in prompt
    for term in [
        "技术问题",
        "输入输出",
        "数据流转",
        "状态变化",
        "处理规则",
        "边界条件",
        "泛泛谈论",
        "技术实现原理",
        "细致解决方法",
        "机制闭合度",
        "关键过程",
        "状态变化",
        "多步骤协同",
        "外部输入",
        "异常场景",
        "结果竞争",
        "前置条件",
        "冲突处理",
        "优先级",
        "判定依据",
        "边界处置",
        "术语一致性",
        "不要判断是否通过",
        "不要把有价值的改进点写成“可选优化”",
        "技术人员式技术抽象",
        "不得用变量名、字段名、接口名、状态枚举、公式或伪代码清单诱导 main-agent 生成工程 RFC",
        "不得诱导 main-agent 写成权利要求或正式专利说明书口吻",
        "技术问题",
        "解决思路",
        "系统组织方式",
        "关键取舍",
        "运行逻辑",
        "坏反馈",
        "好反馈",
        "不对应任何具体 benchmark case",
        "说明系统如何保持同一处理对象的稳定身份",
        "重复请求、执行中断或迟到结果",
        "不同实现载体如何通过统一适配方式接入",
        "## 技术方案评审意见",
        "### 总体评价",
        "### 技术深度修订点",
        "### 关键机制闭合性修订点",
        "### 技术人员式表达修订点",
        "### 给 main-agent 的修订指令",
    ]:
        assert term in prompt
    assert "专利式抽象表达" not in prompt


@pytest.mark.anyio
async def test_technical_solution_checker_retries_once_after_schema_validation_failure(tmp_path: Path) -> None:
    client = FakeCheckerLLMClient(
        [
            {"gate_pass": False, "review_markdown": "意见"},
            {
                "review_markdown": "## 检查意见\n\n- 技术手段不足。",
            },
        ]
    )
    checker = TechnicalSolutionChecker(
        client,
        Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            git_user_name="Test User",
            git_user_email="test@example.com",
            openai_api_key="test-key",
        ),
    )

    result = await checker.check(technical_solution_markdown="## 技术方案\n\n系统根据策略处理任务。")

    assert len(client.prompts) == 2
    assert client.prompts[0]["trace_context"]["attempt"] == 1
    assert client.prompts[1]["trace_context"]["attempt"] == 2
    assert "上一次输出未通过后端 Pydantic schema 校验" in client.prompts[1]["user_prompt"]
    assert '"gate_pass": false' in client.prompts[1]["user_prompt"]


@pytest.mark.anyio
async def test_technical_solution_checker_runs_single_review_by_default(tmp_path: Path) -> None:
    client = FakeCheckerLLMClient(
        [
            {
                "review_markdown": "## 技术方案评审意见\n\n### 技术深度修订点\n1. 补充处理阶段迁移规则。",
            },
        ]
    )
    checker = TechnicalSolutionChecker(
        client,
        Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            git_user_name="Test User",
            git_user_email="test@example.com",
            openai_api_key="test-key",
        ),
    )

    result = await checker.check(technical_solution_markdown="## 技术方案\n\n系统根据策略处理任务。")

    assert len(client.prompts) == 1
    assert {prompt["temperature"] for prompt in client.prompts} == {0.7}
    assert {prompt["timeout"] for prompt in client.prompts} == {TECHNICAL_SOLUTION_CHECK_TIMEOUT_SECONDS}
    assert "补充处理阶段迁移规则" in result.review_markdown
