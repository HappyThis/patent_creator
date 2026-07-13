from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from .judge_runtime import (
    JudgeRuntimeResolutionError,
    PROBE_PROMPT,
    PROBE_SCHEMA,
    RuntimeCandidate,
    RuntimeProbeResult,
    discover_runtime_candidates,
    probe_judge_runtime,
    resolve_judge_runtime,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"runtime")
    return path


def test_discovery_uses_app_then_sdk_pinned_then_path_on_windows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local_app_data = tmp_path / "local"
    codex_home = tmp_path / "custom-codex-home"
    plugin_runtime = _touch(codex_home / "plugins" / ".plugin-appserver" / "codex.exe")
    app_cache_runtime = _touch(local_app_data / "OpenAI" / "Codex" / "bin" / "hash" / "codex.exe")
    pinned_runtime = _touch(tmp_path / "venv" / "codex_cli_bin" / "bin" / "codex.exe")
    path_runtime = _touch(tmp_path / "npm" / "codex.cmd")

    candidates = discover_runtime_candidates(
        platform_name="win32",
        environ={"CODEX_HOME": str(codex_home), "LOCALAPPDATA": str(local_app_data)},
        home=home,
        which=lambda command: str(path_runtime) if command == "codex" else None,
        bundled_codex_path_loader=lambda: pinned_runtime,
    )

    assert [candidate.source for candidate in candidates] == [
        "codex_app",
        "codex_app",
        "sdk_pinned",
        "path_cli",
    ]
    assert candidates[0].path == str(plugin_runtime)
    assert candidates[1].path == str(app_cache_runtime)
    assert candidates[2].path == str(pinned_runtime)
    assert candidates[2].launch_codex_bin is None
    assert candidates[2].launch_mode == "sdk_default"
    assert candidates[3].launch_codex_bin == str(path_runtime)


def test_discovery_records_unavailable_sources_without_user_override(tmp_path: Path) -> None:
    candidates = discover_runtime_candidates(
        platform_name="linux",
        environ={"CODEX_HOME": str(tmp_path / "missing-codex-home")},
        home=tmp_path,
        which=lambda _command: None,
        bundled_codex_path_loader=lambda: (_ for _ in ()).throw(FileNotFoundError("missing pinned runtime")),
    )

    assert [candidate.source for candidate in candidates] == ["codex_app", "sdk_pinned", "path_cli"]
    assert all(candidate.path is None for candidate in candidates)
    assert all(candidate.discovery_error for candidate in candidates)


@pytest.mark.parametrize(
    ("platform_name", "app_relative_path"),
    [
        ("linux", Path(".codex/plugins/.plugin-appserver/codex")),
        ("darwin", Path("Applications/Codex.app/Contents/Resources/codex")),
    ],
)
def test_discovery_supports_linux_plugin_and_macos_app_paths(
    tmp_path: Path,
    platform_name: str,
    app_relative_path: Path,
) -> None:
    app_runtime = _touch(tmp_path / app_relative_path)
    pinned_runtime = _touch(tmp_path / "pinned" / "codex")
    path_runtime = _touch(tmp_path / "path" / "codex")

    candidates = discover_runtime_candidates(
        platform_name=platform_name,
        environ={},
        home=tmp_path,
        which=lambda _command: str(path_runtime),
        bundled_codex_path_loader=lambda: pinned_runtime,
    )

    sources = [candidate.source for candidate in candidates]
    assert sources[-2:] == ["sdk_pinned", "path_cli"]
    assert sources[:-2] and set(sources[:-2]) == {"codex_app"}
    assert str(app_runtime) in {candidate.path for candidate in candidates if candidate.source == "codex_app"}
    pinned = next(candidate for candidate in candidates if candidate.source == "sdk_pinned")
    assert pinned.launch_codex_bin is None


def test_resolver_follows_priority_records_failures_and_returns_json_safe_resolution(tmp_path: Path) -> None:
    app = RuntimeCandidate("codex_app", "/app/codex", "/app/codex")
    pinned = RuntimeCandidate("sdk_pinned", "/sdk/codex", None)
    path_cli = RuntimeCandidate("path_cli", "/path/codex", "/path/codex")
    version_calls: list[str] = []
    probe_calls: list[str] = []

    def version_probe(candidate: RuntimeCandidate, _timeout: float) -> str:
        version_calls.append(candidate.source)
        if candidate.source == "codex_app":
            raise RuntimeError("Authorization: Bearer sk-super-secret")
        return f"codex-cli {candidate.source}"

    async def runtime_probe(candidate: RuntimeCandidate, **_kwargs):
        probe_calls.append(candidate.source)
        if candidate.source == "sdk_pinned":
            raise RuntimeError("gpt-5.6-sol requires a newer version")
        return RuntimeProbeResult(appserver_version="0.144.0", sdk_version="0.1.0b3")

    resolution = asyncio.run(
        resolve_judge_runtime(
            cwd=tmp_path,
            model="gpt-5.6-sol",
            provider="openai",
            reasoning_effort="xhigh",
            candidates=[path_cli, pinned, app],
            version_probe=version_probe,
            runtime_probe=runtime_probe,
        )
    )

    assert version_calls == ["codex_app", "sdk_pinned", "path_cli"]
    assert probe_calls == ["sdk_pinned", "path_cli"]
    assert resolution["policy"] == ["codex_app", "sdk_pinned", "path_cli"]
    assert resolution["selected"] == {
        "source": "path_cli",
        "path": "/path/codex",
        "launch_mode": "explicit",
        "launch_codex_bin": "/path/codex",
        "binary_version": "codex-cli path_cli",
        "appserver_version": "0.144.0",
        "sdk_version": "0.1.0b3",
    }
    assert [attempt["status"] for attempt in resolution["attempts"]] == [
        "rejected",
        "rejected",
        "selected",
    ]
    assert [attempt["stage"] for attempt in resolution["attempts"]] == [
        "version",
        "model_auth_probe",
        "model_auth_probe",
    ]
    serialized = json.dumps(resolution)
    assert "sk-super-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_all_failures_raise_with_the_complete_sanitized_resolution(tmp_path: Path) -> None:
    unavailable = RuntimeCandidate(
        "codex_app",
        None,
        None,
        discovery_error='App missing; "api_key":"plain-secret"',
    )
    pinned = RuntimeCandidate("sdk_pinned", "/sdk/codex", None)

    def version_probe(_candidate: RuntimeCandidate, _timeout: float) -> str:
        return "codex-cli 0.137.0"

    async def runtime_probe(_candidate: RuntimeCandidate, **_kwargs):
        raise RuntimeError("Authorization: Bearer sk-another-secret user@example.com")

    with pytest.raises(JudgeRuntimeResolutionError) as exc_info:
        asyncio.run(
            resolve_judge_runtime(
                cwd=tmp_path,
                model="gpt-5.6-sol",
                provider="openai",
                reasoning_effort="xhigh",
                candidates=[unavailable, pinned],
                version_probe=version_probe,
                runtime_probe=runtime_probe,
            )
        )

    resolution = exc_info.value.resolution
    assert resolution["selected"] is None
    assert [attempt["status"] for attempt in resolution["attempts"]] == ["unavailable", "rejected"]
    serialized = json.dumps(resolution)
    combined = serialized + str(exc_info.value)
    assert "plain-secret" not in combined
    assert "sk-another-secret" not in combined
    assert "user@example.com" not in combined
    assert "[REDACTED]" in combined
    assert "[REDACTED_EMAIL]" in combined


def test_default_probe_uses_sdk_default_for_pinned_and_a_minimal_real_turn(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeThread:
        async def run(self, prompt: str, **kwargs):
            captured["prompt"] = prompt
            captured["turn_kwargs"] = kwargs
            return SimpleNamespace(final_response='{"ok":true}')

    class FakeCodex:
        metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="runtime-version"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

        async def thread_start(self, **kwargs):
            captured["thread_kwargs"] = kwargs
            return FakeThread()

    def codex_config(**kwargs):
        captured["config"] = kwargs
        return kwargs

    def async_codex(config):
        captured["async_config"] = config
        return FakeCodex()

    sdk = SimpleNamespace(
        CodexConfig=codex_config,
        AsyncCodex=async_codex,
        ApprovalMode=SimpleNamespace(deny_all="deny_all"),
        Sandbox=SimpleNamespace(read_only="read_only"),
        ReasoningEffort=lambda value: f"effort:{value}",
        version="sdk-version",
    )
    candidate = RuntimeCandidate("sdk_pinned", "/sdk/pinned/codex", None)

    result = asyncio.run(
        probe_judge_runtime(
            candidate,
            cwd=tmp_path,
            model="gpt-5.6-sol",
            provider="openai",
            reasoning_effort="xhigh",
            service_tier="default",
            sdk=sdk,
        )
    )

    assert captured["config"] == {
        "cwd": str(tmp_path),
        "codex_bin": None,
        "config_overrides": ('model_reasoning_effort="xhigh"',),
    }
    assert captured["thread_kwargs"] == {
        "approval_mode": "deny_all",
        "cwd": str(tmp_path),
        "ephemeral": True,
        "model": "gpt-5.6-sol",
        "model_provider": "openai",
        "sandbox": "read_only",
        "service_tier": "default",
    }
    assert captured["prompt"] == PROBE_PROMPT
    assert captured["turn_kwargs"] == {
        "approval_mode": "deny_all",
        "cwd": str(tmp_path),
        "effort": "effort:xhigh",
        "model": "gpt-5.6-sol",
        "output_schema": PROBE_SCHEMA,
        "sandbox": "read_only",
        "service_tier": "default",
    }
    assert result == RuntimeProbeResult(appserver_version="runtime-version", sdk_version="sdk-version")
