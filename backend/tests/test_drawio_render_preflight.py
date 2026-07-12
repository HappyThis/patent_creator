from __future__ import annotations

import importlib.util
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = REPO_ROOT / "scripts" / "drawio_render_preflight.py"


def load_preflight() -> ModuleType:
    spec = importlib.util.spec_from_file_location("drawio_render_preflight_test", PREFLIGHT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_png(path: Path, width: int, height: int) -> None:
    Image.new("RGB", (width, height), "white").save(path, format="PNG")


def test_preflight_renders_canonical_fixture_and_validates_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = load_preflight()
    commands: list[list[str]] = []
    monkeypatch.setattr(preflight, "assert_drawio_reachable", lambda *_args, **_kwargs: None)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output_path = Path(command[command.index("--output") + 1])
        write_png(output_path, 1500, 900)
        assert kwargs["cwd"] == preflight.FRONTEND_DIR
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    preflight.run_drawio_render_preflight(drawio_url="http://127.0.0.1:8081/?embed=1")

    assert len(commands) == 1
    command = commands[0]
    assert Path(command[command.index("--input") + 1]) == preflight.DEFAULT_FIXTURE.resolve()
    assert Path(command[1]) == preflight.DEFAULT_RENDERER.resolve()
    assert command[command.index("--width") + 1] == "1500"
    assert command[command.index("--height") + 1] == "900"


def test_preflight_rejects_wrong_png_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = load_preflight()
    monkeypatch.setattr(preflight, "assert_drawio_reachable", lambda *_args, **_kwargs: None)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("--output") + 1])
        write_png(output_path, 1200, 900)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    with pytest.raises(preflight.DrawioRenderPreflightError, match="expected a 1500x900 PNG, got 1200x900"):
        preflight.run_drawio_render_preflight(drawio_url="http://127.0.0.1:8081/?embed=1")


def test_preflight_rejects_truncated_png(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = load_preflight()
    monkeypatch.setattr(preflight, "assert_drawio_reachable", lambda *_args, **_kwargs: None)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("--output") + 1])
        write_png(output_path, 1500, 900)
        output_path.write_bytes(output_path.read_bytes()[:24])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    with pytest.raises(preflight.DrawioRenderPreflightError, match="got invalid PNG"):
        preflight.run_drawio_render_preflight(drawio_url="http://127.0.0.1:8081/?embed=1")


def test_preflight_surfaces_renderer_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = load_preflight()
    monkeypatch.setattr(preflight, "assert_drawio_reachable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="browser executable is missing",
        ),
    )

    with pytest.raises(preflight.DrawioRenderPreflightError, match="browser executable is missing"):
        preflight.run_drawio_render_preflight(drawio_url="http://127.0.0.1:8081/?embed=1")


def test_preflight_fails_fast_when_drawio_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = load_preflight()

    def fail_urlopen(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fail_urlopen)

    with pytest.raises(preflight.DrawioRenderPreflightError, match="Draw.io is not reachable"):
        preflight.assert_drawio_reachable("http://127.0.0.1:8081/", timeout_seconds=0.01)


@pytest.mark.parametrize(
    ("script_path", "bootstrap_call", "preflight_call"),
    [
        (
            REPO_ROOT / "scripts" / "start-dev.ps1",
            "Install-PlaywrightChromium -Directory $FrontendDir -NpmCommand $NpmCommand",
            "& $BackendPython (Join-Path $RepoRoot \"scripts\\drawio_render_preflight.py\")",
        ),
        (
            REPO_ROOT / "scripts" / "start-dev.sh",
            "\ninstall_playwright_chromium\n",
            '"${BACKEND_PYTHON}" "${REPO_ROOT}/scripts/drawio_render_preflight.py"',
        ),
    ],
)
def test_startup_scripts_install_chromium_before_render_preflight(
    script_path: Path,
    bootstrap_call: str,
    preflight_call: str,
) -> None:
    script = script_path.read_text(encoding="utf-8")

    assert "playwright install chromium" in script
    assert script.rindex(bootstrap_call) < script.rindex(preflight_call)
