from __future__ import annotations

import sys
from pathlib import Path

EVALUATOR_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "software_patent_solution_github" / "evaluator"
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

import codex_judge  # noqa: E402


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
