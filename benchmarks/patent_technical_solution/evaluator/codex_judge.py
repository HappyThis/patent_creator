from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

try:
    from .process_utils import terminate_process_group  # type: ignore[import-not-found]
except ImportError:
    from process_utils import terminate_process_group  # type: ignore[no-redef]


JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "total_score",
        "dimension_scores",
        "strengths",
        "weaknesses",
        "missing_key_mechanisms",
        "unsupported_claims",
        "score_rationale",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["scored"]},
        "total_score": {"type": "number", "minimum": 0, "maximum": 100},
        "dimension_scores": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "technical_problem",
                "core_concept",
                "technical_depth",
                "mechanism_coherence",
                "technical_effect",
                "feasibility",
                "disclosure_fit",
            ],
            "properties": {
                "technical_problem": {"type": "number", "minimum": 0, "maximum": 15},
                "core_concept": {"type": "number", "minimum": 0, "maximum": 20},
                "technical_depth": {"type": "number", "minimum": 0, "maximum": 25},
                "mechanism_coherence": {"type": "number", "minimum": 0, "maximum": 15},
                "technical_effect": {"type": "number", "minimum": 0, "maximum": 10},
                "feasibility": {"type": "number", "minimum": 0, "maximum": 10},
                "disclosure_fit": {"type": "number", "minimum": 0, "maximum": 5},
            },
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "missing_key_mechanisms": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "score_rationale": {"type": "string"},
    },
}

FIGURE_JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "mode",
        "total_score",
        "figure_score",
        "figures_reviewed",
        "visual_quality_scores",
        "strengths",
        "weaknesses",
        "missing_visual_mechanisms",
        "shape_issues",
        "layout_issues",
        "connector_issues",
        "text_issues",
        "score_caps_applied",
        "score_rationale",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["scored"]},
        "mode": {"type": "string", "enum": ["figure"]},
        "total_score": {"type": "number", "minimum": 0, "maximum": 100},
        "figure_score": {"type": "number", "minimum": 0, "maximum": 100},
        "figures_reviewed": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "assessment"],
                "properties": {
                    "path": {"type": "string"},
                    "assessment": {"type": "string"},
                },
            },
        },
        "visual_quality_scores": {
            "type": "object",
            "additionalProperties": False,
            "required": ["shape_score", "layout_score", "connector_score", "text_score"],
            "properties": {
                "shape_score": {"type": "number", "minimum": 0, "maximum": 100},
                "layout_score": {"type": "number", "minimum": 0, "maximum": 100},
                "connector_score": {"type": "number", "minimum": 0, "maximum": 100},
                "text_score": {"type": "number", "minimum": 0, "maximum": 100},
            },
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "missing_visual_mechanisms": {"type": "array", "items": {"type": "string"}},
        "shape_issues": {"type": "array", "items": {"type": "string"}},
        "layout_issues": {"type": "array", "items": {"type": "string"}},
        "connector_issues": {"type": "array", "items": {"type": "string"}},
        "text_issues": {"type": "array", "items": {"type": "string"}},
        "score_caps_applied": {"type": "array", "items": {"type": "string"}},
        "score_rationale": {"type": "string"},
    },
}

COMBINED_JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "mode",
        "total_score",
        "solution_score",
        "figure_score",
        "integration_score",
        "visual_quality_scores",
        "strengths",
        "weaknesses",
        "missing_key_mechanisms",
        "figure_issues",
        "shape_issues",
        "layout_issues",
        "connector_issues",
        "text_issues",
        "integration_issues",
        "unsupported_claims",
        "score_caps_applied",
        "score_rationale",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["scored"]},
        "mode": {"type": "string", "enum": ["combined"]},
        "total_score": {"type": "number", "minimum": 0, "maximum": 100},
        "solution_score": {"type": "number", "minimum": 0, "maximum": 100},
        "figure_score": {"type": "number", "minimum": 0, "maximum": 100},
        "integration_score": {"type": "number", "minimum": 0, "maximum": 100},
        "visual_quality_scores": {
            "type": "object",
            "additionalProperties": False,
            "required": ["shape_score", "layout_score", "connector_score", "text_score"],
            "properties": {
                "shape_score": {"type": "number", "minimum": 0, "maximum": 100},
                "layout_score": {"type": "number", "minimum": 0, "maximum": 100},
                "connector_score": {"type": "number", "minimum": 0, "maximum": 100},
                "text_score": {"type": "number", "minimum": 0, "maximum": 100},
            },
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "missing_key_mechanisms": {"type": "array", "items": {"type": "string"}},
        "figure_issues": {"type": "array", "items": {"type": "string"}},
        "shape_issues": {"type": "array", "items": {"type": "string"}},
        "layout_issues": {"type": "array", "items": {"type": "string"}},
        "connector_issues": {"type": "array", "items": {"type": "string"}},
        "text_issues": {"type": "array", "items": {"type": "string"}},
        "integration_issues": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "score_caps_applied": {"type": "array", "items": {"type": "string"}},
        "score_rationale": {"type": "string"},
    },
}


def run_codex_judge(
    *,
    mode: str = "solution",
    working_dir: Path,
    case_run_dir: Path | None = None,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
    output_dir: Path,
    codex_bin: str = "codex",
    timeout_seconds: int = 1800,
    progress: Callable[..., None] | None = None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_path = output_dir / "judge_schema.json"
    prompt_path = output_dir / "judge_prompt.md"
    output_path = output_dir / "judge_output.json"
    stdout_path = output_dir / "codex_judge_stdout.txt"
    stderr_path = output_dir / "codex_judge_stderr.txt"
    events_path = output_dir / "codex_judge_events.jsonl"

    schema_path.write_text(json.dumps(schema_for_mode(mode), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt_path.write_text(
        build_judge_prompt(
            mode=mode,
            case_run_dir=case_run_dir,
            request_md=request_md,
            evaluated_artifact_md=evaluated_artifact_md,
            judge_md=judge_md,
            rubric_md=rubric_md,
            reference_solution_md=reference_solution_md,
        ),
        encoding="utf-8",
    )

    command = [
        resolve_codex_bin(codex_bin),
        "exec",
        "--json",
        "--cd",
        str(working_dir),
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=stream_codex_stdout,
        args=(process.stdout, stdout_path, events_path, stdout_chunks, progress),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stream_text_pipe,
        args=(process.stderr, stderr_path, stderr_chunks),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    if process.stdin is not None:
        process.stdin.write(prompt_path.read_text(encoding="utf-8"))
        process.stdin.close()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        raise TimeoutError(f"Codex judge 超时：{timeout_seconds} 秒。")
    except BaseException:
        terminate_process_group(process)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        raise
    stdout_thread.join()
    stderr_thread.join()
    if returncode != 0:
        stderr = "".join(stderr_chunks)
        stdout = "".join(stdout_chunks)
        message = stderr.strip() or stdout.strip() or "Codex judge 执行失败。"
        raise RuntimeError(message)
    if not output_path.exists():
        raise RuntimeError("Codex judge 未生成 judge_output.json。")
    return json.loads(output_path.read_text(encoding="utf-8"))


def stream_codex_stdout(
    pipe: Any,
    stdout_path: Path,
    events_path: Path,
    chunks: list[str],
    progress: Callable[..., None] | None,
) -> None:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, events_path.open("w", encoding="utf-8") as events_handle:
        if pipe is None:
            return
        for line in pipe:
            chunks.append(line)
            stdout_handle.write(line)
            stdout_handle.flush()
            events_handle.write(line)
            events_handle.flush()
            emit_codex_progress(line, progress)


def stream_text_pipe(pipe: Any, path: Path, chunks: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if pipe is None:
            return
        for line in pipe:
            chunks.append(line)
            handle.write(line)
            handle.flush()


def emit_codex_progress(line: str, progress: Callable[..., None] | None) -> None:
    if progress is None:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    event_type = str(event.get("type") or "unknown")
    if event_type == "thread.started":
        progress("judge_codex", "codex thread started", thread_id=event.get("thread_id"))
    elif event_type == "turn.started":
        progress("judge_codex", "codex turn started")
    elif event_type in {"item.started", "item.completed"}:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        progress(
            "judge_codex",
            f"codex {event_type}",
            item_type=item.get("type"),
            item_id=item.get("id"),
        )
    elif event_type == "turn.completed":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        progress(
            "judge_codex",
            "codex turn completed",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_output_tokens=usage.get("reasoning_output_tokens"),
        )


def resolve_codex_bin(codex_bin: str) -> str:
    command_path = Path(codex_bin)
    if command_path.parent != Path("."):
        return codex_bin

    candidates = [codex_bin]
    if is_windows() and not command_path.suffix:
        candidates = [f"{codex_bin}.cmd", f"{codex_bin}.bat", f"{codex_bin}.exe", codex_bin]

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return codex_bin


def is_windows() -> bool:
    return os.name == "nt"


def schema_for_mode(mode: str) -> dict:
    if mode == "solution":
        return JUDGE_SCHEMA
    if mode == "figure":
        return FIGURE_JUDGE_SCHEMA
    if mode == "combined":
        return COMBINED_JUDGE_SCHEMA
    raise ValueError(f"unknown judge mode: {mode}")


def build_judge_prompt(
    *,
    mode: str,
    case_run_dir: Path | None,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
) -> str:
    if mode == "solution":
        return build_solution_judge_prompt(
            request_md=request_md,
            evaluated_artifact_md=evaluated_artifact_md,
            judge_md=judge_md,
            rubric_md=rubric_md,
            reference_solution_md=reference_solution_md,
        )
    if mode == "figure":
        return build_figure_judge_prompt(
            case_run_dir=case_run_dir,
            request_md=request_md,
            evaluated_artifact_md=evaluated_artifact_md,
            judge_md=judge_md,
            rubric_md=rubric_md,
            reference_solution_md=reference_solution_md,
        )
    if mode == "combined":
        return build_combined_judge_prompt(
            case_run_dir=case_run_dir,
            request_md=request_md,
            evaluated_artifact_md=evaluated_artifact_md,
            judge_md=judge_md,
            rubric_md=rubric_md,
            reference_solution_md=reference_solution_md,
        )
    raise ValueError(f"unknown judge mode: {mode}")


def build_solution_judge_prompt(
    *,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
) -> str:
    return f"""你是 Codex-as-judge，负责评估专利交底书“技术方案”章节的质量。

你当前工作目录是该 case 提供给被评测 agent 的探索环境。请按需阅读环境中的材料和项目快照，再基于评分说明、测试项需求、被评估技术方案、隐藏参考方案和 rubric 打分。

注意：
- 被评估对象只有 evaluated_artifact，也就是系统写入 disclosure 技术方案章节的内容。
- 不要评估聊天回复、工具轨迹、子 agent proposal 或文档 diff。
- 参考方案不是唯一答案；若被评估方案不同但技术构思合理、机制深度相当、能解决同一技术问题，不应机械扣分。
- 不要求技术方案写出实现路径、验证材料、接口清单或排期任务；这些内容只有在其本身构成技术手段时才可作为加分依据。
- 对看似合理但材料中没有依据的“已有事实、当前结构、既有能力、具体字段或接口”声明，应列入 unsupported_claims 并酌情扣分。作为新增方案提出的机制不属于 unsupported_claim，但不能冒充已有事实。
- 必须执行 judge.md 和 rubric.md 中的分数校准规则、明确扣分项和分数上限；如果总分超过上限倾向，请在 score_rationale 中解释为什么不适用上限。
- 必须输出符合 JSON schema 的 JSON，不要输出 Markdown。

<judge.md>
{judge_md}
</judge.md>

<request.md>
{request_md}
</request.md>

<evaluated_artifact.md>
{evaluated_artifact_md}
</evaluated_artifact.md>

<reference_solution.md>
{reference_solution_md}
</reference_solution.md>

<rubric.md>
{rubric_md}
</rubric.md>
"""


def build_figure_judge_prompt(
    *,
    case_run_dir: Path | None,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
) -> str:
    case_run_line = f"当前 case run 根目录：{case_run_dir}" if case_run_dir else "当前工作目录是 case run 根目录。"
    return f"""你是 Codex-as-judge，负责评估被测 agent 生成的技术示意图质量。

{case_run_line}

你当前工作目录是 case run 根目录，而不是单纯的项目快照目录。目录含义如下：

- prepared_environment/：被测 agent 可见的探索环境，通常包含 project_snapshot/。你可以按需读取它来确认项目事实和技术对象。
- subject/：被测 agent 的实际运行工作区。请自行查找 subject/data/projects/*/assets/figures/ 下的 figure.json、diagram.html 和 render.png，并实际查看 render.png 后评分。

注意：
- 被评估对象是 subject/ 中由 figure_kit 生成的附图资产；不要评价你自己临时生成的图片。
- 不要因为图片数量达标就给高分；如果没有找到图片、图片无法打开、图片空白、与需求无关或只是模板化框图，应明显扣分。
- evaluated_artifact.md 只是可选辅助文本；figure mode 的核心评分对象是图片是否帮助理解本 case 的技术结构、流程、状态、边界、数据流或协同机制。
- 可以阅读 diagram.html 辅助理解，但最终必须基于 render.png 的可视结果判断图是否清楚。
- 不以某一种固定模板、固定布局或固定配色作为硬性要求；重点评价图是否准确、清晰、有机制表达，而不是是否服从某个格式。
- 必须分别评价形状、布局、箭头/连接线、文字四类视觉质量；连接线穿越节点、长距离跨区、虚实线语义不明、文字过密或形状层级混乱，都应作为实质扣分依据。
- 必须输出符合 JSON schema 的 JSON，不要输出 Markdown。

<figure_judge.md>
{judge_md}
</figure_judge.md>

<request.md>
{request_md}
</request.md>

<optional_evaluated_artifact.md>
{evaluated_artifact_md}
</optional_evaluated_artifact.md>

<reference_solution.md>
{reference_solution_md}
</reference_solution.md>

<rubric.md>
{rubric_md}
</rubric.md>
"""


def build_combined_judge_prompt(
    *,
    case_run_dir: Path | None,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
) -> str:
    case_run_line = f"当前 case run 根目录：{case_run_dir}" if case_run_dir else "当前工作目录是 case run 根目录。"
    return f"""你是 Codex-as-judge，负责评估一份带附图的专利交底书“技术方案”产物。

{case_run_line}

你当前工作目录是 case run 根目录。目录含义如下：

- prepared_environment/：被测 agent 可见的探索环境，通常包含 project_snapshot/。你可以按需读取它来确认项目事实和技术对象。
- subject/：被测 agent 的实际运行工作区。请自行查找 subject/data/projects/*/assets/figures/ 下的 figure.json、diagram.html 和 render.png，并实际查看 render.png 后评价附图。

注意：
- 技术方案正文质量以 prompt 中注入的 evaluated_artifact.md 为准；不要用聊天回复、工具轨迹或完整 disclosure 中其他章节替代它。
- 附图质量和文图一致性需要你自行查看 subject/ 下的图片资产和必要的 disclosure.json。
- 参考方案不是唯一答案；若被评估方案不同但技术构思合理、机制深度相当、能解决同一技术问题，不应机械扣分。
- 对看似合理但材料中没有依据的“已有事实、当前结构、既有能力、具体字段或接口”声明，应列入 unsupported_claims 并酌情扣分。
- total_score 应按 combined_judge.md 的权重综合 solution_score、figure_score 和 integration_score。
- figure_score 必须分别考虑形状、布局、箭头/连接线、文字四类视觉质量；正文质量高不能抵消图片主阅读路径混乱、连接线语义不清或文字过密。
- 必须输出符合 JSON schema 的 JSON，不要输出 Markdown。

<combined_judge.md>
{judge_md}
</combined_judge.md>

<request.md>
{request_md}
</request.md>

<evaluated_artifact.md>
{evaluated_artifact_md}
</evaluated_artifact.md>

<reference_solution.md>
{reference_solution_md}
</reference_solution.md>

<rubric.md>
{rubric_md}
</rubric.md>
"""
