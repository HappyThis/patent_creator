from __future__ import annotations

from . import run_metadata


def test_capture_model_config_records_runtime_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "subject-model")
    monkeypatch.setenv("BENCHMARK_JUDGE_MODEL", "judge-model")
    monkeypatch.setattr(
        run_metadata,
        "read_codex_config",
        lambda: {"model": "judge-model", "model_reasoning_effort": "ultra"},
    )
    monkeypatch.setattr(run_metadata, "package_version", lambda _name: "0.1.0b3")

    config = run_metadata.capture_model_config()
    requested = run_metadata.capture_judge_requested_config()

    assert config["agent"]["provider"] == "openai-compatible"
    assert config["agent"]["model"] == "subject-model"
    assert config["agent"]["api_key_configured"] is True
    assert config["judge"]["model"] == "judge-model"
    assert config["judge"]["reasoning_effort"] == "xhigh"
    assert requested["reasoning_effort"] == "ultra"
    assert "codex_bin" not in config["judge"]
    assert config["judge"]["sdk_version"] == "0.1.0b3"
    assert "sk-secret" not in str(config)


def test_judge_model_defaults_to_gpt55_instead_of_local_codex_model(monkeypatch) -> None:
    monkeypatch.delenv("BENCHMARK_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(run_metadata, "load_repo_env", lambda: None)
    monkeypatch.setattr(
        run_metadata,
        "read_codex_config",
        lambda: {"model": "gpt-5.6-sol", "model_reasoning_effort": "high"},
    )

    requested = run_metadata.capture_judge_requested_config()

    assert requested["model"] == "gpt-5.5"
