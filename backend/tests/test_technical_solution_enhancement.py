from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.technical_solution_enhancement import (
    TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS,
    TechnicalSolutionChangeAssessor,
    TechnicalSolutionEnhancementSummarizer,
    TechnicalSolutionImprovementAdvisor,
    _improvement_advisor_system_prompt,
    _improvement_advisor_user_prompt,
    parse_technical_solution_change_assessment_result,
    parse_technical_solution_enhancement_summary_result,
    parse_technical_solution_improvement_advice_result,
)


class FakeAdviceLLMClient:
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
        on_retry_event: Any = None,
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


def test_technical_solution_improvement_advice_result_strict_schema_accepts_valid_payload() -> None:
    result = parse_technical_solution_improvement_advice_result(
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
def test_technical_solution_improvement_advice_result_strict_schema_rejects_invalid_payload(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        parse_technical_solution_improvement_advice_result(payload)


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


@pytest.mark.anyio
async def test_technical_solution_change_assessor_uses_enhancement_timeout(tmp_path: Path) -> None:
    client = FakeAdviceLLMClient([{"should_review": True, "reason": "本轮影响核心机制。"}])
    assessor = TechnicalSolutionChangeAssessor(
        client,
        Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            git_user_name="Test User",
            git_user_email="test@example.com",
            openai_api_key="test-key",
            llm_timeout=45.0,
        ),
    )

    await assessor.assess(
        user_request="请完善技术方案。",
        technical_solution_markdown="## 技术方案\n\n系统根据策略处理任务。",
        technical_solution_diff="+系统根据策略处理任务。",
    )

    assert client.prompts[0]["timeout"] == TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS


@pytest.mark.anyio
async def test_technical_solution_summarizer_uses_enhancement_timeout(tmp_path: Path) -> None:
    client = FakeAdviceLLMClient([{"applied_summary": "已补充处理阶段迁移规则。"}])
    summarizer = TechnicalSolutionEnhancementSummarizer(
        client,
        Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            git_user_name="Test User",
            git_user_email="test@example.com",
            openai_api_key="test-key",
            llm_timeout=45.0,
        ),
    )

    await summarizer.summarize(
        review_markdown="## 技术方案评审意见\n\n- 补充处理阶段迁移规则。",
        enhanced_technical_solution_markdown="## 技术方案\n\n已补充处理阶段迁移规则。",
        enhancement_diff="+已补充处理阶段迁移规则。",
    )

    assert client.prompts[0]["timeout"] == TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS


def test_technical_solution_advisor_prompt_is_cross_domain_and_compact() -> None:
    prompt = _improvement_advisor_system_prompt() + "\n\n" + _improvement_advisor_user_prompt("## 技术方案\n\n系统根据策略处理任务。")

    assert "JSON Schema" in prompt
    assert '"additionalProperties": false' in prompt
    for term in [
        "先识别技术领域",
        "结构或装置",
        "材料或配方",
        "工艺",
        "软件、数据或控制",
        "技术问题",
        "状态或数据变化",
        "机制闭合性",
        "已有内容充分时不要为了产生建议而扩写",
        "不要默认要求字段表、状态枚举、接口、函数、schema、公式或伪代码",
        "## 技术方案评审意见",
        "### 总体评价",
        "### 技术机制修订点",
        "### 结构与表达修订点",
        "### 给 main-agent 的修订指令",
        "无需继续修改",
    ]:
        assert term in prompt
    assert "专利式抽象表达" not in prompt
    assert "坏反馈" not in prompt
    assert len(prompt) < 1_500


@pytest.mark.anyio
async def test_technical_solution_advisor_retries_once_after_schema_validation_failure(tmp_path: Path) -> None:
    client = FakeAdviceLLMClient(
        [
            {"gate_pass": False, "review_markdown": "意见"},
            {
                "review_markdown": "## 检查意见\n\n- 技术手段不足。",
            },
        ]
    )
    advisor = TechnicalSolutionImprovementAdvisor(
        client,
        Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            git_user_name="Test User",
            git_user_email="test@example.com",
            openai_api_key="test-key",
        ),
    )

    await advisor.advise(technical_solution_markdown="## 技术方案\n\n系统根据策略处理任务。")

    assert len(client.prompts) == 2
    assert client.prompts[0]["trace_context"]["attempt"] == 1
    assert client.prompts[1]["trace_context"]["attempt"] == 2
    assert "上一次输出未通过后端 Pydantic schema 校验" in client.prompts[1]["user_prompt"]
    assert '"gate_pass": false' in client.prompts[1]["user_prompt"]


@pytest.mark.anyio
async def test_technical_solution_advisor_runs_single_review_by_default(tmp_path: Path) -> None:
    client = FakeAdviceLLMClient(
        [
            {
                "review_markdown": "## 技术方案评审意见\n\n### 技术深度修订点\n1. 补充处理阶段迁移规则。",
            },
        ]
    )
    advisor = TechnicalSolutionImprovementAdvisor(
        client,
        Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            git_user_name="Test User",
            git_user_email="test@example.com",
            openai_api_key="test-key",
        ),
    )

    result = await advisor.advise(technical_solution_markdown="## 技术方案\n\n系统根据策略处理任务。")

    assert len(client.prompts) == 1
    assert {prompt["temperature"] for prompt in client.prompts} == {0.7}
    assert {prompt["timeout"] for prompt in client.prompts} == {TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS}
    assert "补充处理阶段迁移规则" in result.review_markdown
