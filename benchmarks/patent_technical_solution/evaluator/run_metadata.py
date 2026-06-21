from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]


def build_run_manifest(
    *,
    run_id: str,
    run_kind: str,
    run_config: dict[str, Any],
    case_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_kind": run_kind,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "benchmark_git": git_metadata(BENCHMARK_DIR),
        "model_config": capture_model_config(),
        "run_config": compact_dict(run_config),
        "case_ids": case_ids,
    }


def capture_model_config() -> dict[str, Any]:
    load_repo_env()
    api_key = os.getenv("OPENAI_API_KEY")
    return {
        "schema_version": 1,
        "subject": {
            "api": "responses",
            "model": _env_str("OPENAI_MODEL", "gpt-5.5"),
            "base_url": _env_str("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "reasoning_effort": _env_str("OPENAI_REASONING_EFFORT", "high").strip().lower(),
            "max_output_tokens": _env_int("OPENAI_MAX_OUTPUT_TOKENS", 8192),
            "web_search_enabled": _env_bool("OPENAI_WEB_SEARCH_ENABLED", True),
            "web_search_context_size": _env_str("OPENAI_WEB_SEARCH_CONTEXT_SIZE", "low").strip().lower(),
            "api_key_configured": bool(api_key),
            "api_key_env_var": "OPENAI_API_KEY" if api_key else None,
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
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


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
