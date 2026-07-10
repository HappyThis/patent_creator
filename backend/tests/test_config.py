from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from app.core import config
from app.core.config import Settings


def test_settings_defaults_use_openai_responses_route(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, git_user_name="Test User", git_user_email="test@example.com")

    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-5.5"
    assert settings.openai_web_search_enabled is True
    assert settings.openai_web_search_context_size == "low"
    assert settings.llm_max_retries == 5
    assert settings.llm_retry_delay_seconds == 5.0
    assert settings.openai_sdk_max_retries == 0
    assert settings.drawio_embed_url.startswith("http://127.0.0.1:8081/?")
    assert "offline=1" in settings.drawio_embed_url
    assert "lang=zh" in settings.drawio_embed_url
    assert settings.drawio_allow_nonlocal is False


def test_importing_config_has_no_app_creation_logging_side_effect() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import logging;"
                "import app.core.config;"
                "print(any(type(handler).__name__ == 'TimedRotatingFileHandler' "
                "for handler in logging.getLogger().handlers))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


def test_settings_from_env_falls_back_to_openai_responses_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "_load_repo_env", lambda: None)
    for name in (
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_WEB_SEARCH_ENABLED",
        "OPENAI_WEB_SEARCH_CONTEXT_SIZE",
        "PATENT_CREATOR_DATA_DIR",
        "PATENT_CREATOR_DRAWIO_EMBED_URL",
        "PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATENT_CREATOR_DATA_DIR", str(tmp_path))

    settings = Settings.from_env()

    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-5.5"
    assert settings.openai_web_search_enabled is True
    assert settings.openai_web_search_context_size == "low"


def test_settings_from_env_treats_empty_values_as_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "_load_repo_env", lambda: None)
    monkeypatch.setenv("PATENT_CREATOR_DATA_DIR", str(tmp_path))
    for name in (
        "OPENAI_MAX_OUTPUT_TOKENS",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
        "OPENAI_WEB_SEARCH_ENABLED",
        "OPENAI_WEB_SEARCH_CONTEXT_SIZE",
        "PATENT_CREATOR_GIT_USER_NAME",
        "PATENT_CREATOR_GIT_USER_EMAIL",
        "PATENT_CREATOR_LLM_TIMEOUT",
        "PATENT_CREATOR_CONTEXT_COMPRESSION_TIMEOUT",
        "PATENT_CREATOR_LLM_MAX_RETRIES",
        "PATENT_CREATOR_LLM_RETRY_DELAY_SECONDS",
        "OPENAI_SDK_MAX_RETRIES",
        "PATENT_CREATOR_ROUND_STEP_DELAY",
        "PATENT_CREATOR_ROUND_FINISH_DELAY",
        "PATENT_CREATOR_CONTEXT_MAX_TOKENS",
        "PATENT_CREATOR_CONTEXT_COMPRESS_THRESHOLD_RATIO",
        "PATENT_CREATOR_CONTEXT_TOKEN_CHAR_COEFFICIENT",
        "PATENT_CREATOR_LOG_LEVEL",
        "PATENT_CREATOR_LOG_BACKUP_DAYS",
        "PATENT_CREATOR_LOG_LLM_PAYLOAD",
        "PATENT_CREATOR_DRAWIO_EMBED_URL",
        "PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO",
    ):
        monkeypatch.setenv(name, "")

    settings = Settings.from_env()

    assert settings.git_user_name == "Patent Creator"
    assert settings.git_user_email == "patent-creator@local"
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_api_key is None
    assert settings.openai_model == "gpt-5.5"
    assert settings.openai_reasoning_effort == "high"
    assert settings.openai_max_output_tokens == 8192
    assert settings.openai_web_search_enabled is True
    assert settings.openai_web_search_context_size == "low"
    assert settings.llm_timeout == 45.0
    assert settings.context_compression_timeout == 180.0
    assert settings.llm_max_retries == 5
    assert settings.llm_retry_delay_seconds == 5.0
    assert settings.openai_sdk_max_retries == 0
    assert settings.round_step_delay == 0.15
    assert settings.round_finish_delay == 0.1
    assert settings.context_max_tokens == 128000
    assert settings.context_compress_threshold_ratio == 0.8
    assert settings.context_token_char_coefficient == 0.5
    assert settings.log_level == "INFO"
    assert settings.log_backup_days == 30
    assert settings.log_llm_payload is False
    assert settings.drawio_embed_url.startswith("http://127.0.0.1:8081/?")
    assert "offline=1" in settings.drawio_embed_url
    assert "lang=zh" in settings.drawio_embed_url
    assert settings.drawio_allow_nonlocal is False


def test_settings_require_explicit_opt_in_for_nonlocal_drawio(monkeypatch) -> None:
    monkeypatch.setattr(config, "_load_repo_env", lambda: None)
    monkeypatch.setenv("PATENT_CREATOR_DRAWIO_EMBED_URL", "https://embed.diagrams.net/")
    monkeypatch.delenv("PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO", raising=False)

    with pytest.raises(ValueError, match="PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO"):
        Settings.from_env()

    monkeypatch.setenv("PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO", "true")
    settings = Settings.from_env()

    assert settings.drawio_embed_url.startswith("https://embed.diagrams.net/")
    assert "offline=1" in settings.drawio_embed_url
    assert "lang=zh" in settings.drawio_embed_url


def test_settings_from_env_expands_user_paths(monkeypatch) -> None:
    monkeypatch.setattr(config, "_load_repo_env", lambda: None)
    monkeypatch.setenv("PATENT_CREATOR_DATA_DIR", "~/.patent_creator")
    monkeypatch.setenv("PATENT_CREATOR_LOG_DIR", "~/patent_creator_logs")

    settings = Settings.from_env()

    assert settings.data_dir == Path.home() / ".patent_creator"
    assert settings.log_dir == Path.home() / "patent_creator_logs"


def test_repo_env_value_normalization_strips_matching_quotes() -> None:
    assert config._normalize_env_value(' "https://api.openai.com/v1" ') == "https://api.openai.com/v1"
    assert config._normalize_env_value(" '' ") == ""
    assert config._normalize_env_value(" 'test-key' ") == "test-key"
    assert config._normalize_env_value('"unterminated') == '"unterminated'
