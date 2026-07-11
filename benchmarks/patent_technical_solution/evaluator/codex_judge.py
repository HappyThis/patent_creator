from __future__ import annotations

import asyncio
import contextlib
import json
import traceback
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator


JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "total_score", "evaluation_report"],
    "properties": {
        "status": {"type": "string", "enum": ["scored"]},
        "total_score": {"type": "number", "minimum": 0, "maximum": 100},
        "evaluation_report": {"type": "string", "minLength": 1},
    },
}


class CodexJudgeTimeout(TimeoutError):
    pass


@dataclass(slots=True)
class JudgeRunResult:
    conclusion: dict[str, Any]
    thread_id: str
    turn_id: str
    model: str
    provider: str
    reasoning_effort: str
    sdk_version: str
    runtime_version: str | None
    turn_started_at: str | None
    turn_finished_at: str | None
    turn_duration_ms: int | None
    usage: dict[str, Any] | None


@dataclass(slots=True)
class _CollectedTurn:
    final_response: str
    started_at: int | None
    completed_at: int | None
    duration_ms: int | None
    usage: dict[str, Any] | None


async def run_codex_judge(
    *,
    case_id: str,
    case_run_dir: Path,
    source_case_dir: Path,
    benchmark_dir: Path,
    logs_dir: Path,
    model: str | None,
    provider: str,
    reasoning_effort: str,
    service_tier: str | None,
    codex_bin: str | None,
    timeout_seconds: int,
) -> JudgeRunResult:
    logs_dir.mkdir(parents=True, exist_ok=True)
    schema_path = logs_dir / "schema.json"
    prompt_path = logs_dir / "prompt.md"
    events_path = logs_dir / "events.jsonl"
    stderr_path = logs_dir / "stderr.log"
    prompt = build_judge_prompt(
        case_id=case_id,
        case_run_dir=case_run_dir,
        source_case_dir=source_case_dir,
        benchmark_dir=benchmark_dir,
    )
    schema_path.write_text(json.dumps(JUDGE_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt_path.write_text(prompt, encoding="utf-8")
    events_path.write_text("", encoding="utf-8")
    stderr_path.unlink(missing_ok=True)

    try:
        sdk = _load_sdk()
        config = sdk.CodexConfig(cwd=str(case_run_dir), codex_bin=codex_bin)
        async with sdk.AsyncCodex(config) as codex:
            resolved_model = model or await _default_model(codex)
            metadata = codex.metadata
            runtime_version = _runtime_version(metadata)
            thread = await codex.thread_start(
                approval_mode=sdk.ApprovalMode.deny_all,
                cwd=str(case_run_dir),
                ephemeral=True,
                model=resolved_model,
                model_provider=provider or None,
                sandbox=sdk.Sandbox.read_only,
            )
            turn_handle = await thread.turn(
                prompt,
                approval_mode=sdk.ApprovalMode.deny_all,
                cwd=str(case_run_dir),
                effort=sdk.ReasoningEffort(reasoning_effort),
                model=resolved_model,
                output_schema=JUDGE_SCHEMA,
                sandbox=sdk.Sandbox.read_only,
                service_tier=service_tier,
            )
            stream = turn_handle.stream()
            try:
                collected = await asyncio.wait_for(
                    _collect_turn(stream, turn_id=turn_handle.id, events_path=events_path),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                with contextlib.suppress(Exception):
                    await turn_handle.interrupt()
                raise CodexJudgeTimeout(f"Codex judge 超时：{timeout_seconds} 秒。") from exc
            finally:
                with contextlib.suppress(Exception):
                    await stream.aclose()

            conclusion = validate_conclusion(collected.final_response)
            return JudgeRunResult(
                conclusion=conclusion,
                thread_id=thread.id,
                turn_id=turn_handle.id,
                model=resolved_model,
                provider=provider,
                reasoning_effort=reasoning_effort,
                sdk_version=sdk.version,
                runtime_version=runtime_version,
                turn_started_at=_unix_seconds_to_iso(collected.started_at),
                turn_finished_at=_unix_seconds_to_iso(collected.completed_at),
                turn_duration_ms=collected.duration_ms,
                usage=collected.usage,
            )
    except BaseException:
        stderr_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


def build_judge_prompt(
    *,
    case_id: str,
    case_run_dir: Path,
    source_case_dir: Path,
    benchmark_dir: Path,
) -> str:
    return f"""你是 Codex-as-judge，负责对专利交底书中的最终“技术方案”进行综合评价。

当前工作目录是本次 Case 的运行目录：
`{case_run_dir.resolve()}`

请先读取以下规则和输入，不要依赖本提示中的摘要替代原文件：

- 评价规则：`{(benchmark_dir / 'judge.md').resolve()}`
- Agent 任务规则：`{(benchmark_dir / 'runner.md').resolve()}`
- Case 需求：`{(source_case_dir / 'request.md').resolve()}`
- Case 隐藏参考方案：`{(source_case_dir / 'reference_solution.md').resolve()}`
- Case 评分标尺：`{(source_case_dir / 'rubric.md').resolve()}`

运行目录中的材料关系如下：

- `prepared_environment/` 是 Agent 执行前的冻结事实环境，其中 `project_snapshot/` 是原始项目快照。
- `subject/` 是 Agent 执行后的完整原始工作区，也是唯一被评价对象。
- 最终交底书位于 `subject/data/projects/*/disclosure.json`。
- Agent 会话事件位于 `subject/data/projects/*/sessions/*.jsonl`，仅用于理解运行事实，不要把聊天回复或工具轨迹当成技术方案正文。
- 配图位于 `subject/data/projects/*/assets/figures/`。只有在 disclosure 中实际引用 figure 时，才按需读取对应 `figure.json`、当前 revision 的 `diagram.drawio` 或 `render.png`。
- 公式直接保存在 disclosure 的 formula block 中，没有额外公式文件。

请自行搜索并读取上述文件，定位 Case `{case_id}` 的最终“技术方案”章节。不要要求评估程序替你提取章节、复制图片或生成清单。

按照评价规则给出一个 0 到 100 的总分和 Markdown 评价报告。报告必须说明主要优点、实际扣分原因，以及公式和配图在本方案中的适用性与实际表达质量。不要评价图片的视觉美观、排版精细度或渲染质量。

最终只返回符合 output schema 的 JSON。
"""


def validate_conclusion(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex judge 未返回合法 JSON。") from exc
    if not isinstance(value, dict):
        raise ValueError("Codex judge 返回值必须是 JSON object。")
    if set(value) != {"status", "total_score", "evaluation_report"}:
        raise ValueError("Codex judge 返回字段不符合 schema。")
    if value.get("status") != "scored":
        raise ValueError("Codex judge status 必须为 scored。")
    score = value.get("total_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 100:
        raise ValueError("Codex judge total_score 必须在 0 到 100 之间。")
    report = value.get("evaluation_report")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("Codex judge evaluation_report 不能为空。")
    return {
        "status": "scored",
        "total_score": score,
        "evaluation_report": report.strip(),
    }


async def _collect_turn(
    stream: AsyncIterator[Any],
    *,
    turn_id: str,
    events_path: Path,
) -> _CollectedTurn:
    completed_turn: Any | None = None
    items: list[Any] = []
    usage: Any | None = None
    with events_path.open("a", encoding="utf-8") as handle:
        async for event in stream:
            handle.write(json.dumps(_notification_json(event), ensure_ascii=False) + "\n")
            handle.flush()
            payload = getattr(event, "payload", None)
            event_turn_id = getattr(payload, "turn_id", None)
            if event.method == "item/completed" and event_turn_id == turn_id:
                items.append(getattr(payload, "item", None))
            elif event.method == "thread/tokenUsage/updated" and event_turn_id == turn_id:
                usage = getattr(payload, "token_usage", None)
            elif event.method == "turn/completed":
                turn = getattr(payload, "turn", None)
                if getattr(turn, "id", None) == turn_id:
                    completed_turn = turn

    if completed_turn is None:
        raise RuntimeError("Codex turn 未收到 turn/completed 事件。")
    status = _enum_value(getattr(completed_turn, "status", None))
    if status == "failed":
        error = getattr(completed_turn, "error", None)
        message = getattr(error, "message", None) or "Codex turn failed。"
        raise RuntimeError(str(message))
    if not items:
        items = list(getattr(completed_turn, "items", None) or [])
    final_response = _final_response(items)
    if not final_response:
        raise RuntimeError("Codex turn 未返回 final answer。")
    return _CollectedTurn(
        final_response=final_response,
        started_at=getattr(completed_turn, "started_at", None),
        completed_at=getattr(completed_turn, "completed_at", None),
        duration_ms=getattr(completed_turn, "duration_ms", None),
        usage=_jsonable(usage) if usage is not None else None,
    )


def _final_response(items: list[Any]) -> str | None:
    fallback: str | None = None
    for item in reversed(items):
        value = _jsonable(item)
        if not isinstance(value, dict) or value.get("type") != "agentMessage":
            continue
        text = value.get("text")
        if not isinstance(text, str):
            continue
        if value.get("phase") == "final_answer":
            return text
        if value.get("phase") is None and fallback is None:
            fallback = text
    return fallback


async def _default_model(codex: Any) -> str:
    listing = await codex.models()
    models = getattr(listing, "data", [])
    default_model = next((item for item in models if getattr(item, "is_default", False)), None)
    if default_model is None:
        raise RuntimeError("Codex 未返回默认模型，必须设置 BENCHMARK_JUDGE_MODEL。")
    value = getattr(default_model, "model", None) or getattr(default_model, "id", None)
    if not isinstance(value, str) or not value:
        raise RuntimeError("Codex 默认模型标识无效。")
    return value


def _load_sdk() -> SimpleNamespace:
    try:
        import openai_codex
        from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
        from openai_codex.api import ReasoningEffort
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Benchmark 依赖 openai-codex；请同步 backend 的 benchmark dependency group。"
        ) from exc
    return SimpleNamespace(
        AsyncCodex=AsyncCodex,
        CodexConfig=CodexConfig,
        ApprovalMode=ApprovalMode,
        Sandbox=Sandbox,
        ReasoningEffort=ReasoningEffort,
        version=openai_codex.__version__,
    )


def _runtime_version(metadata: Any) -> str | None:
    server_info = getattr(metadata, "serverInfo", None)
    value = getattr(server_info, "version", None)
    return str(value) if value else None


def _notification_json(event: Any) -> dict[str, Any]:
    return {
        "method": str(getattr(event, "method", "unknown")),
        "payload": _jsonable(getattr(event, "payload", None)),
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _enum_value(value: Any) -> str | None:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value) if value is not None else None


def _unix_seconds_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
