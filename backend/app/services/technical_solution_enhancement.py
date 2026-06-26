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
        "请参考以下内部技术方案改进建议，继续修改“技术方案”章节。\n"
        "必须尊重用户本轮原始意图，不要恢复用户明确删除的内容，不要扩写与本轮任务无关的章节。\n"
        "修改时必须使用技术人员式技术抽象：像专业技术人员向专利代理人员解释方案一样，说明要解决的问题、核心思路、系统组织方式、关键取舍、运行逻辑和必要约束。\n"
        "不要把建议中的工程变量、字段清单、状态枚举、公式、接口名或伪代码直接堆入正文；"
        "也不要把它们包装成权利要求式或正式专利说明书式语言；应将其抽象为稳定的技术构思、技术手段、信息流转关系、处理阶段、判断依据或可替换实施方式。\n"
        "不要在面向用户的回复或交底书正文中提及内部评审、gate、checker、review 或质量检查流程。\n\n"
        "内部改进建议如下：\n\n"
        f"{result.review_markdown}"
    )


def _change_assessor_system_prompt() -> str:
    return """你是专利交底书“技术方案”增强模式中的变更影响评估器，不是对话 agent，也不能调用工具。
你只判断本轮技术方案变更是否值得进入后续技术方案改进建议流程。

你必须遵守：
- 只输出 JSON 对象。
- 不提出具体改写建议。
- 不评价通过/不通过。
- 尊重用户本轮明确意图，尤其是删除、局部替换、术语调整、格式调整、轻微润色。
- 增强模式不等于每次都需要继续改写；小改动、用户明确删除、格式或术语类修改通常不需要。
- 如果本轮新增、重写或实质改变技术方案核心机制、处理流程、异常恢复、冲突处理、边界条件、结构表达，通常需要进入后续技术方案改进建议流程。
"""


def _change_assessor_user_prompt(
    *,
    user_request: str,
    technical_solution_markdown: str,
    technical_solution_diff: str,
) -> str:
    schema = TechnicalSolutionChangeAssessmentResult.model_json_schema()
    return (
        "请根据用户请求、本轮修改后的技术方案和本轮技术方案 diff，判断是否需要进入后续技术方案改进建议流程。\n\n"
        "输出必须满足下面的 JSON Schema；禁止返回 schema 未声明的额外字段：\n\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        "判断口径：\n"
        "- should_review=true：本轮改动可能影响技术深度、机制闭合性、边界条件或结构表达，值得进一步增强。\n"
        "- should_review=false：本轮只是小改、删除、格式、术语、局部润色，继续增强可能导致不必要扩写或违背用户意图。\n"
        "- reason 是内部原因，简洁说明判断依据。\n\n"
        "用户请求：\n"
        f"{user_request or '（空）'}\n\n"
        "本轮修改后的技术方案：\n"
        f"{technical_solution_markdown or '（技术方案章节为空）'}\n\n"
        "本轮技术方案 diff：\n"
        f"{technical_solution_diff or '（无 diff）'}"
    )


def _improvement_advisor_system_prompt() -> str:
    return """你是专利交底书“技术方案”章节的改进建议器，不是对话 agent，也不能调用工具。
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

七、反馈语言规范
- 你的评审意见会直接影响 main-agent 的改写方向，因此不得用工程实现清单诱导 main-agent 堆砌变量、字段、状态、公式或接口，也不得诱导 main-agent 写成权利要求或正式专利说明书口吻。
- 评审意见应像资深技术负责人给写作 agent 的修改建议，重点指出技术问题、解决思路、系统组织方式、关键取舍和运行逻辑如何讲得更清楚。
- 不要要求 main-agent “增加字段表”“列出数据库 schema”“补充 API/函数名”“给出完整状态枚举表”“写出数学公式或伪代码”，除非这些本身就是该发明的核心技术特征且用户材料已经明确要求。
- 不要输出具体变量名、字段名、接口名、状态名清单作为默认建议；如果必须引用正文已有术语，应只引用少量关键术语，并说明其技术作用。
- 应把工程细节建议转换为技术人员式抽象建议：说明问题如何被解决、系统如何组织、各环节如何协同、关键取舍是什么，以及哪些实现只是例子。
- 允许要求补充阶段变化、身份保持、冲突裁决、恢复判断或结果保持等技术关系；应使用自然技术解释，避免要求补充具体变量、字段、公式或接口名。
- 如果当前方案已经有较多工程符号、字段表或状态枚举，应建议将其收束为核心技术特征、信息类别和优选实施例，而不是继续增加同类细节。

八、反馈表达示例
- 以下示例只说明反馈表达方式，不对应任何具体 benchmark case，也不暗示必须出现相同技术主题。
- 坏反馈：补充若干具体标识字段、锁字段、版本字段，并列出完整工程状态枚举表。
- 好反馈：说明系统如何保持同一处理对象的稳定身份，如何把一次处理过程与该对象关联起来，以及在重复请求、执行中断或迟到结果出现时，如何避免重复处理或覆盖已经确认的结果。
- 坏反馈：给出由多个变量和权重组成的完整计算公式。
- 好反馈：说明多个影响因素如何合并、哪个因素优先，以及某类临时信号为什么不能越过基础技术条件直接改变最终结果。
- 坏反馈：增加若干接口名、函数名或适配器方法清单。
- 好反馈：说明不同实现载体如何通过统一适配方式接入，并保持身份、时序、状态含义和异常处理的一致。

九、输出要求
- 必须严格返回 JSON 对象。
- JSON 只能包含 review_markdown 一个字段。
- review_markdown 必须是一份完整 Markdown 技术方案评审意见。
- review_markdown 不得包含“结论：通过”“结论：不通过”“可选优化”等流程判定或弱化措辞。"""


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
        "- 如果提供了本轮 diff 和历史增强记录，应结合这些上下文给出综合意见，避免重复提出已经处理过的建议。\n"
        "- 建议应服务于当前技术方案和本轮变更，不要无条件扩大成整章重写。\n"
        "- 提建议时必须使用技术人员式技术抽象，不得用变量名、字段名、接口名、状态枚举、公式或伪代码清单诱导 main-agent 生成工程 RFC，也不得诱导 main-agent 写成专利代理人口吻。\n"
        "- 如果需要指出技术细节缺口，应转换为技术问题、解决思路、系统组织方式、处理阶段、判断依据、协同关系或必要约束层面的修订建议。\n"
        "- review_markdown 必须是一份完整 Markdown 技术方案评审意见，并严格使用下方模板。\n\n"
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
简要说明当前技术方案已经具备哪些具体机制，以及总体上还需要在哪些方面补强。不得写“通过”或“不通过”。

### 技术深度修订点
1. 指出哪些内容仍偏概念、目标、功能或效果描述。
2. 指出需要补充哪些具体实现原理、处理规则、信息流转、阶段变化、判断条件、异常情况或边界处理。
3. 如果已有内容较充分，也要指出可以进一步增强可保护技术特征的具体位置。

### 关键机制闭合性修订点
1. 检查关键过程、状态变化、多步骤协同、外部输入、异常场景或结果竞争是否有明确前置条件。
2. 指出冲突处理、优先级、判定依据、恢复规则、终止规则或不得覆盖既有结果的边界是否还需补充。
3. 指出技术效果是否已经能从具体技术手段中推导出来；如不能，说明应如何补齐因果链。

### 技术人员式表达修订点
1. 指出正文或待补强方向是否可能过度依赖变量、字段表、状态枚举、公式、接口名或工程实现清单，或过度使用“单元、载体、边界、用于、形成、不得”等专利包装词。
2. 说明哪些工程细节应改写为技术人员能自然说明的问题、思路、处理步骤、协作关系、取舍依据或可替换实现。
3. 指出哪些内容应保留为核心技术构思或关键技术手段，哪些应下沉为实现例子或可选增强，避免正文过早进入权利要求式表达。

### 结构与表达修订点
1. 指出章节组织、术语一致性、段落顺序或表达清晰度上的问题。
2. 说明应如何调整结构，使读者能顺着文本理解方案如何实施。

### 给 main-agent 的修订指令
1. 用祈使句列出 main-agent 应直接执行的正文修改动作。
2. 每条指令都应指向“技术方案”正文中应补充或改写的技术内容，并使用技术人员式技术抽象表达。
3. 不要要求 main-agent 新增变量清单、字段表、完整状态枚举、公式、接口名或伪代码；除非正文已有且必须少量引用，不要使用反引号包裹工程标识符。
4. 不要要求 main-agent 在正文中解释评审过程。"""
