from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..core import Settings
from ..domain.disclosure import section_title_text, section_to_markdown

TECHNICAL_SOLUTION_TITLE = "技术方案"
TECHNICAL_SOLUTION_CHECK_TIMEOUT_SECONDS = 180.0
TECHNICAL_SOLUTION_CHECK_MAX_ATTEMPTS = 2
TECHNICAL_SOLUTION_CHECK_TEMPERATURE = 0.7


class SupportsTechnicalSolutionCheck(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class TechnicalSolutionCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_markdown: str = Field(min_length=1, description="完整 Markdown 格式技术方案评审意见。")

    @field_validator("review_markdown")
    @classmethod
    def _strip_non_empty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump()


class TechnicalSolutionCheckValidationError(ValueError):
    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class TechnicalSolutionChecker:
    def __init__(
        self,
        llm_client: SupportsTechnicalSolutionCheck,
        settings: Settings,
        *,
        temperature: float = TECHNICAL_SOLUTION_CHECK_TEMPERATURE,
    ) -> None:
        self.llm_client = llm_client
        self.settings = settings
        self.temperature = temperature

    async def check(
        self,
        *,
        technical_solution_markdown: str,
        trace_context: dict[str, Any] | None = None,
    ) -> TechnicalSolutionCheckResult:
        return await self._check_single(
            technical_solution_markdown=technical_solution_markdown,
            trace_context=trace_context,
        )

    async def _check_single(
        self,
        *,
        technical_solution_markdown: str,
        trace_context: dict[str, Any] | None = None,
    ) -> TechnicalSolutionCheckResult:
        base_user_prompt = _checker_user_prompt(technical_solution_markdown)
        timeout = min(self.settings.llm_timeout, TECHNICAL_SOLUTION_CHECK_TIMEOUT_SECONDS)
        last_payload: dict[str, Any] | None = None
        last_error: TechnicalSolutionCheckValidationError | None = None
        for attempt in range(1, TECHNICAL_SOLUTION_CHECK_MAX_ATTEMPTS + 1):
            user_prompt = (
                base_user_prompt
                if attempt == 1
                else _checker_retry_user_prompt(
                    base_user_prompt,
                    invalid_payload=last_payload,
                    validation_error=str(last_error) if last_error is not None else "",
                )
            )
            payload = await self.llm_client.generate_json(
                system_prompt=_checker_system_prompt(),
                user_prompt=user_prompt,
                temperature=self.temperature,
                timeout=timeout,
                trace_context={
                    **(trace_context or {}),
                    "scope": "technical_solution_checker",
                    "attempt": attempt,
                },
            )
            try:
                return parse_technical_solution_check_result(payload)
            except TechnicalSolutionCheckValidationError as exc:
                last_payload = payload
                last_error = exc
                if attempt >= TECHNICAL_SOLUTION_CHECK_MAX_ATTEMPTS:
                    raise TechnicalSolutionCheckValidationError(str(exc), attempts=attempt) from exc

        raise TechnicalSolutionCheckValidationError("technical solution check validation retry exhausted", attempts=0)


def parse_technical_solution_check_result(payload: dict[str, Any]) -> TechnicalSolutionCheckResult:
    try:
        return TechnicalSolutionCheckResult.model_validate(payload)
    except ValidationError as exc:
        raise TechnicalSolutionCheckValidationError(
            f"technical solution check result does not match schema: {exc}",
            attempts=1,
        ) from exc


def technical_solution_markdown(disclosure: dict[str, Any]) -> str:
    for section in disclosure.get("sections", []):
        if section_title_text(section) == TECHNICAL_SOLUTION_TITLE:
            return "\n".join(section_to_markdown(section, 2)).strip()
    return ""


def checker_feedback_user_message(result: TechnicalSolutionCheckResult) -> str:
    return (
        "系统完成了一次“技术方案”评审。\n\n"
        "你必须参考以下评审意见，继续修改“技术方案”章节。\n"
        "重点补强具体技术实现原理、处理规则、数据/状态流转、异常恢复、冲突处理和边界条件。\n"
        "不要把评审过程、评审报告或“系统评审”写入交底书正文。\n"
        "如果你认为某条建议不完全适用，也要通过修改正文使技术方案更清楚、更具体，而不是仅解释原因。\n\n"
        "评审意见如下：\n\n"
        f"{result.review_markdown}"
    )


def _checker_system_prompt() -> str:
    return """你是专利交底书“技术方案”章节的质量检查器和评审器，不是对话 agent，也不能调用工具。
你只根据用户提供的“技术方案”章节正文做只读评价，并输出给 main-agent 参考的修订意见。

一、检查对象
- 只检查“技术方案”章节正文。
- 不评价完整专利申请文件，不评价权利要求布局，不评价商业价值。
- 你的任务是判断该章节是否把具体技术方式讲深、讲清楚。

二、评价目标
- 评价目标只包括两项：技术深度，以及技术方案的结构与表达清晰度。
- 技术深度是主指标；结构与表达是辅助指标。
- 不要输出“通过/不通过”结论。
- 不要把重要问题降级成“可选优化”。
- 即使总体方向正确，也要指出还可以让技术方案更具体、更闭合的修订点。

三、技术深度定义
- 第一层：是否避免泛泛谈论概念、目标、功能或效果。
- 第二层：是否写出了具体解决方法和技术实现原理。
- 第三层：关键机制在冲突、异常、恢复和边界条件下是否仍然闭合。

四、判定示例
- 以下示例是 few-shot 风格的判断参考，不是固定关键词清单；遇到不同技术领域时，应抽象使用同一评价原则。
- 示例 1：正文只说系统进行智能分析、策略调度、动态优化、自动处理、协同管理等抽象方向，却没有说明具体如何分析、如何调度、如何处理、输入输出是什么、处理规则是什么、状态或数据如何变化，应认为技术深度不足。
- 示例 2：正文已经提出了具体模块、记录、流程、状态或算法，但没有说明关键过程的前置条件、多步骤协同或外部输入变化时的冲突处理、异常场景下的判定依据、多个结果竞争时的优先级，以及哪些边界下应保持、回退、终止或不得覆盖既有结果，也应认为技术深度不足。
- 示例 3：正文不仅说明组成要素和处理流程，还说明输入、输出、判断规则、状态或数据变化、异常或边界场景下的处理方式，并能把技术效果落回具体技术手段，通常可以认为具备较充分的技术深度。

五、结构与表达清晰度
- 判断章节展开顺序是否合理。
- 判断术语是否一致、段落职责是否清楚、表达是否流畅。
- 判断读者能否顺着文本理解该技术方式如何工作和如何实施。

六、评审规则
- 你的职责不是决定流程是否继续，而是给出 main-agent 必须参考的修订意见。
- 如果正文停留在概念性、功能性或效果性描述，要明确指出应补充哪些技术实现原理。
- 如果缺少关键处理过程、判断规则、输入输出、数据流转、状态变化、异常情况或边界条件，要明确指出缺口。
- 如果涉及关键过程、状态变化、多步骤协同、外部输入、异常场景或结果竞争，要检查前置条件、冲突处理、优先级、判定依据或边界处置是否充分。
- 如果技术效果没有落回具体技术手段，或结构跳跃、表达含混导致技术方式没有讲清楚，要给出具体改写方向。
- 评审意见必须便于 main-agent 直接据此修改正文，而不是抽象评价。

七、输出要求
- 必须严格返回 JSON 对象。
- JSON 只能包含 review_markdown 一个字段。
- review_markdown 必须是一份完整 Markdown 技术方案评审意见。
- review_markdown 不得包含“结论：通过”“结论：不通过”“可选优化”等流程判定或弱化措辞。"""


def _checker_user_prompt(technical_solution_markdown: str) -> str:
    return (
        "请评审下面的“技术方案”章节，并严格返回 JSON：\n\n"
        f"{_checker_output_schema_text()}\n\n"
        "检查要求：\n"
        "- 技术问题：检查技术方案是否围绕明确技术问题展开，而不是只描述业务目标或功能愿景。\n"
        "- 技术深度：重点检查技术方案是否写出了具体解决方法和技术实现原理，而不是泛泛谈论概念、目标、功能或效果。\n"
        "- 细致解决方法：检查是否给出足够细致的解决方法，而不是只给出方向性模块名称。\n"
        "- 细致程度：检查是否说明关键组成、输入输出、处理过程、判断规则、数据流转、状态变化、异常情况、边界条件和技术效果如何由具体手段产生。\n"
        "- 机制闭合度：如果方案涉及关键过程、状态变化、多步骤协同、外部输入、异常场景或结果竞争，检查是否说明前置条件、冲突处理、优先级、判定依据和边界处置。\n"
        "- 结构表达：检查章节结构是否合理，表达是否流畅，术语一致性是否足够，段落顺序是否能把技术方式表述清楚。\n\n"
        "评审要求：\n"
        "- 不要判断是否通过；不要输出通过/不通过结论。\n"
        "- 不要把有价值的改进点写成“可选优化”；统一写成“建议修订点”或“需要补强”。\n"
        "- 即使当前技术方案整体较好，也要提取能提升稳定性、技术深度和表达清晰度的具体修订意见。\n"
        "- review_markdown 必须是一份完整 Markdown 技术方案评审意见，并严格使用下方模板。\n\n"
        f"{_checker_review_template_text()}\n\n"
        "待检查内容：\n\n"
        f"{technical_solution_markdown or '（技术方案章节为空）'}"
    )


def _checker_retry_user_prompt(
    base_user_prompt: str,
    *,
    invalid_payload: dict[str, Any] | None,
    validation_error: str,
) -> str:
    invalid_payload_text = json.dumps(invalid_payload or {}, ensure_ascii=False, indent=2)
    return (
        f"{base_user_prompt}\n\n"
        "上一次输出未通过后端 Pydantic schema 校验。请重新输出一个严格满足 schema 的 JSON 对象，"
        "不要输出 Markdown 代码块，不要输出解释，不要输出额外字段。\n\n"
        "上一次无效输出：\n"
        f"{invalid_payload_text}\n\n"
        "校验错误：\n"
        f"{validation_error}"
    )


def _checker_output_schema_text() -> str:
    schema = TechnicalSolutionCheckResult.model_json_schema()
    return (
        "输出必须满足下面的 JSON Schema；禁止返回 schema 未声明的额外字段：\n\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def _checker_review_template_text() -> str:
    return """review_markdown 必须使用这个 Markdown 模板：

## 技术方案评审意见

### 总体评价
简要说明当前技术方案已经具备哪些具体机制，以及总体上还需要在哪些方面补强。不得写“通过”或“不通过”。

### 技术深度修订点
1. 指出哪些内容仍偏概念、目标、功能或效果描述。
2. 指出需要补充哪些具体实现原理、处理规则、数据流转、状态变化、判断条件、异常情况或边界处理。
3. 如果已有内容较充分，也要指出可以进一步增强可保护技术特征的具体位置。

### 关键机制闭合性修订点
1. 检查关键过程、状态变化、多步骤协同、外部输入、异常场景或结果竞争是否有明确前置条件。
2. 指出冲突处理、优先级、判定依据、恢复规则、终止规则或不得覆盖既有结果的边界是否还需补充。
3. 指出技术效果是否已经能从具体技术手段中推导出来；如不能，说明应如何补齐因果链。

### 结构与表达修订点
1. 指出章节组织、术语一致性、段落顺序或表达清晰度上的问题。
2. 说明应如何调整结构，使读者能顺着文本理解方案如何实施。

### 给 main-agent 的修订指令
1. 用祈使句列出 main-agent 应直接执行的正文修改动作。
2. 每条指令都应指向“技术方案”正文中应补充或改写的技术内容。
3. 不要要求 main-agent 在正文中解释评审过程。"""
