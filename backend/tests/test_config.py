from __future__ import annotations

from pathlib import Path
import subprocess
import sys

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
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATENT_CREATOR_DATA_DIR", str(tmp_path))

    settings = Settings.from_env()

    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-5.5"
    assert settings.openai_web_search_enabled is True
    assert settings.openai_web_search_context_size == "low"


def test_settings_from_env_expands_user_paths(monkeypatch) -> None:
    monkeypatch.setattr(config, "_load_repo_env", lambda: None)
    monkeypatch.setenv("PATENT_CREATOR_DATA_DIR", "~/.patent_creator")
    monkeypatch.setenv("PATENT_CREATOR_LOG_DIR", "~/patent_creator_logs")

    settings = Settings.from_env()

    assert settings.data_dir == Path.home() / ".patent_creator"
    assert settings.log_dir == Path.home() / "patent_creator_logs"
