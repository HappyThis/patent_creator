from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..drawio_config import DEFAULT_DRAWIO_EMBED_URL, normalize_drawio_embed_url


@dataclass(slots=True)
class Settings:
    data_dir: Path
    git_user_name: str
    git_user_email: str
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    openai_reasoning_effort: str = "high"
    openai_max_output_tokens: int = 8192
    openai_web_search_enabled: bool = True
    openai_web_search_context_size: str = "low"
    openai_model: str = "gpt-5.5"
    llm_timeout: float = 45.0
    context_compression_timeout: float = 180.0
    llm_max_retries: int = 5
    llm_retry_delay_seconds: float = 5.0
    openai_sdk_max_retries: int = 0
    cors_allow_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    round_step_delay: float = 0.15
    round_finish_delay: float = 0.1
    context_max_tokens: int = 128000
    context_compress_threshold_ratio: float = 0.8
    context_token_char_coefficient: float = 0.5
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_backup_days: int = 30
    log_llm_payload: bool = False
    drawio_embed_url: str = DEFAULT_DRAWIO_EMBED_URL
    drawio_allow_nonlocal: bool = False

    def __post_init__(self) -> None:
        self.drawio_embed_url = normalize_drawio_embed_url(
            self.drawio_embed_url,
            allow_nonlocal=self.drawio_allow_nonlocal,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        _load_repo_env()
        repo_dir = Path(__file__).resolve().parents[3]
        data_dir = Path(_env_or("PATENT_CREATOR_DATA_DIR", str(Path.home() / ".patent_creator"))).expanduser()
        log_dir = Path(_env_or("PATENT_CREATOR_LOG_DIR", str(repo_dir / "logs"))).expanduser()
        drawio_allow_nonlocal = _parse_bool_env(os.getenv("PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO"), False)
        drawio_embed_url = normalize_drawio_embed_url(
            _env_or("PATENT_CREATOR_DRAWIO_EMBED_URL", DEFAULT_DRAWIO_EMBED_URL),
            allow_nonlocal=drawio_allow_nonlocal,
        )
        return cls(
            data_dir=data_dir,
            git_user_name=_env_or("PATENT_CREATOR_GIT_USER_NAME", "Patent Creator"),
            git_user_email=_env_or("PATENT_CREATOR_GIT_USER_EMAIL", "patent-creator@local"),
            openai_base_url=_env_or("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_api_key=_env_optional("OPENAI_API_KEY"),
            openai_reasoning_effort=_env_or("OPENAI_REASONING_EFFORT", "high").strip().lower(),
            openai_max_output_tokens=_parse_int_env(os.getenv("OPENAI_MAX_OUTPUT_TOKENS"), 8192),
            openai_web_search_enabled=_parse_bool_env(os.getenv("OPENAI_WEB_SEARCH_ENABLED"), True),
            openai_web_search_context_size=_env_or("OPENAI_WEB_SEARCH_CONTEXT_SIZE", "low").strip().lower(),
            openai_model=_env_or("OPENAI_MODEL", "gpt-5.5"),
            llm_timeout=_parse_float_env(os.getenv("PATENT_CREATOR_LLM_TIMEOUT"), 45.0),
            context_compression_timeout=_parse_float_env(
                os.getenv("PATENT_CREATOR_CONTEXT_COMPRESSION_TIMEOUT"),
                180.0,
            ),
            llm_max_retries=_parse_int_env(os.getenv("PATENT_CREATOR_LLM_MAX_RETRIES"), 5),
            llm_retry_delay_seconds=_parse_float_env(os.getenv("PATENT_CREATOR_LLM_RETRY_DELAY_SECONDS"), 5.0),
            openai_sdk_max_retries=_parse_int_env(os.getenv("OPENAI_SDK_MAX_RETRIES"), 0),
            cors_allow_origins=_parse_csv_env(
                os.getenv("PATENT_CREATOR_CORS_ALLOW_ORIGINS"),
                ("http://127.0.0.1:5173", "http://localhost:5173"),
            ),
            round_step_delay=_parse_float_env(os.getenv("PATENT_CREATOR_ROUND_STEP_DELAY"), 0.15),
            round_finish_delay=_parse_float_env(os.getenv("PATENT_CREATOR_ROUND_FINISH_DELAY"), 0.1),
            context_max_tokens=_parse_int_env(os.getenv("PATENT_CREATOR_CONTEXT_MAX_TOKENS"), 128000),
            context_compress_threshold_ratio=_parse_float_env(
                os.getenv("PATENT_CREATOR_CONTEXT_COMPRESS_THRESHOLD_RATIO"),
                0.8,
            ),
            context_token_char_coefficient=_parse_float_env(
                os.getenv("PATENT_CREATOR_CONTEXT_TOKEN_CHAR_COEFFICIENT"),
                0.5,
            ),
            log_dir=log_dir,
            log_level=_env_or("PATENT_CREATOR_LOG_LEVEL", "INFO"),
            log_backup_days=_parse_int_env(os.getenv("PATENT_CREATOR_LOG_BACKUP_DAYS"), 30),
            log_llm_payload=_parse_bool_env(os.getenv("PATENT_CREATOR_LOG_LLM_PAYLOAD"), False),
            drawio_embed_url=drawio_embed_url,
            drawio_allow_nonlocal=drawio_allow_nonlocal,
        )


def _env_or(name: str, default: str) -> str:
    """读取环境变量；未设置或空字符串均回退到 default。"""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value


def _parse_csv_env(raw_value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not raw_value:
        return default
    values = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    return values or default


def _parse_bool_env(raw_value: str | None, default: bool) -> bool:
    if raw_value is None or raw_value.strip() == "":
        return default
    return raw_value.strip().lower() in ("1", "true", "yes", "on")


def _parse_int_env(raw_value: str | None, default: int) -> int:
    if raw_value is None or raw_value.strip() == "":
        return default
    return int(raw_value)


def _parse_float_env(raw_value: str | None, default: float) -> float:
    if raw_value is None or raw_value.strip() == "":
        return default
    return float(raw_value)


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
        value = _normalize_env_value(value)
        if key and key not in os.environ:
            os.environ[key] = value


def _normalize_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
