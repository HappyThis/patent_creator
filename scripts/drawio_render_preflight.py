#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
DEFAULT_FIXTURE = BACKEND_DIR / "tests" / "fixtures" / "figure-smoke.drawio"
DEFAULT_RENDERER = FRONTEND_DIR / "scripts" / "render-figure-drawio.mjs"
DEFAULT_DRAWIO_URL = "http://127.0.0.1:8081/"
EXPECTED_WIDTH = 1500
EXPECTED_HEIGHT = 900


class DrawioRenderPreflightError(RuntimeError):
    pass


def main() -> int:
    args = parse_args()
    try:
        drawio_url = normalize_drawio_url(args.drawio_url)
        run_drawio_render_preflight(
            drawio_url=drawio_url,
            node_bin=args.node_bin,
            fixture_path=Path(args.fixture),
            renderer_path=Path(args.renderer),
            render_timeout_seconds=args.render_timeout,
            reachability_timeout_seconds=args.reachability_timeout,
        )
    except DrawioRenderPreflightError as exc:
        print(f"Draw.io render preflight failed: {exc}", file=sys.stderr)
        return 1

    print(f"Draw.io render preflight passed: {EXPECTED_WIDTH}x{EXPECTED_HEIGHT} PNG.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the canonical Draw.io smoke fixture and verify its PNG dimensions."
    )
    parser.add_argument(
        "--drawio-url",
        default=os.getenv("PATENT_CREATOR_DRAWIO_EMBED_URL", DEFAULT_DRAWIO_URL),
        help="Draw.io embed URL. Defaults to PATENT_CREATOR_DRAWIO_EMBED_URL.",
    )
    parser.add_argument("--node-bin", default="node", help="Node.js executable used by the renderer.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help=argparse.SUPPRESS)
    parser.add_argument("--renderer", default=str(DEFAULT_RENDERER), help=argparse.SUPPRESS)
    parser.add_argument("--render-timeout", type=float, default=60.0, help=argparse.SUPPRESS)
    parser.add_argument("--reachability-timeout", type=float, default=3.0, help=argparse.SUPPRESS)
    return parser.parse_args()


def normalize_drawio_url(value: str) -> str:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from app.drawio_config import normalize_drawio_embed_url

    allow_nonlocal = os.getenv("PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        return normalize_drawio_embed_url(value, allow_nonlocal=allow_nonlocal)
    except ValueError as exc:
        raise DrawioRenderPreflightError(str(exc)) from exc


def run_drawio_render_preflight(
    *,
    drawio_url: str,
    node_bin: str = "node",
    fixture_path: Path = DEFAULT_FIXTURE,
    renderer_path: Path = DEFAULT_RENDERER,
    render_timeout_seconds: float = 60.0,
    reachability_timeout_seconds: float = 3.0,
) -> None:
    fixture_path = fixture_path.resolve()
    renderer_path = renderer_path.resolve()
    if not fixture_path.is_file():
        raise DrawioRenderPreflightError(f"smoke fixture not found: {fixture_path}")
    if not renderer_path.is_file():
        raise DrawioRenderPreflightError(f"renderer script not found: {renderer_path}")

    assert_drawio_reachable(drawio_url, timeout_seconds=reachability_timeout_seconds)

    with tempfile.TemporaryDirectory(prefix="patent-creator-drawio-preflight-") as temp_dir:
        output_path = Path(temp_dir) / "render.png"
        command = [
            node_bin,
            str(renderer_path),
            "--input",
            str(fixture_path),
            "--output",
            str(output_path),
            "--width",
            str(EXPECTED_WIDTH),
            "--height",
            str(EXPECTED_HEIGHT),
            "--drawio-url",
            drawio_url,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=FRONTEND_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=render_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise DrawioRenderPreflightError(
                f"Node.js executable was not found: {node_bin}. Install Node.js and frontend dependencies first."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DrawioRenderPreflightError(
                f"renderer timed out after {render_timeout_seconds:g} seconds."
            ) from exc
        except OSError as exc:
            raise DrawioRenderPreflightError(f"renderer could not start: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown renderer error").strip()
            raise DrawioRenderPreflightError(
                "renderer exited with status "
                f"{completed.returncode}: {detail}. "
                "Check frontend dependencies and install Chromium with `npx playwright install chromium` if needed."
            )
        if not output_path.is_file():
            raise DrawioRenderPreflightError("renderer exited successfully but did not create render.png.")

        dimensions = png_dimensions(output_path)
        if dimensions != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
            actual = f"{dimensions[0]}x{dimensions[1]}" if dimensions else "invalid PNG"
            raise DrawioRenderPreflightError(
                f"expected a {EXPECTED_WIDTH}x{EXPECTED_HEIGHT} PNG, got {actual}."
            )


def assert_drawio_reachable(drawio_url: str, *, timeout_seconds: float) -> None:
    request = urllib.request.Request(drawio_url, headers={"User-Agent": "patent-creator-drawio-preflight"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", 200)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DrawioRenderPreflightError(
            f"Draw.io is not reachable at {drawio_url} ({exc}). Start the service and retry."
        ) from exc
    if status >= 400:
        raise DrawioRenderPreflightError(f"Draw.io returned HTTP {status} at {drawio_url}.")


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return None
            dimensions = image.size
            image.verify()
        with Image.open(path) as image:
            image.load()
    except (OSError, SyntaxError, ValueError):
        return None
    return dimensions


if __name__ == "__main__":
    raise SystemExit(main())
