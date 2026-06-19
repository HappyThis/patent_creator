from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from app.core import config
from app.core.config import Settings


def test_settings_defaults_use_current_openai_compatible_route(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, git_user_name="Test User", git_user_email="test@example.com")

    assert settings.openai_compat_provider == "openai"
    assert settings.openai_compat_base_url == "https://api.yairouter.com"
    assert settings.openai_model == "gpt-5.5"


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


def test_settings_from_env_falls_back_to_current_openai_compatible_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "_load_repo_env", lambda: None)
    for name in (
        "OPENAI_COMPAT_PROVIDER",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_MODEL",
        "PATENT_CREATOR_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATENT_CREATOR_DATA_DIR", str(tmp_path))

    settings = Settings.from_env()

    assert settings.openai_compat_provider == "openai"
    assert settings.openai_compat_base_url == "https://api.yairouter.com"
    assert settings.openai_model == "gpt-5.5"
