from __future__ import annotations

import json
import subprocess
from pathlib import Path


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
            "additionalProperties": {"type": "number", "minimum": 0},
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "missing_key_mechanisms": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "score_rationale": {"type": "string"},
    },
}


def run_codex_judge(
    *,
    prepared_repo: Path,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
    output_dir: Path,
    codex_bin: str = "codex",
    timeout_seconds: int = 1800,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_path = output_dir / "judge_schema.json"
    prompt_path = output_dir / "judge_prompt.md"
    output_path = output_dir / "judge_output.json"
    stdout_path = output_dir / "codex_judge_stdout.txt"
    stderr_path = output_dir / "codex_judge_stderr.txt"

    schema_path.write_text(json.dumps(JUDGE_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt_path.write_text(
        build_judge_prompt(
            request_md=request_md,
            evaluated_artifact_md=evaluated_artifact_md,
            judge_md=judge_md,
            rubric_md=rubric_md,
            reference_solution_md=reference_solution_md,
        ),
        encoding="utf-8",
    )

    command = [
        codex_bin,
        "exec",
        "--cd",
        str(prepared_repo),
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    completed = subprocess.run(
        command,
        input=prompt_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Codex judge 执行失败。"
        raise RuntimeError(message)
    if not output_path.exists():
        raise RuntimeError("Codex judge 未生成 judge_output.json。")
    return json.loads(output_path.read_text(encoding="utf-8"))


def build_judge_prompt(
    *,
    request_md: str,
    evaluated_artifact_md: str,
    judge_md: str,
    rubric_md: str,
    reference_solution_md: str,
) -> str:
    return f"""你是 Codex-as-judge，负责评估软件专利技术方案质量。

你当前工作目录是被评测项目的固定快照。请先按需阅读当前项目源码、文档和测试，再基于评分说明、测试项需求、被评估技术方案、隐藏参考方案和 rubric 打分。

注意：
- 被评估对象只有 evaluated_artifact，也就是系统写入 disclosure 技术方案章节的内容。
- 不要评估聊天回复、工具轨迹、子 agent proposal 或文档 diff。
- 参考方案不是唯一答案；若被评估方案不同但项目依据充分、技术机制合理，不应机械扣分。
- 对看似合理但当前项目环境中没有依据的具体源码事实、类名、接口或能力，应列入 unsupported_claims 并酌情扣分。
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
