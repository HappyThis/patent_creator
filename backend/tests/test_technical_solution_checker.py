from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.technical_solution_checker import (
    TechnicalSolutionChecker,
    _checker_user_prompt,
    parse_technical_solution_check_result,
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
            "gate_pass": False,
            "review_markdown": "  ## 检查意见\n\n- 技术手段不足。  ",
            "reason": "  缺少输入输出和边界条件说明。  ",
        }
    )

    assert result.gate_pass is False
    assert result.review_markdown == "## 检查意见\n\n- 技术手段不足。"
    assert result.reason == "缺少输入输出和边界条件说明。"


@pytest.mark.parametrize(
    "payload",
    [
        {"gate_pass": "false", "review_markdown": "意见", "reason": "原因"},
        {"gate_pass": False, "review_markdown": "意见", "reason": "原因", "score": 80},
        {"gate_pass": False, "review_markdown": " ", "reason": "原因"},
        {"gate_pass": False, "review_markdown": "意见", "reason": ""},
    ],
)
def test_technical_solution_check_result_strict_schema_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_technical_solution_check_result(payload)


def test_technical_solution_checker_prompt_contains_schema_and_concrete_entities() -> None:
    prompt = _checker_user_prompt("## 技术方案\n\n系统根据策略处理任务。")

    assert "JSON Schema" in prompt
    assert '"additionalProperties": false' in prompt
    for term in [
        "技术问题",
        "技术手段",
        "组成要素",
        "输入输出",
        "数据流",
        "状态变化",
        "触发条件",
        "处理规则",
        "模块协同关系",
        "异常分支",
        "边界条件",
        "可选实施方式",
        "技术效果闭合",
        "术语一致性",
        "可实施闭合链条",
        "关键机制缺失",
        "抽象描述",
        "可保护性",
    ]:
        assert term in prompt


@pytest.mark.anyio
async def test_technical_solution_checker_retries_once_after_schema_validation_failure(tmp_path: Path) -> None:
    client = FakeCheckerLLMClient(
        [
            {"gate_pass": "false", "review_markdown": "意见", "reason": "原因"},
            {
                "gate_pass": False,
                "review_markdown": "## 检查意见\n\n- 技术手段不足。",
                "reason": "第一次输出 gate_pass 类型错误，重试后合法。",
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

    assert result.gate_pass is False
    assert len(client.prompts) == 2
    assert client.prompts[0]["trace_context"]["attempt"] == 1
    assert client.prompts[1]["trace_context"]["attempt"] == 2
    assert "上一次输出未通过后端 Pydantic schema 校验" in client.prompts[1]["user_prompt"]
    assert '"gate_pass": "false"' in client.prompts[1]["user_prompt"]
