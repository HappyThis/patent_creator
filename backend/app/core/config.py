from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    data_dir: Path
    git_user_name: str
    git_user_email: str
    openai_compat_base_url: str = "https://api.deepseek.com"
    openai_compat_api_key: str | None = None
    openai_compat_enable_thinking: bool = True
    openai_model: str = "deepseek-v4-pro"
    llm_timeout: float = 45.0
    context_compression_timeout: float = 180.0
    llm_max_retries: int = 2
    cors_allow_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    round_step_delay: float = 0.15
    round_finish_delay: float = 0.1
    main_agent_max_steps: int = 30
    subagent_max_steps: int = 30
    context_max_tokens: int = 128000
    context_compress_threshold_ratio: float = 0.8
    context_reserved_output_tokens: int = 8000
    context_recent_full_rounds: int = 8
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_backup_days: int = 30
    log_llm_payload: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        _load_repo_env()
        repo_dir = Path(__file__).resolve().parents[3]
        data_dir = Path(_env_or("PATENT_CREATOR_DATA_DIR", str(Path.home() / ".patent_creator")))
        log_dir = Path(_env_or("PATENT_CREATOR_LOG_DIR", str(repo_dir / "logs")))
        return cls(
            data_dir=data_dir,
            git_user_name=os.getenv("PATENT_CREATOR_GIT_USER_NAME", "Patent Creator"),
            git_user_email=os.getenv("PATENT_CREATOR_GIT_USER_EMAIL", "patent-creator@local"),
            openai_compat_base_url=os.getenv("OPENAI_COMPAT_BASE_URL", "https://api.deepseek.com"),
            openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY"),
            openai_compat_enable_thinking=_parse_bool_env(os.getenv("OPENAI_COMPAT_ENABLE_THINKING"), True),
            openai_model=os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
            llm_timeout=float(os.getenv("PATENT_CREATOR_LLM_TIMEOUT", "45")),
            context_compression_timeout=float(os.getenv("PATENT_CREATOR_CONTEXT_COMPRESSION_TIMEOUT", "180")),
            llm_max_retries=int(os.getenv("PATENT_CREATOR_LLM_MAX_RETRIES", "2")),
            cors_allow_origins=_parse_csv_env(
                os.getenv("PATENT_CREATOR_CORS_ALLOW_ORIGINS"),
                ("http://127.0.0.1:5173", "http://localhost:5173"),
            ),
            round_step_delay=float(os.getenv("PATENT_CREATOR_ROUND_STEP_DELAY", "0.15")),
            round_finish_delay=float(os.getenv("PATENT_CREATOR_ROUND_FINISH_DELAY", "0.1")),
            main_agent_max_steps=int(os.getenv("PATENT_CREATOR_MAIN_AGENT_MAX_STEPS", "30")),
            subagent_max_steps=int(os.getenv("PATENT_CREATOR_SUBAGENT_MAX_STEPS", "30")),
            context_max_tokens=int(os.getenv("PATENT_CREATOR_CONTEXT_MAX_TOKENS", "128000")),
            context_compress_threshold_ratio=float(
                os.getenv("PATENT_CREATOR_CONTEXT_COMPRESS_THRESHOLD_RATIO", "0.8")
            ),
            context_reserved_output_tokens=int(os.getenv("PATENT_CREATOR_CONTEXT_RESERVED_OUTPUT_TOKENS", "8000")),
            context_recent_full_rounds=int(os.getenv("PATENT_CREATOR_CONTEXT_RECENT_FULL_ROUNDS", "8")),
            log_dir=log_dir,
            log_level=os.getenv("PATENT_CREATOR_LOG_LEVEL", "INFO"),
            log_backup_days=int(os.getenv("PATENT_CREATOR_LOG_BACKUP_DAYS", "30")),
            log_llm_payload=_parse_bool_env(os.getenv("PATENT_CREATOR_LOG_LLM_PAYLOAD"), False),
        )


def _env_or(name: str, default: str) -> str:
    """读取环境变量；未设置或空字符串均回退到 default。"""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _parse_csv_env(raw_value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not raw_value:
        return default
    values = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    return values or default


def _parse_bool_env(raw_value: str | None, default: bool) -> bool:
    if raw_value is None:
        return default
    return raw_value.strip().lower() in ("1", "true", "yes", "on")


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
