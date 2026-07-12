from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


RUNTIME_POLICY = ("codex_app", "sdk_pinned", "path_cli")
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}
PROBE_PROMPT = 'Return exactly {"ok":true}. Do not use tools.'
_MAX_ERROR_LENGTH = 1_000


@dataclass(frozen=True, slots=True)
class RuntimeCandidate:
    source: str
    path: str | None
    launch_codex_bin: str | None
    discovery_error: str | None = None

    @property
    def launch_mode(self) -> str:
        return "sdk_default" if self.source == "sdk_pinned" else "explicit"


@dataclass(frozen=True, slots=True)
class RuntimeProbeResult:
    appserver_version: str | None
    sdk_version: str | None = None


class JudgeRuntimeResolutionError(RuntimeError):
    def __init__(self, resolution: dict[str, Any]) -> None:
        self.resolution = resolution
        details = []
        for attempt in resolution.get("attempts", []):
            source = str(attempt.get("source") or "unknown")
            stage = str(attempt.get("stage") or "unknown")
            error = attempt.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            details.append(f"{source}/{stage}: {message or attempt.get('status') or 'failed'}")
        suffix = "; ".join(details) if details else "no runtime candidates were discovered"
        super().__init__(f"No usable Codex runtime for Judge: {suffix}")


VersionProbe = Callable[[RuntimeCandidate, float], str]
RuntimeProbe = Callable[..., Awaitable[RuntimeProbeResult | Mapping[str, Any]]]


def discover_runtime_candidates(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    bundled_codex_path_loader: Callable[[], Path] | None = None,
) -> list[RuntimeCandidate]:
    """Discover Codex runtimes in the fixed App -> SDK pinned -> PATH order."""

    platform_name = platform_name or sys.platform
    environ = environ if environ is not None else os.environ
    home = Path(home) if home is not None else Path.home()
    which = which or shutil.which
    bundled_codex_path_loader = bundled_codex_path_loader or _bundled_codex_path

    candidates: list[RuntimeCandidate] = []
    seen_paths: set[str] = set()

    app_paths = _discover_app_runtime_paths(
        platform_name=platform_name,
        environ=environ,
        home=home,
    )
    for path in app_paths:
        _append_path_candidate(candidates, seen_paths, source="codex_app", path=path, explicit=True)
    if not app_paths:
        candidates.append(
            RuntimeCandidate(
                source="codex_app",
                path=None,
                launch_codex_bin=None,
                discovery_error="Codex App runtime was not found.",
            )
        )

    try:
        pinned_path = Path(bundled_codex_path_loader()).expanduser()
    except Exception as exc:
        candidates.append(
            RuntimeCandidate(
                source="sdk_pinned",
                path=None,
                launch_codex_bin=None,
                discovery_error=_safe_message(exc),
            )
        )
    else:
        if pinned_path.is_file():
            _append_path_candidate(
                candidates,
                seen_paths,
                source="sdk_pinned",
                path=pinned_path,
                explicit=False,
            )
        else:
            candidates.append(
                RuntimeCandidate(
                    source="sdk_pinned",
                    path=str(pinned_path),
                    launch_codex_bin=None,
                    discovery_error="SDK pinned Codex runtime does not exist.",
                )
            )

    path_cli = _which_codex(which, platform_name=platform_name)
    if path_cli:
        normalized = _normalized_path(path_cli)
        if normalized in seen_paths:
            candidates.append(
                RuntimeCandidate(
                    source="path_cli",
                    path=str(Path(path_cli).expanduser()),
                    launch_codex_bin=None,
                    discovery_error="PATH resolves to a runtime already listed by an earlier source.",
                )
            )
        else:
            _append_path_candidate(
                candidates,
                seen_paths,
                source="path_cli",
                path=Path(path_cli),
                explicit=True,
            )
    else:
        candidates.append(
            RuntimeCandidate(
                source="path_cli",
                path=None,
                launch_codex_bin=None,
                discovery_error="Codex CLI was not found on PATH.",
            )
        )

    return candidates


async def resolve_judge_runtime(
    *,
    cwd: Path,
    model: str | None,
    provider: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    candidates: Sequence[RuntimeCandidate] | None = None,
    discover: Callable[[], Sequence[RuntimeCandidate]] | None = None,
    version_probe: VersionProbe | None = None,
    runtime_probe: RuntimeProbe | None = None,
    version_timeout_seconds: float = 5.0,
    probe_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Select the first runtime that can execute a minimal turn for the Judge model."""

    if candidates is None:
        candidates = _ordered_candidates((discover or discover_runtime_candidates)())
    else:
        candidates = _ordered_candidates(candidates)
    version_probe = version_probe or probe_binary_version
    runtime_probe = runtime_probe or probe_judge_runtime

    resolution: dict[str, Any] = {
        "policy": list(RUNTIME_POLICY),
        "selected": None,
        "attempts": [],
    }

    for candidate in candidates:
        attempt_started = time.monotonic()
        attempt: dict[str, Any] = {
            "source": candidate.source,
            "path": candidate.path,
            "launch_mode": candidate.launch_mode,
            "status": "pending",
            "stage": "discovery",
            "binary_version": None,
            "appserver_version": None,
            "sdk_version": None,
            "duration_ms": None,
            "error": None,
        }
        resolution["attempts"].append(attempt)

        if candidate.discovery_error or not candidate.path:
            attempt["status"] = "unavailable"
            attempt["error"] = _error_record(
                RuntimeError(candidate.discovery_error or "Runtime path is unavailable.")
            )
            attempt["duration_ms"] = _elapsed_ms(attempt_started)
            continue

        attempt["stage"] = "version"
        try:
            binary_version = await asyncio.to_thread(
                version_probe,
                candidate,
                version_timeout_seconds,
            )
            if not isinstance(binary_version, str) or not binary_version.strip():
                raise RuntimeError("Codex binary returned an empty version.")
        except Exception as exc:
            attempt["status"] = "rejected"
            attempt["error"] = _error_record(exc)
            attempt["duration_ms"] = _elapsed_ms(attempt_started)
            continue
        attempt["binary_version"] = binary_version.strip()

        attempt["stage"] = "model_auth_probe"
        try:
            probe_result_value = await asyncio.wait_for(
                runtime_probe(
                    candidate,
                    cwd=Path(cwd),
                    model=model,
                    provider=provider,
                    reasoning_effort=reasoning_effort,
                    service_tier=service_tier,
                ),
                timeout=probe_timeout_seconds,
            )
            probe_result = _coerce_probe_result(probe_result_value)
        except Exception as exc:
            attempt["status"] = "rejected"
            attempt["error"] = _error_record(exc)
            attempt["duration_ms"] = _elapsed_ms(attempt_started)
            continue

        attempt["status"] = "selected"
        attempt["appserver_version"] = probe_result.appserver_version
        attempt["sdk_version"] = probe_result.sdk_version
        attempt["duration_ms"] = _elapsed_ms(attempt_started)
        resolution["selected"] = {
            "source": candidate.source,
            "path": candidate.path,
            "launch_mode": candidate.launch_mode,
            "launch_codex_bin": candidate.launch_codex_bin,
            "binary_version": attempt["binary_version"],
            "appserver_version": attempt["appserver_version"],
            "sdk_version": attempt["sdk_version"],
        }
        return resolution

    raise JudgeRuntimeResolutionError(resolution)


def probe_binary_version(candidate: RuntimeCandidate, timeout_seconds: float = 5.0) -> str:
    if not candidate.path:
        raise FileNotFoundError("Codex runtime path is unavailable.")
    try:
        completed = subprocess.run(
            [candidate.path, "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Unable to execute Codex version probe: {_safe_message(exc)}") from exc
    if completed.returncode != 0:
        detail = _safe_text(completed.stderr.strip())
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Codex version probe exited with code {completed.returncode}{suffix}")
    version = completed.stdout.strip() or completed.stderr.strip()
    if not version:
        raise RuntimeError("Codex binary returned an empty version.")
    return _safe_text(version)


async def probe_judge_runtime(
    candidate: RuntimeCandidate,
    *,
    cwd: Path,
    model: str | None,
    provider: str,
    reasoning_effort: str,
    service_tier: str | None,
    sdk: Any | None = None,
) -> RuntimeProbeResult:
    """Run one minimal, schema-constrained turn to verify runtime/model/auth compatibility."""

    sdk = sdk or _load_sdk()
    config = sdk.CodexConfig(
        cwd=str(cwd),
        codex_bin=candidate.launch_codex_bin,
        config_overrides=(f"model_reasoning_effort={json.dumps(reasoning_effort)}",),
    )
    async with sdk.AsyncCodex(config) as codex:
        appserver_version = _runtime_version(codex.metadata)
        thread = await codex.thread_start(
            approval_mode=sdk.ApprovalMode.deny_all,
            cwd=str(cwd),
            ephemeral=True,
            model=model,
            model_provider=provider or None,
            sandbox=sdk.Sandbox.read_only,
            service_tier=service_tier,
        )
        result = await thread.run(
            PROBE_PROMPT,
            approval_mode=sdk.ApprovalMode.deny_all,
            cwd=str(cwd),
            effort=sdk.ReasoningEffort(reasoning_effort),
            model=model,
            output_schema=PROBE_SCHEMA,
            sandbox=sdk.Sandbox.read_only,
            service_tier=service_tier,
        )
        try:
            response = json.loads(str(result.final_response).strip())
        except (AttributeError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Codex runtime probe did not return valid JSON.") from exc
        if response != {"ok": True}:
            raise RuntimeError("Codex runtime probe returned an unexpected response.")
        return RuntimeProbeResult(
            appserver_version=appserver_version,
            sdk_version=str(sdk.version) if getattr(sdk, "version", None) else None,
        )


def _discover_app_runtime_paths(
    *,
    platform_name: str,
    environ: Mapping[str, str],
    home: Path,
) -> list[Path]:
    executable = "codex.exe" if platform_name.startswith("win") else "codex"
    codex_home = Path(environ.get("CODEX_HOME") or home / ".codex").expanduser()
    paths = [codex_home / "plugins" / ".plugin-appserver" / executable]

    if platform_name.startswith("win"):
        local_app_data = environ.get("LOCALAPPDATA")
        if local_app_data:
            app_bin = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            hashed = _sorted_existing_paths(app_bin.glob(f"*/{executable}"))
            paths.extend(hashed)
            paths.append(app_bin / executable)
    elif platform_name == "darwin":
        paths.extend(
            [
                Path("/Applications/Codex.app/Contents/Resources/codex"),
                home / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex",
                Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex",
            ]
        )

    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalized_path(path)
        if normalized in seen or not path.is_file():
            continue
        seen.add(normalized)
        result.append(path)
    return result


def _sorted_existing_paths(paths: Any) -> list[Path]:
    existing: list[tuple[float, Path]] = []
    try:
        iterator = list(paths)
    except OSError:
        return []
    for path in iterator:
        try:
            if path.is_file():
                existing.append((path.stat().st_mtime, path))
        except OSError:
            continue
    existing.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in existing]


def _which_codex(which: Callable[[str], str | None], *, platform_name: str) -> str | None:
    commands = ("codex", "codex.cmd", "codex.exe") if platform_name.startswith("win") else ("codex",)
    for command in commands:
        try:
            value = which(command)
        except OSError:
            value = None
        if value:
            return value
    return None


def _append_path_candidate(
    candidates: list[RuntimeCandidate],
    seen_paths: set[str],
    *,
    source: str,
    path: Path,
    explicit: bool,
) -> None:
    path = path.expanduser()
    normalized = _normalized_path(path)
    if normalized in seen_paths:
        return
    seen_paths.add(normalized)
    value = str(path)
    candidates.append(
        RuntimeCandidate(
            source=source,
            path=value,
            launch_codex_bin=value if explicit else None,
        )
    )


def _bundled_codex_path() -> Path:
    from codex_cli_bin import bundled_codex_path

    return bundled_codex_path()


def _load_sdk() -> SimpleNamespace:
    try:
        import openai_codex
        from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
        from openai_codex.api import ReasoningEffort
    except ImportError as exc:
        raise RuntimeError(
            "Missing openai-codex benchmark dependency; synchronize the backend benchmark group."
        ) from exc
    return SimpleNamespace(
        AsyncCodex=AsyncCodex,
        CodexConfig=CodexConfig,
        ApprovalMode=ApprovalMode,
        Sandbox=Sandbox,
        ReasoningEffort=ReasoningEffort,
        version=openai_codex.__version__,
    )


def _runtime_version(metadata: Any) -> str | None:
    server_info = getattr(metadata, "serverInfo", None)
    value = getattr(server_info, "version", None)
    return str(value) if value else None


def _coerce_probe_result(value: RuntimeProbeResult | Mapping[str, Any]) -> RuntimeProbeResult:
    if isinstance(value, RuntimeProbeResult):
        return value
    if isinstance(value, Mapping):
        appserver_version = value.get("appserver_version")
        sdk_version = value.get("sdk_version")
        return RuntimeProbeResult(
            appserver_version=str(appserver_version) if appserver_version else None,
            sdk_version=str(sdk_version) if sdk_version else None,
        )
    raise TypeError("Runtime probe returned an unsupported result type.")


def _error_record(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": _safe_message(exc),
    }


def _safe_message(exc: BaseException) -> str:
    message = str(exc).strip() or "Operation failed without an error message."
    return _safe_text(message)


def _safe_text(value: str) -> str:
    redacted = value
    redacted = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9._-]{4,}", "[REDACTED]", redacted)
    redacted = re.sub(
        r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
        r"authorization|password|client[_-]?secret)\b[\"']?\s*[:=]\s*)"
        r"([\"']?)([^\"'\s,;}]+)([\"']?)",
        r"\1\2[REDACTED]\4",
        redacted,
    )
    redacted = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", redacted)
    if len(redacted) > _MAX_ERROR_LENGTH:
        redacted = redacted[: _MAX_ERROR_LENGTH - 3] + "..."
    return redacted


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _ordered_candidates(candidates: Sequence[RuntimeCandidate]) -> list[RuntimeCandidate]:
    order = {source: index for index, source in enumerate(RUNTIME_POLICY)}
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda item: (order.get(item[1].source, len(order)), item[0]))
    return [candidate for _, candidate in indexed]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


__all__ = [
    "JudgeRuntimeResolutionError",
    "PROBE_PROMPT",
    "PROBE_SCHEMA",
    "RUNTIME_POLICY",
    "RuntimeCandidate",
    "RuntimeProbeResult",
    "discover_runtime_candidates",
    "probe_binary_version",
    "probe_judge_runtime",
    "resolve_judge_runtime",
]
