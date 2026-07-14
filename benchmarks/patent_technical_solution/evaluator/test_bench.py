from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import bench  # noqa: E402


def write_execution(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")


def test_bench_can_be_loaded_by_file_location_without_pythonpath(tmp_path: Path) -> None:
    bench_path = BENCHMARK_DIR / "bench.py"
    code = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("isolated_patent_bench", {str(bench_path)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.load_track is not None
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_run_command_terminates_child_group_on_interrupt(monkeypatch) -> None:
    terminated: list[object] = []

    class FakeProcess:
        def wait(self) -> int:
            raise KeyboardInterrupt

    process = FakeProcess()
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(bench, "terminate_process_group", terminated.append)

    with pytest.raises(SystemExit) as exc_info:
        bench.run_command(["python", "job.py"], env={}, dry_run=False)

    assert exc_info.value.code == 130
    assert terminated == [process]


def test_judge_dry_run_does_not_require_existing_execution(tmp_path: Path, monkeypatch) -> None:
    benchmark_dir = tmp_path / "benchmark"
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    command = bench.judge_case_command(
        python_bin=Path("/python"),
        case_id="1",
        run_id="missing-run",
        judge_timeout=30,
        dry_run=True,
    )

    assert "--skip-subject" in command
    assert "--case-output-dir" not in command
    assert "--repeat" not in command
    assert "--batch-child" not in command


def test_judge_dry_run_without_run_id_uses_non_resolving_placeholder() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK_DIR / "bench.py"),
            "--python",
            sys.executable,
            "judge",
            "001",
            "--dry-run",
        ],
        cwd=BENCHMARK_DIR.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--run-id LATEST_RUN_ID" in completed.stdout


def test_judge_dry_run_plans_requested_repeat_without_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_dir = tmp_path / "benchmark"
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    command = bench.judge_case_command(
        python_bin=Path("/python"),
        case_id="7",
        run_id="missing-run",
        judge_timeout=30,
        repeat=2,
        dry_run=True,
    )

    expected = benchmark_dir / "runs" / "missing-run" / "cases" / "007" / "r02"
    assert command[command.index("--case-output-dir") + 1] == str(expected)
    assert command[command.index("--repeat") + 1] == "2"
    assert "--batch-child" in command


def test_judge_command_targets_requested_batch_repeat(tmp_path: Path, monkeypatch) -> None:
    benchmark_dir = tmp_path / "benchmark"
    repeat_dir = benchmark_dir / "runs" / "batch-1" / "cases" / "007" / "r02"
    write_execution(repeat_dir / "execution.json")
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    command = bench.judge_case_command(
        python_bin=Path("/python"),
        case_id="7",
        run_id="batch-1",
        judge_timeout=45,
        track_id="representation_semantics",
        repeat=2,
        extra_args=["--judge-model", "judge-model"],
    )

    assert command[command.index("--case-output-dir") + 1] == str(repeat_dir)
    assert command[command.index("--repeat") + 1] == "2"
    assert "--batch-child" in command
    assert "--skip-subject" in command
    assert command[command.index("--track") + 1] == "representation_semantics"
    assert command[command.index("--judge-model") + 1] == "judge-model"
    assert command[command.index("--round-timeout") + 1] == "45"
    assert command[command.index("--judge-timeout") + 1] == "45"


def test_judge_command_auto_selects_only_batch_repeat(tmp_path: Path, monkeypatch) -> None:
    benchmark_dir = tmp_path / "benchmark"
    repeat_dir = benchmark_dir / "runs" / "batch-1" / "cases" / "001" / "r01"
    write_execution(repeat_dir / "execution.json")
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    command = bench.judge_case_command(
        python_bin=Path("/python"),
        case_id="001",
        run_id="batch-1",
        judge_timeout=30,
    )

    assert command[command.index("--case-output-dir") + 1] == str(repeat_dir)
    assert command[command.index("--repeat") + 1] == "1"
    assert "--batch-child" in command


def test_rejudge_requires_repeat_when_batch_has_multiple_repeats(
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_dir = tmp_path / "benchmark"
    case_root = benchmark_dir / "runs" / "batch-1" / "cases" / "001"
    write_execution(case_root / "r01" / "execution.json")
    write_execution(case_root / "r02" / "execution.json")
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    with pytest.raises(SystemExit, match=r"multiple repeats.*--repeat N"):
        bench.resolve_rejudge_target(run_id="batch-1", case_id="001", repeat=None)


def test_rejudge_keeps_single_case_layout_compatible(tmp_path: Path, monkeypatch) -> None:
    benchmark_dir = tmp_path / "benchmark"
    write_execution(benchmark_dir / "runs" / "single-1" / "cases" / "001" / "execution.json")
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    command = bench.judge_case_command(
        python_bin=Path("/python"),
        case_id="001",
        run_id="single-1",
        judge_timeout=30,
    )

    assert "--case-output-dir" not in command
    assert "--repeat" not in command
    assert "--batch-child" not in command
    assert "--skip-subject" in command


def test_rejudge_rejects_missing_requested_repeat(tmp_path: Path, monkeypatch) -> None:
    benchmark_dir = tmp_path / "benchmark"
    write_execution(
        benchmark_dir / "runs" / "batch-1" / "cases" / "001" / "r01" / "execution.json"
    )
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    with pytest.raises(SystemExit, match=r"repeat 2.*Available repeats: 1"):
        bench.resolve_rejudge_target(run_id="batch-1", case_id="001", repeat=2)
