from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from ..core import Settings
from ..domain.disclosure import section_title_text, section_to_markdown

TECHNICAL_SOLUTION_TITLE = "技术方案"
TECHNICAL_SOLUTION_CHECK_TIMEOUT_SECONDS = 180.0
TECHNICAL_SOLUTION_CHECK_MAX_ATTEMPTS = 2


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

    gate_pass: StrictBool = Field(description="流程控制字段。通过为 true，不通过为 false。")
    review_markdown: str = Field(min_length=1, description="Markdown 格式的评审意见，供 main-agent 参考。")
    reason: str = Field(min_length=1, description="详细说明为什么给出该 gate 结论。")

    @field_validator("review_markdown", "reason")
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
    def __init__(self, llm_client: SupportsTechnicalSolutionCheck, settings: Settings) -> None:
        self.llm_client = llm_client
        self.settings = settings

    async def check(
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
                temperature=0.1,
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
        "系统完成了一次“技术方案”质量检查。以下检查意见供你参考。\n\n"
        "你需要自行判断是否继续处理：\n"
        "- 如果检查意见指出的缺口确实影响当前交底书质量，可以继续读取必要上下文并修改“技术方案”。\n"
        "- 如果你判断当前内容已经足够，或者检查意见不适用于当前材料条件，可以直接说明本轮已完成。\n"
        "- 不要把检查过程、检查意见、反思过程或“系统检查”这类表述写入交底书正文。\n\n"
        "检查意见如下：\n\n"
        f"{result.review_markdown}\n\n"
        "检查原因：\n\n"
        f"{result.reason}"
    )


def _checker_system_prompt() -> str:
    return (
        "你是专利交底书“技术方案”章节的质量检查器，不是对话 agent，也不能调用工具。"
        "你只根据用户提供的“技术方案”章节正文做只读评价。\n"
        "评价范围只包括：技术深度、交底书语气、结构清晰度。\n"
        "技术深度重点检查技术问题、技术手段、组成要素、输入输出、数据流、状态变化、触发条件、"
        "处理规则、模块协同关系、异常分支、边界条件、可选实施方式和技术效果闭合。\n"
        "交底书语气重点检查是否像技术说明，避免产品功能介绍、需求文档、项目实施计划、营销文案，"
        "也避免提前写成正式权利要求或保护范围设计。\n"
        "结构清晰度重点检查术语一致性、章节层级、段落职责、主线递进、长段堆叠和逻辑跳跃。\n"
        "gate 判定必须偏严格：只有技术方案已经足以让专利代理人员理解方案如何实施、如何成立、"
        "如何区别于常规方案，并且关键技术机制已经闭合时，才 gate_pass=true。\n"
        "如果存在下列任一实质缺口，应 gate_pass=false：技术问题和技术手段没有对应；核心组成要素过于抽象；"
        "输入输出、数据流、状态变化或触发条件缺失；处理规则、模块协同、异常分支或边界条件没有展开；"
        "技术效果没有闭合到具体技术手段；结构主线不清导致代理人难以直接据此撰写。\n"
        "不要因为轻微润色、个别措辞或非关键表达问题判为不通过；但不要把“总体方向正确”误判为通过，"
        "只要关键机制仍停留在概念层、工程口号层或缺少可实施链条，就应 gate_pass=false。\n"
        "必须严格返回 JSON 对象，且只包含 gate_pass、review_markdown、reason 三个字段。"
    )


def _checker_user_prompt(technical_solution_markdown: str) -> str:
    return (
        "请检查下面的“技术方案”章节，并严格返回 JSON：\n\n"
        f"{_checker_output_schema_text()}\n\n"
        "检查要求：\n"
        "- 技术深度：检查技术问题、技术手段、组成要素、输入输出、数据流、状态变化、触发条件、处理规则、模块协同关系、异常分支、边界条件、可选实施方式和技术效果闭合是否清楚。\n"
        "- 语气：检查是否符合交底书技术说明，不像产品介绍、需求文档、项目实施计划、营销文案，也不提前写成正式权利要求或保护范围设计。\n"
        "- 结构：检查章节层级、段落职责、术语一致性、主线递进是否清晰，是否存在长段堆叠、逻辑跳跃或主线不清。\n\n"
        "gate 判定要求：\n"
        "- gate_pass=true：仅当技术问题、技术手段、关键组成要素、输入输出、数据流/状态变化、处理规则、异常边界和技术效果已经形成可实施闭合链条。\n"
        "- gate_pass=false：只要关键机制缺失、机制只停留在抽象描述、缺少状态/数据/边界闭合，或内容会明显拉低技术深度与可保护性。\n"
        "- review_markdown 必须给 main-agent 可执行的修改方向，优先指出应补足的技术手段、组成要素、状态变化、数据流、异常分支、边界条件或技术效果闭合；如果通过，也要简要说明为什么已经足够。\n\n"
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
