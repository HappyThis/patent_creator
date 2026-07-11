from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

EVALUATOR_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "software_patent_solution_github" / "evaluator"
PATENT_BENCH_PATH = Path(__file__).resolve().parents[2] / "benchmarks" / "patent_technical_solution" / "bench.py"


def load_codex_judge() -> Any:
    if str(EVALUATOR_DIR) not in sys.path:
        sys.path.insert(0, str(EVALUATOR_DIR))
    return importlib.import_module("codex_judge")


codex_judge: Any = load_codex_judge()


def test_resolve_codex_bin_prefers_windows_cmd(monkeypatch) -> None:
    def fake_which(command: str) -> str | None:
        if command == "codex.cmd":
            return r"C:\Users\yang\AppData\Roaming\npm\codex.cmd"
        return None

    monkeypatch.setattr(codex_judge, "is_windows", lambda: True)
    monkeypatch.setattr(codex_judge.shutil, "which", fake_which)

    assert codex_judge.resolve_codex_bin("codex") == r"C:\Users\yang\AppData\Roaming\npm\codex.cmd"


def test_resolve_codex_bin_keeps_explicit_path() -> None:
    assert codex_judge.resolve_codex_bin("/opt/codex/bin/codex") == "/opt/codex/bin/codex"


def test_latest_run_for_case_only_accepts_v2_execution(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("patent_technical_solution_bench_test", PATENT_BENCH_PATH)
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    benchmark_dir = tmp_path / "benchmark"
    runs_dir = benchmark_dir / "runs"

    old_case_dir = runs_dir / "historical-run" / "cases" / "001"
    old_case_dir.mkdir(parents=True)
    old_artifact = old_case_dir / "evaluated_artifact.md"
    old_artifact.write_text("## 技术方案\n", encoding="utf-8")
    os.utime(old_artifact, (200, 200))

    v2_case_dir = runs_dir / "v2-run" / "cases" / "001"
    v2_case_dir.mkdir(parents=True)
    execution = v2_case_dir / "execution.json"
    execution.write_text(
        json.dumps({"schema_version": "patent-technical-solution-execution-v2"}),
        encoding="utf-8",
    )
    os.utime(execution, (100, 100))
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    assert bench.latest_run_id_for_case("001") == "v2-run"
