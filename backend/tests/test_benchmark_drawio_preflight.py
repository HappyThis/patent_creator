from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_PATH = REPO_ROOT / "benchmarks" / "patent_technical_solution" / "bench.py"


def load_bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("patent_bench_drawio_preflight_test", BENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("command", ["run", "subject", "batch"])
def test_subject_commands_require_drawio_preflight(command: str) -> None:
    bench = load_bench()

    assert bench.command_requires_drawio_preflight(command) is True


@pytest.mark.parametrize("command", ["judge", "list", "status"])
def test_read_only_commands_skip_drawio_preflight(command: str) -> None:
    bench = load_bench()

    assert bench.command_requires_drawio_preflight(command) is False


def test_benchmark_dry_run_prints_preflight_before_subject_command() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BENCH_PATH),
            "--python",
            sys.executable,
            "subject",
            "001",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.startswith("+ ")]
    assert len(lines) == 2
    assert "drawio_render_preflight.py" in lines[0]
    assert "evaluator" in lines[1] and "run_case.py" in lines[1]


def test_benchmark_judge_dry_run_does_not_print_preflight() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BENCH_PATH),
            "--python",
            sys.executable,
            "judge",
            "001",
            "--run-id",
            "existing-run",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "drawio_render_preflight.py" not in completed.stdout
    assert "run_case.py" in completed.stdout


@pytest.mark.parametrize(
    ("command_args", "target_script"),
    [
        (["run", "001"], "run_case.py"),
        (["judge", "001", "--run-id", "existing-run"], "run_case.py"),
        (["batch", "001"], "run_all.py"),
    ],
)
def test_judge_overrides_are_forwarded_by_public_cli(command_args: list[str], target_script: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BENCH_PATH),
            "--python",
            sys.executable,
            *command_args,
            "--judge-model",
            "judge-model",
            "--judge-provider",
            "judge-provider",
            "--judge-reasoning-effort",
            "xhigh",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    target_command = next(
        line for line in completed.stdout.splitlines() if line.startswith("+ ") and target_script in line
    )
    assert "--judge-model judge-model" in target_command
    assert "--judge-provider judge-provider" in target_command
    assert "--judge-reasoning-effort xhigh" in target_command
    assert "codex-bin" not in target_command


def test_subject_cli_does_not_expose_judge_overrides() -> None:
    completed = subprocess.run(
        [sys.executable, str(BENCH_PATH), "subject", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--judge-model" not in completed.stdout
    assert "--judge-provider" not in completed.stdout
    assert "--judge-reasoning-effort" not in completed.stdout
    assert "codex-bin" not in completed.stdout
