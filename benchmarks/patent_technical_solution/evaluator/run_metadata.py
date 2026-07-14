from __future__ import annotations

import os
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ in supported environments
    tomllib = None  # type: ignore[assignment]

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]
DEFAULT_JUDGE_MODEL = "gpt-5.5"


def capture_model_config() -> dict[str, Any]:
    load_repo_env()
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = _env_str("OPENAI_BASE_URL", "https://api.openai.com/v1")
    codex_config = read_codex_config()
    judge_requested = _judge_requested_config(codex_config)
    return {
        "agent": {
            "api": "responses",
            "provider": _env_str("BENCHMARK_AGENT_PROVIDER", infer_provider(base_url)),
            "model": _env_str("OPENAI_MODEL", "gpt-5.5"),
            "base_url": base_url,
            "reasoning_effort": _env_str("OPENAI_REASONING_EFFORT", "high").strip().lower(),
            "max_output_tokens": _env_int("OPENAI_MAX_OUTPUT_TOKENS", 8192),
            "web_search_enabled": _env_bool("OPENAI_WEB_SEARCH_ENABLED", True),
            "web_search_context_size": _env_str("OPENAI_WEB_SEARCH_CONTEXT_SIZE", "low").strip().lower(),
            "api_key_configured": bool(api_key),
            "api_key_env_var": "OPENAI_API_KEY" if api_key else None,
        },
        "judge": {
            **judge_requested,
            "reasoning_effort": normalize_judge_reasoning_effort(judge_requested["reasoning_effort"]),
            "sdk_version": package_version("openai-codex"),
            "source": "benchmark_env_or_codex_config",
        },
        "runtime": {
            "llm_timeout": _env_float("PATENT_CREATOR_LLM_TIMEOUT", 45.0),
            "llm_max_retries": _env_int("PATENT_CREATOR_LLM_MAX_RETRIES", 2),
        },
        "context_compression": {
            "max_tokens": _env_int("PATENT_CREATOR_CONTEXT_MAX_TOKENS", 128000),
            "compress_threshold_ratio": _env_float("PATENT_CREATOR_CONTEXT_COMPRESS_THRESHOLD_RATIO", 0.8),
            "token_char_coefficient": _env_float("PATENT_CREATOR_CONTEXT_TOKEN_CHAR_COEFFICIENT", 0.5),
            "compression_timeout": _env_float("PATENT_CREATOR_CONTEXT_COMPRESSION_TIMEOUT", 180.0),
        },
    }


def capture_judge_requested_config() -> dict[str, Any]:
    load_repo_env()
    return _judge_requested_config(read_codex_config())


def _judge_requested_config(codex_config: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "provider": _env_optional("BENCHMARK_JUDGE_PROVIDER")
            or _toml_string(codex_config, "model_provider")
            or "openai",
            "model": _env_optional("BENCHMARK_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL,
            "reasoning_effort": (
                _env_optional("BENCHMARK_JUDGE_REASONING_EFFORT")
                or _toml_string(codex_config, "model_reasoning_effort")
                or "high"
            ).strip().lower(),
            "service_tier": _env_optional("BENCHMARK_JUDGE_SERVICE_TIER")
            or _toml_string(codex_config, "service_tier"),
        }
    )


def normalize_judge_reasoning_effort(value: str) -> str:
    normalized = value.strip().lower()
    return "xhigh" if normalized == "ultra" else normalized


def apply_default_judge_config(
    models: dict[str, Any],
    requested: dict[str, Any],
    default_judge: Any | None,
) -> None:
    """Apply a benchmark-owned Judge default before explicit CLI overrides."""

    if default_judge is None:
        return
    values = {
        "model": str(default_judge.model),
        "provider": str(default_judge.provider),
        "reasoning_effort": str(default_judge.reasoning_effort).strip().lower(),
    }
    requested.update(values)
    judge = dict(models.get("judge", {}))
    judge.update(values)
    judge["reasoning_effort"] = normalize_judge_reasoning_effort(values["reasoning_effort"])
    judge["source"] = "benchmark_manifest"
    models["judge"] = judge


def load_repo_env() -> None:
    env_path = REPO_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_codex_config() -> dict[str, Any]:
    if tomllib is None:
        return {}
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return {}
    try:
        value = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def infer_provider(base_url: str) -> str:
    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname in {"api.openai.com", "openai.com"}:
        return "openai"
    return "openai-compatible"


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _toml_string(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def git_metadata(cwd: Path) -> dict[str, Any]:
    commit = run_git(["rev-parse", "HEAD"], cwd=cwd)
    status = run_git(["status", "--short"], cwd=cwd)
    return {
        "commit": commit,
        "dirty": bool(status),
    }


def run_git(args: list[str], *, cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
