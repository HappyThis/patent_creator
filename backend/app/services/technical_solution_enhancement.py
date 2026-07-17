from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..core import Settings
from ..domain.disclosure import section_title_text, section_to_markdown

TECHNICAL_SOLUTION_TITLE = "技术方案"
TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS = 180.0
TECHNICAL_SOLUTION_ADVICE_MAX_ATTEMPTS = 2
TECHNICAL_SOLUTION_ADVICE_TEMPERATURE = 0.7
TECHNICAL_SOLUTION_ASSESS_TEMPERATURE = 0.2
TECHNICAL_SOLUTION_SUMMARY_TEMPERATURE = 0.2

ResultModelT = TypeVar("ResultModelT", bound=BaseModel)


class SupportsTechnicalSolutionGeneration(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: float | None = None,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        ...


class TechnicalSolutionImprovementAdviceResult(BaseModel):
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


class TechnicalSolutionChangeAssessmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    should_review: bool = Field(description="本轮技术方案变更是否需要进入增强建议。")
    reason: str = Field(min_length=1, description="内部原因，说明为什么需要或不需要增强建议。")

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump()


class TechnicalSolutionEnhancementSummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    applied_summary: str = Field(min_length=1, description="根据增强建议实际完成的技术方案改动摘要。")

    @field_validator("applied_summary")
    @classmethod
    def _strip_applied_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump()


class TechnicalSolutionStructuredOutputValidationError(ValueError):
    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class TechnicalSolutionChangeAssessor:
    def __init__(
        self,
        llm_client: SupportsTechnicalSolutionGeneration,
        settings: Settings,
        *,
        temperature: float = TECHNICAL_SOLUTION_ASSESS_TEMPERATURE,
    ) -> None:
        self.llm_client = llm_client
        self.settings = settings
        self.temperature = temperature

    async def assess(
        self,
        *,
        user_request: str,
        technical_solution_markdown: str,
        technical_solution_diff: str,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionChangeAssessmentResult:
        payload = await self.llm_client.generate_json(
            system_prompt=_change_assessor_system_prompt(),
            user_prompt=_change_assessor_user_prompt(
                user_request=user_request,
                technical_solution_markdown=technical_solution_markdown,
                technical_solution_diff=technical_solution_diff,
            ),
            temperature=self.temperature,
            timeout=TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS,
            trace_context={
                **(trace_context or {}),
                "scope": "technical_solution_change_assessor",
            },
            on_retry_event=on_retry_event,
        )
        return parse_technical_solution_change_assessment_result(payload)


class TechnicalSolutionImprovementAdvisor:
    def __init__(
        self,
        llm_client: SupportsTechnicalSolutionGeneration,
        settings: Settings,
        *,
        temperature: float = TECHNICAL_SOLUTION_ADVICE_TEMPERATURE,
    ) -> None:
        self.llm_client = llm_client
        self.settings = settings
        self.temperature = temperature

    async def advise(
        self,
        *,
        technical_solution_markdown: str,
        user_request: str | None = None,
        technical_solution_diff: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionImprovementAdviceResult:
        return await self._advise_once(
            technical_solution_markdown=technical_solution_markdown,
            user_request=user_request,
            technical_solution_diff=technical_solution_diff,
            recent_history=recent_history,
            trace_context=trace_context,
            on_retry_event=on_retry_event,
        )

    async def _advise_once(
        self,
        *,
        technical_solution_markdown: str,
        user_request: str | None = None,
        technical_solution_diff: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionImprovementAdviceResult:
        base_user_prompt = _improvement_advisor_user_prompt(
            technical_solution_markdown,
            user_request=user_request,
            technical_solution_diff=technical_solution_diff,
            recent_history=recent_history,
        )
        timeout = TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS
        last_payload: dict[str, Any] | None = None
        last_error: TechnicalSolutionStructuredOutputValidationError | None = None
        for attempt in range(1, TECHNICAL_SOLUTION_ADVICE_MAX_ATTEMPTS + 1):
            user_prompt = (
                base_user_prompt
                if attempt == 1
                else _improvement_advisor_retry_user_prompt(
                    base_user_prompt,
                    invalid_payload=last_payload,
                    validation_error=str(last_error) if last_error is not None else "",
                )
            )
            payload = await self.llm_client.generate_json(
                system_prompt=_improvement_advisor_system_prompt(),
                user_prompt=user_prompt,
                temperature=self.temperature,
                timeout=timeout,
                trace_context={
                    **(trace_context or {}),
                    "scope": "technical_solution_improvement_advisor",
                    "attempt": attempt,
                },
                on_retry_event=on_retry_event,
            )
            try:
                return parse_technical_solution_improvement_advice_result(payload)
            except TechnicalSolutionStructuredOutputValidationError as exc:
                last_payload = payload
                last_error = exc
                if attempt >= TECHNICAL_SOLUTION_ADVICE_MAX_ATTEMPTS:
                    raise TechnicalSolutionStructuredOutputValidationError(str(exc), attempts=attempt) from exc

        raise TechnicalSolutionStructuredOutputValidationError(
            "technical solution improvement advice validation retry exhausted",
            attempts=0,
        )


class TechnicalSolutionEnhancementSummarizer:
    def __init__(
        self,
        llm_client: SupportsTechnicalSolutionGeneration,
        settings: Settings,
        *,
        temperature: float = TECHNICAL_SOLUTION_SUMMARY_TEMPERATURE,
    ) -> None:
        self.llm_client = llm_client
        self.settings = settings
        self.temperature = temperature

    async def summarize(
        self,
        *,
        review_markdown: str,
        enhanced_technical_solution_markdown: str,
        enhancement_diff: str,
        trace_context: dict[str, Any] | None = None,
        on_retry_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> TechnicalSolutionEnhancementSummaryResult:
        payload = await self.llm_client.generate_json(
            system_prompt=_enhancement_summarizer_system_prompt(),
            user_prompt=_enhancement_summarizer_user_prompt(
                review_markdown=review_markdown,
                enhanced_technical_solution_markdown=enhanced_technical_solution_markdown,
                enhancement_diff=enhancement_diff,
            ),
            temperature=self.temperature,
            timeout=TECHNICAL_SOLUTION_ENHANCEMENT_TIMEOUT_SECONDS,
            trace_context={
                **(trace_context or {}),
                "scope": "technical_solution_enhancement_summarizer",
            },
            on_retry_event=on_retry_event,
        )
        return parse_technical_solution_enhancement_summary_result(payload)


def parse_technical_solution_improvement_advice_result(
    payload: dict[str, Any],
) -> TechnicalSolutionImprovementAdviceResult:
    return _parse_result_model(
        payload,
        TechnicalSolutionImprovementAdviceResult,
        error_context="technical solution improvement advice result",
    )


def parse_technical_solution_change_assessment_result(payload: dict[str, Any]) -> TechnicalSolutionChangeAssessmentResult:
    return _parse_result_model(
        payload,
        TechnicalSolutionChangeAssessmentResult,
        error_context="technical solution change assessment result",
    )


def parse_technical_solution_enhancement_summary_result(
    payload: dict[str, Any],
) -> TechnicalSolutionEnhancementSummaryResult:
    return _parse_result_model(
        payload,
        TechnicalSolutionEnhancementSummaryResult,
        error_context="technical solution enhancement summary result",
    )


def _parse_result_model(
    payload: dict[str, Any],
    model_type: type[ResultModelT],
    *,
    error_context: str,
) -> ResultModelT:
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise TechnicalSolutionStructuredOutputValidationError(
            f"{error_context} does not match schema: {exc}",
            attempts=1,
        ) from exc


def technical_solution_markdown(disclosure: dict[str, Any]) -> str:
    for section in disclosure.get("sections", []):
        if section_title_text(section) == TECHNICAL_SOLUTION_TITLE:
            return "\n".join(section_to_markdown(section, 2)).strip()
    return ""


def enhancement_feedback_user_message(result: TechnicalSolutionImprovementAdviceResult) -> str:
    return (
        "系统正在增强模式下继续完善“技术方案”章节。\n\n"
        "请根据以下内部建议修改“技术方案”，只处理有依据且符合本轮用户意图的实质问题；"
        "不要恢复用户删除的内容、扩写无关章节、堆砌工程清单或提及内部评审流程。\n\n"
        "内部改进建议如下：\n\n"
        f"{result.review_markdown}"
    )


def _change_assessor_system_prompt() -> str:
    return """你是专利交底书“技术方案”增强模式的变更影响评估器，不能调用工具。
只判断本轮变化是否实质影响技术问题、核心机制、领域相关关系、成立条件、技术效果或章节结构。格式、术语、轻微润色和局部删除通常无需继续增强；不得借增强恢复用户删除的内容。
只输出符合 schema 的 JSON，不给改写建议或通过结论。"""


def _change_assessor_user_prompt(
    *,
    user_request: str,
    technical_solution_markdown: str,
    technical_solution_diff: str,
) -> str:
    schema = TechnicalSolutionChangeAssessmentResult.model_json_schema()
    return (
        "判断是否需要进入技术方案改进流程，并严格按 JSON Schema 输出：\n\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        "用户请求：\n"
        f"{user_request or '（空）'}\n\n"
        "本轮修改后的技术方案：\n"
        f"{technical_solution_markdown or '（技术方案章节为空）'}\n\n"
        "本轮技术方案 diff：\n"
        f"{technical_solution_diff or '（无 diff）'}"
    )


def _improvement_advisor_system_prompt() -> str:
    return """你是专利交底书“技术方案”章节的只读改进建议器，不能调用工具。只评价技术深度、机制闭合性和结构表达，不评价商业价值或权利要求布局。

先识别技术领域，只检查适用关系：
- 结构或装置：组成、位置、连接、作用及工作条件；
- 材料或配方：组成、比例、结构、相互作用及适用条件；
- 工艺：步骤、顺序、工艺条件、过程变化和结果；
- 软件、数据或控制：输入、触发、处理、状态或数据变化、输出、异常和边界。

所有领域都检查技术问题、核心手段、必要关系、成立条件和技术效果因果链。只提出会实质改善正确性、可实施性或理解的建议；已有内容充分时不要为了产生建议而扩写。建议应指向具体正文问题并使用技术人员式抽象；不要默认要求字段表、状态枚举、接口、函数、schema、公式或伪代码，也不要写成权利要求口吻。

严格输出只含 review_markdown 的 JSON。review_markdown 使用指定模板，不写通过/不通过或内部流程说明。"""


def _improvement_advisor_user_prompt(
    technical_solution_markdown: str,
    *,
    user_request: str | None = None,
    technical_solution_diff: str | None = None,
    recent_history: list[dict[str, Any]] | None = None,
) -> str:
    context_parts = []
    if user_request is not None:
        context_parts.append(f"当前用户请求：\n{user_request or '（空）'}")
    if technical_solution_diff:
        context_parts.append(f"本轮技术方案 diff：\n{technical_solution_diff}")
    if recent_history:
        context_parts.append(
            "最近 3 轮技术方案增强历史（完整记录，仅供避免重复建议）：\n"
            f"{json.dumps(recent_history[-3:], ensure_ascii=False, indent=2)}"
        )
    context_text = "\n\n".join(context_parts)
    return (
        "请评审下面的“技术方案”章节，并严格返回 JSON：\n\n"
        f"{_improvement_advisor_output_schema_text()}\n\n"
        f"{context_text + chr(10) + chr(10) if context_text else ''}"
        f"{_improvement_advisor_review_template_text()}\n\n"
        "待检查内容：\n\n"
        f"{technical_solution_markdown or '（技术方案章节为空）'}"
    )


def _improvement_advisor_retry_user_prompt(
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


def _improvement_advisor_output_schema_text() -> str:
    schema = TechnicalSolutionImprovementAdviceResult.model_json_schema()
    return (
        "输出必须满足下面的 JSON Schema；禁止返回 schema 未声明的额外字段：\n\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def _enhancement_summarizer_system_prompt() -> str:
    return """你是专利交底书“技术方案”增强总结器，不是对话 agent，也不能调用工具。
你只根据内部改进建议、二次增强后的技术方案和二次增强 diff，总结本轮实际完成了哪些修改。

你必须遵守：
- 只输出 JSON 对象。
- 不提出新建议。
- 不评价质量好坏。
- 不暴露 gate、checker、review 等内部机制词。
- applied_summary 应简短、具体，说明实际改动，不超过 300 字。
"""


def _enhancement_summarizer_user_prompt(
    *,
    review_markdown: str,
    enhanced_technical_solution_markdown: str,
    enhancement_diff: str,
) -> str:
    schema = TechnicalSolutionEnhancementSummaryResult.model_json_schema()
    return (
        "请总结本轮根据内部改进建议实际完成的技术方案修改，并严格返回 JSON。\n\n"
        "输出必须满足下面的 JSON Schema；禁止返回 schema 未声明的额外字段：\n\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        "内部改进建议：\n"
        f"{review_markdown or '（无）'}\n\n"
        "二次增强后的技术方案：\n"
        f"{enhanced_technical_solution_markdown or '（技术方案章节为空）'}\n\n"
        "二次增强 diff：\n"
        f"{enhancement_diff or '（无 diff）'}"
    )


def _improvement_advisor_review_template_text() -> str:
    return """review_markdown 必须使用这个 Markdown 模板：

## 技术方案评审意见

### 总体评价
说明所属技术领域、已具备的核心机制及仍存在的实质问题；没有实质问题时明确说明。

### 技术机制修订点
只列出与该领域相关、会影响正确性、可实施性、机制闭合或技术效果因果链的问题；没有则写“无”。

### 结构与表达修订点
只列出影响理解或实施的章节、术语、顺序和表达问题；没有则写“无”。

### 给 main-agent 的修订指令
按优先级列出可直接执行的正文修改；若前两节均无问题，写“无需继续修改”。"""
