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


def test_latest_solution_run_skips_historical_figure_mode(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("patent_technical_solution_bench_test", PATENT_BENCH_PATH)
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    benchmark_dir = tmp_path / "benchmark"
    runs_dir = benchmark_dir / "runs"

    def write_run(run_id: str, mode: str, mtime: int) -> None:
        case_dir = runs_dir / run_id / "cases" / "001"
        case_dir.mkdir(parents=True)
        artifact = case_dir / "evaluated_artifact.md"
        artifact.write_text("## 技术方案\n", encoding="utf-8")
        (case_dir / "input_manifest.json").write_text(
            json.dumps({"run_config": {"mode": mode}}),
            encoding="utf-8",
        )
        os.utime(artifact, (mtime, mtime))

    write_run("solution-run", "solution", 100)
    write_run("newer-figure-run", "figure", 200)
    monkeypatch.setattr(bench, "BENCHMARK_DIR", benchmark_dir)

    assert bench.latest_run_id_for_case("001") == "solution-run"
