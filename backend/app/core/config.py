from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    data_dir: Path
    git_user_name: str
    git_user_email: str
    openai_compat_base_url: str = "https://api.deepseek.com/v1"
    openai_compat_api_key: str | None = None
    openai_model: str = "deepseek-v4-pro"
    llm_timeout: float = 45.0
    cors_allow_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    round_step_delay: float = 0.15
    round_finish_delay: float = 0.1

    @classmethod
    def from_env(cls) -> "Settings":
        _load_repo_env()
        backend_dir = Path(__file__).resolve().parents[2]
        data_dir = Path(os.getenv("PATENT_CREATOR_DATA_DIR", backend_dir / "data"))
        return cls(
            data_dir=data_dir,
            git_user_name=os.getenv("PATENT_CREATOR_GIT_USER_NAME", "Patent Creator"),
            git_user_email=os.getenv("PATENT_CREATOR_GIT_USER_EMAIL", "patent-creator@local"),
            openai_compat_base_url=os.getenv("OPENAI_COMPAT_BASE_URL", "https://api.deepseek.com/v1"),
            openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
            llm_timeout=float(os.getenv("PATENT_CREATOR_LLM_TIMEOUT", "45")),
            cors_allow_origins=_parse_csv_env(
                os.getenv("PATENT_CREATOR_CORS_ALLOW_ORIGINS"),
                ("http://127.0.0.1:5173", "http://localhost:5173"),
            ),
            round_step_delay=float(os.getenv("PATENT_CREATOR_ROUND_STEP_DELAY", "0.15")),
            round_finish_delay=float(os.getenv("PATENT_CREATOR_ROUND_FINISH_DELAY", "0.1")),
        )


def _parse_csv_env(raw_value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not raw_value:
        return default
    values = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    return values or default


def _load_repo_env() -> None:
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value
