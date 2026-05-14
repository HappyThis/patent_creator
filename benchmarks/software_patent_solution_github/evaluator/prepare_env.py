from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def load_snapshot(case_dir: Path) -> dict[str, Any]:
    return json.loads((case_dir / "snapshot.json").read_text(encoding="utf-8"))


def snapshot_repo_url(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("repo_url") or snapshot.get("repo")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("snapshot.json 缺少 repo_url/repo。")
    repo = value.strip()
    if repo.startswith("http://") or repo.startswith("https://") or repo.startswith("git@"):
        return repo
    return f"https://github.com/{repo}.git"


def snapshot_commit(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("commit") or snapshot.get("snapshot_commit")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("snapshot.json 缺少 commit/snapshot_commit。")
    return value.strip()


def prepare_project_checkout(case_dir: Path, destination: Path) -> Path:
    snapshot = load_snapshot(case_dir)
    repo_url = snapshot_repo_url(snapshot)
    commit = snapshot_commit(snapshot)
    fetch_depth = int(snapshot.get("checkout_policy", {}).get("fetch_depth") or 1)

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination / ".git").exists():
        current = _run(["git", "rev-parse", "HEAD"], cwd=destination, check=False).stdout.strip()
        if current == commit:
            return destination

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    _run(["git", "init"], cwd=destination)
    _run(["git", "remote", "add", "origin", repo_url], cwd=destination)
    fetch = _run(
        ["git", "fetch", "--depth", str(fetch_depth), "origin", commit],
        cwd=destination,
        check=False,
    )
    if fetch.returncode != 0:
        _run(["git", "fetch", "origin", commit], cwd=destination)
    _run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=destination)

    include_submodules = bool(snapshot.get("checkout_policy", {}).get("include_submodules"))
    if include_submodules:
        _run(["git", "submodule", "update", "--init", "--recursive"], cwd=destination)

    return destination


def _run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        command_text = " ".join(command)
        message = completed.stderr.strip() or completed.stdout.strip() or "命令执行失败。"
        raise RuntimeError(f"{command_text}: {message}")
    return completed
