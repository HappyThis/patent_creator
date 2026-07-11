from __future__ import annotations

import sys
from pathlib import Path

EVALUATOR_DIR = Path(__file__).resolve().parent
if str(EVALUATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_DIR))

import run_metadata


def test_resolve_codex_bin_skips_broken_path_and_uses_working_runtime(monkeypatch) -> None:
    monkeypatch.delenv("BENCHMARK_CODEX_BIN", raising=False)
    monkeypatch.setattr(run_metadata.shutil, "which", lambda _name: "/broken/codex")
    monkeypatch.setattr(
        run_metadata,
        "codex_binary_version",
        lambda path: "codex-cli 1.0" if path == "/Applications/ChatGPT.app/Contents/Resources/codex" else None,
    )

    assert run_metadata.resolve_codex_bin() == "/Applications/ChatGPT.app/Contents/Resources/codex"


def test_capture_model_config_records_runtime_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "subject-model")
    monkeypatch.setattr(run_metadata, "read_codex_config", lambda: {"model": "judge-model"})
    monkeypatch.setattr(run_metadata, "resolve_codex_bin", lambda: "/opt/codex")
    monkeypatch.setattr(run_metadata, "package_version", lambda _name: "0.1.0b3")

    config = run_metadata.capture_model_config()

    assert config["agent"]["provider"] == "openai-compatible"
    assert config["agent"]["model"] == "subject-model"
    assert config["agent"]["api_key_configured"] is True
    assert config["judge"]["model"] == "judge-model"
    assert config["judge"]["codex_bin"] == "/opt/codex"
    assert config["judge"]["sdk_version"] == "0.1.0b3"
    assert "sk-secret" not in str(config)
